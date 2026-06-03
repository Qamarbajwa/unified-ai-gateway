from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT_DIR / "services" / "mcp-server"
sys.path.insert(0, str(MCP_DIR))

from project_registry import scan_connected_tools


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit connected external tool workspaces.")
    parser.add_argument("--persist", action="store_true", help="write .harness/tool_inventory.json")
    parser.add_argument("--registry", help="path to connected_tools.json")
    parser.add_argument("--snapshot", help="path to tool_inventory.json")
    args = parser.parse_args()

    result = scan_connected_tools(args.registry, args.snapshot, persist=args.persist)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
