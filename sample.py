"""List or run public server samples with a personal development key."""

import argparse
import json
import os
from uuid import UUID

from nova_sdk import Client
from run import prepare, consult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="List public sample IDs; does not start a case")
    mode.add_argument("--case", type=UUID, help="Run a public sample UUID (uses team development credit)")
    args = parser.parse_args()
    key = os.environ.get("NOVA_DEV_KEY")
    if not key:
        parser.error("Set NOVA_DEV_KEY in your environment; do not put keys in source files")
    client = Client(api_base=os.environ.get("NOVA_API_BASE") or "https://nova.snubhai.org",
                    token=key, sample=True, timeout=30)
    if args.list:
        result = client._request("GET", "/v1/sample/cases")
    else:
        prepared = prepare()
        client.create_sample(str(args.case))
        result = consult(client, prepared)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
