from unittest.mock import patch

import pytest

from crawler.browser_stability import SettledSnapshot, supports_stable_reuse


@pytest.mark.parametrize("host", ["printograph.com", "www.printograph.com"])
@pytest.mark.parametrize("product_type", ["1", "27", "36"])
def test_only_reviewed_source_types_allow_reuse(host, product_type):
    assert supports_stable_reuse(f"https://{host}/product", {"product_type": product_type})


@pytest.mark.parametrize("url,product_type", [
    ("https://example.com/product", "1"),
    ("https://printograph.com.example.com/product", "1"),
    ("https://printograph.com/product", "unknown"),
    ("https://www.wsdisplay.com/product", "1"),
])
def test_unknown_sources_keep_original_settling(url, product_type):
    assert not supports_stable_reuse(url, {"product_type": product_type})


def test_reuse_requires_prior_stability_and_exact_activity_token():
    settled = SettledSnapshot()
    assert not settled.matches("form", (1, 2, 3, 4), idle=True)
    settled.remember("form", (1, 2, 3, 4))
    assert settled.matches("form", (1, 2, 3, 4), idle=True)


@pytest.mark.parametrize("index", range(4))
def test_navigation_selection_requests_and_response_progress_invalidate(index):
    settled = SettledSnapshot()
    token = (1, 2, 3, 4)
    changed = list(token)
    changed[index] += 1
    settled.remember("form", token)
    assert not settled.matches("form", tuple(changed), idle=True)
    assert not settled.matches("form", token, idle=True)


@pytest.mark.parametrize("signature,idle", [("changed", True), ("form", False)])
def test_transient_dom_or_busy_state_cannot_revive_old_certificate(signature, idle):
    settled = SettledSnapshot()
    settled.remember("form", (1,))
    assert not settled.matches(signature, (1,), idle=idle)
    assert not settled.matches("form", (1,), idle=True)


def test_reuse_does_not_extend_certification_and_expired_state_cannot_revive():
    with patch("crawler.browser_stability.time.monotonic", return_value=10) as now:
        settled = SettledSnapshot()
        settled.remember("form", (1,))
        now.return_value = 10.9
        assert settled.matches("form", (1,), idle=True)
        now.return_value = 11.01
        assert not settled.matches("form", (1,), idle=True)
        now.return_value = 10.9
        assert not settled.matches("form", (1,), idle=True)
