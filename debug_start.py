"""Diagnostic wrapper — prints each import step to find the crash."""
import sys
print(f"Python {sys.version}", flush=True)

print("1. importing stdlib...", flush=True)
import asyncio, json, logging, os, time
from pathlib import Path

print("2. importing httpx...", flush=True)
import httpx

print("3. importing websockets...", flush=True)
import websockets

print("4. importing fastapi...", flush=True)
from fastapi import FastAPI

print("5. importing attendance_engine...", flush=True)
from attendance_engine import engine as attendance_engine

print("6. importing face_db...", flush=True)
import face_db

print("7. importing main module...", flush=True)
try:
    import main
    print("8. import main SUCCEEDED", flush=True)
except SystemExit as e:
    print(f"8. CAUGHT SystemExit: code={e.code}", flush=True)
    import traceback
    traceback.print_exc()
except BaseException as e:
    print(f"8. CAUGHT {type(e).__name__}: {e}", flush=True)
    import traceback
    traceback.print_exc()

print("9. checking main.app...", flush=True)
if hasattr(main, 'app'):
    print("10. starting uvicorn server...", flush=True)
    import uvicorn
    uvicorn.run(main.app, host="0.0.0.0", port=8897, log_level="info")
else:
    print("ERROR: main.app not found", flush=True)
