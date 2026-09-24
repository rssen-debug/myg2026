"""Downloads the YuNet face-detection model into ./models/."""
import os
import sys

import requests

MODELS = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    mdir = os.path.join(here, "models")
    os.makedirs(mdir, exist_ok=True)
    for name, url in MODELS.items():
        dst = os.path.join(mdir, name)
        if os.path.exists(dst) and os.path.getsize(dst) > 1000:
            print(f"[ok] {name} already present")
            continue
        print(f"[..] downloading {name} ...")
        try:
            r = requests.get(url, timeout=300)
            r.raise_for_status()
            with open(dst, "wb") as f:
                f.write(r.content)
            print(f"[ok] {name} ({len(r.content) // 1024} KB)")
        except Exception as e:
            print(f"[!!] failed {name}: {e}")
            sys.exit(1)
    print("All models ready.")


if __name__ == "__main__":
    main()
