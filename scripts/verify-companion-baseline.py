#!/usr/bin/env python3
"""Prepare private baseline receipts; never approve, clone, restore or publish.

Use the actual Hermes data directory explicitly. ``backup`` accepts one complete
ZIP/tar containing a normal SQL export and wp-content. Archives stay private.
``updraft-backup`` accepts that backup generation's database .gz plus all plugins,
themes, uploads (including numbered splits) and others ZIPs. Repeat --component
for each ZIP and --expected-part for every part listed by Updraft's backup UI;
an omitted final split cannot be inferred from downloaded files alone. This
streaming conversion does not alter the originals or contact a remote service.
``runtime`` runs the registered read-only renderer; absent trusted shipping and
customer-flow fixtures remain unknown and cannot generate a passed receipt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--capture-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Verify and preserve a complete exported backup")
    backup.add_argument("--environment", choices=("production", "staging"), required=True)
    backup.add_argument("--archive", type=Path, required=True)
    updraft = commands.add_parser("updraft-backup", help="Verify and privately combine all explicit UpdraftPlus components")
    updraft.add_argument("--environment", choices=("production", "staging"), required=True)
    updraft.add_argument("--database", type=Path, required=True)
    updraft.add_argument("--component", type=Path, action="append", required=True)
    updraft.add_argument("--expected-part", action="append", required=True,
                         help="Every part listed by the backup UI, e.g. db, plugins, themes, uploads, uploads2, others")
    commands.add_parser("preserve", help="Record captured staging differences against both retained backups")
    commands.add_parser("isolation", help="Recheck trusted connector isolation and runtime fingerprints")
    runtime = commands.add_parser("runtime", help="Run actual representative storefront checks; unknown stays held")
    runtime.add_argument("--product-id", action="append", required=True)
    commands.add_parser("receipts", help="List validated receipt hashes for later human review")
    args = parser.parse_args()
    # No implicit .env or current-directory data selection. This script cannot
    # silently switch the user's existing profile/database root.
    if not args.data_dir.is_absolute() or not args.data_dir.is_dir() or args.data_dir.is_symlink():
        parser.error("--data-dir must be an existing absolute Hermes data directory")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from crawler.autonomous_product_pipeline import StagingBaseline
    from crawler.companion_baseline_setup import BaselineSetupVerifier
    from crawler.companion_environments import CompanionEnvironmentRegistry
    from crawler.companion_storefront_verifier import CompanionStorefrontVerifier
    root = args.data_dir / "autonomous-companion"
    environments = CompanionEnvironmentRegistry(root)
    verifier = BaselineSetupVerifier(StagingBaseline(root, environments),
                                     storefront_verifier=CompanionStorefrontVerifier(environments))
    try:
        if args.command == "backup":
            result = verifier.register_backup(args.capture_id, args.environment, args.archive)
        elif args.command == "updraft-backup":
            result = verifier.register_updraft_backup(args.capture_id, args.environment, args.database,
                                                       args.component, args.expected_part)
        elif args.command == "preserve":
            result = verifier.preserve_unreleased_work(args.capture_id)
        elif args.command == "isolation":
            result = asyncio.run(verifier.verify_isolation(args.capture_id))
        elif args.command == "runtime":
            result = asyncio.run(verifier.verify_runtime_parity(args.capture_id, args.product_id))
        else:
            result = verifier.receipts(args.capture_id)
    except (ValueError, OSError) as error:
        reason = str(error)
        result = {"status": "held", "reason": reason if re.fullmatch(r"[a-z0-9_]{1,120}", reason) else "baseline_setup_unavailable"}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("status") == "passed" or result.get("complete") else 2


if __name__ == "__main__":
    raise SystemExit(main())
