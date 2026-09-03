"""BrainBridge — memory backend over the official Google Tasks API.

Each memory entry = one task:
  title  -> the entry's title  (maybe with a date prefix)
  notes  -> the content

Tasks API: https://developers.google.com/tasks
Quota for consumer: 60 req/min — plenty for a memory journal.
"""

from __future__ import annotations

import httpx

TASKS_API = "https://tasks.googleapis.com/tasks/v1"
MEMORY_LIST_TITLE = "BrainBridge Memory"
NOTE_MAX = 3000  # chars per task.notes (we split longer content into parts)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def ensure_list(token: str) -> str:
    """Find (or create) the BrainBridge Memory tasklist; return its id."""
    r = httpx.get(f"{TASKS_API}/users/@me/lists", headers=_h(token), timeout=30)
    r.raise_for_status()
    for lst in r.json().get("items", []):
        if lst.get("title") == MEMORY_LIST_TITLE:
            return lst["id"]
    r2 = httpx.post(f"{TASKS_API}/users/@me/lists", headers=_h(token),
                    json={"title": MEMORY_LIST_TITLE}, timeout=30)
    r2.raise_for_status()
    return r2.json()["id"]


def list_notes(token: str, show_completed: bool = True, limit: int = 300) -> list[dict]:
    lid = ensure_list(token)
    notes: list[dict] = []
    page_token = None
    while True:
        params: dict = {"tasklist": lid, "maxResults": 100}
        if show_completed:
            params["showCompleted"] = "true"
            params["showHidden"] = "true"
        if page_token:
            params["pageToken"] = page_token
        r = httpx.get(f"{TASKS_API}/lists/{lid}/tasks", headers=_h(token),
                      params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        for t in d.get("items", []):
            notes.append({
                "id": t["id"],
                "title": (t.get("title") or "").strip(),
                "content": (t.get("notes") or "").strip(),
                "status": t.get("status", "needsAction"),
                "created": t.get("created", ""),
                "updated": t.get("updated", ""),
            })
            if len(notes) >= limit:
                return notes
        page_token = d.get("nextPageToken")
        if not page_token:
            break
    return notes


def split_content(content: str) -> list[str]:
    content = content.strip()
    if len(content) <= NOTE_MAX:
        return [content]
    parts, chunk = [], ""
    for line in content.splitlines():
        if len(chunk) + len(line) + 1 > NOTE_MAX:
            if chunk:
                parts.append(chunk)
            chunk = line
        else:
            chunk = (chunk + "\n" + line).strip()
    if chunk:
        parts.append(chunk)
    return parts or [""]


def create_note(token: str, title: str, content: str) -> dict:
    lid = ensure_list(token)
    parts = split_content(content)
    first_id = None
    for i, part in enumerate(parts):
        t = title.strip()[:150] if i == 0 else f"{title.strip()[:130]} (part {i+1})"
        r = httpx.post(f"{TASKS_API}/lists/{lid}/tasks", headers=_h(token),
                       json={"title": t, "notes": part}, timeout=30)
        r.raise_for_status()
        if first_id is None:
            first_id = r.json().get("id")
    return {"id": first_id, "title": title, "content": content, "parts": len(parts)}


def get_note(token: str, note_id: str) -> dict | None:
    lid = ensure_list(token)
    r = httpx.get(f"{TASKS_API}/lists/{lid}/tasks/{note_id}",
                  headers=_h(token), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json()
    return {"id": d["id"], "title": (d.get("title") or "").strip(),
            "content": (d.get("notes") or "").strip(),
            "status": d.get("status", "needsAction")}


def delete_note(token: str, note_id: str) -> bool:
    lid = ensure_list(token)
    r = httpx.delete(f"{TASKS_API}/lists/{lid}/tasks/{note_id}",
                     headers=_h(token), timeout=30)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def search_notes(token: str, keyword: str, limit: int = 50) -> list[dict]:
    kw = keyword.lower()
    return [n for n in list_notes(token, limit=limit)
            if kw in n["title"].lower() or kw in n["content"].lower()]


def render_context(notes: list[dict], limit: int = 8, max_chars: int = 6000) -> str:
    """Compact 'what is already known' text for an AI prompt."""
    out: list[str] = []
    total = 0
    for n in notes[-limit:]:
        block = f"### {n['title']}\n{n['content'][:1500]}\n"
        if total + len(block) > max_chars:
            break
        out.append(block)
        total += len(block)
    return "\n".join(out)
