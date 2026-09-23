"""Token estimates: ~4 characters per token, calibrated from real usage.

Exact counts depend on each model's tokenizer; the budget only needs a
conservative estimate. After every run that reports ``usage``, the observed
input tokens per estimated token are folded into a per-instance factor
(exponential moving average, clamped), so estimates track the provider.
"""
from __future__ import annotations

import math
import threading

CHARS_PER_TOKEN = 4.0
_MIN_FACTOR, _MAX_FACTOR = 0.5, 2.5
_ALPHA = 0.3

_lock = threading.Lock()
_factors: dict[str, float] = {}


def estimate(text: str, instance_id: str | None = None) -> int:
    if not text:
        return 0
    base = len(text) / CHARS_PER_TOKEN
    with _lock:
        factor = _factors.get(instance_id or "", 1.0)
    return int(math.ceil(base * factor))


def calibrate(instance_id: str, estimated_tokens: int, actual_input_tokens: int | None) -> None:
    if not instance_id or not actual_input_tokens or estimated_tokens <= 0:
        return
    observed = max(_MIN_FACTOR, min(_MAX_FACTOR, actual_input_tokens / estimated_tokens))
    with _lock:
        prev = _factors.get(instance_id, 1.0)
        _factors[instance_id] = prev + _ALPHA * (observed - prev)


def factor(instance_id: str) -> float:
    with _lock:
        return _factors.get(instance_id, 1.0)
