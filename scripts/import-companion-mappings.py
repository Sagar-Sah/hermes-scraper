#!/usr/bin/env python3
"""Preview or apply the reviewed thirteen-product mapping-only seed.

This script never starts Hermes, imports crawler.app, accesses a supplier or
changes site data. Apply requires the exact manifest SHA printed by plan.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from crawler.companion_mapping_import import ReviewedMappingImporter, _json, _read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('plan', 'apply'))
    parser.add_argument('--source-data', type=Path, required=True)
    parser.add_argument('--target-data', type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--assessment', type=Path)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--approve-manifest-sha256')
    args = parser.parse_args()
    try:
        importer = ReviewedMappingImporter(source_data=args.source_data, target_data=args.target_data, evidence_root=args.evidence_root)
        if args.mode == 'plan':
            if args.assessment is None or args.approve_manifest_sha256 is not None:
                raise ValueError('mapping_import_plan_requires_assessment_only')
            manifest = importer.plan(args.assessment)
            importer.write_manifest(manifest, args.manifest)
            result = {'status': 'review_required', 'manifest_path': str(args.manifest),
                      'manifest_sha256': manifest['manifest_sha256'], 'product_ids': manifest['product_ids'],
                      'historical_objects': len(manifest['copy_objects']), 'runtime_writes': 0,
                      'supplier_requests': 0, 'automation_started': False}
        else:
            if args.assessment is not None:
                raise ValueError('mapping_import_apply_uses_frozen_manifest')
            manifest = _json(_read(importer.evidence, args.manifest))
            result = importer.apply(manifest, expected_sha=args.approve_manifest_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, TypeError, KeyError, IndexError, AttributeError) as exc:
        # Do not expose file contents, credentials, dealer amounts or arbitrary
        # operating-system/source exception strings in console status.
        reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'mapping_import_[a-z0-9_]+', str(exc)) else 'mapping_import_validation_failed'
        print(json.dumps({'status': 'held', 'reason': reason,
                          'supplier_requests': 0, 'automation_started': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
