#!/usr/bin/env python3
"""Import one verified campaign product into a separate local Companion seed.

This copies public source evidence and does not import account sessions or
replace the original campaign. The target can then audit today's public pages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


SEED_ID = "companion-4over-business-cards"
SOURCE_CAMPAIGN = "printit-89-20260927"
PRODUCT_ID = "9532"
EVIDENCE_FIELDS = ("fourover_rows_evidence", "fourover_option_map_evidence", "fourover_price_table_evidence")


def copy_evidence(source: Path, target: Path, info: dict):
    relative, digest = info["object_path"], info["sha256"]
    if (not relative.startswith("objects/") or ".." in Path(relative).parts
            or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        raise ValueError("Invalid evidence reference")
    original = (source / relative).resolve()
    if not original.is_relative_to(source.resolve()) or original.is_symlink() or not original.is_file():
        raise ValueError("Evidence path is unavailable")
    raw = original.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Evidence hash changed")
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError("Target evidence conflicts with the source")
        return
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def import_seed(source: Path, target: Path):
    source, target = source.resolve(), target.resolve()
    campaign = json.loads((source / "campaigns" / (SOURCE_CAMPAIGN + ".json")).read_text())
    product = next(item for item in campaign["products"] if item["id"] == PRODUCT_ID)
    if (product.get("supplier_url") != "https://4over.com/brown-kraft-business-cards"
            or product.get("stage") != "finalized"):
        raise ValueError("Brown Kraft source mapping is not finalized")
    for key in EVIDENCE_FIELDS:
        copy_evidence(source, target, product[key])
    seed_product = {key: product[key] for key in ("id", "title", "storefront_url", "supplier_url", "supplier", "stage",
                    "reason", "row_count", "updated_at", *EVIDENCE_FIELDS) if key in product}
    seed = {"id": SEED_ID, "name": "Product Companion · 4over Business Cards (evidence subset)",
            "status": "completed", "created_at": campaign["created_at"], "updated_at": campaign["updated_at"],
            "products": [seed_product], "completion_groups": [[PRODUCT_ID]], "active_group_index": 1}
    folder = target / "campaigns"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (SEED_ID + ".json")
    if destination.exists():
        existing = json.loads(destination.read_text())
        if existing != seed:
            raise ValueError("Existing Companion seed differs; refusing to overwrite")
        return {"status": "already_present", "campaign": SEED_ID, "product_id": PRODUCT_ID}
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as handle:
            os.chmod(temporary, 0o600)
            json.dump(seed, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "imported", "campaign": SEED_ID, "product_id": PRODUCT_ID}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--target-data", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(import_seed(args.source_data, args.target_data)))
