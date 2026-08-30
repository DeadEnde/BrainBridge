"""BrainBridge — login flow: popup browser → Google auth → auto-saved session.

End-user flow (no cookie copy-paste, ever):
    user runs the MCP server → first tool call finds no valid session →
    a browser window pops up (Google sign-in) → user logs in / agrees →
    Playwright saves storage_state.json automatically → all tools work.

Alternative (silent): read cookies from an installed browser
(chrome / firefox / brave / edge / safari / arc) via --browser-cookies.
"""

from __future__ import annotations

import os
import subprocess

LOGIN_MODE_ENV = "BRAINBRIDGE_LOGIN_MODE"  # "popup" (default) | "browser" | "manual"


def auth_check(test: bool = False, timeout: int = 120) -> tuple[bool, str]:
    """Run `notebooklm auth check [--test]`. Returns (is_valid, detail)."""
    args = ["auth", "check"]
    if test:
        args.append("--test")
    try:
        proc = subprocess.run(
            ["notebooklm", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "auth check timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    return ("Authentication is valid" in out), out[:600]


def login_via_popup(browser: str | None = None, wait_minutes: int = 6) -> dict:
    """Launch a browser popup for Google sign-in; session is saved automatically.

    Args:
        browser: 'chrome' (system Chrome — best UX, reuses existing Google SSO),
                 'msedge', or None → bundled Chromium.
        wait_minutes: max wait for the human to finish signing in (default 6).
    """
    args = ["login", "--browser-timeout", str(wait_minutes * 60)]
    if browser:
        args += ["--browser", browser]
    try:
        proc = subprocess.run(
            ["notebooklm", *args],
            capture_output=True,
            text=True,
            timeout=(wait_minutes + 2) * 60,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "login timed out before sign-in completed"}

    ok = proc.returncode == 0
    if ok:
        valid, _ = auth_check(test=True)
        ok = valid
    return {"ok": ok, "detail": out[:600]}


def login_via_browser_cookies(browser: str = "chrome") -> dict:
    """Silent: read Google cookies from an installed browser (no popup).

    Best on the user's own machine where they are already signed in.
    """
    valid, _ = auth_check(test=True)
    if valid:
        return {"ok": True, "detail": "session already valid — nothing to import"}
    try:
        proc = subprocess.run(
            ["notebooklm", "auth", "refresh", "--browser-cookies", browser],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "browser cookies refresh timed out"}
    valid, detail = auth_check(test=True)
    if valid:
        return {"ok": True, "detail": out[:400]}
    return {"ok": False, "error": "could not import browser cookies",
            "detail": (out + "\n" + detail)[:700]}


def ensure_auth(popup: bool = True, browser: str | None = None) -> dict:
    """Make sure we have a valid session. Starts the popup login if not.

    Fallback chain (automatic):
      1. valid session?            -> 'already_valid'
      2. popup mode: browser popup (system Chrome if reachable, else bundled Chromium)
      3. browser-cookies silent    -> 'browser_cookies'
      4. manual instructions       -> 'failed' (user runs notebooklm login)

    Returns {'ok': bool, 'action': str, 'detail': str}.
    """
    valid, detail = auth_check(test=True)
    if valid:
        return {"ok": True, "action": "already_valid", "detail": detail}

    mode = os.environ.get(LOGIN_MODE_ENV, "popup").lower()
    browsers: list[str | None] = [browser]
    env_browser = os.environ.get("BRAINBRIDGE_LOGIN_BROWSER")
    if env_browser and env_browser not in browsers:
        browsers.append(env_browser)
    if not any(browsers):
        browsers = ["chrome", None]  # system Chrome first (best SSO UX), then bundled

    if mode == "popup":
        for b in browsers:
            res = login_via_popup(browser=b, wait_minutes=6)
            if res["ok"]:
                return {"ok": True, "action": "login", "detail": res.get("detail", "")}
            # if it failed because browser/playwright unavailable, continue the chain
        # popup didn't work -> try silent browser-cookies
        silent = login_via_browser_cookies(os.environ.get("BRAINBRIDGE_LOGIN_BROWSER", "chrome"))
        if silent["ok"]:
            return {"ok": True, "action": "browser_cookies", "detail": silent.get("detail", "")}
        return {"ok": False, "action": "failed",
                "detail": ("Popup login could not complete in this environment "
                           "(no display / no browser). " + (silent.get("error") or silent.get("detail") or ""))[:600]}

    if mode == "browser":
        res = login_via_browser_cookies(os.environ.get("BRAINBRIDGE_LOGIN_BROWSER", "chrome"))
        return {"ok": res["ok"], "action": "browser_cookies" if res["ok"] else "failed",
                "detail": res.get("detail", "")}

    # manual mode
    return {"ok": False, "action": "failed",
            "detail": ("No valid NotebookLM session. Run `notebooklm login` "
                       "(or set BRAINBRIDGE_LOGIN_MODE=popup|browser) and try again. "
                       "Original check: " + detail)}


def logout() -> dict:
    """Clear the saved session (local profile only — does NOT sign out of Google)."""
    try:
        proc = subprocess.run(["notebooklm", "auth", "logout"],
                              capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "logout timed out"}
    return {"ok": proc.returncode == 0, "detail": out[:300]}
