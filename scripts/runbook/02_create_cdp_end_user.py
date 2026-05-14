"""Ensure a CDP end user exists with the given email.

AgentCore Payments' create_payment_instrument requires linkedAccounts.email to
point at an *already-registered* CDP end user. Running this once before the
resource creation script avoids the 500 InternalServerException.

Reads:
  CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET — from env
  CDP_END_USER_EMAIL — the email to register (also used in step 03)

Usage:
  CDP_END_USER_EMAIL=you@example.com python 02_create_cdp_end_user.py
"""
import asyncio
import os
import sys

from cdp import CdpClient
from cdp.openapi_client.models.authentication_method import AuthenticationMethod
from cdp.openapi_client.models.email_authentication import EmailAuthentication


def must_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: {name} not set", file=sys.stderr)
        sys.exit(1)
    return v


async def main() -> None:
    email = must_env("CDP_END_USER_EMAIL")
    async with CdpClient(
        api_key_id=must_env("CDP_API_KEY_ID"),
        api_key_secret=must_env("CDP_API_KEY_SECRET"),
        wallet_secret=must_env("CDP_WALLET_SECRET"),
    ) as cdp:
        existing = await cdp.end_user.list_end_users()
        for u in existing.end_users or []:
            for am in u.authentication_methods or []:
                inst = am.actual_instance
                if isinstance(inst, EmailAuthentication) and inst.email == email:
                    print(f"end user already exists: user_id={u.user_id} email={email}")
                    return
        am = AuthenticationMethod(EmailAuthentication(type="email", email=email))
        user = await cdp.end_user.create_end_user(authentication_methods=[am])
        print(f"created end user: user_id={user.user_id} email={email}")


if __name__ == "__main__":
    asyncio.run(main())
