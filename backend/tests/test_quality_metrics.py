"""Real metric tests on programmatic images — run in the USER terminal.

These exercise the actual cv2/numpy pipelines. Inside DSH the cv2 import
fails with a plain ImportError (docs/DEVENV.md AMFI constraint), so the
whole file skips there — `pytest.importorskip("cv2", exc_type=ImportError)`
is the designed skip guard for that failure shape.
"""

import pytest

pytest.importorskip("cv2", exc_type=ImportError)


def test_sharpness_distinguishes_blur():
    import numpy as np
    from PIL import Image, ImageFilter

    from app.quality import sharpness

    rng = np.random.default_rng(42)
    sharp_arr = rng.integers(0, 256, size=(256, 256), dtype=np.uint8)
    sharp_img = Image.fromarray(sharp_arr, "L").convert("RGB")
    blur_img = sharp_img.filter(ImageFilter.GaussianBlur(3.0))

    sharp_score, sharp_details = sharpness.score(sharp_img)
    blur_score, blur_details = sharpness.score(blur_img)

    assert sharp_score - blur_score > 25
    assert blur_details["laplacian_var"] < sharp_details["laplacian_var"]
    assert 0.0 <= blur_score <= 100.0
    assert 0.0 <= sharp_score <= 100.0


def test_exposure_flags_overexposure():
    import numpy as np
    from PIL import Image

    from app.quality import exposure

    width, height = 256, 128
    base = np.tile(np.linspace(0, 255, width, dtype=np.float64), (height, 1))
    over = np.clip(base + 80.0, 0, 255).astype(np.uint8)
    img = Image.fromarray(over, "L").convert("RGB")

    score, details = exposure.score(img)

    assert score < 50
    assert details["clipped_high"] > 0.2
    assert details["clipped_low"] == 0.0
    assert details["mean_luma"] > 127.5


def test_noise_penalizes_noisy_dark_image():
    import numpy as np
    from PIL import Image

    from app.quality import noise

    rng = np.random.default_rng(7)
    height, width = 256, 256
    clean = np.tile(np.linspace(0, 55, height, dtype=np.float64)[:, None], (1, width))
    noisy = np.clip(
        clean + rng.normal(0, 45, size=(height, width)), 0, 255
    ).astype(np.uint8)
    clean_img = Image.fromarray(clean.astype(np.uint8), "L").convert("RGB")
    noisy_img = Image.fromarray(noisy, "L").convert("RGB")

    clean_score, clean_details = noise.score(clean_img)
    noisy_score, noisy_details = noise.score(noisy_img)

    assert clean_score - noisy_score > 15
    assert clean_details["dark_ratio"] > 0.9
    assert noisy_details["noise_index"] > clean_details["noise_index"]


def test_noise_neutral_when_no_dark_region():
    import numpy as np
    from PIL import Image

    from app.quality import noise

    bright = Image.fromarray(np.full((64, 64), 220, dtype=np.uint8), "L").convert("RGB")

    score, details = noise.score(bright)

    assert score == 70.0
    assert details["dark_ratio"] == 0.0
    assert details["noise_index"] is None


def test_composition_detects_tilted_lines():
    import math

    import numpy as np
    from PIL import Image

    from app.quality import composition

    def grating(theta_deg: float) -> Image.Image:
        """Full-frame line grating: constant along a line tilted by theta_deg
        from horizontal. Synthesized directly (instead of PIL.rotate) so no
        border-fill artifacts pollute the gradient orientation histogram."""
        size = 256
        angle = math.radians(theta_deg)
        yy, xx = np.mgrid[0:size, 0:size]
        phase = (-xx * math.sin(angle) + yy * math.cos(angle)) * (2 * math.pi / 24.0)
        arr = 127.5 + 127.5 * np.sin(phase)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "L").convert("RGB")

    flat_score, flat_details = composition.score(grating(0))
    tilt_score, tilt_details = composition.score(grating(5))

    assert flat_details["tilt_deg"] < 1.5
    assert abs(tilt_details["tilt_deg"] - 5.0) <= 1.5
    assert tilt_score < flat_score


def test_faces_blank_image_has_no_faces():
    import numpy as np
    from PIL import Image

    from app.quality import faces

    blank = Image.fromarray(np.full((128, 128), 200, dtype=np.uint8), "L").convert("RGB")

    score_haar, details_haar = faces.score(blank, face_engine="haar")
    assert score_haar is None
    assert details_haar["faces"] == 0
    assert details_haar["engine"] == "haar"

    # auto: mediapipe is not installed -> degrades to haar (details agree)
    score_auto, details_auto = faces.score(blank, face_engine="auto")
    assert score_auto is None
    assert details_auto["faces"] == 0
    assert details_auto["engine"] in ("haar", "mediapipe")

    score_none, details_none = faces.score(blank, face_engine="none")
    assert score_none is None
    assert details_none == {}
