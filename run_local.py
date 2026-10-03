from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
API_PORT = os.getenv("VENDOR_RISK_PORT", "8001")
UI_PORT = os.getenv("UI_PORT", "8501")
os.environ.setdefault("VENDOR_RISK_BASE_URL", f"http://127.0.0.1:{API_PORT}")


def start(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT)


def wait_for_api(url: str, proc: subprocess.Popen, timeout_seconds: float = 10.0) -> None:
    """Wait until the mock API is reachable or fail with a useful message."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Vendor-risk API exited during startup with code {proc.returncode}. "
                "Check the terminal output above (a port conflict is a common cause)."
            )
        try:
            response = requests.get(url, timeout=0.5)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Vendor-risk API did not become ready within {timeout_seconds:.0f}s: {url}")


def _handle_termination(signum: int, frame: object) -> None:
    """Route SIGTERM through normal cleanup (useful for IDE/terminal stop actions)."""
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_termination)
    procs: list[subprocess.Popen] = []
    try:
        print(f"Starting vendor-risk API on http://127.0.0.1:{API_PORT} ...")
        api_proc = start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mock_api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                API_PORT,
            ]
        )
        procs.append(api_proc)
        wait_for_api(f"http://127.0.0.1:{API_PORT}/health", api_proc)
        print("Vendor-risk API is ready.")

        try:
            __import__("streamlit")
        except ImportError:
            print("Streamlit is not installed. Run: pip install -r requirements.txt")
            print("The mock API is still running. Press Ctrl+C to stop.")
        else:
            print(f"Starting UI on http://127.0.0.1:{UI_PORT} ...")
            procs.append(
                start(
                    [
                        sys.executable,
                        "-m",
                        "streamlit",
                        "run",
                        "app.py",
                        "--server.port",
                        UI_PORT,
                    ]
                )
            )

        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"A local process exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping local services ...")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
