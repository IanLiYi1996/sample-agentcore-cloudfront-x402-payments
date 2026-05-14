# 部署实战记录

AgentCore Payments + CloudFront x402 端到端部署与调试日志（2026-05-12）。

本文档记录从零搭建到链上付款成功的完整过程，包含官方 `QUICKSTART.md` 未覆盖的若干坑及修复方法。
官方文档基于 preview 早期 schema，launch 后 API 和 CDP 依赖都变了。

## 最终成果

- **Payment tx**: [`0x9a96e6de7402b2e35134391b1269778040429190d7e072506f1135f984daf87b`](https://sepolia.basescan.org/tx/0x9a96e6de7402b2e35134391b1269778040429190d7e072506f1135f984daf87b)
- **Payer**: `0x9e944f57D44e757f0ef4acc6f18f406860ADdaA3` （-0.001 USDC）
- **Seller**: `0xF72bE87831Fe0536314e23df4b8F97e42F3B017B` （+0.001 USDC）
- **Block**: 41404209 on Base Sepolia

Agent 用自然语言请求 `"Get me the premium article"`，完整走完 402 → ProcessPayment → EIP-3009 链上转账 → 200 + content，全程 ~3 秒。

## 架构

```
┌──────────────┐  invoke_agent_runtime   ┌────────────────────────┐
│  Test client │────────────────────────▶│ AgentCore Runtime      │
└──────────────┘                         │ (container, arm64)     │
                                         │   FastAPI + Strands    │
                                         │   Claude Sonnet 4      │
                                         └───────┬────────────────┘
                                                 │
        ┌────────────────────────────────────────┼──────────────────┐
        │                                        │                  │
        ▼                                        ▼                  ▼
┌──────────────────┐  GET /api/premium-  ┌──────────────┐    ProcessPayment
│ CloudFront +     │◀──────────article   │ request_     │    ┌────────────────┐
│ Lambda@Edge      │  + X-PAYMENT hdr    │ content_     │───▶│ AgentCore      │
│ (seller)         │─────────────────────▶ with_payment │    │ Payments       │
└───────┬──────────┘                     └──────────────┘    │ (us-west-2)    │
        │ broadcast EIP-3009                                 └──────┬─────────┘
        ▼                                                           │
┌───────────────────────────────────────────────────────────────────┴───────┐
│                       Coinbase CDP Server Wallet                          │
│  Embedded Wallet (email OTP) + Delegated Signing (via walletSecret)       │
│  USDC transferWithAuthorization on Base Sepolia                           │
└───────────────────────────────────────────────────────────────────────────┘
```

## 前置条件

| 资源 | 说明 |
|------|------|
| AWS 账号 | `463470973226`，IAM user `ianlee` 具备 admin 权限 |
| AWS CLI | 配置 `us-east-1` 为默认（CloudFront / Lambda@Edge 约束） |
| Node 18+, CDK 2.x | seller 和 payer 基础设施 |
| Docker + buildx | 构建 **arm64** 容器（AgentCore Runtime 要求） |
| Python 3.10+ | payer-agent SDK 要求；本机用 `uv venv --python 3.14` |
| Coinbase CDP | API key（`id` + `privateKey`）+ **独立的** `walletSecret` |
| 两个 Base Sepolia 地址 | 一个自己的作为 seller 收款地址；另一个 payer 由 CDP 自动生成 |

### CDP 侧前置动作（官方文档未强调）

1. **启用 Embedded Wallets 的 Delegated Signing**：CDP Portal → Project → Wallet → **Embedded Wallets** → Policies → 启用 "Delegated signing"。没启用会在 `create_payment_instrument` 阶段 500 失败。
2. **WalletHub 允许 CORS origin**：Embedded Wallets → CORS → 加 `https://hub.cdp.coinbase.com`。没配会导致 WalletHub 登录时报 `Project must specify a valid CORS origin`。
3. **CDP 里预先存在 end user**：`linkedAccounts.email` 必须指向 CDP 项目里已登记的 email end user，**不能是占位符**如 `test@example.com`。

## 部署顺序

### Step 1 — Seller infra（us-east-1）

```bash
cd seller-infrastructure
echo "PAYMENT_RECIPIENT_ADDRESS=0xF72bE87831Fe0536314e23df4b8F97e42F3B017B" > .env
npm install
npx cdk bootstrap aws://463470973226/us-east-1
npx cdk deploy --require-approval never
```

产出：
- `DistributionUrl = https://doogv4r5wyctf.cloudfront.net`
- `PaymentApiEndpoint = https://doogv4r5wyctf.cloudfront.net/api/`

CloudFront 分发生效 ~10 分钟。

### Step 2 — Payer infra（us-west-2）

⚠️ **必须显式指定 region**，否则跟着 `AWS_REGION` 环境变量走，可能误放到 `us-east-1`。

```bash
cd ../payer-infrastructure
npm install
npx cdk bootstrap aws://463470973226/us-west-2
AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy --all --require-approval never
```

产出 IAM 角色：
- `AgentCorePaymentsResourceRetrievalRole`
- `AgentCorePaymentsProcessPaymentRole`
- `AgentCorePaymentsManagementRole`
- `x402-payer-agent-runtime-role`

### Step 3 — 补 IAM 策略（仓库 CDK 缺失）

CDK 给 `ResourceRetrievalRole` 挂的 policy 权限不全，导致 `create_payment_manager` / `create_payment_instrument` 报 500。**必须手动补齐**：

```bash
aws iam put-role-policy --role-name AgentCorePaymentsResourceRetrievalRole \
  --policy-name WorkloadIdentityPatch \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:GetWorkloadIdentity",
        "bedrock-agentcore:GetResourcePaymentToken"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:us-west-2:463470973226:token-vault/default",
        "arn:aws:bedrock-agentcore:us-west-2:463470973226:token-vault/default/*",
        "arn:aws:bedrock-agentcore:us-west-2:463470973226:token-vault/default/paymentcredentialprovider/*",
        "arn:aws:bedrock-agentcore:us-west-2:463470973226:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:us-west-2:463470973226:workload-identity-directory/default/workload-identity/*"
      ]
    }]
  }'
```

### Step 4 — 创建 AgentCore Payments 资源

用 `/tmp/create_payments.py` 创建（脚本内容已保存）。关键点：

- 用 **boto3 ≥ 1.43**（preview API 在 1.42 及以下未暴露）；本机 Python 3.9 不够用，用 `uv venv --python 3.14`。
- `paymentInstrumentType="EMBEDDED_CRYPTO_WALLET"`（**不是** QUICKSTART 里的 `CRYPTO_WALLET`）
- `paymentInstrumentDetails.embeddedCryptoWallet`（**不是** `cryptoWallet`）
- 必填 `linkedAccounts`，值指向 CDP 项目里**已存在**的 email end user
- `create_payment_session` 用 `expiryTimeInMinutes`（**不是** `expiryDuration`）

#### 先在 CDP 项目里建 end user

```python
from cdp import CdpClient
from cdp.openapi_client.models.email_authentication import EmailAuthentication
from cdp.openapi_client.models.authentication_method import AuthenticationMethod

async with CdpClient(api_key_id=..., api_key_secret=..., wallet_secret=...) as cdp:
    am = AuthenticationMethod(EmailAuthentication(type="email", email="<your-email>"))
    await cdp.end_user.create_end_user(authentication_methods=[am])
```

#### 然后跑资源创建

```python
provider = cp.create_payment_credential_provider(
    name="X402CdpProvider",
    credentialProviderVendor="CoinbaseCDP",
    providerConfigurationInput={"coinbaseCdpConfiguration": {
        "apiKeyId": cdp_id, "apiKeySecret": cdp_secret, "walletSecret": wallet_secret,
    }},
)

manager = cp.create_payment_manager(
    name="X402Manager",
    authorizerType="AWS_IAM",
    roleArn="arn:aws:iam::463470973226:role/AgentCorePaymentsResourceRetrievalRole",
)

connector = cp.create_payment_connector(
    paymentManagerId=manager["paymentManagerId"],
    name="X402CdpConnector",
    type="CoinbaseCDP",
    credentialProviderConfigurations=[{
        "coinbaseCDP": {"credentialProviderArn": provider["credentialProviderArn"]}
    }],
)

instrument = dp.create_payment_instrument(
    paymentManagerArn=manager["paymentManagerArn"],
    paymentConnectorId=connector["paymentConnectorId"],
    userId="test-user-12345",
    paymentInstrumentType="EMBEDDED_CRYPTO_WALLET",
    paymentInstrumentDetails={"embeddedCryptoWallet": {
        "network": "ETHEREUM",
        "linkedAccounts": [{"email": {"emailAddress": "<your-email>"}}],
    }},
    clientToken=str(uuid.uuid4()),
)
# 返回里有 paymentInstrumentDetails.embeddedCryptoWallet.walletAddress —— 这就是 payer 钱包

session = dp.create_payment_session(
    paymentManagerArn=manager["paymentManagerArn"],
    userId="test-user-12345",
    expiryTimeInMinutes=480,
    limits={"maxSpendAmount": {"value": "1.0", "currency": "USD"}},
)
```

### Step 5 — WalletHub 授权（人工一次性）

Instrument 返回里有 `redirectUrl`（形如 `https://hub.cdp.coinbase.com/xxxxxxx`）。

1. 浏览器打开 `redirectUrl`
2. **必须用 Email OTP 登录**——用创建 instrument 时的那个邮箱
   - ⚠️ 不要用 Google OAuth 登录：CDP 会按认证方式建新的 end user，和 email-auth user 持有的钱包不在同一个 end user 下，导致 WalletHub "No accounts found"
3. 看到 wallet 后，授权 Delegated Signing

### Step 6 — faucet

https://faucet.circle.com/ → Base Sepolia → 打给 `walletAddress`（instrument 返回的地址，**不是** seller 地址）。

### Step 7 — 部署 Payer Agent

```bash
cd ../payer-agent
# 填 .env (MANAGER_ARN / PAYMENT_INSTRUMENT_ID / PAYMENT_SESSION_ID / USER_ID
#        / PROCESS_PAYMENT_ROLE_ARN / SELLER_API_URL / AWS_REGION=us-west-2
#        / BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0)

uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 \
  .venv/bin/python scripts/deploy_to_agentcore.py
```

### Step 8 — 调用

```python
import boto3, json, uuid
dp = boto3.client("bedrock-agentcore", region_name="us-west-2")
resp = dp.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:us-west-2:463470973226:runtime/x402PayerAgent-xxxxxxxxxx",
    runtimeSessionId=str(uuid.uuid4()),  # 必须 ≥33 字符
    payload=json.dumps({"prompt": "Get me the premium article"}).encode(),
)
print(resp["response"].read().decode())
```

## 踩过的坑（按发生顺序）

### 坑 1 — boto3 没有 AgentCore Payments API

`boto3 1.42.97` 里没有 `create_payment_*` 方法。系统 Python 3.9 装不上 `boto3 ≥ 1.43`（需要 Python ≥ 3.10）。

**修**：`uv venv --python /home/linuxbrew/.linuxbrew/bin/python3.14`，`boto3 1.43.6` 里齐全。

### 坑 2 — Payer CDK 误部到 us-east-1

`AWS_REGION=us-west-2 cdk deploy` 不生效——CDK 读 `AWS_REGION`（来自 shell）而不是显式传的值。结果 stack 进了 `us-east-1`，SNS/CloudWatch 等 region-scoped 资源错位。

**修**：`AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 cdk deploy`。

### 坑 3 — `create_payment_manager` AccessDenied

CDK 给 `ResourceRetrievalRole` 挂的权限是 `bedrock-agentcore:GetIdentity / RetrieveToken`（**不存在**的 action 名称）。正确是 `GetWorkloadAccessToken / CreateWorkloadIdentity / GetResourcePaymentToken`，且 Resource 必须包含 `token-vault/default/*`。见 Step 3。

### 坑 4 — `create_payment_instrument` Schema 变了

- QUICKSTART.md 用的是 `"CRYPTO_WALLET"` + `"cryptoWallet"` + 没有 `linkedAccounts`。
- 实际 launch 后 API 是 `"EMBEDDED_CRYPTO_WALLET"` + `"embeddedCryptoWallet"` + **必填 `linkedAccounts`**。

### 坑 5 — `linkedAccounts.email` 必须是 CDP 里真实存在的 end user

用 `test@example.com` 或任意占位符会 500：`InternalServerException: Failed to create payment instrument`。

**修**：先用 CDP SDK 的 `cdp.end_user.create_end_user(authentication_methods=[EmailAuthentication(...)])` 注册一个 email end user，**再**把这个 email 填到 `linkedAccounts`。

### 坑 6 — CDP Delegated Signing 没启用

同上 500，症状一样。

**修**：CDP Portal → Embedded Wallets → Policies → 启用 "Delegated signing"。

### 坑 7 — `create_payment_session` 字段名变了

QUICKSTART.md 用 `expiryDuration`，实际 API 是 `expiryTimeInMinutes`。

### 坑 8 — AgentCore Runtime 只接受 arm64 镜像

Deploy 脚本 `docker build` 没传 `--platform`，在 x86 机器 build 的是 amd64，报：
`ValidationException: Architecture incompatible for uri '...'. Supported platforms: [arm64]`

**修**：改 `scripts/deploy_to_agentcore.py` 的 build 命令为 `docker buildx build --platform linux/arm64 --load ...`。arm64 emulation 下 build 会比 amd64 慢 3-5 倍。

### 坑 9 — Deploy 脚本 env 变量 allowlist 过时

脚本只注入 `CDP_API_KEY_ID / CDP_API_KEY_SECRET / CDP_WALLET_SECRET / CDP_WALLET_ADDRESS / NETWORK_ID`（AgentKit 时代的变量），新版 agent 读的是 `MANAGER_ARN / PAYMENT_SESSION_ID / PAYMENT_INSTRUMENT_ID / PROCESS_PAYMENT_ROLE_ARN / USER_ID`。Runtime 启动后这些变量都没注入，调任何工具都会失败。

**修**：改 `scripts/deploy_to_agentcore.py` 的 `get_env_vars()` allowlist。

### 坑 10 — FastAPI `/invocations` 422

签名是 `async def invocations(request: InvokeRequest)`，Pydantic strict 校验失败时 uvicorn h11 层先报 "Invalid HTTP request received"，body 被吞，Pydantic 收到空 body → 422。AgentCore proxy 的 request 协议层跟 FastAPI strict mode 配合不好。

**修**：改成读 raw body：

```python
@app.post("/invocations")
async def invocations(http_request: Request):
    raw = await http_request.body()
    body = json.loads(raw) if raw else {}
    request = InvokeRequest(**body)
    ...
```

### 坑 11 — Bedrock model 不支持 on-demand throughput

`BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-20250514-v1:0` → `ValidationException: Invocation of model ID ... with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile`。

**修**：换成 cross-region inference profile：`us.anthropic.claude-sonnet-4-20250514-v1:0`。

### 坑 12 — Faucet 钱打错地址

第一次我把 faucet USDC 打到了 **seller 地址**（收款方）而不是 **payer 地址**（付款方）。payer 没钱，ProcessPayment 生成的签名永远无法结算。

**修**：打到 instrument 返回的 `walletAddress`（payer 钱包），这是 CDP 为 instrument 自动创建的、由 AgentCore Payments 代签的钱包。

### 坑 13 — WalletHub "No accounts found"

用 Google OAuth 登录 WalletHub 时，CDP 按 OAuth 认证方式建了一个**新的** end user（和 email-auth end user 不同），新 user 下没钱包，所以显示 "No accounts found"。

**修**：登出，改用 **Email OTP** 登录，填创建 instrument 时用的邮箱。

### 坑 14 — `request_content_with_payment` 用 POST 被 CloudFront 403

CloudFront `/api/*` cache behavior `AllowedMethods = [GET, HEAD, OPTIONS]`，但 agent 代码用 `httpx.Client.post(...)` retry。CloudFront 直接 403 拦截，请求都到不了 Lambda@Edge。

**修**：改 `agent/tools/content.py` 的 retry 从 `client.post` 改为 `client.get`。x402 标准本来就是重放 GET + `X-PAYMENT` header。

## 最终文件变更清单

| 文件 | 修改 |
|---|---|
| `seller-infrastructure/.env` | 新增，填入 `PAYMENT_RECIPIENT_ADDRESS` |
| `payer-agent/.env` | 新增，填入 MANAGER_ARN / PAYMENT_SESSION_ID / PAYMENT_INSTRUMENT_ID / PROCESS_PAYMENT_ROLE_ARN / USER_ID / SELLER_API_URL，BEDROCK_MODEL_ID 用 inference profile |
| `payer-agent/scripts/deploy_to_agentcore.py` | docker build 加 `--platform linux/arm64`；env vars allowlist 更新为 AgentCore Payments 相关变量 |
| `payer-agent/agent/api_server.py` | `/invocations` handler 改读 raw body（避免 Pydantic strict 422） |
| `payer-agent/agent/tools/content.py` | `request_content_with_payment` 把 `client.post` 改为 `client.get` |
| IAM role `AgentCorePaymentsResourceRetrievalRole` | 手动加 `WorkloadIdentityPatch` inline policy（覆盖 `CreateWorkloadIdentity` + `GetWorkloadAccessToken` + `GetResourcePaymentToken` + `token-vault/*` 资源） |

## 清理

```bash
# seller
cd seller-infrastructure && AWS_REGION=us-east-1 npx cdk destroy --force

# payer infra
cd ../payer-infrastructure && AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 \
  npx cdk destroy --all --force

# agent runtime + ECR
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id x402PayerAgent-6aY6NhHuua --region us-west-2
aws ecr delete-repository --repository-name x402-payer-agent --force --region us-west-2

# AgentCore Payments 资源
python - <<'EOF'
import boto3
cp = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
dp = boto3.client("bedrock-agentcore", region_name="us-west-2")
# 删 instrument / session / connector / manager / provider（按依赖顺序）
EOF
```

CDP 侧的 end user 和 wallet 不会自动删（它们在 Coinbase 那边）。测试网 USDC 留着没影响。

## 相关资源

| 名称 | 值 |
|------|-----|
| Manager ARN | `arn:aws:bedrock-agentcore:us-west-2:463470973226:payment-manager/x402manager-fhwpqlncrq` |
| Payment Session | `payment-session-mnh8OypxPDk4x1n` |
| Payment Instrument | `payment-instrument-LScf3BTc4b3aVnf` |
| Agent Runtime | `arn:aws:bedrock-agentcore:us-west-2:463470973226:runtime/x402PayerAgent-6aY6NhHuua` |
| CloudFront | `https://doogv4r5wyctf.cloudfront.net` |
| Payer wallet | `0x9e944f57D44e757f0ef4acc6f18f406860ADdaA3` |
| Seller wallet | `0xF72bE87831Fe0536314e23df4b8F97e42F3B017B` |

## 参考

- [AgentCore Payments 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments.html)
- [x402 协议规范](https://github.com/coinbase/x402/tree/main/specs)
- [Coinbase CDP](https://docs.cdp.coinbase.com/)
- [awslabs/agentcore-samples setup_roles.sh](https://github.com/awslabs/agentcore-samples) — 权威的 IAM 配置参考
