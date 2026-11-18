"""analyze_photo orchestration tests — DSH-safe (no native libraries).

Per docs/DEVENV.md, cv2/numpy/PIL._imaging cannot load inside DSH, so these
tests monkeypatch the per-dimension score functions and the decode step:
they verify ONLY analyze_photo's aggregation, branch and clamping logic.
The real metric math is covered by test_quality_metrics.py (user terminal).

``app.imaging`` is imported INSIDE each test (not at module top) because
test_imaging_cache.py re-imports app.imaging mid-suite (sys.modules pop +
import_module); binding it at collection time would patch a stale module
object that analyze_photo never sees.
"""

from app.quality import analyze_photo


def test_analyze_photo_aggregates_all_dimensions(monkeypatch):
    from app import imaging
    from app.quality import composition, exposure, faces, noise, sharpness

    monkeypatch.setattr(imaging, "decode_for_analysis", lambda path: object())
    monkeypatch.setattr(
        sharpness, "score", lambda img: (11.0, {"laplacian_var": 3.5})
    )
    monkeypatch.setattr(
        exposure, "score", lambda img: (22.0, {"clipped_high": 0.0, "clipped_low": 0.0, "mean_luma": 127.5})
    )
    monkeypatch.setattr(noise, "score", lambda img: (33.0, {"dark_ratio": 1.0, "noise_index": 0.1}))
    monkeypatch.setattr(composition, "score", lambda img: (44.0, {"tilt_deg": 0.5}))
    monkeypatch.setattr(
        faces,
        "score",
        lambda img, engine="auto": (55.0, {"faces": 2, "closed_eyes": 0, "engine": "haar"}),
    )

    result = analyze_photo("/fake/path.jpg")

    assert result == {
        "sharpness": 11.0,
        "exposure": 22.0,
        "noise": 33.0,
        "composition": 44.0,
        "faces": 55.0,
        "details": {
            "sharpness": {"laplacian_var": 3.5},
            "exposure": {"clipped_high": 0.0, "clipped_low": 0.0, "mean_luma": 127.5},
            "noise": {"dark_ratio": 1.0, "noise_index": 0.1},
            "composition": {"tilt_deg": 0.5},
            "faces": {"faces": 2, "closed_eyes": 0, "engine": "haar"},
        },
    }


def test_analyze_photo_returns_none_on_decode_error(monkeypatch):
    from app import imaging

    def boom(path):
        raise imaging.DecodeError("undecodable file")

    monkeypatch.setattr(imaging, "decode_for_analysis", boom)

    assert analyze_photo("/gone.jpg") is None


def test_analyze_photo_face_engine_none_yields_none_faces(monkeypatch):
    """faces.score's real 'none' branch must return (None, {}) without cv2."""
    from app import imaging
    from app.quality import composition, exposure, noise, sharpness

    monkeypatch.setattr(imaging, "decode_for_analysis", lambda path: object())
    monkeypatch.setattr(sharpness, "score", lambda img: (50.0, {}))
    monkeypatch.setattr(exposure, "score", lambda img: (50.0, {}))
    monkeypatch.setattr(noise, "score", lambda img: (50.0, {}))
    monkeypatch.setattr(composition, "score", lambda img: (50.0, {}))

    result = analyze_photo("/fake.jpg", face_engine="none")

    assert result["faces"] is None
    assert result["details"]["faces"] == {}
    # other dimensions still scored
    assert result["sharpness"] == 50.0


def test_analyze_photo_clamps_out_of_range_scores(monkeypatch):
    from app import imaging
    from app.quality import composition, exposure, faces, noise, sharpness

    monkeypatch.setattr(imaging, "decode_for_analysis", lambda path: object())
    monkeypatch.setattr(sharpness, "score", lambda img: (250.0, {}))
    monkeypatch.setattr(exposure, "score", lambda img: (22.0, {}))
    monkeypatch.setattr(noise, "score", lambda img: (-10.0, {}))
    monkeypatch.setattr(composition, "score", lambda img: (44.0, {}))
    monkeypatch.setattr(faces, "score", lambda img, engine="auto": (999.0, {}))

    result = analyze_photo("/fake.jpg")

    assert result["sharpness"] == 100.0
    assert result["noise"] == 0.0
    assert result["faces"] == 100.0
