"""Check USDC balances on Base Sepolia for payer + seller addresses.

Usage:
  python 04_check_balances.py <payer_address> <seller_address>
  python 04_check_balances.py 0x9e94...  0xF72b...
"""
import json
import sys
import urllib.request

USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
RPC = "https://sepolia.base.org"


def balance_of(addr: str) -> int:
    data = "0x70a08231" + "0" * 24 + addr[2:].lower()
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC, "data": data}, "latest"],
    }).encode()
    req = urllib.request.Request(RPC, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        result = json.loads(r.read()).get("result", "0x0")
    return int(result, 16) if result else 0


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    for label, addr in [("payer ", sys.argv[1]), ("seller", sys.argv[2])]:
        bal = balance_of(addr)
        print(f"{label}  {addr}: {bal / 1_000_000:.6f} USDC  ({bal} units)")


if __name__ == "__main__":
    main()
