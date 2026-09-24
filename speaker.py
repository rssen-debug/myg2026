"""ASR + audio energy: faster-whisper words, RMS envelope, active speaker."""
import subprocess
import wave

import numpy as np

import config


def ensure_wav(src, dst, sr=16000):
    subprocess.run([config.FFMPEG, "-y", "-i", src, "-ar", str(sr), "-ac", "1", dst],
                   check=True, capture_output=True)
    return dst


def transcribe(path, model_size=None):
    """-> ([(word, t0, t1)], language). Empty list if faster-whisper missing."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  [speaker] faster-whisper not installed -> no transcript")
        return [], "en"
    model = WhisperModel(model_size or config.WHISPER_MODEL,
                         device="cpu", compute_type="int8")
    segs, info = model.transcribe(path, word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in (s.words or []):
            words.append((w.word.strip(), float(w.start), float(w.end)))
    return words, info.language


def wav_rms(path, window_sec=0.1, sr=16000):
    """RMS envelope of a mono 16-bit wav, one value per window."""
    try:
        with wave.open(path, "rb") as w:
            n = w.getnframes()
            sr = w.getframerate()
            data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
    except Exception:
        return np.zeros(1, dtype=np.float32)
    data /= 32768.0
    win = max(1, int(sr * window_sec))
    if len(data) < win:
        return np.array([float(np.sqrt(np.mean(data ** 2) + 1e-9))])
    n_windows = len(data) // win
    data = data[:n_windows * win].reshape(n_windows, win)
    return np.sqrt(np.mean(data ** 2, axis=1) + 1e-9).astype(np.float32)


def rms_z(rms, t0, t1, window_sec=0.1):
    """z-score of mean energy inside [t0,t1] vs the whole envelope."""
    if len(rms) < 4:
        return 0.0
    i0 = max(0, int(t0 / window_sec))
    i1 = min(len(rms), max(i0 + 1, int(t1 / window_sec)))
    local = float(np.mean(rms[i0:i1]))
    mu, sd = float(rms.mean()), float(rms.std())
    if sd < 1e-6:
        return 0.0
    return (local - mu) / sd


