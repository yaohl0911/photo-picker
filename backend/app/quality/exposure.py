"""Exposure: histogram clipping ratios, linear penalty from 100.

Well-exposed means the histogram avoids the extreme ends: fewer than 5% of
pixels in [0, 25] AND fewer than 5% in [230, 255]. When a side exceeds the
allowance, its overshoot is penalized linearly (200 points per unit of
excess ratio), so ~55% clipped on one side alone reaches zero.
mean_luma is recorded as evidence (and for downstream UI display).
"""


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def score(img) -> tuple[float, dict]:
    """Score an RGB PIL.Image for exposure; higher = better exposed."""
    import numpy as np

    gray = np.asarray(img.convert("L"), dtype=np.float64)
    total = float(gray.size)
    if total == 0:
        return 0.0, {"clipped_high": 0.0, "clipped_low": 0.0, "mean_luma": 0.0}

    clipped_low = float(np.count_nonzero(gray <= 25.0)) / total
    clipped_high = float(np.count_nonzero(gray >= 230.0)) / total
    mean_luma = float(gray.mean())

    if clipped_low < 0.05 and clipped_high < 0.05:
        value = 100.0
    else:
        value = 100.0 - 200.0 * max(0.0, clipped_low - 0.05) - 200.0 * max(0.0, clipped_high - 0.05)

    return _clamp(value), {
        "clipped_high": round(clipped_high, 4),
        "clipped_low": round(clipped_low, 4),
        "mean_luma": round(mean_luma, 2),
    }
