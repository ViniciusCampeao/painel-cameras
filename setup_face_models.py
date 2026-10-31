#!/usr/bin/env python3
"""
Setup script para face_recognition_models
Coloca os modelos nos lugares certos para o face_recognition funcionar
"""

import urllib.request
import os
import sys
from pathlib import Path

def setup_face_models():
    target_dir = Path.home() / ".face_recognition_lib"
    target_dir.mkdir(parents=True, exist_ok=True)

    models = {
        "mmod_human_face_detector.dat": "https://github.com/ageitgey/face_recognition_models/raw/master/face_recognition_models/models/mmod_human_face_detector.dat",
        "shape_predictor_68_face_landmarks.dat": "https://github.com/ageitgey/face_recognition_models/raw/master/face_recognition_models/models/shape_predictor_68_face_landmarks.dat",
        "resnet_34_recognize_faces_v1.dat": "https://github.com/ageitgey/face_recognition_models/raw/master/face_recognition_models/models/resnet_34_recognize_faces_v1.dat",
    }

    print(f"Configurando modelos em: {target_dir}\n")

    for name, url in models.items():
        path = target_dir / name

        if path.exists():
            size = path.stat().st_size / 1024 / 1024
            print(f"✓ {name} ({size:.1f}MB)")
            continue

        try:
            print(f"↓ {name}...", end=" ", flush=True)
            urllib.request.urlretrieve(url, path)
            size = path.stat().st_size / 1024 / 1024
            print(f"✓ ({size:.1f}MB)")
        except Exception as e:
            print(f"✗ {e}")
            return False

    print(f"\n✓ Modelos prontos!\n")
    return True

if __name__ == "__main__":
    success = setup_face_models()
    sys.exit(0 if success else 1)
