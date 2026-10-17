"""Short-lived certification of an already settled, unchanged browser form.

This stores no prices or snapshots. Callers must read the live DOM again and
check source activity before reusing a certification for traversal bookkeeping.
Price publication still requires its own complete stability window.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse


def supports_stable_reuse(url, snapshot):
    """Only reviewed source forms may skip redundant bookkeeping waits.

    An arbitrary calculator can schedule a late update without a loader or
    recognizable request. Keep its original settling behavior until a source
    readiness contract is reviewed; a product-type attribute alone is no proof.
    """
    return (
        (urlparse(url).hostname or "").casefold()
        in {"printograph.com", "www.printograph.com"}
        and str(snapshot.get("product_type")) in {"1", "27", "36"}
    )


class SettledSnapshot:
    """Avoid consecutive identical waits without extending the quiet window."""

    def __init__(self, max_age_seconds: float = 1.0):
        self.max_age_seconds = max_age_seconds
        self.clear()

    def clear(self):
        self.signature = None
        self.token = None
        self.settled_at = None

    def remember(self, signature, token):
        self.signature = signature
        self.token = token
        self.settled_at = time.monotonic()

    def matches(self, signature, token, *, idle: bool):
        age = (
            time.monotonic() - self.settled_at
            if self.settled_at is not None
            else None
        )
        current = (
            idle
            and age is not None
            and 0 <= age <= self.max_age_seconds
            and self.signature == signature
            and self.token == token
        )
        if not current:
            # A transient state must not revive an earlier certification when
            # it happens to return to the same control values and price.
            self.clear()
        return current
