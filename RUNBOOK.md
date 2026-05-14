# 复现手册

从零开始把这套 demo 在你自己的 AWS 账号上跑通，端到端达成"AI Agent 用测试网 USDC 自主付款"。

适用于 2026-05 之后的 AgentCore Payments preview。本手册已基于一次完整成功部署调通过（参见 `DEPLOYMENT_NOTES.md` 第 14 个坑都踩完了），按顺序照做即可。

预计**首次部署**：1-2 小时（其中 CloudFront 分发占 10-15 分钟，docker arm64 build 占 5-10 分钟，剩下都是配置）。

---

## Part 0 — 你需要准备的东西

### 软件（本机）

| 工具 | 版本 | 检查命令 |
|------|------|---------|
| AWS CLI | 2.x | `aws --version` |
| AWS CDK | 2.x | `npx cdk --version`（不需要全局装） |
| Node.js | 18+ | `node --version` |
| Docker + buildx | 25+ | `docker buildx version` |
| Python | 3.10+ | `python3 --version`（系统 Python 不够新就用 `uv venv --python 3.14`） |
| `uv` | 任意 | `uv --version`（没有的话：`curl -LsSf https://astral.sh/uv/install.sh \| sh`） |

### 账号

- **AWS 账号**：本机 `aws sts get-caller-identity` 能回复你的 ARN，用户最好有 admin 权限（否则要自己挑 IAM policy）
- **Coinbase CDP 项目**：https://portal.cdp.coinbase.com/ 免费注册
- **两个 Base Sepolia 钱包地址**：一个做 seller 收款，另一个由 CDP 自动生成做 payer

### 钱

- **AWS**：CloudFront、Lambda@Edge、AgentCore Runtime 都按用量计费。Demo 流量下，**12 小时跑下来 $1 都不到**。但跑完别忘了 `cdk destroy`（步骤在最后）。
- **测试网 USDC**：Circle faucet 免费

---

## Part 1 — Coinbase CDP 一次性配置

### 1.1 拿三个 key

登录 https://portal.cdp.coinbase.com/，新建 / 选一个 project：

1. **API Keys** 页面 → 创建 Secret API Key（Ed25519）
   - 拿到 `id`（UUID 格式）和 `privateKey`（很长的 base64）
2. **Server Wallets** → **Wallet API** → 生成 **Wallet Secret**
   - 拿到 `walletSecret`（这是**第三个独立的 secret**，不是 API key 的一部分；只显示一次，立刻保存）

### 1.2 启用 Embedded Wallets 的 Delegated Signing

> ⚠️ 这一步不做，后面 `create_payment_instrument` 会 500 失败。

CDP Portal → **Embedded Wallets** → **Policies** → 启用 **Delegated signing**

### 1.3 允许 WalletHub 的 origin

> ⚠️ 不做的话用户授权时报 `Project must specify a valid CORS origin`。

Embedded Wallets → **CORS** / **Allowed Origins** → 加 `https://hub.cdp.coinbase.com`

---

## Part 2 — 克隆与初步设置

```bash
git clone https://github.com/aws-samples/sample-agentcore-cloudfront-x402-payments
cd sample-agentcore-cloudfront-x402-payments
```

把 CDP 三个 key 和你的邮箱设进环境变量（**这一段每次新开 shell 都要重设**，不要写进 `.env` 或 git commit）：

```bash
export CDP_API_KEY_ID='<id>'
export CDP_API_KEY_SECRET='<privateKey 整段 base64>'
export CDP_WALLET_SECRET='<walletSecret>'
export CDP_END_USER_EMAIL='your-real@email.com'   # 待会儿要登 WalletHub 收 OTP，必须是真邮箱
```

---

## Part 3 — 部署 Seller（CloudFront + Lambda@Edge）

> 这一段固定 `us-east-1`（Lambda@Edge 限制）。约 10-15 分钟（CloudFront 分发慢）。

### 3.1 写 seller 的 .env

```bash
echo "PAYMENT_RECIPIENT_ADDRESS=<你的卖家地址>" > seller-infrastructure/.env
```

`<你的卖家地址>` 是任何你控制的 Base Sepolia 地址（MetaMask 新建账户切到 Base Sepolia 复制即可）。这个地址是收钱用的。

### 3.2 部署

```bash
cd seller-infrastructure
npm install
npx cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1   # 首次
npx cdk deploy --require-approval never
```

部署完记下输出里的 **`X402DistributionUrl`**（形如 `https://dxxxxxxxxxxxxx.cloudfront.net`）。

```bash
cd ..
```

---

## Part 4 — 部署 Payer Infrastructure（IAM 角色）

> 这一段必须 `us-west-2`（AgentCore Payments 当前只支持的 region）。

### 4.1 部署

⚠️ **必须显式传所有 region 变量**——只设 `AWS_REGION` 不够，CDK 会优先读 shell 的 `AWS_REGION`。

```bash
cd payer-infrastructure
npm install
npx cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-west-2

AWS_REGION=us-west-2 \
AWS_DEFAULT_REGION=us-west-2 \
CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy --all --require-approval never
```

部署完记下：
- `ProcessPaymentRoleArn`（形如 `arn:aws:iam::<account>:role/AgentCorePaymentsProcessPaymentRole`）
- `ResourceRetrievalRoleArn`

```bash
cd ..
```

### 4.2 ⚠️ 关键：补 IAM 策略

CDK 给 `AgentCorePaymentsResourceRetrievalRole` 挂的权限是错的（用了不存在的 action 名），要手动补齐才能 `create_payment_*`：

```bash
AWS_REGION=us-west-2 ./scripts/runbook/01_patch_iam_role.sh
```

这一步加上 `CreateWorkloadIdentity` / `GetWorkloadAccessToken` / `GetResourcePaymentToken` 等权限，并把 Resource 扩到 `token-vault/default/*`。

---

## Part 5 — 创建 AgentCore Payments 资源

### 5.1 装 boto3 ≥ 1.43（preview API 需要）

```bash
uv venv --python 3.10 .venv-runbook    # 或 3.11/3.12/3.14 都行
source .venv-runbook/bin/activate
uv pip install 'boto3>=1.43.0' 'cdp-sdk>=1.0.0'
```

### 5.2 在 CDP 项目里建 end user

> ⚠️ 不做的话 `create_payment_instrument` 会报 500 `InternalServerException: Failed to create payment instrument`。
> AgentCore 要求 `linkedAccounts.email` 必须是 CDP **已存在的** end user 邮箱，不能是占位符。

```bash
python scripts/runbook/02_create_cdp_end_user.py
```

输出形如 `created end user: user_id=feba4777-... email=your-real@email.com`。

### 5.3 创建 Payments 资源

把 4.1 输出的 `ResourceRetrievalRoleArn` 设进环境，然后跑：

```bash
export RESOURCE_RETRIEVAL_ROLE_ARN='arn:aws:iam::<account>:role/AgentCorePaymentsResourceRetrievalRole'

python scripts/runbook/03_create_payments_resources.py
```

成功后会打印一段 `=== PASTE INTO payer-agent/.env ===`，里面有：
- `MANAGER_ARN`
- `PAYMENT_SESSION_ID`
- `PAYMENT_INSTRUMENT_ID`
- **`WALLET_ADDRESS`**（payer 钱包地址，下一步要给它打 USDC）
- **`WALLETHUB_URL`**（一次性授权页）

> 脚本是幂等的——provider/manager/connector 重跑会复用现成的，但 instrument 和 session 每次新建。如果你只是想刷新 session，直接重跑即可。

### 5.4 ⚠️ 关键：去 WalletHub 一次性授权

> 不授权的话 ProcessPayment 时会报 "Delegated signing grant not active"。

1. 浏览器打开 `WALLETHUB_URL`
2. **必须用 Email OTP 登录**——填 `CDP_END_USER_EMAIL`，邮箱里收验证码
   - ⚠️ 不要用 Google OAuth：CDP 会按认证方式建一个**新的** end user，新 user 下没你的钱包，会显示 "No accounts found"
3. 登录后，授权 **Delegated Signing**

### 5.5 给 payer 钱包打测试网 USDC

到 https://faucet.circle.com/ → 选 **Base Sepolia** → 粘贴 `WALLET_ADDRESS`（步骤 5.3 输出的那个）→ 领取。

> ⚠️ 一定打到 **payer wallet**（步骤 5.3 输出的），不是 seller 地址。Faucet 一般给 10 USDC，够跑很多次（demo 最贵 0.01 USDC/次）。

等 30-60 秒，检查到账：

```bash
python scripts/runbook/04_check_balances.py <PAYER_WALLET_ADDR> <SELLER_ADDR>
```

应该看到 payer ≈ 10 USDC，seller = 0。

---

## Part 6 — 部署 Payer Agent

### 6.1 写 .env

```bash
cd payer-agent
cat > .env <<EOF
AWS_REGION=us-west-2

# 留空，部署脚本会自动填
AGENT_RUNTIME_ARN=

# Bedrock model — 必须用 inference profile（不能用 on-demand）
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0

# 从 step 5.3 输出粘贴
MANAGER_ARN=arn:aws:bedrock-agentcore:us-west-2:<account>:payment-manager/x402manager-xxxxxxxxxx
PAYMENT_SESSION_ID=payment-session-xxxxxxxxxxxxxxxx
PAYMENT_INSTRUMENT_ID=payment-instrument-xxxxxxxxxxxxxxxx
PROCESS_PAYMENT_ROLE_ARN=arn:aws:iam::<account>:role/AgentCorePaymentsProcessPaymentRole
USER_ID=test-user-12345

# 从 step 3.2 部署输出粘贴
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

> 镜像必须是 **arm64**（AgentCore Runtime 强制要求）。仓库里这个 sample 已经在 `scripts/deploy_to_agentcore.py` 里改用了 `docker buildx build --platform linux/arm64`，并把 `.env` 里 AgentCore Payments 相关的环境变量都注入到 runtime 容器（这两点是部署成功的前提，已合入仓库）。

> arm64 build 在 x86 机器上是 emulation，会比较慢，正常 5-15 分钟。

```bash
AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 \
  python scripts/deploy_to_agentcore.py
```

成功后输出形如：
```
✅ Runtime deployed successfully!
   ARN: arn:aws:bedrock-agentcore:us-west-2:...:runtime/x402PayerAgent-xxxxxxxxxx
  Updated .env → AGENT_RUNTIME_ARN=...
```

`.env` 自动被填好了。

```bash
cd ..
```

---

## Part 7 — 端到端测试

```bash
python scripts/runbook/05_invoke_agent.py "Get me the premium article"
```

期望输出（agent 会用自然语言告诉你成功状态 + 链上 tx）：

```json
{
  "response": "Excellent! I've successfully retrieved the premium article for you...
   Payment: 0.001 USDC. Transaction: 0x...
   The transaction has been successfully settled on-chain.",
  "status": "success",
  "session_id": "..."
}
```

链上验证：

```bash
python scripts/runbook/04_check_balances.py <PAYER_WALLET_ADDR> <SELLER_ADDR>
# payer  ...: 9.999000 USDC   <- 减少 0.001
# seller ...: 0.001000 USDC   <- 增加 0.001
```

如果链上没有变化但 agent 报 success——大概率是 settlement 还没确认（Base Sepolia 一个 block ≈ 2 秒），等 5 秒重查。

---

## Part 8 — 常用调试命令

### 看 agent runtime 日志

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<RUNTIME_ID>-DEFAULT \
  --region us-west-2 --since 5m --format short --follow
```

### 看 Lambda@Edge 日志

> 注意 Lambda@Edge 日志写在**执行节点的就近 region**，不一定是部署 region。

```bash
# 列出所有 region 的 PaymentVerifier 日志组
for r in us-west-2 us-east-1 us-east-2 ap-southeast-1; do
  echo "=== $r ==="
  aws logs describe-log-groups --region $r \
    --log-group-name-prefix /aws/lambda/us-east-1.X402 \
    --query 'logGroups[].logGroupName' --output text
done

# 然后 tail
aws logs tail <log-group-name> --region <r> --since 5m
```

### 看链上 tx 详情

打开 `https://sepolia.basescan.org/tx/<tx_hash>`，可以看到 from/to/amount/gas。

### Session 预算用完了

ProcessPayment 报 "Budget exceeded"：用脚本重新生成一个 session：

```bash
python scripts/runbook/03_create_payments_resources.py
# 复制新的 PAYMENT_SESSION_ID 到 payer-agent/.env，重部 runtime
```

或者跳过资源重建直接调 API：

```python
import boto3
dp = boto3.client("bedrock-agentcore", region_name="us-west-2")
s = dp.create_payment_session(
    paymentManagerArn="<MANAGER_ARN>",
    userId="test-user-12345",
    expiryTimeInMinutes=480,
    limits={"maxSpendAmount": {"value": "5.0", "currency": "USD"}},
)
print(s["paymentSession"]["paymentSessionId"])
```

---

## Part 9 — 清理（很重要）

跑完 demo 别让 CloudFront / Runtime / ECR 一直挂着浪费钱。

### 9.1 删 AgentCore Runtime + ECR 镜像

```bash
# 列出所有 x402PayerAgent runtime
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2 \
  --query 'agentRuntimes[?starts_with(agentRuntimeName, `x402PayerAgent`)].agentRuntimeId' \
  --output text

# 一个个删
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id <runtime-id> --region us-west-2

# ECR 镜像
aws ecr delete-repository --repository-name x402-payer-agent --force --region us-west-2
```

### 9.2 删 AgentCore Payments 资源

```python
import boto3
cp = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
dp = boto3.client("bedrock-agentcore", region_name="us-west-2")

# Sessions
for s in dp.list_payment_sessions(paymentManagerArn="<MANAGER_ARN>")["paymentSessions"]:
    dp.delete_payment_session(paymentSessionId=s["paymentSessionId"], paymentManagerArn="<MANAGER_ARN>")

# Instruments
for i in dp.list_payment_instruments(paymentManagerArn="<MANAGER_ARN>")["paymentInstruments"]:
    dp.delete_payment_instrument(paymentInstrumentId=i["paymentInstrumentId"], paymentManagerArn="<MANAGER_ARN>")

# Connectors → Manager → Provider
for m in cp.list_payment_managers()["paymentManagers"]:
    if m["name"] != "X402Manager":
        continue
    for c in cp.list_payment_connectors(paymentManagerId=m["paymentManagerId"])["paymentConnectors"]:
        cp.delete_payment_connector(paymentConnectorId=c["paymentConnectorId"])
    cp.delete_payment_manager(paymentManagerId=m["paymentManagerId"])

cp.delete_payment_credential_provider(name="X402CdpProvider")
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

### 9.4 不会被自动清理的

- **CDP 端的 end user 和 wallet**：保留在 Coinbase 那边，不影响。如果想删，在 CDP Portal 里手动操作
- **Base Sepolia 上的测试 USDC**：测试币没价值，留着即可
- **ECR Bootstrap stack（CDKToolkit）**：跨 demo 通用，不用删

---

## Part 10 — 全部踩过的坑速查

如果你卡在某一步，先到这里查：

| 症状 | 原因 | 修法 |
|---|---|---|
| `boto3` 没有 `create_payment_*` API | boto3 < 1.43 | 升级 + Python ≥ 3.10 |
| `cdk deploy` 部到错的 region | shell `AWS_REGION` 优先 | 显式传 `AWS_REGION=...` `AWS_DEFAULT_REGION=...` |
| `create_payment_manager` AccessDenied | CDK IAM policy 错 | 跑 `01_patch_iam_role.sh` |
| `create_payment_instrument` 500 | CDP delegated signing 没启 | CDP Portal 启用 |
| `create_payment_instrument` 500 | linkedAccounts.email 不是真实 end user | 跑 `02_create_cdp_end_user.py` |
| WalletHub `Project must specify a valid CORS origin` | CDP 没配 origin | CDP Portal 加 `https://hub.cdp.coinbase.com` |
| WalletHub "No accounts found" | 用 Google OAuth 登录建了新 user | 改用 Email OTP 登录 |
| AgentCore Runtime `Architecture incompatible` | docker build 不是 arm64 | 已修；用 `docker buildx --platform linux/arm64` |
| Runtime 422 + uvicorn "Invalid HTTP request" | FastAPI strict 模式问题 | 已修；`/invocations` 读 raw body |
| `bedrock:CountTokens` AccessDenied | Bedrock model 用 on-demand 不支持 | 用 `us.anthropic.claude-...` inference profile |
| ProcessPayment 报 "Delegated signing grant not active" | WalletHub 没授权 | 跑 5.4 |
| 付款后 seller 还 403 | agent 用 POST retry 被 CloudFront 拦截 | 已修；`request_content_with_payment` 改用 GET |
| Faucet 给的钱不见了 | 打到 seller 地址而不是 payer | 重打到 instrument 返回的 walletAddress |

详细原因和文档参考见 `DEPLOYMENT_NOTES.md`。

---

## 文件清单

```
sample-agentcore-cloudfront-x402-payments/
├── RUNBOOK.md                  ← 本文档
├── DEPLOYMENT_NOTES.md          ← 部署日志（包含所有踩坑细节）
├── ARCHITECTURE.md              ← 原理与架构说明
├── scripts/runbook/
│   ├── 01_patch_iam_role.sh
│   ├── 02_create_cdp_end_user.py
│   ├── 03_create_payments_resources.py
│   ├── 04_check_balances.py
│   └── 05_invoke_agent.py
├── seller-infrastructure/       ← CDK: CloudFront + Lambda@Edge (us-east-1)
├── payer-infrastructure/        ← CDK: IAM roles (us-west-2)
└── payer-agent/                 ← Strands Agent + AgentCore Runtime
    ├── .env                     ← 你的配置（不入 git）
    ├── scripts/deploy_to_agentcore.py   ← 已修复 arm64 + env vars
    ├── agent/api_server.py      ← 已修复 /invocations raw body
    └── agent/tools/content.py   ← 已修复 retry 用 GET
```

---

## 一句话总结流程

```
CDP 配置 (Part 1)
   ↓
seller CDK (Part 3, us-east-1)
   ↓
payer CDK (Part 4, us-west-2) → 补 IAM (4.2)
   ↓
建 CDP end user (5.2) → 建 Payments 资源 (5.3) → WalletHub 授权 (5.4) → faucet (5.5)
   ↓
部署 agent (Part 6)
   ↓
端到端测试 (Part 7)
```

跑通的那一刻，你会在 https://sepolia.basescan.org/ 上看到一个 AI Agent 自主发起的 0.001 USDC 转账。
