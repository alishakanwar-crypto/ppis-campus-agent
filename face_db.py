"""
Face registration and encoding management.

Handles capturing face images, computing 128-d face encodings,
and persisting them to the SQLite database for later recognition.
"""

import io
import logging
import time
from pathlib import Path

import numpy as np

try:
    import face_recognition
except ImportError:
    face_recognition = None

try:
    from PIL import Image
except ImportError:
    Image = None

import database as db

logger = logging.getLogger("ppis-agent.face_db")

FACE_IMAGES_DIR = Path(__file__).parent / "face_images"
FACE_IMAGES_DIR.mkdir(exist_ok=True)


def encode_face_from_image(image_bytes: bytes) -> tuple[np.ndarray, bytes] | None:
    """Detect a single face in an image and return its 128-d encoding.

    Returns (encoding_array, jpeg_cropped_face) or None if no face found.
    """
    if face_recognition is None:
        logger.error("face_recognition library not installed")
        return None

    # Use PIL to force RGB conversion, resize large images, and ensure
    # C-contiguous uint8 array (some Windows dlib builds reject large images)
    if Image is not None:
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        max_dim = 800
        if max(pil_img.size) > max_dim:
            pil_img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        img_array = np.ascontiguousarray(np.array(pil_img, dtype=np.uint8))
    else:
        img_array = face_recognition.load_image_file(io.BytesIO(image_bytes))
    logger.info(f"Image ready: shape={img_array.shape}, dtype={img_array.dtype}")
    try:
        face_locations = face_recognition.face_locations(img_array, model="hog")
    except Exception as e:
        logger.error(f"face_locations failed: {e}")
        raise
    logger.info(f"face_locations found: {len(face_locations)}")

    if not face_locations:
        logger.warning("No face detected in image")
        return None

    if len(face_locations) > 1:
        logger.info(f"Multiple faces detected ({len(face_locations)}), using largest")
        face_locations = sorted(
            face_locations,
            key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]),
            reverse=True,
        )

    encodings = face_recognition.face_encodings(img_array, [face_locations[0]])
    if not encodings:
        logger.warning("Could not compute face encoding")
        return None

    return encodings[0], _crop_face_jpeg(img_array, face_locations[0])


def _crop_face_jpeg(img_array: np.ndarray, location: tuple) -> bytes:
    """Crop a face region from an image and return as JPEG bytes."""
    top, right, bottom, left = location
    # Add some padding
    h, w = img_array.shape[:2]
    pad = int((bottom - top) * 0.3)
    top = max(0, top - pad)
    bottom = min(h, bottom + pad)
    left = max(0, left - pad)
    right = min(w, right + pad)

    face_img = img_array[top:bottom, left:right]
    if Image is not None:
        pil_img = Image.fromarray(face_img)
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    # Fallback: return empty bytes (shouldn't happen in practice)
    return b""


def register_face(person_id: str, name: str, role: str, phone: str,
                  angle: str, image_bytes: bytes) -> dict:
    """Register a face from an uploaded image.

    Args:
        person_id: Unique ID (e.g. "TEST001")
        name: Display name
        role: Role description
        phone: WhatsApp phone number for notifications
        angle: Capture angle ("front", "left", "right")
        image_bytes: Raw JPEG/PNG image bytes

    Returns:
        dict with registration result
    """
    result = encode_face_from_image(image_bytes)
    if result is None:
        return {"success": False, "error": "No face detected in image"}

    encoding, cropped_face = result

    # Save cropped face image to disk
    ts = int(time.time())
    filename = f"{person_id}_{angle}_{ts}.jpg"
    filepath = FACE_IMAGES_DIR / filename
    with open(filepath, "wb") as f:
        f.write(cropped_face)

    # Serialize encoding to bytes for SQLite storage
    encoding_bytes = encoding.tobytes()

    face_id = db.save_face_encoding(
        person_id=person_id,
        name=name,
        role=role,
        phone=phone,
        angle=angle,
        encoding_bytes=encoding_bytes,
        image_path=str(filepath),
    )

    logger.info(f"Registered face: {name} ({person_id}), angle={angle}, id={face_id}")
    return {
        "success": True,
        "face_id": face_id,
        "person_id": person_id,
        "angle": angle,
        "image_file": filename,
    }


def load_known_faces() -> dict:
    """Load all registered face encodings from the database.

    Returns dict mapping person_id -> {name, role, phone, encodings: [np.array, ...]}.
    """
    rows = db.get_all_face_encodings()
    persons: dict = {}

    for row in rows:
        pid = row["person_id"]
        encoding = np.frombuffer(row["encoding"], dtype=np.float64)

        if pid not in persons:
            persons[pid] = {
                "name": row["name"],
                "role": row["role"],
                "phone": row["phone"],
                "encodings": [],
            }
        persons[pid]["encodings"].append(encoding)

    logger.info(f"Loaded {len(persons)} registered person(s) with "
                f"{sum(len(p['encodings']) for p in persons.values())} face encoding(s)")
    return persons


def get_registered_list() -> list[dict]:
    """Return list of registered persons (without raw encodings)."""
    return db.get_registered_persons()


def delete_person(person_id: str) -> int:
    """Delete all face encodings for a person."""
    return db.delete_person_faces(person_id)
