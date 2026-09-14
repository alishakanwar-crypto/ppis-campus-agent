"""One thread at a time inside the native face libraries.

dlib's detector and encoder, and InsightFace's ONNX session, are not safe to
call from two threads at once. The agent has four callers that can be inside
them together — the classwise attendance scan, the teacher sighting scan, the
mood watcher and a face sync — and two of those meeting inside native code
ends the whole process with a Windows access violation (exit code
-1073741819) and not one line in the log, which is exactly what the campus PC
has been doing every minute.

Every native face call in the agent goes through here, so the native work is
serialised while the Python around it stays concurrent.
"""

from __future__ import annotations

import threading

try:
    import face_recognition
except ImportError:  # pragma: no cover - the agent logs this at startup
    face_recognition = None

# One lock for dlib and InsightFace together: they share the same process and
# a crash in either takes the agent down the same way.
NATIVE_LOCK = threading.RLock()


def face_locations(*args, **kwargs):
    with NATIVE_LOCK:
        return face_recognition.face_locations(*args, **kwargs)


def face_encodings(*args, **kwargs):
    with NATIVE_LOCK:
        return face_recognition.face_encodings(*args, **kwargs)


def load_image_file(*args, **kwargs):
    with NATIVE_LOCK:
        return face_recognition.load_image_file(*args, **kwargs)


def insight_get(app, image):
    """Run an InsightFace analysis under the same lock as dlib."""
    with NATIVE_LOCK:
        return app.get(image)
