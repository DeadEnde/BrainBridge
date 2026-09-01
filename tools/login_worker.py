#!/usr/bin/env python3
"""BrainBridge Multi — hosted-browser login worker.

Runs on a machine with a display (Xvfb) + the notebooklm CLI (bundled Chromium).
Loops:
  1. GET /auth/tickets/next (owner key)          -> oldest pending ticket
  2. tell the gateway the noVNC URL (so the page can show it)
  3. run `notebooklm login --storage /tmp/bb_ticket_<id>.json` on the display
  4. poll `notebooklm auth check --test` on that storage until valid
  5. POST /auth/tickets/{id}/collect {state_b64} -> user gets their API key

Env:
  BRAINBRIDGE_BASE   default https://brain-bridge-six.vercel.app/api
  BRAINBRIDGE_KEY    owner gateway key (required)
  BRAINBRIDGE_VNC_URL optional noVNC base URL (else reads /home/user/tunnel_url.txt)
  DISPLAY            :99
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("BRAINBRIDGE_BASE", "https://brain-bridge-six.vercel.app/api").rstrip("/")
KEY = os.environ.get("BRAINBRIDGE_KEY", "").strip()
DISPLAY = os.environ.get("DISPLAY", ":99")
VNC_PASSWORD = "Brain2026"
H = {"Authorization": f"Bearer {KEY}"}
LOG = Path("/home/user/login_worker.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def b64gzip(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data, 9)).decode()


def vnc_url() -> str:
    url = os.environ.get("BRAINBRIDGE_VNC_URL", "").strip()
    if not url:
        try:
            base_url = Path("/home/user/tunnel_url.txt").read_text().strip()
            if base_url:
                url = base_url.rstrip("/") + "/vnc.html?autoconnect=true&resize=scale"
        except OSError:
            url = ""
    return url


def login_path(tid: str) -> Path:
    return Path("/tmp") / f"bb_ticket_{tid}.json"


def free_display() -> None:
    """Stop any previous notebooklm browser login so ours gets the display."""
    for pat in ("notebooklm login", "chromium.*--remote-debugging"):
        try:
            subprocess.run(["pkill", "-f", pat], timeout=10,
                           capture_output=True, text=True)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(2)


def wait_valid(storage: Path, timeout: int, tick: int = 12) -> bool:
    """Poll `auth check --test` on this exact storage file."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "notebooklm", "--storage", str(storage),
                 "auth", "check", "--test"],
                capture_output=True, text=True, timeout=90, env={**os.environ, "DISPLAY": DISPLAY},
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if "Authentication is valid" in out:
                return True
            if "No valid" in out or "expired" in out.lower():
                time.sleep(tick)
                continue
        except Exception as e:  # noqa: BLE001
            log(f"  check err: {e}")
        time.sleep(tick)
    return False


def handle_ticket(tid: str) -> None:
    storage = login_path(tid)
    log(f"== ticket {tid}: opening hosted browser ==")
    url = vnc_url()
    if url:
        try:
            httpx.post(f"{BASE}/auth/tickets/{tid}/url", headers=H, timeout=30,
                       json={"browser_url": url, "browser_password": VNC_PASSWORD})
        except Exception as e:  # noqa: BLE001
            log(f"  set-url err: {e}")

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "notebooklm", "login", "--browser", "chromium",
             "--storage", str(storage), "--browser-timeout", "1500"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "DISPLAY": DISPLAY},
        )
    except Exception as e:  # noqa: BLE001
        log(f"  spawn err: {e}")
        httpx.post(f"{BASE}/auth/tickets/{tid}/fail", headers=H, timeout=30)
        return

    ok = wait_valid(storage, timeout=1500)
    # give the CLI a moment to flush, then check again
    for _ in range(3):
        if ok:
            break
        time.sleep(5)
        ok = wait_valid(storage, timeout=30)

    if ok and storage.exists():
        raw = storage.read_bytes()
        try:
            httpx.post(f"{BASE}/auth/tickets/{tid}/collect", headers=H, timeout=120,
                       json={"state_b64": b64gzip(raw)})
            log(f"== ticket {tid}: collected ✅ ==")
        except Exception as e:  # noqa: BLE001
            log(f"  collect err: {e}")
            httpx.post(f"{BASE}/auth/tickets/{tid}/fail", headers=H, timeout=30)
    else:
        log(f"== ticket {tid}: FAILED (timeout/no valid session) ==")
        httpx.post(f"{BASE}/auth/tickets/{tid}/fail", headers=H, timeout=30)
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        storage.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> None:
    if not KEY:
        log("BRAINBRIDGE_KEY env missing — exit")
        return
    log(f"worker up | base={BASE} | display={DISPLAY}")
    # ensure a display server
    try:
        subprocess.run(["xdpyinfo"], capture_output=True, timeout=10,
                       env={**os.environ, "DISPLAY": DISPLAY})
    except FileNotFoundError:
        subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1400x900x24"],
                         env=os.environ)
    while True:
        try:
            r = httpx.get(f"{BASE}/auth/tickets/next", headers=H, timeout=60)
            d = r.json()
            tid = d.get("ticket_id")
            if tid:
                handle_ticket(tid)
            else:
                time.sleep(8)
        except Exception as e:  # noqa: BLE001
            log(f"poll err: {e}")
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
