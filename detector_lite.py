import cv2
import numpy as np
import threading
from pathlib import Path

try:
    from ultralytics import YOLO
    YOLO_OK = True
except ImportError:
    YOLO_OK = False

try:
    import face_recognition
    try:
        face_recognition.load_image_file  # test if models are installed
        FACE_OK = True
    except (AttributeError, Exception):
        FACE_OK = False
except (ImportError, ModuleNotFoundError):
    FACE_OK = False


class FaceManager:
    def __init__(self, known_faces_dir="/home/vinicius/vigilancia/known_faces"):
        self._encodings = []
        self._names = []
        self._tolerance = 0.5
        self._load_known(known_faces_dir)

    def _load_known(self, known_faces_dir):
        if not FACE_OK:
            print("[faces] face_recognition não instalado")
            return

        base_dir = Path(known_faces_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        self._encodings.clear()
        self._names.clear()

        for person_dir in sorted(base_dir.iterdir()):
            if not person_dir.is_dir():
                continue
            name = person_dir.name
            for img_path in person_dir.glob("*"):
                if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                try:
                    img = face_recognition.load_image_file(str(img_path))
                    encs = face_recognition.face_encodings(img)
                    for enc in encs:
                        self._encodings.append(enc)
                        self._names.append(name)
                except Exception as e:
                    print(f"[faces] erro ao carregar {img_path}: {e}")

        print(f"[faces] carregado {len(self._encodings)} encodings para {len(set(self._names))} pessoas")

    def identify(self, frame_rgb, locations=None):
        if not FACE_OK:
            return []

        try:
            if locations is None:
                locations = face_recognition.face_locations(frame_rgb, model="hog")

            if not locations:
                return []

            encodings = face_recognition.face_encodings(frame_rgb, locations)
            results = []

            for (top, right, bottom, left), enc in zip(locations, encodings):
                if not self._encodings:
                    name = "desconhecido"
                    distance = 1.0
                else:
                    distances = face_recognition.face_distance(self._encodings, enc)
                    best_match_idx = np.argmin(distances)
                    best_distance = distances[best_match_idx]

                    if best_distance < self._tolerance:
                        name = self._names[best_match_idx]
                        distance = best_distance
                    else:
                        name = "desconhecido"
                        distance = best_distance

                results.append({
                    "name": name,
                    "distance": distance,
                    "bbox": (left, top, right, bottom),
                })

            return results
        except ModuleNotFoundError as e:
            print(f"[faces] erro: modelos não instalados ({e}). Execute: pip install git+https://github.com/ageitgey/face_recognition_models")
            return []
        except Exception as e:
            print(f"[faces] erro ao identificar: {e}")
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

        if FACE_OK:
            try:
                self.face_mgr = FaceManager()
                print("[detector] FaceManager carregado")
            except Exception as e:
                print(f"[detector] erro ao carregar FaceManager: {e}")

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

            if has_person and self.face_mgr:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                self.last_faces = self.face_mgr.identify(rgb)
            else:
                self.last_faces = []

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

        for face in self.last_faces:
            x1, y1, x2, y2 = face["bbox"]
            is_known = face["name"] != "desconhecido"
            color = (0, 255, 0) if is_known else (0, 0, 255)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            text = f"{face['name']} ({1 - face['distance']:.0%})"
            self._put_label(frame, text, x1, y2 + 18, color)

    @staticmethod
    def _put_label(frame, text, x, y, color):
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(frame, (x, y - th - 4), (x + tw, y + 4), (0, 0, 0), -1)
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
