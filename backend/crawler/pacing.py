"""Validated per-source ceilings shared by browser and HTTP extraction."""

import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(value, default=30, now=None):
    """Honor delta-seconds and HTTP dates without accepting NaN/infinity."""
    fallback = max(0.0, float(default))
    if value is None:
        return fallback
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return fallback
    return max(0.0, seconds) if math.isfinite(seconds) else fallback


def source_pacing(profile, config):
    """Resolve explicit profile values, otherwise the configured 2/2 defaults.

    Reject invalid values instead of silently increasing a requested ceiling or
    allowing nonfinite numbers to disable the shared limiter.
    """
    rate_value = profile.get("requests_per_second", config.requests_per_second)
    concurrency_value = profile.get("max_concurrency", config.source_concurrency)
    try:
        if isinstance(rate_value, bool) or isinstance(concurrency_value, bool):
            raise ValueError()
        rate = float(rate_value)
        concurrency_number = float(concurrency_value)
        if (
            not math.isfinite(rate)
            or not 0.1 <= rate <= 100
            or not math.isfinite(concurrency_number)
            or not concurrency_number.is_integer()
            or not 1 <= concurrency_number <= 64
        ):
            raise ValueError()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Source pacing requires 0.1–100 finite requests per second and "
            "1–64 whole concurrent requests"
        ) from exc
    return rate, int(concurrency_number)
