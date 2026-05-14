# 完整部署文档

把这套 demo 在你自己的 AWS 账号上从零跑通——AI Agent 用测试网 USDC 自主完成 HTTP 402 付款。

读完照做即可独立部署。前面解释**这是什么、各组件干什么**，中间是**逐步操作 + 预期输出**，后面是**完整故障排查矩阵 + 清理流程**。

不需要先看其他文档。如果你想深入原理，再去读 `ARCHITECTURE.md`；想知道每个修复对应哪个真实坑，再去读 `DEPLOYMENT_NOTES.md`。

**预计耗时**：首次部署 1-2 小时（其中 CloudFront 分发 10-15 分钟、Docker arm64 build 5-10 分钟、剩余都是配置 + 等待）。

**适用版本**：AgentCore Payments preview（2026-05 之后），CDP 新版 Ed25519 Secret API Key。

---

## 目录

- [Part A — 你需要理解的原理（10 分钟阅读）](#part-a--你需要理解的原理10-分钟阅读)
- [Part B — 准备工作](#part-b--准备工作)
- [Part 1 — Coinbase CDP 一次性配置](#part-1--coinbase-cdp-一次性配置)
- [Part 2 — 克隆代码与设置环境变量](#part-2--克隆代码与设置环境变量)
- [Part 3 — 部署 Seller（CloudFront + Lambda@Edge）](#part-3--部署-sellercloudfront--lambdaedge)
- [Part 4 — 部署 Payer Infrastructure（IAM 角色）](#part-4--部署-payer-infrastructureiam-角色)
- [Part 5 — 创建 AgentCore Payments 资源](#part-5--创建-agentcore-payments-资源)
- [Part 6 — 部署 Payer Agent](#part-6--部署-payer-agent)
- [Part 7 — 端到端测试](#part-7--端到端测试)
- [Part 8 — 调试命令](#part-8--调试命令)
- [Part 9 — 清理（很重要）](#part-9--清理很重要)
- [Part 10 — 故障排查矩阵](#part-10--故障排查矩阵)
- [附录 — 文件清单与下一步](#附录--文件清单与下一步)

---

## Part A — 你需要理解的原理（10 分钟阅读）

### A.1 这套系统在解决什么问题

AI Agent 想要"付费访问 API"很别扭：
- 没法注册账号（每个供应商都要人类 KYC）
- 微支付（$0.001 一次）跑不动传统卡组织（手续费比金额大）
- 平台账号 + 月结模式跟 Agent 的实时性冲突

x402 协议的方案：**用 HTTP 402 状态码 + 区块链稳定币结算**。

```
Agent: GET /api/article
Server: 402 Payment Required + { 你需要付 0.001 USDC 给 0xF72b... }
Agent: (本地签一张 EIP-3009 授权单)
Agent: GET /api/article + X-PAYMENT: <签名>
Server: (上链兑付授权单) → 200 OK + 内容
```

但 Agent 不能持有钱包私钥（泄露就完了）。**AgentCore Payments** 解决这个：把签名能力托管在 AWS 后端 + Coinbase CDP 上。Agent 只持有 IAM 身份，不碰 key。

### A.2 五个核心组件

```
┌──────────────┐  invoke_agent_runtime  ┌────────────────────────┐
│  本地客户端   │───────────────────────▶│  AgentCore Runtime     │
└──────────────┘                        │   (容器, arm64)        │
                                        │   FastAPI + Strands    │
                                        │   Claude Sonnet 4      │
                                        └─────┬──────────────┬───┘
                                              │              │
              GET /api/article + X-PAYMENT    │              │ ProcessPayment
                ┌─────────────────────────────┘              │
                ▼                                            ▼
┌──────────────────────────────┐           ┌─────────────────────────┐
│  CloudFront + Lambda@Edge    │           │  AgentCore Payments     │
│  (seller, us-east-1)         │           │  (us-west-2 only)       │
│  - 验签                       │           └────────────┬────────────┘
│  - 调 facilitator 上链        │                        │ delegated signing
│  - 200 + content              │                        ▼
└────────────┬─────────────────┘           ┌─────────────────────────┐
             │ broadcast EIP-3009          │  Coinbase CDP           │
             ▼                             │  (Server + Embedded)    │
┌─────────────────────────────────────────┴┐│  - 持有 walletSecret    │
│  Base Sepolia 测试网                      ││  - 代签 EIP-712 类型    │
│  USDC: payer -= 1000  /  seller += 1000  ││    化数据               │
└──────────────────────────────────────────┘└─────────────────────────┘
```

| 组件 | 职责 | 部署位置 |
|------|------|---------|
| **AgentCore Runtime** | 跑你的 Agent 容器（FastAPI + Strands + Claude） | us-west-2 |
| **AgentCore Payments** | 接收 ProcessPayment 调用 → 让 CDP 代签 | us-west-2 |
| **Coinbase CDP** | 持有真实钱包私钥 → delegated signing | Coinbase 托管 |
| **Seller (CloudFront + Lambda@Edge)** | 卖付费 API 内容 + 验证 X-PAYMENT 头 | us-east-1（强制） |
| **Base Sepolia** | 链上结算 USDC | 公链测试网 |

### A.3 一笔付款的端到端流程

```
1. Agent 收到 prompt "Get me the premium article"
2. Agent 调 GET /api/premium-article → 收到 402 + 付款要求
3. Agent 调 ProcessPayment（带上 402 响应里的 x402_payload）
   ↓ AWS 后端：取 walletSecret → 让 CDP 用 EIP-712 签名
   ↓ 返回 PROOF_GENERATED + 签名
4. Agent 把签名 base64 后塞进 X-PAYMENT header，重发 GET /api/premium-article
5. Lambda@Edge 解码 X-PAYMENT → 调 facilitator broadcast 上链
6. Base Sepolia: USDC 从 payer 转到 seller（一个 block ≈ 2 秒）
7. Lambda@Edge 收到上链确认 → 放行 → 200 + 内容
```

**整个流程 ~3 秒**（其中 LLM 推理 1-2 秒，链上结算 1-2 秒）。

### A.4 关键安全点

- **Agent 不持有私钥**：所有签名走 AgentCore Payments → CDP 后端
- **预算硬限制**：`PaymentSession.maxSpendAmount`，超了直接拒绝，链上不会发任何东西
- **CDP 端的 delegated signing 需要用户一次性授权**：通过 Email OTP 登 WalletHub 完成
- **EIP-3009 nonce 防重放**：每笔签名只能上链一次

### A.5 ⚠️ 这套系统**不**做退款

链上交易不可逆。AgentCore Payments 和 x402 协议都没有 RefundPayment / DisputePayment 这类 API。如果 seller 想退，得自己用 ERC20.transfer 反向打回——这是应用层的事。

---

## Part B — 准备工作

### B.1 软件检查清单（本机）

| 工具 | 版本 | 检查命令 | 没装时 |
|------|------|---------|--------|
| AWS CLI | 2.x | `aws --version` | https://aws.amazon.com/cli/ |
| AWS CDK | 2.x | `npx cdk --version` | 不需要全局装 |
| Node.js | 18+ | `node --version` | https://nodejs.org/ |
| Docker + buildx | 25+ | `docker buildx version` | https://docs.docker.com/engine/install/ |
| Python | 3.10+ | `python3 --version` | 系统 Python 不够新就用下面的 `uv` |
| `uv` | 任意 | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `gh` (可选) | 任意 | `gh --version` | 用来 fork repo |

> **Python 3.9 不够**——boto3 ≥ 1.43 要求 Python ≥ 3.10。如果系统是 3.9，用 `uv venv --python 3.10` 创建虚拟环境，或装 brew 上的 python 3.14。

### B.2 账号

1. **AWS 账号**：跑 `aws sts get-caller-identity` 能返回你的 ARN，最好是 admin 权限
2. **Coinbase Developer Platform**：https://portal.cdp.coinbase.com/ 免费注册
3. **一个 Base Sepolia 收款地址**：MetaMask 新建账户切到 "Base Sepolia" 网络复制地址即可（不需要任何余额）

### B.3 费用提示

- AWS 端：CloudFront + Lambda@Edge + AgentCore Runtime + ECR——demo 流量下，跑半天不到 $1
- 链上：Base Sepolia 测试网 USDC 由 Circle faucet 免费给（每次 10 USDC，够跑几百次）
- **测试完别忘了 Part 9 的清理**

---

## Part 1 — Coinbase CDP 一次性配置

CDP 提供 payer 钱包的托管服务。需要拿三个 secret + 启用一个开关 + 配一个 CORS。这步不做后面 `create_payment_instrument` 必然 500 失败。

### 1.1 拿三个 Secret

登录 https://portal.cdp.coinbase.com/，新建/选一个 Project：

**第一组（API Key）**：左侧 **API Keys** → "Create API Key" → 选 **Secret API Key (Ed25519)** → 拿到：
- `id`：UUID 格式（36 字符）
- `privateKey`：很长的 base64 字符串（80+ 字符）

**第二组（Wallet Secret）**：左侧 **Server Wallets** → **Wallet API** → "Generate Wallet Secret" → 拿到：
- `walletSecret`：另一个长字符串（180+ 字符）

⚠️ **`walletSecret` 和上面的 API Key 是分开的**，必须单独生成。**只显示一次，立刻保存到密码管理器**。

### 1.2 启用 Embedded Wallets 的 Delegated Signing

> 不开启这个，AgentCore 在用 CDP 代签时会被拒绝，表现为 `create_payment_instrument` 返回 `InternalServerException: Failed to create payment instrument`。

CDP Portal → **Embedded Wallets** → **Policies** → 启用 **Delegated signing**

### 1.3 配 WalletHub 的 CORS Origin

> 不配的话用户去 WalletHub 授权时报 `Project must specify a valid CORS origin to complete an OAuth flow`。

Embedded Wallets → **CORS** / **Allowed Origins** → 添加：
```
https://hub.cdp.coinbase.com
```

---

## Part 2 — 克隆代码与设置环境变量

### 2.1 克隆

```bash
git clone https://github.com/IanLiYi1996/sample-agentcore-cloudfront-x402-payments.git
# 或上游：git clone https://github.com/aws-samples/sample-agentcore-cloudfront-x402-payments.git
cd sample-agentcore-cloudfront-x402-payments
```

> 上游 sample 还没合入本文档对应的修复，建议用我的 fork。如果用上游 sample，自己手动应用 `payer-agent/` 下三个文件的修改（见 commit `80803dd`）。

### 2.2 设环境变量

把刚拿到的 CDP 三个 secret 和你的真实邮箱设到当前 shell：

```bash
export CDP_API_KEY_ID='<id, UUID>'
export CDP_API_KEY_SECRET='<privateKey 整段 base64>'
export CDP_WALLET_SECRET='<walletSecret>'
export CDP_END_USER_EMAIL='your-real@email.com'    # 后面要登 WalletHub 收 OTP，必须真邮箱
```

⚠️ **这些都是当前 shell 临时变量**——别写进 `.env` 也别 git commit。

验证一下：

```bash
echo "API_KEY_ID len=${#CDP_API_KEY_ID} (~36)"
echo "API_SECRET len=${#CDP_API_KEY_SECRET} (~88)"
echo "WALLET_SECRET len=${#CDP_WALLET_SECRET} (~184)"
```

长度对得上的话就可以下一步了。

---

## Part 3 — 部署 Seller（CloudFront + Lambda@Edge）

部署位置 **us-east-1**（Lambda@Edge 强制）。耗时 10-15 分钟。

### 3.1 写 seller 的 .env

```bash
echo "PAYMENT_RECIPIENT_ADDRESS=<你的卖家收款地址>" > seller-infrastructure/.env
```

`<你的卖家收款地址>` 是任何你控制的 Base Sepolia 地址。MetaMask 新建一个账户切网络到 Base Sepolia 复制即可。这个地址只用来收钱，不需要任何初始余额。

### 3.2 部署

```bash
cd seller-infrastructure
npm install
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
npx cdk bootstrap aws://${ACCOUNT}/us-east-1   # 首次需要
npx cdk deploy --require-approval never
```

部署成功后输出形如：

```
Outputs:
X402SellerStack.DistributionUrl = https://dxxxxxxxxxxxxx.cloudfront.net
X402SellerStack.PaymentApiEndpoint = https://dxxxxxxxxxxxxx.cloudfront.net/api/
X402SellerStack.DistributionId = ABCDEFGHIJKLMN
```

**记下 `DistributionUrl`**，Part 6.1 要用。

```bash
cd ..
```

---

## Part 4 — 部署 Payer Infrastructure（IAM 角色）

部署位置 **us-west-2**（AgentCore Payments 当前唯一可用区）。耗时 ~5 分钟。

### 4.1 部署

⚠️ **必须显式传所有 region 变量**——CDK 会优先读 shell 的 `AWS_REGION`，单设 `CDK_DEFAULT_REGION` 不够。

```bash
cd payer-infrastructure
npm install
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
npx cdk bootstrap aws://${ACCOUNT}/us-west-2

AWS_REGION=us-west-2 \
AWS_DEFAULT_REGION=us-west-2 \
CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy --all --require-approval never
```

输出里记下两个 ARN：

```
X402PayerAgentStack.ProcessPaymentRoleArn = arn:aws:iam::<account>:role/AgentCorePaymentsProcessPaymentRole
X402PayerAgentStack.ResourceRetrievalRoleArn = arn:aws:iam::<account>:role/AgentCorePaymentsResourceRetrievalRole
```

```bash
cd ..
```

### 4.2 ⚠️ 关键：补 IAM 策略（CDK 漏的权限）

CDK 给 `ResourceRetrievalRole` 挂的 IAM action 写错了名字（用了不存在的 action 像 `bedrock-agentcore:GetIdentity`）。不补就 `create_payment_manager` AccessDenied。

```bash
AWS_REGION=us-west-2 ./scripts/runbook/01_patch_iam_role.sh
```

输出形如：

```
Patching role AgentCorePaymentsResourceRetrievalRole (account=..., region=us-west-2)...
Done. Verify:
[
    "bedrock-agentcore:GetWorkloadAccessToken",
    "bedrock-agentcore:CreateWorkloadIdentity",
    ...
    "bedrock-agentcore:GetResourcePaymentToken"
]
```

---

## Part 5 — 创建 AgentCore Payments 资源

这部分创建：CDP credential provider → payment manager → connector → instrument（钱包）→ session（预算）。

### 5.1 装 boto3 ≥ 1.43

> preview API（`create_payment_*` 等）只在 boto3 ≥ 1.43 暴露，这个版本要求 Python ≥ 3.10。

```bash
uv venv --python 3.10 .venv-runbook    # 也可以用 3.11/3.12/3.14
source .venv-runbook/bin/activate
uv pip install 'boto3>=1.43.0' 'cdp-sdk>=1.0.0'
```

验证：

```bash
python -c "import boto3; print(boto3.__version__)"   # 应 ≥ 1.43.0
python -c "import boto3; c=boto3.client('bedrock-agentcore-control', region_name='us-west-2'); \
  print('create_payment_manager' in dir(c))"          # 应 True
```

### 5.2 在 CDP 项目里建 end user

> AgentCore 要求 `linkedAccounts.email` 必须指向 CDP 项目里**已存在的** end user。用占位邮箱（如 `test@example.com`）会报 `InternalServerException: Failed to create payment instrument`。

```bash
python scripts/runbook/02_create_cdp_end_user.py
```

预期输出：

```
created end user: user_id=<uuid> email=your-real@email.com
```

或者（如果之前已经建过）：

```
end user already exists: user_id=<uuid> email=your-real@email.com
```

### 5.3 建 Payments 全套资源

把 4.1 的 `ResourceRetrievalRoleArn` 填进环境变量，跑：

```bash
export RESOURCE_RETRIEVAL_ROLE_ARN='arn:aws:iam::<account>:role/AgentCorePaymentsResourceRetrievalRole'

python scripts/runbook/03_create_payments_resources.py
```

这一步如果配置都对了，会按顺序成功 5 步：

```
[1/5] Ensuring credential provider…
  provider_arn = arn:aws:bedrock-agentcore:us-west-2:...:token-vault/default/paymentcredentialprovider/X402CdpProvider
[2/5] Ensuring payment manager…
  manager_arn = arn:aws:bedrock-agentcore:us-west-2:...:payment-manager/x402manager-xxxxxxxxxx
[3/5] Ensuring payment connector…
  connector_id = x402cdpconnector-xxxxxxxxxx
[4/5] Creating embedded crypto wallet instrument (linked to your-real@email.com)…
  instrument_id  = payment-instrument-xxxxxxxxxxxxxxxx
  wallet_address = 0x9e944f57D44e757f0ef4acc6f18f406860ADdaA3
  WalletHub URL  = https://hub.cdp.coinbase.com/xxxxxxxxx
[5/5] Creating payment session (max $1.0)…
  session_id = payment-session-xxxxxxxxxxxxxxxx

=== PASTE INTO payer-agent/.env ===
MANAGER_ARN=...
PAYMENT_SESSION_ID=...
PAYMENT_INSTRUMENT_ID=...
USER_ID=test-user-12345

# Fund this wallet with testnet USDC at https://faucet.circle.com/
# Then open this URL once and authorize delegated signing:
#   https://hub.cdp.coinbase.com/xxxxxxxxx
# Wallet: 0x9e944f57...
```

记下：
- `wallet_address`（payer 钱包地址）—— **下一步要打 USDC 到这里**
- `WalletHub URL` —— 5.4 要打开的链接
- 下面那段 env 块 —— Part 6.1 要粘到 `.env`

> 这个脚本是幂等的——provider/manager/connector 重跑会复用现有的，但 instrument 和 session 每次都新建。如果只想刷新 session（比如预算用完），直接重跑即可，新的 IDs 会自动写入 `/tmp/agentcore_payments_out.json`。

### 5.4 ⚠️ WalletHub 一次性授权（必做）

> 不授权的话 ProcessPayment 时会报 "Delegated signing grant not active for your wallet"。

1. 浏览器打开 5.3 输出的 **WalletHub URL**
2. 选 **"Sign in with email"**（**不要**用 Google OAuth）
   - 输入 `CDP_END_USER_EMAIL`（步骤 2.2 设的那个邮箱）
   - 邮箱里收 OTP 验证码 → 输入
3. 登录后，**授权 Delegated Signing**（页面会有"Grant"或"Authorize"按钮）

> ⚠️ 用 Google OAuth 登录会出现 "No accounts found" —— CDP 会按认证方式建一个**新的** end user，新 user 下没钱包。必须用 Email OTP，跟 5.2 创建 end user 时用的认证方式保持一致。

### 5.5 Faucet 给 payer 钱包打 USDC

到 https://faucet.circle.com/ → 选 **Base Sepolia** → 粘贴 5.3 输出的 `wallet_address`（**不是** seller 地址！）→ 领取。

⚠️ 一定要打到 **payer 钱包**——就是 5.3 中 `instrument` 返回的那个地址。打错的话钱回不来（虽然是测试币没价值），但 Agent 没法签出有效付款。

等 30-60 秒到账，验证：

```bash
python scripts/runbook/04_check_balances.py <PAYER_WALLET> <SELLER_ADDRESS>
```

预期：

```
payer   0x9e944f57...: 10.000000 USDC  (10000000 units)
seller  0xF72bE878...: 0.000000 USDC  (0 units)
```

---

## Part 6 — 部署 Payer Agent

### 6.1 写 .env

```bash
cd payer-agent
cat > .env <<EOF
AWS_REGION=us-west-2

# 部署脚本会自动填这一行
AGENT_RUNTIME_ARN=

# Bedrock model — ⚠️ 必须用 inference profile（"us." 前缀），on-demand 不支持
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0

# 从 step 5.3 输出粘贴这四个
MANAGER_ARN=arn:aws:bedrock-agentcore:us-west-2:<account>:payment-manager/x402manager-xxxxxxxxxx
PAYMENT_SESSION_ID=payment-session-xxxxxxxxxxxxxxxx
PAYMENT_INSTRUMENT_ID=payment-instrument-xxxxxxxxxxxxxxxx
USER_ID=test-user-12345

# 从 step 4.1 输出粘贴
PROCESS_PAYMENT_ROLE_ARN=arn:aws:iam::<account>:role/AgentCorePaymentsProcessPaymentRole

# 从 step 3.2 输出粘贴
SELLER_API_URL=https://dxxxxxxxxxxxxx.cloudfront.net

API_PORT=8080
OTEL_EXPORTER_OTLP_ENDPOINT=
OTEL_CONSOLE_EXPORT=false
ENVIRONMENT=development
EOF
```

### 6.2 装 agent 依赖

```bash
uv venv --python 3.10 .venv
source .venv/bin/activate
uv pip install -e '.[dev]'
```

### 6.3 部署到 AgentCore Runtime

> 镜像必须是 **arm64**（AgentCore Runtime 强制）。`scripts/deploy_to_agentcore.py` 已经修复为 `docker buildx build --platform linux/arm64`。
> arm64 build 在 x86 机器上是 emulation，5-15 分钟。

```bash
AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 \
  python scripts/deploy_to_agentcore.py
```

成功输出：

```
Deploying x402PayerAgent to AgentCore Runtime (Container Mode)
  Region: us-west-2
  Role: arn:aws:iam::...:role/x402-payer-agent-runtime-role
Setting up ECR repository...
  Repository: ...amazonaws.com/x402-payer-agent
Building and pushing Docker image...
  Build complete
  Push complete
Creating AgentCore Runtime...
  Runtime ID: x402PayerAgent-xxxxxxxxxx
Waiting for runtime to be ready...
  Status: CREATING
  Status: READY

✅ Runtime deployed successfully!
   ARN: arn:aws:bedrock-agentcore:us-west-2:...:runtime/x402PayerAgent-xxxxxxxxxx
  Updated .env → AGENT_RUNTIME_ARN=...

Testing invocation...
  Response: ...
✅ Invocation test passed!
```

`payer-agent/.env` 已自动填好 `AGENT_RUNTIME_ARN`。

```bash
cd ..
```

---

## Part 7 — 端到端测试

```bash
python scripts/runbook/05_invoke_agent.py "Get me the premium article"
```

预期输出（Agent 用自然语言告诉你成功状态 + 链上 tx）：

```
>> runtime: arn:aws:bedrock-agentcore:us-west-2:...:runtime/x402PayerAgent-xxxxxxxxxx
>> prompt:  Get me the premium article
>> session: <uuid>

contentType: application/json
{
  "response": "Excellent! I've successfully retrieved the premium article for you. Here are the details:

  ## Payment Summary
  - Amount: 0.001 USDC (1000 micro-USDC)
  - Transaction: 0x9a96e6de7402b2e35134391b1269778040429190d7e072506f1135f984daf87b
  - Network: Base Sepolia (84532)
  - Status: Successfully settled

  ## Premium Article: \"The Future of AI and Blockchain Integration\"
  ...",
  "status": "success",
  "session_id": "..."
}
```

链上验证（5 秒后再跑，等 settlement 确认）：

```bash
python scripts/runbook/04_check_balances.py <PAYER_WALLET> <SELLER_ADDRESS>
```

```
payer   0x9e944f57...: 9.999000 USDC   ← 减少 0.001
seller  0xF72bE878...: 0.001000 USDC   ← 增加 0.001
```

到 https://sepolia.basescan.org/tx/<tx_hash> 可以看到链上的转账详情。

🎉 **完成了！** 你刚见证了一个 AI Agent 自主在测试网链上付款。

### 7.1 多跑几个

```bash
python scripts/runbook/05_invoke_agent.py "What services are available?"
python scripts/runbook/05_invoke_agent.py "Get me the weather data"
python scripts/runbook/05_invoke_agent.py "Get me the market analysis"
```

每次都会真扣钱：

| 服务 | 价格 (USDC) |
|------|------------|
| `get_premium_article` | 0.001 |
| `get_weather_data` | 0.0005 |
| `get_market_analysis` | 0.002 |
| `get_research_report` | 0.005 |
| `get_dataset` | 0.01 |
| `get_tutorial` | 0.003 |

session 预算（5.3 设的 `$1.0 USD`）够跑几百次。

---

## Part 8 — 调试命令

### 8.1 看 Agent Runtime 日志

```bash
RUNTIME_ID=$(grep AGENT_RUNTIME_ARN payer-agent/.env | sed 's|.*runtime/||')
aws logs tail /aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT \
  --region us-west-2 --since 5m --format short --follow
```

关心的关键行：
- `Headers: {...}` — 收到的 invocation 请求
- `process_payment` debug 日志
- `PROOF_GENERATED` — ProcessPayment 成功
- `httpx.HTTPStatusError 402/403/200` — content fetch 状态

### 8.2 看 Lambda@Edge 日志

> Lambda@Edge 日志写在执行节点的就近 region，不一定是部署 region。

```bash
# 找出所有 region 的 PaymentVerifier 日志组
for r in us-west-2 us-east-1 us-east-2 ap-southeast-1 eu-central-1; do
  echo "=== $r ==="
  aws logs describe-log-groups --region $r \
    --log-group-name-prefix /aws/lambda/us-east-1.X402SellerStack-PaymentVerifier \
    --query 'logGroups[].logGroupName' --output text 2>/dev/null
done

# 然后 tail 找到的那个
aws logs tail <log-group-name> --region <region> --since 5m
```

### 8.3 看链上 tx

```
https://sepolia.basescan.org/tx/<tx_hash>
```

### 8.4 直接调 ProcessPayment（绕过 Agent 调试用）

```bash
python - <<'EOF'
import boto3, uuid, json

# 1. assume ProcessPaymentRole
sts = boto3.client('sts')
creds = sts.assume_role(
    RoleArn='arn:aws:iam::<account>:role/AgentCorePaymentsProcessPaymentRole',
    RoleSessionName='debug',
)['Credentials']

dp = boto3.client(
    'bedrock-agentcore', region_name='us-west-2',
    aws_access_key_id=creds['AccessKeyId'],
    aws_secret_access_key=creds['SecretAccessKey'],
    aws_session_token=creds['SessionToken'],
)

# 2. process payment (using a dummy x402 payload)
r = dp.process_payment(
    paymentManagerArn='<MANAGER_ARN>',
    paymentSessionId='<SESSION_ID>',
    paymentInstrumentId='<INSTRUMENT_ID>',
    userId='test-user-12345',
    paymentType='CRYPTO_X402',
    paymentInput={'cryptoX402': {
        'version': '2',
        'payload': {
            'scheme': 'exact',
            'network': 'base-sepolia',
            'amount': '1000',
            'asset': '0x036CbD53842c5426634e7929541eC2318f3dCF7e',
            'payTo': '<SELLER_ADDR>',
            'maxTimeoutSeconds': 60,
            'extra': {'name': 'USDC', 'version': '2'},
        },
    }},
)
print(json.dumps(r, default=str, indent=2))
EOF
```

### 8.5 Session 预算用完了

```bash
python - <<'EOF'
import boto3
dp = boto3.client('bedrock-agentcore', region_name='us-west-2')
s = dp.create_payment_session(
    paymentManagerArn='<MANAGER_ARN>',
    userId='test-user-12345',
    expiryTimeInMinutes=480,
    limits={'maxSpendAmount': {'value': '5.0', 'currency': 'USD'}},
)
print(s['paymentSession']['paymentSessionId'])
EOF
# 然后把新 ID 填进 payer-agent/.env，重部 agent
```

---

## Part 9 — 清理（很重要）

跑完 demo 别让 CloudFront / Runtime / ECR 一直挂着浪费钱。按依赖顺序删：

### 9.1 删 AgentCore Runtime + ECR

```bash
# 列出所有 x402PayerAgent runtime
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2 \
  --query 'agentRuntimes[?starts_with(agentRuntimeName, `x402PayerAgent`)].agentRuntimeId' \
  --output text

# 一个个删
for r in $(aws bedrock-agentcore-control list-agent-runtimes --region us-west-2 \
  --query 'agentRuntimes[?starts_with(agentRuntimeName, `x402PayerAgent`)].agentRuntimeId' \
  --output text); do
  aws bedrock-agentcore-control delete-agent-runtime \
    --agent-runtime-id $r --region us-west-2
done

# ECR repo
aws ecr delete-repository --repository-name x402-payer-agent --force --region us-west-2
```

### 9.2 删 AgentCore Payments 资源

```bash
python - <<'EOF'
import boto3
cp = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
dp = boto3.client("bedrock-agentcore", region_name="us-west-2")

managers = [m for m in cp.list_payment_managers()["paymentManagers"] if m["name"] == "X402Manager"]
for m in managers:
    arn = m["paymentManagerArn"]; mid = m["paymentManagerId"]
    # sessions
    for s in dp.list_payment_sessions(paymentManagerArn=arn).get("paymentSessions", []):
        dp.delete_payment_session(paymentSessionId=s["paymentSessionId"], paymentManagerArn=arn)
    # instruments
    for i in dp.list_payment_instruments(paymentManagerArn=arn).get("paymentInstruments", []):
        dp.delete_payment_instrument(paymentInstrumentId=i["paymentInstrumentId"], paymentManagerArn=arn)
    # connectors
    for c in cp.list_payment_connectors(paymentManagerId=mid).get("paymentConnectors", []):
        cp.delete_payment_connector(paymentConnectorId=c["paymentConnectorId"])
    # manager
    cp.delete_payment_manager(paymentManagerId=mid)

# provider
try:
    cp.delete_payment_credential_provider(name="X402CdpProvider")
    print("Deleted credential provider")
except Exception as e:
    print(f"(provider delete skipped: {e})")
EOF
```

### 9.3 删 CDK stacks

```bash
cd seller-infrastructure
AWS_REGION=us-east-1 npx cdk destroy --force
cd ..

cd payer-infrastructure
AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 \
  npx cdk destroy --all --force
cd ..
```

### 9.4 不会被自动清理的（不影响）

- **CDP 端的 end user 和 wallet**：在 Coinbase 那边，留着不要钱
- **Base Sepolia 上的测试 USDC**：测试币没价值
- **CDKToolkit bootstrap stack**：跨项目通用，不用删

---

## Part 10 — 故障排查矩阵

按"看到的错误"找原因。如果没找到对应条目，去 `DEPLOYMENT_NOTES.md` 看更详细的复盘。

### 10.1 boto3 / Python 相关

| 症状 | 原因 | 修法 |
|------|------|------|
| `AttributeError: ... has no attribute 'create_payment_manager'` | boto3 < 1.43 | `uv pip install 'boto3>=1.43'` |
| `Because the current Python version (3.9.x) does not satisfy Python>=3.10` | 系统 Python 太旧 | `uv venv --python 3.10` 或装 Python 3.14 |
| `cdp-sdk` import 失败 | 没装 | `uv pip install 'cdp-sdk'` |

### 10.2 CDK 部署相关

| 症状 | 原因 | 修法 |
|------|------|------|
| Payer stack 跑到了 us-east-1 不是 us-west-2 | shell 的 `AWS_REGION` 优先级高于 CDK 配置 | 显式传 `AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2` |
| `Lambda@Edge functions can only be created in us-east-1` | CDK env 没指 us-east-1 | 部 seller 时显式 `aws://${ACCOUNT}/us-east-1` |
| CDK bootstrap 报 `does not exist` | 该 region/account 还没 bootstrap | `npx cdk bootstrap aws://${ACCOUNT}/<region>` |

### 10.3 AgentCore Payments API 相关

| 症状 | 原因 | 修法 |
|------|------|------|
| `create_payment_manager` AccessDenied | CDK 给 ResourceRetrievalRole 的 IAM 权限错了 | 跑 `01_patch_iam_role.sh` |
| `create_payment_instrument` 500 InternalServerException | CDP 的 Delegated Signing 没启 | CDP Portal Embedded Wallets > Policies 启用 |
| `create_payment_instrument` 500 InternalServerException | `linkedAccounts.email` 不是真实 end user | 跑 `02_create_cdp_end_user.py` |
| `create_payment_instrument` 字段错（`cryptoWallet must be EMBEDDED_CRYPTO_WALLET`） | API schema 跟 QUICKSTART.md 不一致（preview 改过） | 用 `EMBEDDED_CRYPTO_WALLET` + `embeddedCryptoWallet` + `linkedAccounts` |
| `create_payment_session` 报 `expiryDuration` 不识别 | 字段改名了 | 用 `expiryTimeInMinutes` |
| ProcessPayment 报 "Delegated signing grant not active" | WalletHub 没授权 | Part 5.4 |
| ProcessPayment 报 "Budget exceeded" | session 预算用光 | Part 8.5 重建 session |
| ProcessPayment 报 "Insufficient funds" | payer 钱包没 USDC 或打错地址 | 重新 faucet 到正确地址（Part 5.5）|

### 10.4 WalletHub / CDP 用户授权

| 症状 | 原因 | 修法 |
|------|------|------|
| `Project must specify a valid CORS origin` | CDP 没配 origin | Part 1.3 |
| WalletHub 登录后显示 "No accounts found" | 用 Google OAuth 登的，CDP 建了一个新的 end user | 改用 Email OTP 登录（用建 end user 时的邮箱） |
| WalletHub 收不到 OTP 验证码 | 邮箱不对/被屏蔽 | 检查垃圾邮件，确认 `CDP_END_USER_EMAIL` 真实有效 |

### 10.5 AgentCore Runtime 部署

| 症状 | 原因 | 修法 |
|------|------|------|
| `ValidationException: Architecture incompatible ... Supported platforms: [arm64]` | docker build 是 amd64 | 已修：`scripts/deploy_to_agentcore.py` 用 `docker buildx --platform linux/arm64` |
| docker build 失败 `unknown flag: --load` | buildx 太旧 | `docker buildx version` 看一下，旧版本升级 Docker |
| Runtime 起来后调用 422 + `Invalid HTTP request received` | FastAPI Pydantic strict 模式跟 AgentCore proxy 协议层冲突 | 已修：`/invocations` 改读 raw body |
| Runtime 起来后调用报 `bedrock:CountTokens AccessDenied` 但能继续运行 | 用了 inference profile 而 IAM 没 CountTokens 权限 | 不致命，agent fallback 到估算 |
| Runtime 调用报 `Invocation of model ID ... with on-demand throughput isn't supported` | 用了纯 model ID 而不是 inference profile | `BEDROCK_MODEL_ID` 用 `us.anthropic.claude-...` |

### 10.6 端到端付款链路

| 症状 | 原因 | 修法 |
|------|------|------|
| ProcessPayment 成功但 retry 时 seller 返回 403 + `Error from cloudfront` | agent retry 用 POST 被 CloudFront `/api/*` 拦截（只允许 GET/HEAD/OPTIONS） | 已修：`request_content_with_payment` 改用 GET |
| ProcessPayment 成功，retry 还是 402 几次后超时 | Base Sepolia 链上 settlement 慢（一个 block ≈ 2 秒） | 等 5-10 秒再重试 |
| Seller 始终 402 | Lambda@Edge 验签失败 | 看 Part 8.2 Lambda@Edge 日志，找 `signature mismatch` 之类 |
| Faucet 钱"消失了" | 打到 seller 地址而非 payer | Part 5.5 重打到正确 wallet_address |
| Agent 回复 "$1000 USDC" | LLM 把 raw units 当美元了，实际是 0.001 USDC（`amount: "1000"` × 10^-6） | 不影响功能，理解为 `1000 micro-USDC` 即可 |

### 10.7 找不到这里？

去 `DEPLOYMENT_NOTES.md` —— 那里按时间顺序记了 14 个真实踩坑，每个都有详细背景。

---

## 附录 — 文件清单与下一步

### A1. 文件清单

```
sample-agentcore-cloudfront-x402-payments/
├── DEPLOY.md                    ← 本文档（一份就够）
├── RUNBOOK.md                   ← 简版逐步手册（DEPLOY 子集）
├── ARCHITECTURE.md              ← 原理深入（x402、EIP-3009、信任模型）
├── DEPLOYMENT_NOTES.md          ← 实际部署日志（14 个坑的复盘）
├── README.md / QUICKSTART.md    ← 仓库原带（preview 早期版本，已过时，参考即可）
│
├── scripts/runbook/
│   ├── 01_patch_iam_role.sh           ← 补 IAM 权限
│   ├── 02_create_cdp_end_user.py      ← 建 CDP end user
│   ├── 03_create_payments_resources.py ← 建 Payments 全套（幂等）
│   ├── 04_check_balances.py           ← 链上余额查询
│   └── 05_invoke_agent.py             ← 调 deployed runtime
│
├── seller-infrastructure/       ← CDK: CloudFront + Lambda@Edge (us-east-1)
│   └── .env                     ← PAYMENT_RECIPIENT_ADDRESS=...
│
├── payer-infrastructure/        ← CDK: IAM roles (us-west-2)
│
└── payer-agent/                 ← Strands Agent + AgentCore Runtime
    ├── .env                     ← MANAGER_ARN / SELLER_API_URL / etc.
    ├── scripts/deploy_to_agentcore.py    ← 已修复 (arm64 + env vars)
    ├── agent/api_server.py               ← 已修复 (/invocations raw body)
    └── agent/tools/content.py            ← 已修复 (retry GET)
```

### A2. 想做更多

- **跑 Web UI**：`cd web-ui && npm install && npm run dev`，配合 `payer-agent/.venv/bin/python -m agent.api_server` 起后端
- **改用更便宜模型**：把 `BEDROCK_MODEL_ID` 换成 `us.anthropic.claude-haiku-4-5-20251001-v1:0`，速度更快
- **加自定义付费 endpoint**：改 `seller-infrastructure/lib/lambda-edge/content-config.ts` 加新路径和价格，然后 `cdk deploy`
- **跑端到端测试套件**：`cd payer-agent && pytest tests/ -v`
- **给上游回贡献**：本仓库改动是高质量 PR 素材（4 个真 bug + runbook），有兴趣可以提 PR 到 https://github.com/aws-samples/sample-agentcore-cloudfront-x402-payments

### A3. 一句话总结

```
CDP 配置 (Part 1) → 克隆 (Part 2) → seller (Part 3) → payer infra + IAM (Part 4)
   → 建 Payments 资源 (Part 5) → 部 agent (Part 6) → 跑测试 (Part 7) → 用完清理 (Part 9)
```

跑通的那一刻，你会在 https://sepolia.basescan.org/ 上看到一个 AI Agent 自主发起的 0.001 USDC 转账。这是从一个被遗忘 26 年的 HTTP 状态码，到 AI Agent 真正自主参与经济活动的，**完整一条端到端链路**。
