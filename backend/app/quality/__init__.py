"""Quality analysis orchestration for photo-picker (spec §5 quality).

``analyze_photo`` decodes a file once (via app.imaging.decode_for_analysis)
and fans it out to the five per-dimension scorers in this package. The
package top level imports ONLY the standard library: the dimension modules
defer cv2/numpy/PIL imports into their functions so that
``import app.quality`` is safe everywhere, including DSH processes where
native extensions cannot load (docs/DEVENV.md).

Per-dimension contract: ``score(img) -> (float, dict)`` where img is an RGB
PIL.Image, the float is clamped to 0-100 (higher is better) and the dict
holds the raw evidence (returned under ``details`` here). faces.score takes
an extra ``face_engine`` argument and may return ``(None, {...})`` — no
faces found means the dimension does not participate.
"""

from __future__ import annotations

_DIMENSIONS = ("sharpness", "exposure", "noise", "composition")


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def analyze_photo(path, face_engine: str = "auto") -> dict | None:
    """Analyze one photo file; return the five dimension scores or None.

    Returns None when the file cannot be decoded (undecodable/corrupt RAW,
    missing file) — the caller records it and moves on. Any dimension score
    is clamped to 0-100; ``faces`` stays None when no face was detected or
    the face engine is disabled.
    """
    from app import imaging
    from app.quality import composition, exposure, faces, noise, sharpness

    try:
        img = imaging.decode_for_analysis(path)
    except imaging.DecodeError:
        return None

    sharp_score, sharp_details = sharpness.score(img)
    exposure_score, exposure_details = exposure.score(img)
    noise_score, noise_details = noise.score(img)
    composition_score, composition_details = composition.score(img)
    faces_score, faces_details = faces.score(img, face_engine)

    return {
        "sharpness": _clamp(sharp_score),
        "exposure": _clamp(exposure_score),
        "noise": _clamp(noise_score),
        "composition": _clamp(composition_score),
        "faces": None if faces_score is None else _clamp(faces_score),
        "details": {
            "sharpness": sharp_details,
            "exposure": exposure_details,
            "noise": noise_details,
            "composition": composition_details,
            "faces": faces_details,
        },
    }
