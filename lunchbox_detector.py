"""
Lunch Box Detection Software
=============================
Detects when a student opens a lunch box using camera feed.
Uses YOLOv8 object detection to identify food items, bowls,
and utensils appearing in frame — indicating a lunch box was opened.

Usage:
    python lunchbox_detector.py

Requirements:
    pip install ultralytics opencv-python customtkinter Pillow
"""
import sys
import os

# Disable YOLO online checks to prevent crashes on startup
os.environ["YOLO_AUTOINSTALL"] = "false"
os.environ["YOLO_OFFLINE"] = "true"
os.environ.setdefault("ULTRALYTICS_OFFLINE", "1")

print("[1/4] Loading core modules...")
sys.stdout.flush()

import cv2
import time
import logging
import threading
from datetime import datetime
from collections import deque
from pathlib import Path

print("[2/4] Checking YOLO library...")
sys.stdout.flush()
try:
    import ultralytics
except ImportError:
    print("ERROR: ultralytics not installed. Run: pip install ultralytics")
    input("Press Enter to exit...")
    sys.exit(1)

print("[3/4] YOLO library ready")
sys.stdout.flush()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("lunchbox_detector")

# --------------- Configuration ---------------

# YOLO COCO classes related to food/eating
FOOD_CLASSES = {
    46: "banana",
    47: "apple",
    48: "sandwich",
    49: "orange",
    50: "broccoli",
    51: "carrot",
    52: "hot dog",
    53: "pizza",
    54: "donut",
    55: "cake",
}

UTENSIL_CLASSES = {
    42: "fork",
    43: "knife",
    44: "spoon",
}

CONTAINER_CLASSES = {
    45: "bowl",
    39: "bottle",
    41: "cup",
}

# All classes we monitor
ALL_MONITORED_CLASSES = {**FOOD_CLASSES, **UTENSIL_CLASSES, **CONTAINER_CLASSES}

# Detection settings
CONFIDENCE_THRESHOLD = 0.35
EVENT_COOLDOWN_SECONDS = 30  # Minimum time between alerts for same detection
FRAMES_TO_CONFIRM = 3  # Number of consecutive frames with food to trigger alert
SNAPSHOT_DIR = Path("lunchbox_snapshots")


class LunchBoxDetector:
    """Core detection engine for lunch box opening events."""

    def __init__(self, camera_source=0, model_path="yolov8n.pt"):
        """
        Args:
            camera_source: Camera index (0 for webcam) or RTSP/IP URL
            model_path: Path to YOLO model weights
        """
        self.camera_source = camera_source
        self.model_path = model_path
        self.model = None
        self.cap = None
        self.running = False
        self.detection_thread = None

        # State tracking
        self.food_frame_count = 0
        self.last_alert_time = 0
        self.detection_history = deque(maxlen=100)
        self.current_detections = []
        self.alert_active = False
        self.total_events = 0

        # Callbacks
        self.on_alert = None  # Called when lunch box opening detected
        self.on_frame = None  # Called with each processed frame
        self.on_status = None  # Called with status updates

        # Create snapshot directory
        SNAPSHOT_DIR.mkdir(exist_ok=True)

    def load_model(self):
        """Load YOLOv8 model."""
        logger.info(f"Loading YOLO model: {self.model_path}")
        self.model = ultralytics.YOLO(self.model_path)
        logger.info("Model loaded successfully")

    def start(self):
        """Start the detection loop."""
        if self.running:
            return

        if self.model is None:
            self.load_model()

        self.cap = cv2.VideoCapture(self.camera_source)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera: {self.camera_source}")

        self.running = True
        self.detection_thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.detection_thread.start()
        logger.info(f"Detection started on camera: {self.camera_source}")

        if self.on_status:
            self.on_status("Running — monitoring for lunch box activity")

    def stop(self):
        """Stop the detection loop."""
        self.running = False
        if self.detection_thread:
            self.detection_thread.join(timeout=3)
        if self.cap:
            self.cap.release()
            self.cap = None
        logger.info("Detection stopped")
        if self.on_status:
            self.on_status("Stopped")

    def _detection_loop(self):
        """Main detection loop running in background thread."""
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                logger.warning("Failed to read frame, retrying...")
                time.sleep(0.1)
                continue

            # Run YOLO inference
            results = self.model(frame, verbose=False, conf=CONFIDENCE_THRESHOLD)

            # Parse detections
            food_detected = []
            all_detected = []

            for result in results:
                for box in result.boxes:
                    cls_id = int(box.cls[0])
                    conf = float(box.conf[0])
                    if cls_id in ALL_MONITORED_CLASSES:
                        label = ALL_MONITORED_CLASSES[cls_id]
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        detection = {
                            "class_id": cls_id,
                            "label": label,
                            "confidence": conf,
                            "bbox": (x1, y1, x2, y2),
                            "is_food": cls_id in FOOD_CLASSES,
                            "is_utensil": cls_id in UTENSIL_CLASSES,
                            "is_container": cls_id in CONTAINER_CLASSES,
                        }
                        all_detected.append(detection)
                        if cls_id in FOOD_CLASSES or cls_id in UTENSIL_CLASSES:
                            food_detected.append(detection)

            self.current_detections = all_detected

            # Draw detections on frame
            annotated_frame = self._annotate_frame(frame, all_detected)

            # Check for lunch box opening event
            if food_detected:
                self.food_frame_count += 1
            else:
                self.food_frame_count = max(0, self.food_frame_count - 1)

            # Trigger alert if food detected consistently
            if self.food_frame_count >= FRAMES_TO_CONFIRM:
                self._handle_alert(annotated_frame, food_detected)

            # Send frame to UI
            if self.on_frame:
                self.on_frame(annotated_frame)

            time.sleep(0.03)  # ~30 FPS limit

    def _annotate_frame(self, frame, detections):
        """Draw bounding boxes and labels on frame."""
        annotated = frame.copy()

        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            label = f"{det['label']} {det['confidence']:.0%}"

            # Color based on type
            if det["is_food"]:
                color = (0, 0, 255)  # Red for food
            elif det["is_utensil"]:
                color = (0, 165, 255)  # Orange for utensils
            else:
                color = (255, 200, 0)  # Blue for containers

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, label, (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        # Status overlay
        status = "MONITORING"
        status_color = (0, 255, 0)
        if self.alert_active:
            status = "LUNCH BOX DETECTED!"
            status_color = (0, 0, 255)

        cv2.putText(annotated, status, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, status_color, 2)
        cv2.putText(annotated, f"Events: {self.total_events}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        return annotated

    def _handle_alert(self, frame, food_detected):
        """Handle a lunch box opening detection event."""
        now = time.time()
        if now - self.last_alert_time < EVENT_COOLDOWN_SECONDS:
            return  # Still in cooldown

        self.last_alert_time = now
        self.alert_active = True
        self.total_events += 1

        # Save snapshot
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = SNAPSHOT_DIR / f"lunchbox_event_{timestamp}.jpg"
        cv2.imwrite(str(snapshot_path), frame)

        items = [d["label"] for d in food_detected]
        event = {
            "timestamp": datetime.now().isoformat(),
            "items_detected": items,
            "snapshot": str(snapshot_path),
            "event_number": self.total_events,
        }
        self.detection_history.append(event)

        logger.info(f"ALERT: Lunch box opened! Items: {items}")

        if self.on_alert:
            self.on_alert(event)

        # Reset alert after cooldown
        threading.Timer(5.0, self._clear_alert).start()

    def _clear_alert(self):
        """Clear the alert state."""
        self.alert_active = False


# --------------- Desktop GUI ---------------

def run_gui():
    """Launch the desktop GUI application."""
    try:
        import customtkinter as ctk
        from PIL import Image, ImageTk
    except ImportError:
        print("GUI dependencies missing. Install with:")
        print("  pip install customtkinter Pillow")
        print("\nRunning in headless mode instead...")
        run_headless()
        return

    print("Starting Lunch Box Detector GUI...")
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    class LunchBoxApp(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title("Lunch Box Detector - PPIS")
            self.geometry("1000x700")
            self.minsize(800, 500)
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.detector = LunchBoxDetector()
            self.detector.on_frame = self.update_frame
            self.detector.on_alert = self.handle_alert
            self.detector.on_status = self.update_status

            self._build_ui()
            print("GUI window created successfully")

        def _build_ui(self):
            # Top bar
            top_frame = ctk.CTkFrame(self)
            top_frame.pack(fill="x", padx=10, pady=5)

            ctk.CTkLabel(top_frame, text="Lunch Box Detector",
                         font=ctk.CTkFont(size=20, weight="bold")).pack(side="left", padx=10)

            self.status_label = ctk.CTkLabel(top_frame, text="Ready",
                                             font=ctk.CTkFont(size=14))
            self.status_label.pack(side="right", padx=10)

            # Camera feed
            self.camera_frame = ctk.CTkFrame(self)
            self.camera_frame.pack(fill="both", expand=True, padx=10, pady=5)

            self.camera_label = ctk.CTkLabel(self.camera_frame, text="Camera feed will appear here\n\nClick 'Start' to begin monitoring",
                                             font=ctk.CTkFont(size=16))
            self.camera_label.pack(fill="both", expand=True)

            # Controls
            control_frame = ctk.CTkFrame(self)
            control_frame.pack(fill="x", padx=10, pady=5)

            self.start_btn = ctk.CTkButton(control_frame, text="Start",
                                           command=self.start_detection,
                                           fg_color="green", width=120)
            self.start_btn.pack(side="left", padx=5)

            self.stop_btn = ctk.CTkButton(control_frame, text="Stop",
                                          command=self.stop_detection,
                                          fg_color="red", width=120,
                                          state="disabled")
            self.stop_btn.pack(side="left", padx=5)

            # Camera source input
            ctk.CTkLabel(control_frame, text="Camera:").pack(side="left", padx=(20, 5))
            self.camera_input = ctk.CTkEntry(control_frame, width=200,
                                             placeholder_text="0 (webcam) or RTSP URL")
            self.camera_input.pack(side="left", padx=5)
            self.camera_input.insert(0, "0")

            # Event counter
            self.event_label = ctk.CTkLabel(control_frame, text="Events: 0",
                                            font=ctk.CTkFont(size=14, weight="bold"))
            self.event_label.pack(side="right", padx=10)

            # Alert panel
            self.alert_frame = ctk.CTkFrame(self, fg_color="transparent")
            self.alert_frame.pack(fill="x", padx=10, pady=5)

            self.alert_label = ctk.CTkLabel(self.alert_frame, text="",
                                            font=ctk.CTkFont(size=14))
            self.alert_label.pack(side="left", padx=10)

            # Log area
            log_frame = ctk.CTkFrame(self)
            log_frame.pack(fill="x", padx=10, pady=5)

            ctk.CTkLabel(log_frame, text="Detection Log:",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=5)

            self.log_text = ctk.CTkTextbox(log_frame, height=100)
            self.log_text.pack(fill="x", padx=5, pady=5)

        def start_detection(self):
            camera_src = self.camera_input.get().strip()
            try:
                camera_src = int(camera_src)
            except ValueError:
                pass  # It's a URL string

            self.detector.camera_source = camera_src
            try:
                self.detector.start()
                self.start_btn.configure(state="disabled")
                self.stop_btn.configure(state="normal")
            except Exception as e:
                self.update_status(f"Error: {e}")

        def stop_detection(self):
            self.detector.stop()
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

        def update_frame(self, frame):
            """Update camera feed in GUI (called from detection thread)."""
            try:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = frame_rgb.shape[:2]
                max_w, max_h = 900, 450
                scale = min(max_w / w, max_h / h)
                new_w, new_h = int(w * scale), int(h * scale)
                frame_resized = cv2.resize(frame_rgb, (new_w, new_h))
                img = Image.fromarray(frame_resized)
                self.after(0, lambda: self._update_frame_ui(img))
            except Exception:
                pass

        def _update_frame_ui(self, img):
            """Update frame on main thread."""
            photo = ImageTk.PhotoImage(img)
            self.camera_label.configure(image=photo, text="")
            self.camera_label.image = photo

        def handle_alert(self, event):
            """Handle detection alert (called from detection thread)."""
            self.after(0, lambda: self._handle_alert_ui(event))

        def _handle_alert_ui(self, event):
            """Update alert UI on main thread."""
            items = ", ".join(event["items_detected"])
            timestamp = datetime.now().strftime("%H:%M:%S")
            msg = f"[{timestamp}] LUNCH BOX OPENED - Items: {items}"

            self.alert_frame.configure(fg_color="red")
            self.alert_label.configure(text=f"ALERT: {msg}")
            self.event_label.configure(text=f"Events: {event['event_number']}")

            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")

            self.after(5000, lambda: self.alert_frame.configure(fg_color="transparent"))

        def update_status(self, status):
            """Update status label."""
            self.status_label.configure(text=status)

        def on_close(self):
            self.detector.stop()
            self.destroy()

    try:
        print("Creating application window...")
        app = LunchBoxApp()
        print("Window ready - starting main loop")
        app.mainloop()
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")


def run_headless():
    """Run detection without GUI (for testing or server use)."""
    detector = LunchBoxDetector(camera_source=0)

    def on_alert(event):
        items = ", ".join(event["items_detected"])
        print(f"\n{'='*50}")
        print(f"ALERT: Lunch box opened!")
        print(f"Time: {event['timestamp']}")
        print(f"Items: {items}")
        print(f"Snapshot: {event['snapshot']}")
        print(f"{'='*50}\n")

    detector.on_alert = on_alert

    print("Starting Lunch Box Detector (headless mode)...")
    print("Press Ctrl+C to stop\n")

    detector.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        detector.stop()
        print("\nDetector stopped.")


if __name__ == "__main__":
    _log = open("lunchbox_debug.log", "w")
    try:
        _log.write("Starting...\n"); _log.flush()
        print("[4/4] Launching GUI...")
        sys.stdout.flush()
        _log.write("Calling run_gui...\n"); _log.flush()
        run_gui()
        _log.write("run_gui returned normally\n"); _log.flush()
    except BaseException as e:
        msg = f"FATAL ERROR: {type(e).__name__}: {e}"
        print(f"\n{msg}")
        _log.write(msg + "\n"); _log.flush()
        import traceback
        traceback.print_exc()
        traceback.print_exc(file=_log)
    finally:
        _log.write("Exiting\n"); _log.close()
        input("\nPress Enter to exit...")
