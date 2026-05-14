"""Invoke the deployed AgentCore Runtime end-to-end.

Reads AGENT_RUNTIME_ARN from env or from payer-agent/.env.

Usage:
  python 05_invoke_agent.py "Get me the premium article"
  AGENT_RUNTIME_ARN=arn:... python 05_invoke_agent.py "What services are available?"
"""
import json
import os
import sys
import uuid
from pathlib import Path

import boto3


def load_runtime_arn() -> str:
    arn = os.environ.get("AGENT_RUNTIME_ARN")
    if arn:
        return arn
    # Try payer-agent/.env
    env_file = Path(__file__).resolve().parents[2] / "payer-agent" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("AGENT_RUNTIME_ARN="):
                return line.split("=", 1)[1].strip()
    print("ERROR: AGENT_RUNTIME_ARN not set and not found in payer-agent/.env", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Get me the premium article"
    region = os.environ.get("AWS_REGION", "us-west-2")
    runtime_arn = load_runtime_arn()
    session_id = str(uuid.uuid4())

    print(f">> runtime: {runtime_arn}")
    print(f">> prompt:  {prompt}")
    print(f">> session: {session_id}\n")

    dp = boto3.client("bedrock-agentcore", region_name=region)
    resp = dp.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode(),
    )

    ct = resp.get("contentType", "")
    print(f"contentType: {ct}\n")
    body = resp["response"]
    if "text/event-stream" in ct:
        for chunk in body.iter_lines():
            if chunk:
                print(chunk.decode())
    else:
        data = body.read()
        try:
            print(json.dumps(json.loads(data), indent=2, ensure_ascii=False))
        except Exception:
            print(data.decode(errors="replace"))


if __name__ == "__main__":
    main()
