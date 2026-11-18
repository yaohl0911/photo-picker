"""Sharpness: Laplacian variance, log-normalized to 0-100.

The Laplacian highlights high-frequency edges; its variance over an image is
the classic blur metric. Both the full image and its central 50% crop are
measured and the weaker value wins, so a sharp center inside a soft frame
(e.g. motion-blurred edges) does not overstate sharpness.

Normalization anchors (plan Task 4): var 10 -> ~10, var 1000 -> ~90,
interpolated logarithmically because Laplacian variance spans decades.
Values above var 1000 saturate toward 100.
"""

import math


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def score(img) -> tuple[float, dict]:
    """Score an RGB PIL.Image for sharpness; higher = crisper."""
    import cv2
    import numpy as np

    gray = np.asarray(img.convert("L"), dtype=np.float64)
    lap = cv2.Laplacian(gray, cv2.CV_64F)

    height, width = gray.shape
    y0, y1 = int(height * 0.25), int(height * 0.75)
    x0, x1 = int(width * 0.25), int(width * 0.75)
    center = lap[y0:y1, x0:x1]
    var = float(lap.var())
    if center.size:
        var = min(var, float(center.var()))

    if var <= 0.0:
        return 0.0, {"laplacian_var": 0.0}

    normalized = 10.0 + 80.0 * math.log(var / 10.0) / math.log(100.0)
    return _clamp(normalized), {"laplacian_var": round(var, 3)}
