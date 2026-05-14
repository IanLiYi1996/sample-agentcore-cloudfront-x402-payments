# Runbook 脚本

供 `RUNBOOK.md` 使用，按顺序执行。

| 序号 | 脚本 | 用途 | 依赖输入 |
|------|------|------|---------|
| 01 | `01_patch_iam_role.sh` | 给 `AgentCorePaymentsResourceRetrievalRole` 补 CDK 漏掉的 IAM 权限 | `AWS_REGION`，已部署 payer-infrastructure |
| 02 | `02_create_cdp_end_user.py` | 在 CDP 项目里建一个 email 类型的 end user | `CDP_API_KEY_ID/SECRET/WALLET_SECRET`、`CDP_END_USER_EMAIL` |
| 03 | `03_create_payments_resources.py` | 建 AgentCore Payments 全套资源 + 输出钱包地址 | 02 的 env + `RESOURCE_RETRIEVAL_ROLE_ARN` |
| 04 | `04_check_balances.py` | 查 Base Sepolia 上 payer / seller 的 USDC 余额 | 两个地址 |
| 05 | `05_invoke_agent.py` | 调用部署好的 AgentCore Runtime，跑端到端付款 | `AGENT_RUNTIME_ARN`（可从 `payer-agent/.env` 自动读） |

详细使用见根目录的 `RUNBOOK.md`。
