"""Diagnostic script to find exactly where main.py startup fails."""
import sys
import os
import traceback

# Force unbuffered output
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
os.environ["PYTHONUNBUFFERED"] = "1"

# Intercept sys.exit
_real_exit = sys.exit
def _catch_exit(code=0):
    print(f"\n*** sys.exit({code}) CALLED ***")
    traceback.print_stack()
    _real_exit(code)
sys.exit = _catch_exit

# Intercept os._exit
_real_os_exit = os._exit
def _catch_os_exit(code=0):
    print(f"\n*** os._exit({code}) CALLED ***")
    traceback.print_stack()
    sys.stdout.flush()
    _real_os_exit(code)
os._exit = _catch_os_exit

# Clear all __pycache__ directories
import shutil
for root, dirs, files in os.walk(os.path.dirname(os.path.abspath(__file__))):
    for d in dirs:
        if d == "__pycache__":
            p = os.path.join(root, d)
            shutil.rmtree(p, ignore_errors=True)
            print(f"  Cleared {p}")

print("=== DIAGNOSTIC: Step-by-step import test ===")
print()

steps = [
    ("numpy",            "import numpy"),
    ("face_recognition", "import face_recognition"),
    ("dlib",             "import dlib"),
    ("cv2",              "import cv2"),
    ("PIL",              "from PIL import Image"),
    ("httpx",            "import httpx"),
    ("websockets",       "import websockets"),
    ("fastapi",          "from fastapi import FastAPI"),
    ("database",         "import database"),
    ("face_db",          "import face_db"),
    ("attendance_engine","from attendance_engine import engine"),
    ("main (import)",    "import main"),
]

for name, stmt in steps:
    try:
        print(f"  [{name}] importing... ", end="", flush=True)
        exec(stmt)
        print("OK", flush=True)
    except SystemExit as e:
        print(f"FAILED (sys.exit({e.code}))", flush=True)
        traceback.print_exc()
        break
    except Exception as e:
        print(f"FAILED ({type(e).__name__}: {e})", flush=True)
        traceback.print_exc()
        # Continue to next import

print()
print("=== All imports done. Starting uvicorn... ===")
print(flush=True)

try:
    import uvicorn
    import main as _main
    port = getattr(_main, 'config', {}).get("local_port", 8897)
    print(f"  Starting on port {port}...")
    uvicorn.run(_main.app, host="0.0.0.0", port=port, log_level="info")
except SystemExit as e:
    print(f"  uvicorn exited with code {e.code}")
except Exception as e:
    print(f"  uvicorn failed: {type(e).__name__}: {e}")
    traceback.print_exc()
