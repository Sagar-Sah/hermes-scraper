"""Separate immutable legacy Printograph observations from scoped quotes."""

from urllib.parse import urlparse


def requires_service_context(row):
    return (
        urlparse(str(row.get("product_url", ""))).hostname
        in {"www.printograph.com", "printograph.com"}
        and str(row.get("source_product_id")) in {"1", "27", "36"}
        and row.get("provenance") in {"direct_quote", "source_table"}
        and row.get("pricing_scope") not in {"base_product", "listed_option_candidate"}
    )


def is_legacy_unscoped(row):
    """No historical service choice is reconstructed or guessed here."""
    context = row.get("pricing_context") or {}
    services = context.get("service_context") or {}
    return requires_service_context(row) and not (
        row.get("record_schema_version") == 2
        and context.get("schema_version") == 2
        and services.get("version") == 2
    )


def current_rows(rows):
    for row in rows:
        if not is_legacy_unscoped(row):
            yield row
