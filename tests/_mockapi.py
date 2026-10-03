"""Start the mock vendor API for tests that need it (no-op if one is already listening)."""
import atexit
import os
import subprocess
import sys
import time

import requests

_proc = None
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure():
    global _proc
    base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001")
    try:
        if requests.get(base + "/health", timeout=1).ok:
            return
    except requests.RequestException:
        pass
    _proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--port", "8001", "--log-level", "error"], cwd=ROOT)
    for _ in range(40):
        try:
            if requests.get("http://127.0.0.1:8001/health", timeout=0.5).ok:
                return
        except requests.RequestException:
            time.sleep(0.25)


def stop():
    """Kept for symmetry; the server is shared by all test modules and stopped at interpreter exit."""


@atexit.register
def _shutdown():
    if _proc:
        _proc.terminate()
