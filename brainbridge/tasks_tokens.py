"""BrainBridge — shared helpers for Google Tasks token lifecycle (rotation).

Google rotates refresh tokens for unverified/testing apps: each refresh
returns a NEW refresh token and invalidates the old one. Every call site MUST
persist the rotated token, keeping the previous one as a fallback.
"""

from __future__ import annotations

from fastapi import HTTPException

from . import oauth_flow
from .secret import decrypt_state, encrypt_state
from .users_store import get_store


def refresh_and_rotate(u: dict) -> str:
    """Refresh Google's access token for a tasks user and PERSIST the rotated
    refresh token (Google invalidates the previous one right away for
    unverified apps). Keeps the previous token as fallback."""
    cands: list[tuple[str, str]] = []
    try:
        cands.append(("current", decrypt_state(u["refresh_token"]).decode()))
    except Exception:  # noqa: BLE001
        pass
    if u.get("refresh_token_prev"):
        try:
            cands.append(("prev", decrypt_state(u["refresh_token_prev"]).decode()))
        except Exception:  # noqa: BLE001
            pass
    last: Exception | None = None
    for slot, tok in cands:
        try:
            at, new_rt = oauth_flow.refresh_access_token(tok)
        except HTTPException as e:
            last = e
            continue
        if new_rt:
            if slot == "current":
                u["refresh_token_prev"] = u["refresh_token"]
            u["refresh_token"] = encrypt_state(new_rt.encode())
            get_store().put_user(u)
        return at
    raise HTTPException(
        401, f"Refresh token rejected (last error: {last})" if last
        else "No refresh token stored for this user"
    )


def resolve_tasks_user(token: str) -> dict | None:
    """Look up a tasks-provider user by API key (lenient on trailing -/_ that
    people drop when copying). Returns None if not found / not a tasks user."""
    if not token:
        return None
    store = get_store()
    rec = store.get_user(token)
    if not rec:
        for cand in (token + "-", token + "_",
                     token[:-1] if token.endswith(("-", "_")) else token):
            if cand != token and cand:
                rec = store.get_user(cand)
                if rec:
                    break
    return rec if rec and rec.get("provider") == "tasks" else None
