# 原理与架构说明

这份文档介绍 AgentCore Payments + CloudFront x402 demo 背后的协议、组件、信任链和数据流。目标是让读完之后能清楚：

- **x402 协议**在 HTTP 层做了什么
- **AgentCore Payments** 在 x402 的哪个环节里
- **Coinbase CDP** 的 Server Wallet / Embedded Wallet / Delegated Signing 分别是什么
- 一笔付款背后在链上发生了什么（EIP-3009）
- 为什么需要 Lambda@Edge / CloudFront / Base Sepolia 这些具体组件

## 1. 背景问题：Agent 怎么为内容付费？

传统 API 付费模式：
1. 用户注册平台账号，绑定信用卡
2. 平台发 API key
3. 每次 API 调用由平台对着 API key 计费，月结

在 **agent 经济**（Claude、GPT 等自主执行任务的模型）下这套模式很别扭：
- Agent 要动态发现新服务，没法预先注册
- 微支付（0.001 美元级别）跑不起传统卡组织
- Agent 需要在 **每次请求** 粒度上授权和支付，不是月结
- 供应方想要无账户、无合同就能收钱

**x402** 是 Coinbase 2024 提出的协议，用 HTTP 状态码 `402 Payment Required`（1999 年就保留但一直没人用的状态码）解决这个问题：把支付能力塞到 HTTP 本身。

## 2. x402 协议：HTTP 层的付款握手

### 基本流程

```
Client (agent)                                Server (API provider)
     │                                                 │
     │  GET /api/premium-article                       │
     │───────────────────────────────────────────────▶ │
     │                                                 │
     │        HTTP 402 Payment Required                │
     │  { accepts: [{ scheme, network, amount, payTo, │
     │                 asset, maxTimeoutSeconds, … }]}│
     │ ◀───────────────────────────────────────────── │
     │                                                 │
     │  ┌── 用户/agent 决定付款 ──┐                     │
     │  │                         │                    │
     │  │  生成支付凭证            │                    │
     │  │  (EIP-3009 签名)        │                    │
     │  └─────────────────────────┘                    │
     │                                                 │
     │  GET /api/premium-article                       │
     │  X-PAYMENT: <base64(signed authorization)>      │
     │───────────────────────────────────────────────▶ │
     │                                                 │
     │                                     ┌───────────┼──────────┐
     │                                     │ facilitator 验证      │
     │                                     │ broadcast 上链        │
     │                                     └───────────┼──────────┘
     │                                                 │
     │        HTTP 200 OK + content                    │
     │        X-PAYMENT-RESPONSE: <settlement>         │
     │ ◀───────────────────────────────────────────── │
```

### 402 响应里的 `accepts`（本 demo 的真实例子）

```json
{
  "x402Version": 2,
  "error": "Payment required to access this resource",
  "resource": {
    "url": "/api/premium-article",
    "description": "Protected resource at /api/premium-article",
    "mimeType": "application/json"
  },
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:84532",
    "amount": "1000",
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "payTo": "0xF72bE87831Fe0536314e23df4b8F97e42F3B017B",
    "maxTimeoutSeconds": 60,
    "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"}
  }],
  "extensions": {}
}
```

字段含义：
- `scheme: exact` — 支付金额必须完全一致（vs `upto`）
- `network: eip155:84532` — CAIP-2 格式，Base Sepolia
- `amount: "1000"` — **raw units**，USDC 是 6 位小数，所以这是 0.001 USDC
- `asset: 0x036CbD…CF7e` — Base Sepolia 上的 USDC 合约地址
- `payTo: 0xF72b…017B` — seller 收款地址
- `extra.assetTransferMethod: eip3009` — 用 EIP-3009 `transferWithAuthorization` 方式支付

### 为什么是 GET 重放而不是 POST

第二次请求**必须是 GET**。原因：
- x402 是 "发现 + 重放"模式：第一次 GET 得到 402，第二次带 X-PAYMENT header 的 GET 重放同一个 URL
- POST 会被 CloudFront 等 CDN 挡在外面（`AllowedMethods` 默认只有 GET/HEAD/OPTIONS）
- GET 是幂等的，符合内容 API 的语义

## 3. EIP-3009：为什么不用普通 `transfer`？

如果用普通 `ERC20.transfer(to, amount)`，payer 必须：
1. 自己持有 ETH 付 gas
2. 自己发一笔交易
3. 等交易 mined
4. 再发第二次 HTTP 请求带 tx hash 给 seller

太慢、太麻烦、payer 还要管 gas。

**EIP-3009** (`transferWithAuthorization`) 解决这个：

```solidity
function transferWithAuthorization(
    address from,          // payer
    address to,            // seller
    uint256 value,         // amount
    uint256 validAfter,    // unix timestamp
    uint256 validBefore,   // unix timestamp
    bytes32 nonce,         // random
    uint8 v, bytes32 r, bytes32 s    // payer 的 EIP-712 签名
) external
```

核心机制：
- **payer 只签名，不上链**
- 任何人（facilitator / seller / 第三方）拿着签名调用合约，USDC 就从 payer 扣走转给 seller
- **谁调用，谁付 gas**——通常是 seller 或 facilitator 付 gas，payer 只需要有 USDC，不需要 ETH
- `nonce + validAfter/validBefore` 防止重放和过期

这让**付款像发 email 一样**：payer 签一张"授权单"，seller 拿着单子去链上兑付。对 agent 来说很关键：agent 不需要 gas、不需要等 mining、不需要管链上交易的状态机。

### 签名细节（EIP-712 类型化数据）

```
TransferWithAuthorization(
    address from,
    address to,
    uint256 value,
    uint256 validAfter,
    uint256 validBefore,
    bytes32 nonce
)

Domain:
    name: "USDC"
    version: "2"
    chainId: 84532
    verifyingContract: 0x036CbD53842c5426634e7929541eC2318f3dCF7e
```

payer 用自己的私钥对这个类型化结构签 EIP-712，得到 `v, r, s`。整段（`from, to, value, validAfter, validBefore, nonce, signature`）被 base64 编码塞到 HTTP `X-PAYMENT` header 里。

## 4. AgentCore Payments：托管这个签名过程

问题：agent 是**服务端软件**，让它管私钥风险太大（key 一旦泄露钱就没了）。

AgentCore Payments 的思路：**agent 不持有 key，由 AWS + Coinbase 共同托管**。

### 四层对象模型

```
┌─────────────────────────────────────────────────────────────┐
│  PaymentCredentialProvider                                  │
│  — 把 CDP 的 (apiKeyId, apiKeySecret, walletSecret) 存到   │
│    AWS Secrets Manager 里，得到一个 ARN                    │
│  — 这些 credential 只给 AgentCore 服务自己用，              │
│    调用方拿不到原始值                                       │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  PaymentManager                                             │
│  — 一个逻辑聚合，关联一个 ResourceRetrievalRole             │
│  — 用 AWS_IAM 授权模式：调用方的 IAM 身份决定能不能付钱     │
└────────────────────────┬────────────────────────────────────┘
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
┌─────────────────────────┐  ┌──────────────────────────────┐
│  PaymentConnector       │  │  PaymentSession              │
│  — 绑定到 Provider      │  │  — per-user 预算             │
│  — 指定 vendor (CDP)    │  │  — maxSpendAmount (USD)      │
│                         │  │  — expiryTimeInMinutes       │
└─────────────────────────┘  └──────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  PaymentInstrument                                          │
│  — 真实的钱包实体                                           │
│  — CDP Embedded Wallet + Delegated Signing                  │
│  — 绑定到一个 end user (通过 linkedAccounts.email)          │
│  — 返回 walletAddress 和 WalletHub redirectUrl              │
└─────────────────────────────────────────────────────────────┘
```

### ProcessPayment 调用时发生的事

```python
dp.process_payment(
    paymentManagerArn=...,
    paymentSessionId=...,    # 预算控制
    paymentInstrumentId=...,  # 从哪个钱包签
    userId=...,
    paymentType="CRYPTO_X402",
    paymentInput={"cryptoX402": {
        "version": "2",
        "payload": { ... x402_payload ... }
    }}
)
```

服务端做的：
1. 鉴权：调用者是否有 ProcessPaymentRole
2. 预算检查：PaymentSession 的 `maxSpendAmount` 够不够
3. 取 credential：用 ResourceRetrievalRole assume → 从 Secrets Manager 取 CDP keys
4. 构造 EIP-3009 authorization：`from=wallet, to=payTo, value=amount, …`
5. 发给 CDP Server Wallet API，要求"以 delegated signing 身份用 walletAddress 对这个结构签 EIP-712"
6. 返回 `{status: "PROOF_GENERATED", payload: { signature, authorization }}`

⚠️ **ProcessPayment 不上链**。只生成签名（EIP-3009 proof），真正的上链由 seller 侧的 facilitator 做。

### 为什么叫"Delegated Signing"

传统 CDP Embedded Wallet 的使用方式：
- 用户在前端用邮箱/Google 登录
- 每次转账用户都得在前端 confirm（像 MetaMask 弹窗）

但 agent 场景里，**用户不在场**——agent 自己 autonomous 决定付款。Delegated Signing 是 CDP 提供的特殊模式：
- 用户**一次性**授权：允许后端（持有 `walletSecret`）代替用户签名
- 此后后端用 `walletSecret` 就能直接调 CDP API 签任意 EIP-712 结构，不需要用户再 confirm

授权这步就是 WalletHub 里那个"Grant delegated signing"按钮。AgentCore Payments 就是这个后端，它用你上传的 `walletSecret` 去 CDP 签。

## 5. 信任链与职责分离

这个 demo 里有 **4 个信任域**：

```
┌──────────────┐  1. IAM身份   ┌───────────────────┐
│ 调用方 (你)  │───授权────────▶│ AgentCore Payments│
└──────────────┘                │   (AWS 托管)      │
                                └────────┬──────────┘
                                         │ 2. walletSecret
                                         ▼
                                ┌───────────────────┐
                                │ Coinbase CDP      │
                                │ (Server Wallet +  │
                                │  Embedded Wallet) │
                                └────────┬──────────┘
                                         │ 3. 用户一次性授权
                                         ▼  (delegated signing)
                                ┌───────────────────┐
                                │ 你 (真实 email    │
                                │  的 end user)     │
                                └───────────────────┘
                                         │
                                         │ 4. 链上资产
                                         ▼
                                ┌───────────────────┐
                                │ Base Sepolia 链   │
                                │ USDC 余额         │
                                └───────────────────┘
```

- **IAM 信任**：ProcessPaymentRole 只有被授权的调用方（Agent Runtime / 你本人）能 assume
- **AgentCore ↔ CDP**：靠你上传的 walletSecret 建立关系。walletSecret 只在 AWS Secrets Manager 里，调用方看不见
- **CDP ↔ end user**：靠 WalletHub 上的一次性 OAuth/OTP 授权建立关系。没这步 CDP 会拒绝代签
- **链上**：最终是你的钱包地址在 Base Sepolia 上有 USDC 余额，别人看到的只是地址之间的转账

任何一环断掉，链路就跑不通——这也是为什么 debug 起来那么麻烦。

## 6. Seller 侧：CloudFront + Lambda@Edge

Seller 的挑战：
- 想要**无服务器**、低延迟、全球分发
- 要能**识别并验证 x402 支付**
- 要**原子地**在支付验证成功时才返回内容

CloudFront 本身只是 CDN，不会懂 x402。所以用 **Lambda@Edge** 在 CloudFront 边缘节点注入逻辑：

```
Client
   │ GET /api/premium-article  (+ maybe X-PAYMENT)
   ▼
CloudFront edge (就近 PoP)
   │
   │ ┌─── [cache behavior /api/*] ───┐
   │ │  origin-request Lambda@Edge: │
   │ │    PaymentVerifierFn         │
   │ └───────────┬──────────────────┘
   │             │
   │             ▼  如果 X-PAYMENT header 不存在或无效
   │     返回 402 + accepts 列表
   │             │
   │             │  如果有效
   │             ▼  调 facilitator.broadcastEIP3009(signature)
   │     链上成功 → 放行 → S3 取内容 → 200
   │
   ▼
Client
```

Lambda@Edge 的好处：
- 运行在 **CloudFront PoP**，延迟低
- **完全无状态**：每次请求独立
- 不用自己维护服务器

### Lambda@Edge 的限制（踩过的坑）

- 只能部署在 `us-east-1`
- 不能带 env vars（seller 的 `payTo` 地址被写死在代码里，由 CDK 构建时注入）
- 执行时间和内存都有更严格的限制

### 为什么 CloudFront 的 AllowedMethods 默认 = GET/HEAD/OPTIONS

CloudFront 缓存只针对 GET。允许 POST 会让 cache 行为变复杂（POST body 怎么参与 cache key）。默认配置契合 **x402 就是要 GET 重放**这个设计。（但仓库 CDK 建的 `/api/*` 也只开了 GET，所以 agent 端必须用 GET retry。）

## 7. 完整数据流：一次成功付款

```
  USER PROMPT: "Get me the premium article"
       │
       ▼
┌────────────────────────────┐
│ AgentCore Runtime          │
│  Claude Sonnet 4 思考       │
│  选择 request_content tool │
└──────────┬─────────────────┘
           │
           │ 1. httpx.get("https://<cloudfront>/api/premium-article")
           ▼
┌────────────────────────────┐
│ CloudFront → Lambda@Edge   │
│  无 X-PAYMENT header       │
│  返回 402 + accepts        │
└──────────┬─────────────────┘
           │
           │ 2. agent 读到 402，选择 process_payment tool
           ▼
┌────────────────────────────┐
│ AgentCore Runtime          │
│  调 AWS_IAM ProcessPayment │
│  (via STS AssumeRole)      │
└──────────┬─────────────────┘
           │
           │ 3. process_payment(x402_payload=...)
           ▼
┌────────────────────────────┐
│ AgentCore Payments 服务端  │
│  - 检查 session 预算        │
│  - 取 walletSecret          │
│  - 构造 EIP-3009 authz      │
│  - 调 CDP.signTypedData     │
│    (delegated signing)     │
│  - 返回 PROOF_GENERATED +   │
│    签名                     │
└──────────┬─────────────────┘
           │
           │ 4. proof 缓存在 agent runtime 内存里
           │    agent 选择 request_content_with_payment tool
           ▼
┌────────────────────────────┐
│ AgentCore Runtime          │
│  base64(proof) → X-PAYMENT │
│  httpx.get(url, headers)   │
└──────────┬─────────────────┘
           │
           │ 5. GET /api/premium-article + X-PAYMENT header
           ▼
┌────────────────────────────┐
│ CloudFront → Lambda@Edge   │
│  解码 X-PAYMENT             │
│  调 x402 facilitator        │
│  (目前是 Coinbase facilitator│
│   或自建)                   │
└──────────┬─────────────────┘
           │
           │ 6. facilitator 调 USDC.transferWithAuthorization(...)
           ▼
┌────────────────────────────┐
│ Base Sepolia 链上           │
│  USDC: payer -= 1000       │
│  USDC: seller += 1000      │
│  tx: 0x9a96...             │
└──────────┬─────────────────┘
           │
           │ 7. 链上成功 → facilitator 返回 success 给 Lambda@Edge
           ▼
┌────────────────────────────┐
│ Lambda@Edge                │
│  放行 → 从 S3 origin 取内容 │
│  返回 200 + body +          │
│    X-PAYMENT-RESPONSE       │
│    (settlement 详情)        │
└──────────┬─────────────────┘
           │
           ▼
┌────────────────────────────┐
│ AgentCore Runtime          │
│  agent 把内容归纳成自然语言 │
│  返回给用户                 │
└────────────────────────────┘
```

延迟分布（实测 ~3 秒）：
- Bedrock LLM 推理：~1-2 秒（最大头）
- ProcessPayment（AgentCore → CDP → 签名）：~0.5 秒
- 链上 settlement（Base Sepolia 一个 block ≈ 2 秒）：~1-2 秒
- HTTP + Lambda@Edge：~0.2 秒

## 8. 安全模型

### Agent 侧
- Agent Runtime 不持有任何私钥
- Agent 只能在 PaymentSession 预算内花钱（`maxSpendAmount: 1.0 USD`）
- 预算超了 ProcessPayment 会拒绝，链上什么都不会发生
- Session 有过期时间（`expiryTimeInMinutes`）

### AWS 侧
- ProcessPaymentRole 是 agent runtime role 能 assume 的受限角色
- ResourceRetrievalRole 只被 `bedrock-agentcore.amazonaws.com` 服务本身 assume，调用方 assume 不到
- walletSecret 存在 Secrets Manager，只有 ResourceRetrievalRole 的身份能读

### CDP 侧
- walletSecret 即便泄露，攻击者也必须先通过 WalletHub OAuth 授权（end user email OTP），才能进行 delegated signing
- Wallet 地址和资产绑定在 Coinbase 托管基础设施上，不是一个裸的以太坊 EOA

### 链上
- EIP-3009 签名里的 `validAfter/validBefore` 窗口通常很短（本 demo: validAfter=now, validBefore=now+60s）
- nonce 随机，防止签名重放
- 攻击者即便拿到 X-PAYMENT header，也只能用一次（nonce 被消费），且只能转到 `payTo` 地址

多层保护，任何单点泄漏都不至于让资产丢失。

## 9. 这套系统解决了什么、没解决什么

**解决了**
- Agent 自主发现并为内容付费，无需预先注册
- 微支付（亚美分级别）经济上可行（Base Sepolia 测试网 gas 几乎为 0）
- 卖方无需实现账户体系——靠链上地址天然隔离
- 付款行为有**密码学审计轨迹**（链上 tx 公开可查）
- 支付授权 vs 支付执行分离，降低密钥泄露风险

**没解决**
- **Cold start 问题**：end user 还是得一次性去 WalletHub 授权
- **定价发现**：402 响应只告诉你价格，但没有 "compare providers" 协议
- **退款 / 争议**：链上交易不可逆
- **合规**：在测试网随意，主网涉及 KYC/AML 各地法规
- **链限制**：目前只支持 Base Sepolia / Ethereum 等 EVM 链；Solana 是另一套路径
- **规模效应**：一笔 tx 一个 block 确认，高频场景需要状态通道或 L3 等方案

## 10. 延伸阅读

- [EIP-3009 spec](https://eips.ethereum.org/EIPS/eip-3009) — transferWithAuthorization 原始提案
- [EIP-712 spec](https://eips.ethereum.org/EIPS/eip-712) — 类型化数据签名
- [x402 protocol specs](https://github.com/coinbase/x402/tree/main/specs)
- [AgentCore Payments 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments.html)
- [Coinbase CDP Embedded Wallets](https://docs.cdp.coinbase.com/embedded-wallets/docs/overview)
- [CAIP-2 network identifiers](https://chainagnostic.org/CAIPs/caip-2) — `eip155:84532` 这种格式
- [Base Sepolia block explorer](https://sepolia.basescan.org/)
- [Circle faucet](https://faucet.circle.com/) — 测试网 USDC
