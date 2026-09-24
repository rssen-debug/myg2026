"""Windows-safe vision engine: OpenCV YuNet face detection.
No MediaPipe. The model is downloaded by setup_models.py into ./models/.

YuNet alone is enough for everything this pipeline does: the crop follows the
largest face, the shot score rewards big centred frontal faces, and the
thumbnail portrait is cut around the detected box. YuNet also returns 5-point
landmarks (eyes/nose), which is all eye_contact_score needs. The LBF 68-point
landmark model used to be loaded here for a mouth_open() active-speaker signal
that nothing called - it cost a 54 MB download and a 54 MB file in the
workspace to compute a number no caller ever read.
"""
import os
import subprocess

import cv2
import numpy as np

import config

YUNET = os.path.join(config.MODELS_DIR, "face_detection_yunet_2023mar.onnx")

EVEN = lambda v: int(v) & ~1

_engine = None


class FaceEngine:
    def __init__(self, score_thr=0.6):
        self.ok = os.path.exists(YUNET)
        if not self.ok:
            print("  [vision] WARNING: model missing -> center-crop fallback. "
                  "Run: python setup_models.py")
            return
        self.det = cv2.FaceDetectorYN.create(YUNET, "", (320, 320),
                                             score_thr, 0.3, 5000)

    def detect_faces(self, frame):
        """-> [{'box':(x,y,w,h), 'lm5':5x2, 'score':float}] (frame coords)."""
        if not self.ok:
            return []
        h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        try:
            _, faces = self.det.detect(frame)
        except cv2.error:
            return []
        out = []
        if faces is None:
            return out
        for f in faces:
            out.append({"box": tuple(int(v) for v in f[:4]),
                        "lm5": np.asarray(f[4:14], dtype=np.float32).reshape(5, 2),
                        "score": float(f[14])})
        return out

    @staticmethod
    def eye_contact_score(face):
        """1.0 = frontal gaze. Uses nose offset vs eye midpoint (yaw) + roll."""
        re, le, nose = face["lm5"][0], face["lm5"][1], face["lm5"][2]
        eye_mid = (re + le) / 2.0
        eye_dist = float(np.linalg.norm(le - re)) + 1e-6
        yaw = abs(float(nose[0] - eye_mid[0])) / eye_dist
        roll = abs(float(re[1] - le[1])) / eye_dist
        return float(np.clip(1.0 - 2.2 * yaw - 1.5 * roll, 0.0, 1.0))


def engine():
    global _engine
    if _engine is None:
        _engine = FaceEngine()
    return _engine


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    return inter / float(aw * ah + bw * bh - inter + 1e-6)


def score_shot(video_path, n_samples=8):
    """0..1 quality of a clip as 'people footage'. < SHOT_SCORE_MIN -> reject.
    Rewards big, centered, frontal faces; rejects clips without faces (shoes/floor)."""
    eng = engine()
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    if total < n_samples:
        cap.release()
        return 0.0
    scores, face_hits = [], 0
    for k in range(n_samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / n_samples))
        ok, f = cap.read()
        if not ok:
            continue
        h, w = f.shape[:2]
        faces = eng.detect_faces(f)
        if not faces:
            scores.append(0.0)
            continue
        face_hits += 1
        fb = max(faces, key=lambda d: d["box"][2] * d["box"][3])
        x, y, bw, bh = fb["box"]
        size = (bw * bh) / (w * h)
        cx = x + bw / 2
        center = 1 - min(1.0, abs(cx - w / 2) / (w / 2))
        eye = eng.eye_contact_score(fb)
        penalty = 0.3 if len(faces) > 2 else 0.0
        scores.append(0.5 * min(size * 8, 1) + 0.25 * center + 0.25 * eye - penalty)
    cap.release()
    face_ratio = face_hits / max(n_samples, 1)
    if face_ratio < config.FACE_RATIO_MIN:
        return 0.0
    return float(np.median(scores)) if scores else 0.0


def compute_crop_path(video_path, detect_every=5, alpha=0.12, deadzone=0.03,
                      t_start=0.0, t_end=None):
    """Smoothed face-center path for 9:16 cropping, optionally limited to the
    [t_start, t_end] window (big CPU win - only analyze what we will use).
    -> (fps, W, H, [(t_abs, cx_in_source_px)]). Center fallback when no face."""
    eng = engine()
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    if t_start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, t_start * 1000)
    sw = 320
    sh = max(2, int(sw * H / max(W, 1)))
    path, cx_s, last_cx, i = [], None, None, 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t_abs = t_start + i / fps
        if t_end is not None and t_abs > t_end:
            break
        if i % detect_every == 0:
            small = cv2.resize(frame, (sw, sh)) if (W, H) != (sw, sh) else frame
            faces = eng.detect_faces(small)
            if faces:
                bx, _, bw, _ = max(faces, key=lambda f: f["box"][2] * f["box"][3])["box"]
                last_cx = (bx + bw / 2) * (W / sw)
            target = last_cx if last_cx is not None else W / 2
            if cx_s is None:
                cx_s = target
            if abs(target - cx_s) > W * deadzone:
                cx_s += alpha * (target - cx_s)
            path.append((t_abs, float(np.clip(cx_s, 0, W))))
        i += 1
    cap.release()
    if not path:
        path = [(t_start, W / 2)]
    return fps, W, H, path


def cx_at(path, t):
    cx = path[0][1]
    for tt, v in path:
        if tt <= t:
            cx = v
        else:
            break
    return cx


def sample_frames(video_path, n=5):
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    out = []
    for k in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / max(n, 1)))
        ok, f = cap.read()
        if ok:
            out.append(f)
    cap.release()
    return out
