"""
Face registration and encoding management.

Handles capturing face images, computing face encodings,
and persisting them to the SQLite database for later recognition.

Supports two encoding backends:
  - face_recognition (128-d dlib embeddings)
  - InsightFace/ArcFace (512-d normalized embeddings, more accurate)
"""

import io
import logging
import tempfile
import time
from pathlib import Path

import numpy as np

try:
    import dlib
except ImportError:
    dlib = None

try:
    import face_recognition
except ImportError:
    face_recognition = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from insightface.app import FaceAnalysis
    _INSIGHTFACE_AVAILABLE = True
except ImportError:
    FaceAnalysis = None
    _INSIGHTFACE_AVAILABLE = False

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

    # Load image via PIL -> numpy (avoids dlib numpy ABI issues on Windows)
    try:
        if Image is not None:
            pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            max_dim = 480
            if max(pil_img.size) > max_dim:
                pil_img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            img_array = np.asarray(pil_img, dtype=np.uint8)
        else:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                    f.write(image_bytes)
                    tmp_path = f.name
                img_array = face_recognition.load_image_file(tmp_path)
            finally:
                if tmp_path:
                    Path(tmp_path).unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed to load image: {e}")
        return None
    logger.info(f"Image ready: shape={img_array.shape}, dtype={img_array.dtype}")
    face_locations = face_recognition.face_locations(img_array, model="hog")
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


def _get_insightface_app():
    """Lazily initialize and return a shared InsightFace FaceAnalysis instance."""
    if not _INSIGHTFACE_AVAILABLE:
        return None
    if not hasattr(_get_insightface_app, "_app"):
        try:
            app = FaceAnalysis(name="buffalo_l",
                               providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            _get_insightface_app._app = app
            logger.info("InsightFace engine initialized for registration (buffalo_l)")
        except Exception as e:
            logger.warning(f"InsightFace init failed in face_db: {e}")
            _get_insightface_app._app = None
    return _get_insightface_app._app


def encode_face_insightface(image_bytes: bytes) -> tuple[np.ndarray, bytes] | None:
    """Detect a face using InsightFace and return its 512-d ArcFace embedding.

    Returns (embedding_array, jpeg_cropped_face) or None if no face found.
    """
    app = _get_insightface_app()
    if app is None:
        return None

    try:
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_array = np.asarray(pil_img, dtype=np.uint8)
        if img_array.ndim != 3 or img_array.shape[2] != 3:
            logger.error(f"Bad image shape for InsightFace: {img_array.shape}")
            return None
        img_bgr = img_array[:, :, ::-1].copy()
    except Exception as e:
        logger.error(f"Failed to load image for InsightFace: {e}")
        return None

    faces = app.get(img_bgr)
    if not faces:
        logger.warning("InsightFace: No face detected in image")
        return None

    if len(faces) > 1:
        logger.info(f"InsightFace: Multiple faces ({len(faces)}), using largest")
        faces = sorted(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) *
                        (f.bbox[3] - f.bbox[1]), reverse=True)

    face = faces[0]
    embedding = face.normed_embedding  # 512-d normalized

    # Crop face for storage
    bbox = face.bbox.astype(int)
    x1, y1, x2, y2 = bbox
    h, w = img_array.shape[:2]
    pad_x = int((x2 - x1) * 0.3)
    pad_y = int((y2 - y1) * 0.3)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)
    face_crop = img_array[y1:y2, x1:x2]
    if Image is not None:
        pil_crop = Image.fromarray(face_crop)
        buf = io.BytesIO()
        pil_crop.save(buf, format="JPEG", quality=85)
        cropped_bytes = buf.getvalue()
    else:
        cropped_bytes = b""

    return embedding, cropped_bytes


def register_face(person_id: str, name: str, role: str, phone: str,
                  angle: str, image_bytes: bytes) -> dict:
    """Register a face from an uploaded image.

    Tries InsightFace first (512-d ArcFace), falls back to face_recognition (128-d).
    Both encodings are saved when InsightFace is available.

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
    # Always try legacy encoding (for backward compatibility)
    legacy_result = encode_face_from_image(image_bytes)
    if legacy_result is None and not _INSIGHTFACE_AVAILABLE:
        return {"success": False, "error": "No face detected in image"}

    # Save cropped face image to disk
    ts = int(time.time())
    filename = f"{person_id}_{angle}_{ts}.jpg"
    filepath = FACE_IMAGES_DIR / filename

    face_ids = []

    # Save legacy 128-d encoding
    if legacy_result is not None:
        encoding, cropped_face = legacy_result
        with open(filepath, "wb") as f:
            f.write(cropped_face)
        encoding_bytes = encoding.tobytes()
        fid = db.save_face_encoding(
            person_id=person_id, name=name, role=role, phone=phone,
            angle=angle, encoding_bytes=encoding_bytes,
            image_path=str(filepath),
            encoding_type="face_recognition_128d",
        )
        face_ids.append(fid)
        logger.info(f"Registered legacy 128-d face: {name} ({person_id}), id={fid}")

    # Also save InsightFace 512-d encoding
    insight_result = encode_face_insightface(image_bytes)
    if insight_result is not None:
        embedding_if, cropped_if = insight_result
        if not legacy_result:
            # Save cropped face from InsightFace if legacy didn't produce one
            with open(filepath, "wb") as f:
                f.write(cropped_if)
        encoding_bytes_if = embedding_if.astype(np.float32).tobytes()
        fid_if = db.save_face_encoding(
            person_id=person_id, name=name, role=role, phone=phone,
            angle=angle, encoding_bytes=encoding_bytes_if,
            image_path=str(filepath),
            encoding_type="insightface_512d",
        )
        face_ids.append(fid_if)
        logger.info(f"Registered InsightFace 512-d face: {name} ({person_id}), id={fid_if}")

    if not face_ids:
        return {"success": False, "error": "No face detected in image"}

    return {
        "success": True,
        "face_id": face_ids[0],
        "person_id": person_id,
        "angle": angle,
        "image_file": filename,
        "encodings_saved": len(face_ids),
    }


def load_known_faces(encoding_type: str = "face_recognition_128d") -> dict:
    """Load registered face encodings from the database.

    Args:
        encoding_type: Which encodings to load.
            'face_recognition_128d' for legacy 128-d (default)
            'insightface_512d' for InsightFace ArcFace embeddings

    Returns dict mapping person_id -> {name, role, phone, encodings: [np.array, ...]}.
    """
    rows = db.get_all_face_encodings(encoding_type=encoding_type)
    persons: dict = {}

    dtype = np.float32 if encoding_type == "insightface_512d" else np.float64

    for row in rows:
        pid = row["person_id"]
        encoding = np.frombuffer(row["encoding"], dtype=dtype)

        if pid not in persons:
            persons[pid] = {
                "name": row["name"],
                "role": row["role"],
                "phone": row["phone"],
                "encodings": [],
            }
        else:
            # Keep the phone field with the most numbers (later registrations
            # may include both parents while the first only had one)
            existing = persons[pid]["phone"] or ""
            incoming = row["phone"] or ""
            if incoming.count(",") > existing.count(","):
                persons[pid]["phone"] = incoming
        persons[pid]["encodings"].append(encoding)

    logger.info(f"Loaded {len(persons)} person(s) with "
                f"{sum(len(p['encodings']) for p in persons.values())} "
                f"{encoding_type} encoding(s)")
    return persons


def migrate_to_insightface() -> dict:
    """Re-encode all existing faces with InsightFace from saved images.

    Scans all registered faces that have image_path on disk,
    computes InsightFace 512-d embeddings, and saves them alongside
    the existing 128-d encodings.

    Returns summary dict.
    """
    if not _INSIGHTFACE_AVAILABLE:
        return {"success": False, "error": "InsightFace not installed"}

    rows = db.get_all_face_encodings(encoding_type="face_recognition_128d")
    existing_if = db.get_all_face_encodings(encoding_type="insightface_512d")

    # Build set of (person_id, angle) pairs that already have InsightFace encodings
    already_done = set()
    for r in existing_if:
        already_done.add((r["person_id"], r["angle"]))

    migrated = 0
    skipped = 0
    failed = 0

    for row in rows:
        pid = row["person_id"]
        angle = row["angle"]

        if (pid, angle) in already_done:
            skipped += 1
            continue

        image_path = row.get("image_path", "")
        if not image_path or not Path(image_path).exists():
            failed += 1
            logger.warning(f"Migration skip {pid}/{angle}: image not found at {image_path}")
            continue

        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            result = encode_face_insightface(image_bytes)
            if result is None:
                failed += 1
                logger.warning(f"Migration skip {pid}/{angle}: InsightFace found no face")
                continue

            embedding, _ = result
            encoding_bytes = embedding.astype(np.float32).tobytes()
            db.save_face_encoding(
                person_id=pid, name=row["name"], role=row["role"],
                phone=row["phone"], angle=angle,
                encoding_bytes=encoding_bytes, image_path=image_path,
                encoding_type="insightface_512d",
            )
            migrated += 1
            logger.info(f"Migrated {pid}/{angle} to InsightFace")
        except Exception as e:
            failed += 1
            logger.error(f"Migration error {pid}/{angle}: {e}")

    summary = {
        "success": True,
        "migrated": migrated,
        "skipped": skipped,
        "failed": failed,
        "total_legacy": len(rows),
    }
    logger.info(f"InsightFace migration complete: {summary}")
    return summary


def get_registered_list() -> list[dict]:
    """Return list of registered persons (without raw encodings)."""
    return db.get_registered_persons()


def delete_person(person_id: str) -> int:
    """Delete all face encodings for a person."""
    return db.delete_person_faces(person_id)
