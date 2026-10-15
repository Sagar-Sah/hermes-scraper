#!/usr/bin/env python3
"""Inventory frozen local crawler evidence without network or model access."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from crawler.evidence_inventory import build_inventory, render_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--object-root", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    result = build_inventory(args.database, args.object_root, run_id=args.run_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    args.report.write_text(render_report(result))
    print(json.dumps({"output": str(args.output), "report": str(args.report),
                      "routes": len(result["routes"]), "captures": sum(result["attribution_counts"].values()),
                      "lookup_domains": len(result["successful_lookup_domains"]),
                      "network_calls": result["network_calls"]}))


if __name__ == "__main__":
    main()
