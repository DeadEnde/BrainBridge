#!/usr/bin/env python3
"""BrainBridge — Token Factory (worker side).

One process, three jobs — keeps EVERY session's Google tokens alive:

 1) GUARDIAN (network only, thread)
    Every GUARDIAN_INTERVAL minutes it asks the gateway to rotate tokens:
      - owner:  POST /auth/refresh
      - users:  POST /auth/refresh-all  (batched until next_offset is None)
    Google replies with fresh __Secure-1PSIDTS for each session, so users
    never have to re-login (Vercel cron is a backup for this).

 2) POPUP WORKER (display, main loop, high priority)
    Polls POST /auth/tickets/next. Opens `notebooklm login` in the hosted
    browser (Xvfb + noVNC tunnel). The user authorizes in the popup, the CLI
    saves the fresh session, the worker uploads it -> user gets an API key.

 3) MINT DAEMON (display, main loop)
    Polls GET /auth/pending/next. A user pasted a Cookie-Editor export that
    lacks __Secure-1PSIDTS (HttpOnly after Chrome's App-Bound Encryption).
    The worker loads SID etc. into Chromium, visits accounts.google.com +
    notebook.google.com — Google MINT a fresh 1PSIDTS — captures the state
    and uploads it -> user gets an API key. (This is how `notebooklm auth
    refresh`-less paste import succeeds.)

Env:
  BRAINBRIDGE_BASE        default https://brain-bridge-six.vercel.app/api
  BRAINBRIDGE_KEY         owner gateway key (required)
  GUARDIAN_INTERVAL       minutes between token rotations (default 20)
  DISPLAY                 :99
  BRAINBRIDGE_VNC_URL     optional (else /home/user/tunnel_url.txt)
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("BRAINBRIDGE_BASE", "https://brain-bridge-six.vercel.app/api").rstrip("/")
KEY = os.environ.get("BRAINBRIDGE_KEY", "").strip()
DISPLAY = os.environ.get("DISPLAY", ":99")
INTERVAL = int(os.environ.get("GUARDIAN_INTERVAL", "20"))  # minutes
VNC_PASSWORD = "Brain2026"
LOG = Path("/home/user/token_factory.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def h(extra: dict | None = None) -> dict:
    d = {"Authorization": f"Bearer {KEY}"}
    if extra:
        d.update(extra)
    return d


def post(path: str, json_body: dict | None = None, timeout: int = 180):
    r = httpx.post(f"{BASE}{path}", headers=h(), json=json_body, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def get(path: str, timeout: int = 60):
    r = httpx.get(f"{BASE}{path}", headers=h(), timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


# --------------------------------------------------------------------------- 1) GUARDIAN
def guardian_loop() -> None:
    if not KEY:
        log("guardian: no BRAINBRIDGE_KEY — abort")
        return
    log(f"guardian: up (interval={INTERVAL}min, base={BASE})")
    while True:
        try:
            # owner
            code, d = post("/auth/refresh", timeout=240)
            log(f"guardian: owner refresh -> {code} ok={d.get('ok') if isinstance(d, dict) else '?'}")
            # users, batched
            offset = 0
            while True:
                code, d = post(f"/auth/refresh-all?limit=20&offset={offset}", timeout=300)
                if code != 200:
                    log(f"guardian: refresh-all err {code} {str(d)[:120]}")
                    break
                log(f"guardian: refresh-all ok={d.get('ok')} fail={d.get('fail')} "
                    f"total={d.get('total')} next={d.get('next_offset')}")
                nxt = d.get("next_offset")
                if nxt is None:
                    break
                offset = nxt
        except Exception as e:  # noqa: BLE001
            log(f"guardian: cycle err: {e}")
        time.sleep(INTERVAL * 60)


# --------------------------------------------------------------------------- helpers (display)
def vnc_url() -> str:
    url = os.environ.get("BRAINBRIDGE_VNC_URL", "").strip()
    if not url:
        try:
            base_url = Path("/home/user/tunnel_url.txt").read_text().strip()
            if base_url:
                url = base_url.rstrip("/") + "/vnc.html?autoconnect=true&resize=scale"
        except OSError:
            pass
    return url


def ensure_display() -> None:
    if not Path("/proc").exists():
        return
    try:
        subprocess.run(["xdpyinfo"], capture_output=True, timeout=10,
                       env={**os.environ, "DISPLAY": DISPLAY})
    except FileNotFoundError:
        subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1400x900x24"], env=os.environ)
    # VNC + websockify + tunnel are supervised by supervise.sh


def update_ticket_url(tid: str) -> None:
    url = vnc_url()
    if url:
        code, _ = post(f"/auth/tickets/{tid}/url",
                       {"browser_url": url, "browser_password": VNC_PASSWORD}, timeout=60)
        log(f"popup: set ticket {tid} url -> {code}")


# --------------------------------------------------------------------------- 2) POPUP WORKER
def ticket_dir() -> Path:
    d = Path.home() / ".notebooklm" / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def handle_popup(tid: str) -> None:
    storage = ticket_dir() / f"bb_ticket_{tid}.json"
    try:
        storage.unlink(missing_ok=True)
    except OSError:
        pass
    log(f"popup: ticket {tid} — opening browser on {DISPLAY} (user authorizes / 25 min)")
    update_ticket_url(tid)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "notebooklm", "login", "--browser", "chromium",
             "--storage", str(storage), "--browser-timeout", "1500"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "DISPLAY": DISPLAY},
        )
    except Exception as e:  # noqa: BLE001
        log(f"popup: spawn err {e}")
        post(f"/auth/tickets/{tid}/fail")
        return

    deadline = time.time() + 1500
    ok = False
    while time.time() < deadline:
        time.sleep(12)
        if proc.poll() is not None and not (storage.exists() and storage.stat().st_size > 200):
            log(f"popup: login process exited early (rc={proc.returncode}) — aborting ticket {tid}")
            post(f"/auth/tickets/{tid}/fail")
            return
        try:
            if storage.exists() and storage.stat().st_size > 200:
                r = subprocess.run([sys.executable, "-m", "notebooklm", "--storage",
                                    str(storage), "auth", "check", "--test"],
                                   capture_output=True, text=True, timeout=90,
                                   env={**os.environ, "DISPLAY": DISPLAY})
                if "Authentication is valid" in ((r.stdout or "") + (r.stderr or "")):
                    ok = True
                    break
        except Exception:  # noqa: BLE001
            pass
    if ok and storage.exists():
        raw = storage.read_bytes()
        code, d = post(f"/auth/tickets/{tid}/collect",
                       {"state_b64": base64.b64encode(gzip.compress(raw, 9)).decode()},
                       timeout=240)
        log(f"popup: ticket {tid} collected -> {code} {str(d)[:160]}")
    else:
        log(f"popup: ticket {tid} FAILED (timeout)")
        post(f"/auth/tickets/{tid}/fail")
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    try:
        storage.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- 3) MINT DAEMON
def build_state(cookies: list[dict]) -> dict:
    """Cookie-Editor export -> Playwright storage_state (sameSite normalized)."""
    out = []
    for c in cookies:
        ss = c.get("sameSite") or "Lax"
        ss = {"lax": "Lax", "strict": "Strict", "no_restriction": "None",
              "none": "None"}.get(ss, "Lax")
        exp = c.get("expirationDate")
        out.append({
            "name": c["name"], "value": c["value"],
            "domain": c["domain"], "path": c.get("path") or "/",
            "expires": float(exp) if exp else -1,
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": ss,
        })
    return {"cookies": out}


def mint_cookies(cookies: list[dict]) -> tuple[bool, str]:
    """Load SID(-family) into Chromium, visit Google, capture fresh 1PSIDTS."""
    from playwright.sync_api import sync_playwright
    state = build_state(cookies)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state=state)
        page = ctx.new_page()
        page.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        page.goto("https://notebook.google.com", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        page.goto("https://notebook.google.com", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        title = page.title()
        capped = ctx.storage_state()
        b.close()
        names = [c["name"] for c in capped["cookies"]]
        if "Sign in" in title or "__Secure-1PSIDTS" not in names:
            return False, f"title={title[:60]} 1PSIDTS={'__Secure-1PSIDTS' in names}"
        return True, json.dumps(capped)


async def handle_mint(pid: str, label: str, cookies: list[dict]) -> None:
    log(f"mint: {pid} — loading {len(cookies)} cookies into Chromium...")
    try:
        ok, payload = mint_cookies(cookies)
    except Exception as e:  # noqa: BLE001
        log(f"mint: {pid} ERROR {e}")
        return
    if not ok:
        log(f"mint: {pid} REJECTED by Google ({payload})")
        post(f"/auth/pending/{pid}/failed", timeout=60)
        return
    raw = payload.encode()
    code, d = post(f"/auth/pending/{pid}/mint",
                   {"state_b64": base64.b64encode(gzip.compress(raw, 9)).decode(),
                    "label": label}, timeout=240)
    log(f"mint: {pid} -> {code} {str(d)[:160]}")


# --------------------------------------------------------------------------- main
def main() -> None:
    if not KEY:
        log("BRAINBRIDGE_KEY missing — exit")
        return
    import threading
    threading.Thread(target=guardian_loop, daemon=True).start()
    ensure_display()
    log(f"token factory up | jobs: guardian({INTERVAL}min) + popup + mint")
    while True:
        # 1) popup tickets first (users waiting in the VNC browser)
        try:
            code, d = get("/auth/tickets/next")
            if code == 200 and d.get("ticket_id"):
                handle_popup(d["ticket_id"])
                continue
        except Exception as e:  # noqa: BLE001
            log(f"tickets poll err: {e}")
        # 2) cookie mint queue
        try:
            code, d = get("/auth/pending/next")
            if code == 200 and d.get("pending_id"):
                if isinstance(d, dict) and d.get("cookies"):
                    handle_mint(d["pending_id"], d.get("label", ""), d["cookies"])
                continue
        except Exception as e:  # noqa: BLE001
            log(f"pending poll err: {e}")
        time.sleep(8)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
