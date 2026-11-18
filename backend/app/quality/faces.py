"""Faces: face detection with eye-state check (engines: mediapipe | haar).

Three engines selected by config (face_engine):
- 'none'  — dimension disabled: (None, {}).
- 'auto'  — try mediapipe FaceMesh; fall back to Haar on any import/runtime
            failure (mediapipe is an optional dependency).
- 'haar'  — OpenCV CascadeClassifier (bundled frontal face + eye models,
            located via cv2.data.haarcascades).

Scoring: a face with both eyes open is the baseline; closed eyes reduce the
score by 40 points per closed eye over all faces:
score = 100 - 40 * closed / (2 * faces). No face detected → (None, {...}):
the dimension does not participate in this photo's ranking.

mediapipe eye closure uses the Eye Aspect Ratio (EAR) < 0.20 rule on the
standard FaceMesh eye landmark indices; Haar counts detected eyes inside
the upper half of each face ROI (fewer than 2 detected = closed).
"""

import math


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _face_score(faces: int, closed: int) -> float:
    if faces <= 0:
        return 100.0
    return _clamp(100.0 - 40.0 * (closed / (2.0 * faces)))


def score(img, face_engine: str = "auto") -> tuple[float | None, dict]:
    """Score an RGB PIL.Image for face quality; None when no face found."""
    if face_engine == "none":
        return None, {}

    if face_engine in ("auto", "mediapipe"):
        try:
            return _score_mediapipe(img)
        except Exception:  # mediapipe absent/unusable -> degrade to haar
            pass

    return _score_haar(img)


def _score_haar(img) -> tuple[float | None, dict]:
    import cv2
    import numpy as np

    # OpenCV 5.x 移除了主模块的 CascadeClassifier（opencv-python<5
    # 才有）；缺失时返回 (None, …) 表示该维度未评估，评分端按
    # 缺失维度处理（重归一化），不崩不误报。
    cascade_cls = getattr(cv2, "CascadeClassifier", None)
    if cascade_cls is None:
        return None, {"faces": 0, "closed_eyes": 0, "engine": "haar-unavailable"}

    gray = np.asarray(img.convert("L"), dtype=np.uint8)

    face_cascade = cascade_cls(
        str(cv2.data.haarcascades) + "haarcascade_frontalface_default.xml"
    )
    eye_cascade = cascade_cls(
        str(cv2.data.haarcascades) + "haarcascade_eye.xml"
    )

    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
    )
    n = int(len(faces))
    if n == 0:
        return None, {"faces": 0, "closed_eyes": 0, "engine": "haar"}

    closed = 0
    for (x, y, w, h) in faces:
        if eye_cascade is None or eye_cascade.empty():
            break  # no eye model: treat all eyes as open
        roi = gray[y + int(h * 0.2) : y + int(h * 0.6), x : x + w]
        if roi.size == 0:
            continue
        eyes = eye_cascade.detectMultiScale(
            roi, scaleFactor=1.1, minNeighbors=4, minSize=(12, 12)
        )
        closed += max(0, 2 - int(len(eyes)))

    return _face_score(n, closed), {
        "faces": n,
        "closed_eyes": closed,
        "engine": "haar",
    }


# mediapipe FaceMesh eye landmark indices
_RIGHT_EYE = (33, 160, 158, 133, 153, 144)  # p1..p6
_LEFT_EYE = (362, 385, 387, 263, 373, 380)
_EAR_THRESHOLD = 0.20


def _eye_aspect_ratio(landmarks, indices, width: float, height: float):
    """EAR = (|p2-p6| + |p3-p5|) / (2 |p1-p4|), normalized pixel coords."""
    pts = []
    for idx in indices:
        lm = landmarks[idx]
        pts.append((lm.x * width, lm.y * height))
    p1, p2, p3, p4, p5, p6 = pts
    vertical = math.hypot(p2[0] - p6[0], p2[1] - p6[1]) + math.hypot(
        p3[0] - p5[0], p3[1] - p5[1]
    )
    horizontal = math.hypot(p1[0] - p4[0], p1[1] - p4[1])
    if horizontal <= 0:
        return 1.0  # degenerate: treat as open
    return vertical / (2.0 * horizontal)


def _score_mediapipe(img) -> tuple[float | None, dict]:
    import mediapipe as mp  # may raise ImportError (optional dependency)

    width, height = img.size
    results = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=True,
        refine_landmarks=True,
        max_num_faces=8,
    ).process(img)
    if results is None or not results.multi_face_landmarks:
        return None, {"faces": 0, "closed_eyes": 0, "engine": "mediapipe"}

    closed = 0
    n = len(results.multi_face_landmarks)
    for landmarks in results.multi_face_landmarks:
        for eye in (_RIGHT_EYE, _LEFT_EYE):
            if _eye_aspect_ratio(landmarks, eye, width, height) < _EAR_THRESHOLD:
                closed += 1

    return _face_score(n, closed), {
        "faces": n,
        "closed_eyes": closed,
        "engine": "mediapipe",
    }
