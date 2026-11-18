"""Real-decoding tests for app.imaging — require PIL's C extension.

This file is skipped entirely inside DSH (native extensions cannot load
there; see docs/DEVENV.md) and runs in the user's terminal, where real
JPG/PNG decoding happens. rawpy is always faked via sys.modules injection
(its imread/postprocess/extract_thumb surface is mocked; the resulting
ndarray/bytes still go through real PIL code paths).
"""

import io
import sys
import types
from pathlib import Path

import pytest

# Gate the whole file: in DSH the C extension raises a plain ImportError
# (AMFI library validation), which importorskip only skips when
# exc_type=ImportError (default catches just ModuleNotFoundError).
pytest.importorskip("PIL._imaging", exc_type=ImportError)

from PIL import Image  # noqa: E402

from app import imaging  # noqa: E402


def _write_jpeg(path, size, color=(180, 40, 60)):
    Image.new("RGB", size, color).save(path, format="JPEG", quality=92)
    return path


def _write_png(path, size, mode="RGB"):
    color = 128 if mode == "L" else (10, 200, 90)
    Image.new(mode, size, color).save(path, format="PNG")
    return path


class _FakeRaw:
    """Context-manager stand-in for the object rawpy.imread() returns."""

    def __init__(self, postprocess_exc=None, thumb=None, thumb_exc=None):
        self.postprocess_exc = postprocess_exc
        self.postprocess_calls = []
        self.thumb = thumb
        self.thumb_exc = thumb_exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def postprocess(self, **kwargs):
        self.postprocess_calls.append(kwargs)
        if self.postprocess_exc is not None:
            raise self.postprocess_exc
        import numpy as np

        arr = np.zeros((64, 96, 3), dtype=np.uint8)
        arr[..., 0] = 200  # reddish image, 96 wide x 64 high
        return arr

    def extract_thumb(self):
        if self.thumb_exc is not None:
            raise self.thumb_exc
        return self.thumb


def _install_fake_rawpy(monkeypatch, raw):
    mod = types.ModuleType("rawpy")
    mod.imread = lambda _path: raw
    monkeypatch.setitem(sys.modules, "rawpy", mod)
    return mod


def test_decode_jpeg_rgb_mode_and_downscale(tmp_path):
    p = _write_jpeg(tmp_path / "a.jpg", (800, 600))
    img = imaging.decode(str(p), max_side=256)
    assert img.mode == "RGB"
    assert img.size == (256, 192)  # 800x600 -> 4:3 -> 256x192 exactly
    assert max(img.size) <= 256


def test_decode_png_portrait_keeps_ratio(tmp_path):
    p = _write_png(tmp_path / "b.png", (300, 900))
    img = imaging.decode(str(p), max_side=256)
    assert img.mode == "RGB"
    assert max(img.size) <= 256
    w, h = img.size
    assert abs(w * 3 - h) <= 1  # 1:3 aspect preserved


def test_decode_does_not_upscale_small_images(tmp_path):
    p = _write_jpeg(tmp_path / "small.jpg", (100, 80))
    assert imaging.decode(str(p), max_side=512).size == (100, 80)
    # max_side=0 (or None) disables scaling entirely.
    assert imaging.decode(str(p), max_side=0).size == (100, 80)


def test_decode_converts_grayscale_to_rgb(tmp_path):
    p = _write_png(tmp_path / "gray.png", (64, 64), mode="L")
    img = imaging.decode(str(p), max_side=64)
    assert img.mode == "RGB"


def test_decode_raw_via_mocked_rawpy(tmp_path, monkeypatch):
    raw = _FakeRaw()
    _install_fake_rawpy(monkeypatch, raw)
    p = tmp_path / "IMG_0001.CR3"
    p.write_bytes(b"fake raw bytes")

    img = imaging.decode(str(p), max_side=128)

    assert isinstance(img, Image.Image)
    assert img.mode == "RGB"
    assert img.size == (96, 64)  # ndarray (64, 96, 3), 96 <= 128 -> untouched
    # rawpy must be asked for camera WB + half size demosaic.
    assert raw.postprocess_calls == [
        {"use_camera_wb": True, "half_size": True}
    ]


def test_decode_raw_fallback_to_embedded_preview(tmp_path, monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (7, 90, 7)).save(buf, format="JPEG")
    raw = _FakeRaw(
        postprocess_exc=RuntimeError("sensor data unreadable"),
        thumb=types.SimpleNamespace(format="JPEG", data=buf.getvalue()),
    )
    _install_fake_rawpy(monkeypatch, raw)
    p = tmp_path / "IMG_0002.ARW"
    p.write_bytes(b"fake raw bytes")

    img = imaging.decode(str(p), max_side=64)

    assert isinstance(img, Image.Image)
    assert img.mode == "RGB"
    assert img.size == (32, 32)


def test_raw_embedded_preview_bitmap_thumb(monkeypatch, tmp_path):
    import numpy as np

    arr = np.zeros((16, 20, 3), dtype=np.uint8)
    raw = _FakeRaw(thumb=types.SimpleNamespace(format="BITMAP", data=arr))
    _install_fake_rawpy(monkeypatch, raw)
    p = tmp_path / "IMG_0003.DNG"
    p.write_bytes(b"fake raw bytes")

    img = imaging.raw_embedded_preview(str(p))

    assert isinstance(img, Image.Image)
    assert img.size == (20, 16)


def test_decode_raw_all_strategies_fail_raises_decodeerror(tmp_path, monkeypatch):
    raw = _FakeRaw(postprocess_exc=RuntimeError("bad"), thumb_exc=RuntimeError("no thumb"))
    _install_fake_rawpy(monkeypatch, raw)
    p = tmp_path / "IMG_0004.NEF"
    p.write_bytes(b"fake raw bytes")

    with pytest.raises(imaging.DecodeError):
        imaging.decode(str(p), max_side=64)


def test_decode_rawpy_imread_failure_raises_decodeerror(tmp_path, monkeypatch):
    mod = types.ModuleType("rawpy")

    def _boom(_path):
        raise RuntimeError("unsupported raw file")

    mod.imread = _boom
    monkeypatch.setitem(sys.modules, "rawpy", mod)
    p = tmp_path / "IMG_0005.CR2"
    p.write_bytes(b"fake raw bytes")

    with pytest.raises(imaging.DecodeError):
        imaging.decode(str(p), max_side=64)


def test_decode_corrupt_file_raises_decodeerror(tmp_path):
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"definitely not an image \x00\xff\xfe")

    with pytest.raises(imaging.DecodeError):
        imaging.decode(str(p), max_side=64)


def test_decode_heic_real_file(tmp_path):
    pytest.importorskip("pillow_heif", exc_type=ImportError)
    try:
        # pillow_heif wraps a broken native import in DeferredError, so the
        # package import above can succeed while using it still fails; force
        # the C module to load to detect that case now.
        import _pillow_heif  # noqa: F401
    except ImportError:
        pytest.skip("pillow_heif native extension unavailable")
    import pillow_heif

    src = Image.new("RGB", (64, 48), (200, 120, 30))
    p = tmp_path / "c.heic"
    pillow_heif.from_pillow(src).save(p, quality=33)

    img = imaging.decode(str(p), max_side=32)

    assert isinstance(img, Image.Image)
    assert img.mode == "RGB"
    assert img.size == (32, 24)


def test_heif_opener_registered_exactly_once(tmp_path, monkeypatch):
    mod = types.ModuleType("pillow_heif")
    calls = []
    mod.register_heif_opener = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "pillow_heif", mod)
    monkeypatch.setattr(imaging, "_HEIF_REGISTERED", False)

    p1 = _write_jpeg(tmp_path / "x1.jpg", (40, 40))
    p2 = _write_jpeg(tmp_path / "x2.jpg", (40, 40))
    imaging.decode(str(p1), max_side=16)
    imaging.decode(str(p2), max_side=16)

    assert calls == [1]  # registered once, not per decode
    assert imaging._HEIF_REGISTERED is True


def test_missing_pillow_heif_is_tolerated(tmp_path, monkeypatch):
    # None in sys.modules makes `import pillow_heif` raise ImportError;
    # decoding of standard formats must keep working.
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    monkeypatch.setattr(imaging, "_HEIF_REGISTERED", False)

    p = _write_jpeg(tmp_path / "plain.jpg", (40, 40))
    img = imaging.decode(str(p), max_side=16)

    assert isinstance(img, Image.Image)
    assert imaging._HEIF_REGISTERED is True


def test_decode_for_analysis_caps_at_256(tmp_path):
    p = _write_jpeg(tmp_path / "big.jpg", (1000, 1000))
    img = imaging.decode_for_analysis(str(p))
    assert img.size == (256, 256)
