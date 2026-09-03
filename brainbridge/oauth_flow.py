"""BrainBridge — official Google OAuth (Tasks) helpers.

The user clicks "Sign in with Google" -> the real Google consent screen
("BrainBridge wants access to your Google Tasks") -> Continue -> we get a
long-lived refresh_token. No cookies, no VNC, no export — this is the
official, revocable OAuth flow, exactly like every professional Google
integration.

Env:
  GOOGLE_CLIENT_ID        (Google Cloud Console -> Credentials -> OAuth client ID)
  GOOGLE_CLIENT_SECRET
  GOOGLE_REDIRECT_URI     default https://brain-bridge-six.vercel.app/api/oauth2callback
"""

from __future__ import annotations

import os
import urllib.parse

import httpx
from fastapi import HTTPException

SCOPES = " ".join([
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
])

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _cfg() -> tuple[str, str]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    cs = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not cs:
        raise HTTPException(
            503,
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (Google Cloud Console -> APIs & Services -> "
            "Credentials -> OAuth client ID [Web application]).",
        )
    return cid, cs


def redirect_uri() -> str:
    return (os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
            or "https://brain-bridge-six.vercel.app/api/oauth2callback")


def auth_url(state: str, prompt: str = "consent") -> str:
    cid, _ = _cfg()
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": prompt,
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    """code -> tokens. Raises HTTPException with a readable message on error."""
    cid, cs = _cfg()
    r = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": cid,
        "client_secret": cs,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }, timeout=30)
    if r.status_code != 200:
        raise HTTPException(400, f"Google token exchange failed: {r.text[:200]}")
    d = r.json()
    if "refresh_token" not in d:
        raise HTTPException(
            400,
            "Google did not return a refresh_token (the consent screen may have "
            "been skipped because you already approved: re-try with prompt=consent "
            "or revoke the app at myaccount.google.com/permissions).",
        )
    return d


def refresh_access_token(refresh_token: str) -> str:
    cid, cs = _cfg()
    r = httpx.post(TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": cid,
        "client_secret": cs,
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code != 200:
        raise HTTPException(401, f"Refresh token rejected: {r.text[:200]}")
    return r.json()["access_token"]


def user_email(access_token: str) -> str:
    return user_profile(access_token).get("email", "")


def user_profile(access_token: str) -> dict:
    """Email + stable account id (sub) for the authorized user."""
    r = httpx.get("https://openidconnect.googleapis.com/v1/userinfo",
                  headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code != 200:
        r = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo",
                      params={"alt": "json"},
                      headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(401, f"userinfo failed: {r.text[:200]}")
    j = r.json()
    return {"email": j.get("email", ""), "sub": j.get("sub", "")}
