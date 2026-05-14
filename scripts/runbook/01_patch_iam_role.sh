#!/usr/bin/env bash
# Patch AgentCorePaymentsResourceRetrievalRole with permissions the CDK forgot.
# Without this patch, create_payment_manager / create_payment_instrument fail with 500.

set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_NAME="AgentCorePaymentsResourceRetrievalRole"

echo "Patching role $ROLE_NAME (account=$ACCOUNT_ID, region=$REGION)..."

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name WorkloadIdentityPatch \
  --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockAgentCorePaymentAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:GetWorkloadIdentity",
        "bedrock-agentcore:DeleteWorkloadIdentity",
        "bedrock-agentcore:UpdateWorkloadIdentity",
        "bedrock-agentcore:ListWorkloadIdentities",
        "bedrock-agentcore:GetResourcePaymentToken"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:token-vault/default",
        "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:token-vault/default/*",
        "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:token-vault/default/paymentcredentialprovider/*",
        "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:workload-identity-directory/default/workload-identity/*"
      ]
    }
  ]
}
EOF
)"

echo "Done. Verify:"
aws iam get-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name WorkloadIdentityPatch \
  --query 'PolicyDocument.Statement[0].Action' \
  --output json
