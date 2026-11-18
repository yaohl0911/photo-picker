"""Noise: high-frequency energy in dark regions relative to signal.

Sensor noise is most visible in shadows, so the metric only looks at pixels
with luma < 60. noise_index = mean(|Laplacian| over dark pixels) /
(mean dark luma + 1): the +1 keeps pure-black regions well-defined (their
clipped-to-zero noise is genuinely invisible). The score maps the index
through 100/(1+index): clean dark areas score near 100, heavy shadow noise
lands near 20-25. Without any dark region (bright/overexposed frame) the
dimension is unmeasurable and returns the neutral 70.
"""


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def score(img) -> tuple[float, dict]:
    """Score an RGB PIL.Image for shadow noise; higher = cleaner."""
    import cv2
    import numpy as np

    gray = np.asarray(img.convert("L"), dtype=np.float64)
    dark_mask = gray < 60.0
    dark_ratio = float(np.count_nonzero(dark_mask)) / float(max(1, gray.size))

    if dark_ratio < 0.01:  # no meaningful dark region to judge
        return 70.0, {"dark_ratio": round(dark_ratio, 4), "noise_index": None}

    lap = cv2.Laplacian(gray, cv2.CV_64F)
    high_freq = float(np.abs(lap[dark_mask]).mean())
    dark_signal = float(gray[dark_mask].mean())
    noise_index = high_freq / (dark_signal + 1.0)

    value = 100.0 / (1.0 + noise_index)
    return _clamp(value), {
        "dark_ratio": round(dark_ratio, 4),
        "noise_index": round(noise_index, 4),
    }
