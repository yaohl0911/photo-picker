"""Composition: horizon-level tilt via the gradient orientation histogram.

Sobel gradients are perpendicular to edges, so a horizontal line produces
gradients pointing at 90° (mod 180). The dominant gradient orientation of
the image therefore reveals the tilt of the dominant line structure:
tilt_deg = |dominant_orientation - 90|. A full 180-bin magnitude-weighted
orientation histogram finds the peak; the peak ±3 bins are averaged with
doubled angles (circular statistics) so bins near the 0/180 wrap stay
correct. Score: tilt < 1.5° = 100, decaying linearly to 0 at 10°.
Flat images carry no orientation evidence and return the neutral 70.
"""

import math


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _score_from_tilt(tilt_deg: float) -> float:
    if tilt_deg <= 1.5:
        return 100.0
    if tilt_deg >= 10.0:
        return 0.0
    return 100.0 * (10.0 - tilt_deg) / (10.0 - 1.5)


def _dominant_orientation(hist) -> float:
    """Weighted circular mean of the histogram peak's neighborhood (deg 0-180)."""
    peak = int(max(range(len(hist)), key=lambda b: hist[b]))
    vec_x = 0.0
    vec_y = 0.0
    for offset in range(-3, 4):
        bin_index = (peak + offset) % len(hist)
        weight = float(hist[bin_index])
        if weight <= 0.0:
            continue
        center_deg = bin_index + 0.5
        angle = math.radians(2.0 * center_deg)  # doubled: mean of directions mod 180
        vec_x += weight * math.cos(angle)
        vec_y += weight * math.sin(angle)
    dominant = math.degrees(math.atan2(vec_y, vec_x)) / 2.0
    if dominant < 0.0:
        dominant += 180.0
    return dominant


def score(img) -> tuple[float, dict]:
    """Score an RGB PIL.Image for level composition; higher = straighter."""
    import cv2
    import numpy as np

    gray = np.asarray(img.convert("L"), dtype=np.float64)
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)

    threshold = max(1.0, float(mag.mean()))
    mask = mag > threshold
    if not np.any(mask):  # flat image: no line structure to measure
        return 70.0, {"tilt_deg": None}

    angles = np.degrees(np.arctan2(gy[mask], gx[mask])) % 180.0
    hist, _ = np.histogram(
        angles, bins=180, range=(0.0, 180.0), weights=mag[mask]
    )

    dominant = _dominant_orientation(hist)
    tilt = abs(dominant - 90.0)  # gradient ⊥ line: distance from vertical
    return _clamp(_score_from_tilt(tilt)), {"tilt_deg": round(float(tilt), 3)}
