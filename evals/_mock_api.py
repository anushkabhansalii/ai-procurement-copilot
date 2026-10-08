"""Start the mock vendor-risk API if it is not already running, so the eval scripts work on their own.

Without it every vendor lookup returns "unavailable" and the cases are scored against an outage.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def ensure() -> None:
    port = os.getenv("VENDOR_RISK_PORT", "8001")
    base = os.getenv("VENDOR_RISK_BASE_URL", f"http://127.0.0.1:{port}").rstrip("/")
    try:
        if requests.get(base + "/health", timeout=1).ok:
            return
    except requests.RequestException:
        pass
    print(f"Mock vendor API not reachable at {base}; starting it for this run.")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--port", port, "--log-level", "error"], cwd=ROOT)
    atexit.register(proc.terminate)
    for _ in range(40):
        try:
            if requests.get(base + "/health", timeout=0.5).ok:
                return
        except requests.RequestException:
            time.sleep(0.25)
    raise SystemExit(f"Mock vendor API did not start at {base}")
