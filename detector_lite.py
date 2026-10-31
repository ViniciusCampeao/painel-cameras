import cv2
import numpy as np
import threading
from pathlib import Path

try:
    from ultralytics import YOLO
    YOLO_OK = True
except ImportError:
    YOLO_OK = False

FACE_OK = False  # Face recognition desabilitado


class FaceManager:
    def __init__(self, known_faces_dir="/home/vinicius/vigilancia/known_faces"):
        self._encodings = []
        self._names = []

    def identify(self, frame_rgb, locations=None):
        return []


class Detector:
    def __init__(self):
        self.model = None
        self.face_mgr = None
        self.frame_count = 0
        self.last_detections = []
        self.last_faces = []
        self._lock = threading.Lock()

        if YOLO_OK:
            try:
                self.model = YOLO("yolov8n.pt")
                print("[detector] YOLO carregado")
            except Exception as e:
                print(f"[detector] erro ao carregar YOLO: {e}")


    def process(self, frame):
        if not self.model:
            return frame

        with self._lock:
            self.frame_count += 1

            if self.frame_count % 3 == 0:
                self._detect(frame)

            annotated = frame.copy()
            self._draw(annotated)
            return annotated

    def _detect(self, frame):
        if not self.model:
            return

        try:
            results = self.model(frame, conf=0.45, verbose=False)[0]
            detections = []
            has_person = False

            for box in results.boxes:
                cls_id = int(box.cls[0])
                label = self.model.names[cls_id]
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])

                detections.append({
                    "label": label,
                    "conf": conf,
                    "bbox": (x1, y1, x2, y2),
                })

                if label == "person":
                    has_person = True

            self.last_detections = detections

        except Exception as e:
            print(f"[detector] erro ao detectar: {e}")

    def _draw(self, frame):
        colors = {
            "person": (0, 120, 255),
            "dog": (0, 200, 100),
            "cat": (0, 200, 100),
            "car": (255, 100, 0),
            "truck": (255, 100, 0),
        }

        for det in self.last_detections:
            x1, y1, x2, y2 = det["bbox"]
            color = colors.get(det["label"], (200, 200, 0))
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            text = f"{det['label']} {det['conf']:.0%}"
            self._put_label(frame, text, x1, y1 - 6, color)

    @staticmethod
    def _put_label(frame, text, x, y, color):
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(frame, (x, y - th - 4), (x + tw, y + 4), (0, 0, 0), -1)
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
