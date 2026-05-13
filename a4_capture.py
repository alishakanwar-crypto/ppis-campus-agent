"""
A4 Sheet Capture Module — Controlled in-school face registration.

This module implements a manual face capture method where students hold
an A4 sheet with their name written in black sketch pen. The system:
  1. Captures multiple frames over a 10-second window
  2. Detects and encodes the student's face
  3. Reads the name from the A4 sheet via OCR
  4. Validates the name against the student database
  5. Stores the face encoding linked to the matched student

This provides a reliable backup registration method that doesn't depend
on parent-submitted photos.
"""

import asyncio
import io
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageFilter
except ImportError:
    Image = None

try:
    import face_recognition
except ImportError:
    face_recognition = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import easyocr
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

try:
    import pytesseract
    _TESSERACT_AVAILABLE = True
except ImportError:
    _TESSERACT_AVAILABLE = False

import database as db
import face_db

logger = logging.getLogger("ppis-agent.a4_capture")

_IST = timezone(timedelta(hours=5, minutes=30))

# OCR reader singleton (lazy init)
_ocr_reader = None

CAPTURE_LOG_DIR = Path(__file__).parent / "a4_capture_logs"
CAPTURE_LOG_DIR.mkdir(exist_ok=True)


def _get_ocr_reader():
    """Lazily initialize EasyOCR reader."""
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    if _EASYOCR_AVAILABLE:
        try:
            _ocr_reader = easyocr.Reader(['en'], gpu=False)
            logger.info("EasyOCR reader initialized")
            return _ocr_reader
        except Exception as e:
            logger.warning(f"EasyOCR init failed: {e}")
    return None


def _ocr_from_image(img_array: np.ndarray) -> str:
    """Extract text from image using available OCR engine.

    Tries EasyOCR first, then Tesseract as fallback.
    Returns extracted text (uppercase, stripped).
    """
    # Try EasyOCR
    reader = _get_ocr_reader()
    if reader is not None:
        try:
            results = reader.readtext(img_array, detail=0)
            text = " ".join(results).strip().upper()
            if text:
                return text
        except Exception as e:
            logger.warning(f"EasyOCR failed: {e}")

    # Fallback to Tesseract
    if _TESSERACT_AVAILABLE:
        try:
            if Image is not None:
                pil_img = Image.fromarray(img_array)
                text = pytesseract.image_to_string(pil_img).strip().upper()
                if text:
                    return text
        except Exception as e:
            logger.warning(f"Tesseract OCR failed: {e}")

    return ""


def _preprocess_for_ocr(img_array: np.ndarray) -> np.ndarray:
    """Preprocess image to improve OCR accuracy on A4 sheet text.

    Applies:
    - Grayscale conversion
    - Adaptive thresholding for black text on white paper
    - Noise reduction
    """
    if cv2 is None:
        return img_array

    # Convert to grayscale
    if len(img_array.shape) == 3:
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    else:
        gray = img_array

    # Adaptive threshold to isolate black text on white paper
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2
    )

    # Denoise
    denoised = cv2.medianBlur(binary, 3)

    return denoised


def _extract_a4_region(img_array: np.ndarray) -> np.ndarray | None:
    """Try to detect and crop the white A4 sheet region from the image.

    Uses contour detection to find the largest white rectangular area.
    Returns cropped region or None if not found.
    """
    if cv2 is None:
        return None

    # Convert to grayscale
    if len(img_array.shape) == 3:
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    else:
        gray = img_array.copy()

    # Threshold to find white regions
    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)

    # Find contours
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Find largest rectangular contour (likely the A4 sheet)
    img_area = img_array.shape[0] * img_array.shape[1]
    best_contour = None
    best_area = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        # A4 sheet should be at least 5% and at most 60% of image
        if area < img_area * 0.05 or area > img_area * 0.6:
            continue
        # Check if roughly rectangular
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
        if len(approx) >= 4 and area > best_area:
            best_area = area
            best_contour = cnt

    if best_contour is None:
        return None

    # Crop the bounding rectangle
    x, y, w, h = cv2.boundingRect(best_contour)
    cropped = img_array[y:y+h, x:x+w]
    return cropped


def _fuzzy_match_name(ocr_name: str, db_students: list[dict]) -> dict | None:
    """Find the best matching student from OCR-extracted name.

    Uses fuzzy string matching with a threshold of 0.7.
    Returns matched student dict or None.
    """
    ocr_name = re.sub(r'[^A-Z\s]', '', ocr_name.upper()).strip()
    if not ocr_name or len(ocr_name) < 3:
        return None

    best_match = None
    best_ratio = 0.0

    for student in db_students:
        student_name = student.get("name", "").upper().strip()
        if not student_name:
            continue

        # Direct substring match
        if ocr_name in student_name or student_name in ocr_name:
            return student

        # Fuzzy match
        ratio = SequenceMatcher(None, ocr_name, student_name).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = student

    if best_ratio >= 0.7:
        return best_match

    return None


def _get_all_students() -> list[dict]:
    """Get all registered students from the face database."""
    faces = db.get_all_face_encodings()
    # Group by person_id to get unique students
    students = {}
    for f in faces:
        pid = f.get("person_id", "")
        if pid and pid not in students:
            students[pid] = {
                "person_id": pid,
                "name": f.get("name", ""),
                "phone": f.get("phone", ""),
            }
    return list(students.values())


def _log_capture(student_name: str, grade: str, status: str, details: str = ""):
    """Log capture attempt with IST timestamp."""
    now_ist = datetime.now(_IST)
    log_entry = (
        f"[{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}] "
        f"Student: {student_name} | Class: {grade} | "
        f"Status: {status}"
    )
    if details:
        log_entry += f" | {details}"
    logger.info(f"[A4_CAPTURE] {log_entry}")

    # Also append to daily log file
    log_file = CAPTURE_LOG_DIR / f"capture_{now_ist.strftime('%Y-%m-%d')}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_entry + "\n")


async def capture_and_register(
    capture_func,
    dvr: dict,
    channel: int,
    camera_label: str,
    grade: str = "",
    num_frames: int = 5,
    capture_interval: float = 2.0,
) -> dict:
    """Perform A4 sheet capture for a single student.

    Captures multiple frames over ~10 seconds, performs face detection
    and OCR, validates against DB, and registers the face if valid.

    Args:
        capture_func: Async function to capture a snapshot (returns bytes)
        dvr: DVR configuration dict
        channel: Camera channel number
        camera_label: Human-readable camera name
        grade: Expected grade/class (for filtering students)
        num_frames: Number of frames to capture (default 5 over 10 sec)
        capture_interval: Seconds between captures (default 2.0)

    Returns:
        dict with result: {success, student_name, status, details}
    """
    now_ist = datetime.now(_IST)
    logger.info(f"[A4_CAPTURE] Starting capture on {camera_label} at "
                f"{now_ist.strftime('%I:%M %p IST')}")

    frames = []
    face_encodings_collected = []
    ocr_texts = []

    # Phase 1: Capture multiple frames
    for i in range(num_frames):
        try:
            img_bytes = await capture_func(dvr, channel)
            if img_bytes:
                frames.append(img_bytes)
                logger.info(f"[A4_CAPTURE] Frame {i+1}/{num_frames} captured "
                           f"({len(img_bytes)} bytes)")
        except Exception as e:
            logger.warning(f"[A4_CAPTURE] Frame {i+1} capture failed: {e}")

        if i < num_frames - 1:
            await asyncio.sleep(capture_interval)

    if not frames:
        _log_capture("UNKNOWN", grade, "Failed", "No frames captured from camera")
        return {"success": False, "status": "Failed",
                "error": "No frames captured from camera"}

    # Phase 2: Process each frame for face detection and OCR
    best_face_encoding = None
    best_face_quality = 0.0
    best_face_image = None
    all_ocr_text = ""

    for frame_bytes in frames:
        try:
            if Image is not None:
                pil_img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
                img_array = np.asarray(pil_img, dtype=np.uint8)
            else:
                continue
        except Exception:
            continue

        # Face detection
        face_locations = face_recognition.face_locations(
            img_array, number_of_times_to_upsample=2, model="hog"
        )

        if len(face_locations) > 1:
            _log_capture("UNKNOWN", grade, "Retry",
                        f"Multiple faces detected ({len(face_locations)}) - rejected")
            continue  # Skip frames with multiple faces

        if len(face_locations) == 1:
            encodings = face_recognition.face_encodings(img_array, face_locations)
            if encodings:
                # Measure face quality by size (larger = better)
                top, right, bottom, left = face_locations[0]
                face_area = (bottom - top) * (right - left)
                if face_area > best_face_quality:
                    best_face_quality = face_area
                    best_face_encoding = encodings[0]
                    best_face_image = frame_bytes
                face_encodings_collected.append(encodings[0])

        # OCR: try to extract name from A4 sheet region
        a4_region = _extract_a4_region(img_array)
        if a4_region is not None:
            processed = _preprocess_for_ocr(a4_region)
            text = _ocr_from_image(processed)
            if text:
                ocr_texts.append(text)
        else:
            # Try OCR on lower half of image (where A4 is likely held)
            h = img_array.shape[0]
            lower_half = img_array[h//3:, :]
            processed = _preprocess_for_ocr(lower_half)
            text = _ocr_from_image(processed)
            if text:
                ocr_texts.append(text)

    # Phase 3: Validate results
    if best_face_encoding is None:
        _log_capture("UNKNOWN", grade, "Failed",
                    "No clear single face detected in any frame")
        return {"success": False, "status": "Failed",
                "error": "No clear single face detected in any frame. "
                         "Ensure student stands alone facing camera."}

    if not ocr_texts:
        _log_capture("UNKNOWN", grade, "Failed",
                    "Could not read name from A4 sheet")
        return {"success": False, "status": "Failed",
                "error": "Could not read name from A4 sheet. "
                         "Ensure name is written in bold black on white paper."}

    # Combine OCR results (most common text wins)
    from collections import Counter
    all_ocr_text = Counter(ocr_texts).most_common(1)[0][0]
    logger.info(f"[A4_CAPTURE] OCR extracted name: '{all_ocr_text}'")

    # Phase 4: Match OCR name to student database
    students = _get_all_students()
    matched_student = _fuzzy_match_name(all_ocr_text, students)

    if matched_student is None:
        _log_capture(all_ocr_text, grade, "Failed",
                    f"Name '{all_ocr_text}' not found in student database")
        return {"success": False, "status": "Failed",
                "error": f"Name '{all_ocr_text}' from A4 sheet does not match "
                         f"any student in database. Flagged for review.",
                "ocr_name": all_ocr_text,
                "needs_review": True}

    person_id = matched_student["person_id"]
    student_name = matched_student["name"]

    # Phase 5: Register the face encoding
    result = face_db.register_face(
        person_id=person_id,
        name=student_name,
        role="Student",
        phone=matched_student.get("phone", ""),
        angle="front",
        image_bytes=best_face_image,
    )

    if result.get("success"):
        _log_capture(student_name, grade, "Success",
                    f"Face registered (quality={best_face_quality:.0f}px², "
                    f"frames_with_face={len(face_encodings_collected)}/{len(frames)})")
        return {
            "success": True,
            "status": "Success",
            "student_name": student_name,
            "person_id": person_id,
            "ocr_name": all_ocr_text,
            "face_quality": best_face_quality,
            "frames_captured": len(frames),
            "frames_with_face": len(face_encodings_collected),
        }
    else:
        _log_capture(student_name, grade, "Failed",
                    f"Face encoding failed: {result.get('error', 'unknown')}")
        return {"success": False, "status": "Failed",
                "error": result.get("error", "Face encoding failed"),
                "student_name": student_name}


async def batch_capture_class(
    capture_func,
    dvr: dict,
    channel: int,
    camera_label: str,
    grade: str,
    student_count: int = 1,
    capture_interval: float = 2.0,
    between_students_wait: float = 5.0,
) -> list[dict]:
    """Capture faces for multiple students in a class sequentially.

    Waits between_students_wait seconds between each student to allow
    the next student to step in front of the camera.

    Args:
        capture_func: Async snapshot capture function
        dvr: DVR configuration
        channel: Camera channel
        camera_label: Camera display name
        grade: Class/section
        student_count: Number of students to capture
        capture_interval: Seconds between frames per student
        between_students_wait: Seconds to wait between students

    Returns:
        List of result dicts for each student
    """
    results = []
    now_ist = datetime.now(_IST)
    logger.info(f"[A4_CAPTURE] Batch capture started for {grade} "
               f"({student_count} students) at {now_ist.strftime('%I:%M %p IST')}")

    for i in range(student_count):
        logger.info(f"[A4_CAPTURE] Waiting for student {i+1}/{student_count}...")
        if i > 0:
            await asyncio.sleep(between_students_wait)

        result = await capture_and_register(
            capture_func=capture_func,
            dvr=dvr,
            channel=channel,
            camera_label=camera_label,
            grade=grade,
            num_frames=5,
            capture_interval=capture_interval,
        )
        results.append(result)

        status = result.get("status", "Unknown")
        name = result.get("student_name", result.get("ocr_name", "UNKNOWN"))
        logger.info(f"[A4_CAPTURE] Student {i+1}: {name} — {status}")

    # Summary
    success_count = sum(1 for r in results if r.get("success"))
    logger.info(f"[A4_CAPTURE] Batch complete: {success_count}/{student_count} successful")

    return results


def get_capture_logs(date_str: str = "") -> list[str]:
    """Get capture logs for a given date (default: today).

    Returns list of log lines.
    """
    if not date_str:
        date_str = datetime.now(_IST).strftime("%Y-%m-%d")

    log_file = CAPTURE_LOG_DIR / f"capture_{date_str}.log"
    if not log_file.exists():
        return []

    with open(log_file, "r", encoding="utf-8") as f:
        return f.readlines()
