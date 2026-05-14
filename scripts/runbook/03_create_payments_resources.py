"""Create / reuse AgentCore Payments resources for the x402 demo.

Idempotent: re-running reuses existing provider / manager / connector by name
and creates a fresh instrument + session each call (these are cheap to keep).

Inputs (env):
  CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET
  CDP_END_USER_EMAIL              — must match a CDP end user (see step 02)
  RESOURCE_RETRIEVAL_ROLE_ARN     — from payer-infrastructure CDK output
  AWS_REGION                      — defaults to us-west-2
  USER_ID                         — defaults to test-user-12345
  MAX_SPEND_USD                   — defaults to 1.0

Outputs:
  Writes /tmp/agentcore_payments_out.json with all IDs.
  Prints the summary block at the end (paste into payer-agent/.env).
"""
import json
import os
import sys
import uuid

import boto3


def must_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: {name} not set", file=sys.stderr)
        sys.exit(1)
    return v


def main() -> None:
    region = os.environ.get("AWS_REGION", "us-west-2")
    user_id = os.environ.get("USER_ID", "test-user-12345")
    max_spend = os.environ.get("MAX_SPEND_USD", "1.0")
    email = must_env("CDP_END_USER_EMAIL")
    role_arn = must_env("RESOURCE_RETRIEVAL_ROLE_ARN")
    cdp_id = must_env("CDP_API_KEY_ID")
    cdp_secret = must_env("CDP_API_KEY_SECRET")
    wallet_secret = must_env("CDP_WALLET_SECRET")

    cp = boto3.client("bedrock-agentcore-control", region_name=region)
    dp = boto3.client("bedrock-agentcore", region_name=region)

    print("[1/5] Ensuring credential provider…")
    try:
        provider = cp.create_payment_credential_provider(
            name="X402CdpProvider",
            credentialProviderVendor="CoinbaseCDP",
            providerConfigurationInput={
                "coinbaseCdpConfiguration": {
                    "apiKeyId": cdp_id,
                    "apiKeySecret": cdp_secret,
                    "walletSecret": wallet_secret,
                }
            },
        )
        provider_arn = provider["credentialProviderArn"]
    except Exception as e:
        if "already exists" not in str(e):
            raise
        provider_arn = cp.get_payment_credential_provider(
            name="X402CdpProvider"
        )["credentialProviderArn"]
        print("  (exists, reusing)")
    print(f"  provider_arn = {provider_arn}")

    print("[2/5] Ensuring payment manager…")
    managers = cp.list_payment_managers().get("paymentManagers", [])
    manager = next((m for m in managers if m.get("name") == "X402Manager"), None)
    if manager:
        manager_arn = manager["paymentManagerArn"]
        manager_id = manager["paymentManagerId"]
        print("  (exists, reusing)")
    else:
        resp = cp.create_payment_manager(
            name="X402Manager",
            authorizerType="AWS_IAM",
            roleArn=role_arn,
        )
        manager_arn = resp["paymentManagerArn"]
        manager_id = resp["paymentManagerId"]
    print(f"  manager_arn = {manager_arn}")

    print("[3/5] Ensuring payment connector…")
    connectors = cp.list_payment_connectors(paymentManagerId=manager_id).get(
        "paymentConnectors", []
    )
    connector = next(
        (c for c in connectors if c.get("name") == "X402CdpConnector"), None
    )
    if connector:
        connector_id = connector["paymentConnectorId"]
        print("  (exists, reusing)")
    else:
        resp = cp.create_payment_connector(
            paymentManagerId=manager_id,
            name="X402CdpConnector",
            type="CoinbaseCDP",
            credentialProviderConfigurations=[
                {"coinbaseCDP": {"credentialProviderArn": provider_arn}}
            ],
        )
        connector_id = resp["paymentConnectorId"]
    print(f"  connector_id = {connector_id}")

    print(f"[4/5] Creating embedded crypto wallet instrument (linked to {email})…")
    instrument = dp.create_payment_instrument(
        paymentManagerArn=manager_arn,
        paymentConnectorId=connector_id,
        userId=user_id,
        paymentInstrumentType="EMBEDDED_CRYPTO_WALLET",
        paymentInstrumentDetails={
            "embeddedCryptoWallet": {
                "network": "ETHEREUM",
                "linkedAccounts": [{"email": {"emailAddress": email}}],
            }
        },
        clientToken=str(uuid.uuid4()),
    )
    pi = instrument["paymentInstrument"]
    instrument_id = pi["paymentInstrumentId"]
    wallet_addr = (
        pi.get("paymentInstrumentDetails", {})
          .get("embeddedCryptoWallet", {})
          .get("walletAddress")
    )
    redirect_url = (
        pi.get("paymentInstrumentDetails", {})
          .get("embeddedCryptoWallet", {})
          .get("redirectUrl")
    )
    print(f"  instrument_id  = {instrument_id}")
    print(f"  wallet_address = {wallet_addr}")
    print(f"  WalletHub URL  = {redirect_url}")

    print(f"[5/5] Creating payment session (max ${max_spend})…")
    session = dp.create_payment_session(
        paymentManagerArn=manager_arn,
        userId=user_id,
        expiryTimeInMinutes=480,
        limits={"maxSpendAmount": {"value": max_spend, "currency": "USD"}},
    )
    session_id = session["paymentSession"]["paymentSessionId"]
    print(f"  session_id = {session_id}")

    out = {
        "MANAGER_ARN": manager_arn,
        "PAYMENT_INSTRUMENT_ID": instrument_id,
        "PAYMENT_SESSION_ID": session_id,
        "USER_ID": user_id,
        "WALLET_ADDRESS": wallet_addr,
        "WALLETHUB_URL": redirect_url,
        "CREDENTIAL_PROVIDER_ARN": provider_arn,
        "CONNECTOR_ID": connector_id,
    }
    out_path = "/tmp/agentcore_payments_out.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved to {out_path}")
    print("\n=== PASTE INTO payer-agent/.env ===")
    print(f"MANAGER_ARN={manager_arn}")
    print(f"PAYMENT_SESSION_ID={session_id}")
    print(f"PAYMENT_INSTRUMENT_ID={instrument_id}")
    print(f"USER_ID={user_id}")
    print()
    print(f"# Fund this wallet with testnet USDC at https://faucet.circle.com/")
    print(f"# Then open this URL once and authorize delegated signing:")
    print(f"#   {redirect_url}")
    print(f"# Wallet: {wallet_addr}")


if __name__ == "__main__":
    main()
