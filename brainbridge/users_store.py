"""BrainBridge Multi — per-user session store.

Backend auto-detection (first match wins):
  1. Upstash Redis (Vercel KV):  env KV_REST_API_URL + KV_REST_API_TOKEN
  2. Vercel Blob:                env BLOB_READ_WRITE_TOKEN
  3. Local files:                ./data/users/*.json  (dev/sandbox)

Records are small JSON dicts: {key, label, state_b64, created, updated, note}.
state_b64 = raw base64 of the Playwright storage_state.json that the
notebooklm CLI uses with --storage <path>.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import vercel_blob
except ImportError:  # pragma: no cover
    vercel_blob = None

BASE_DIR = Path(os.environ.get("BRAINBRIDGE_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))


# --------------------------------------------------------------------------- helpers
def _now() -> float:
    return time.time()


def _b64_state_bytes(state_b64: str) -> bytes:
    import base64
    return base64.b64decode(state_b64)


# --------------------------------------------------------------------------- File
class FileStore:
    name = "file"

    def __init__(self, base_dir: Path = BASE_DIR):
        # On serverless (Vercel) only /tmp is writable; the repo root is
        # read-only. If the configured dir can't be created, fall back to /tmp
        # (ephemeral but at least the API doesn't 500; KV/Blob is the durable fix).
        for candidate in (base_dir, Path(os.environ.get("TMPDIR", "/tmp")) / "bbdata"):
            try:
                self.dir = candidate / "users"
                self.tdir = candidate / "tickets"
                self.dir.mkdir(parents=True, exist_ok=True)
                self.tdir.mkdir(parents=True, exist_ok=True)
                self._writable = True
                break
            except OSError:
                continue
        else:
            self.dir = Path("/tmp") / "bbdata" / "users"
            self.tdir = Path("/tmp") / "bbdata" / "tickets"
            self._writable = False

    def get_user(self, key: str) -> dict | None:
        p = self.dir / f"{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def put_user(self, rec: dict) -> None:
        (self.dir / f"{rec['key']}.json").write_text(json.dumps(rec))

    def list_users(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                continue
        return out

    def delete_user(self, key: str) -> bool:
        p = self.dir / f"{key}.json"
        if p.exists():
            p.unlink()
            return True
        return False

    # tickets (same structure)
    def get_ticket(self, tid: str) -> dict | None:
        p = self.tdir / f"{tid}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def put_ticket(self, rec: dict) -> None:
        (self.tdir / f"{rec['ticket_id']}.json").write_text(json.dumps(rec))

    def list_tickets(self, status: str | None = None) -> list[dict]:
        out = []
        for p in self.tdir.glob("*.json"):
            try:
                rec = json.loads(p.read_text())
            except Exception:
                continue
            if status and rec.get("status") != status:
                continue
            out.append(rec)
        return out

    def delete_ticket(self, tid: str) -> bool:
        p = self.tdir / f"{tid}.json"
        if p.exists():
            p.unlink()
            return True
        return False



    # pending cookie-mint requests (same JSON shape, different folder)
    def get_pending(self, pid: str) -> dict | None:
        p = self.tdir / f"mint_{pid}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def put_pending(self, rec: dict) -> None:
        (self.tdir / f"mint_{rec['pending_id']}.json").write_text(json.dumps(rec))

    def list_pending(self, status: str | None = None) -> list[dict]:
        out = []
        for p in self.tdir.glob("mint_*.json"):
            try:
                rec = json.loads(p.read_text())
            except Exception:
                continue
            if status and rec.get("status") != status:
                continue
            out.append(rec)
        return out

    def delete_pending(self, pid: str) -> bool:
        p = self.tdir / f"mint_{pid}.json"
        if p.exists():
            p.unlink()
            return True
        return False

# --------------------------------------------------------------------------- Upstash KV (REST)
class KVStore:
    name = "kv"

    def __init__(self):
        self.url = os.environ["KV_REST_API_URL"].rstrip("/")
        self.token = os.environ["KV_REST_API_TOKEN"]
        self.h = {"Authorization": f"Bearer {self.token}"}

    def _get(self, key: str) -> str | None:
        r = httpx.get(f"{self.url}/get/{key}", headers=self.h, timeout=10)
        r.raise_for_status()
        return r.json().get("result")

    def _set(self, key: str, value: str) -> None:
        r = httpx.post(f"{self.url}/set/{key}", content=value,
                       headers={**self.h, "Content-Type": "text/plain"}, timeout=10)
        r.raise_for_status()

    def _del(self, key: str) -> None:
        try:
            httpx.post(f"{self.url}/del/{key}", headers=self.h, timeout=10)
        except Exception:
            try:
                httpx.delete(f"{self.url}/{key}", headers=self.h, timeout=10)
            except Exception as e:  # noqa: BLE001
                print(f"[store:kv] del failed: {e}", file=sys.stderr)

    def _keys(self, pattern: str) -> list[str]:
        r = httpx.get(f"{self.url}/keys/{pattern}", headers=self.h, timeout=10)
        r.raise_for_status()
        return list(r.json().get("result", []))

    def _get_json(self, key: str) -> dict | None:
        v = self._get(key)
        if not v:
            return None
        try:
            return json.loads(v)
        except Exception:
            return None

    def get_user(self, key: str) -> dict | None:
        return self._get_json(f"bb:user:{key}")

    def put_user(self, rec: dict) -> None:
        self._set(f"bb:user:{rec['key']}", json.dumps(rec))

    def list_users(self) -> list[dict]:
        out = []
        for k in self._keys("bb:user:*"):
            rec = self._get_json(k)
            if rec:
                out.append(rec)
        return out

    def delete_user(self, key: str) -> bool:
        if self._get(f"bb:user:{key}") is None:
            return False
        self._del(f"bb:user:{key}")
        return True

    def get_ticket(self, tid: str) -> dict | None:
        return self._get_json(f"bb:ticket:{tid}")

    def put_ticket(self, rec: dict) -> None:
        self._set(f"bb:ticket:{rec['ticket_id']}", json.dumps(rec))

    def list_tickets(self, status: str | None = None) -> list[dict]:
        out = []
        for k in self._keys("bb:ticket:*"):
            rec = self._get_json(k)
            if rec and (status is None or rec.get("status") == status):
                out.append(rec)
        return out

    def delete_ticket(self, tid: str) -> bool:
        if self._get(f"bb:ticket:{tid}") is None:
            return False
        self._del(f"bb:ticket:{tid}")
        return True



    def get_pending(self, pid: str) -> dict | None:
        return self._get_json(f"bb:pending:{pid}")

    def put_pending(self, rec: dict) -> None:
        self._set(f"bb:pending:{rec['pending_id']}", json.dumps(rec))

    def list_pending(self, status: str | None = None) -> list[dict]:
        out = []
        for k in self._keys("bb:pending:*"):
            rec = self._get_json(k)
            if rec and (status is None or rec.get("status") == status):
                out.append(rec)
        return out

    def delete_pending(self, pid: str) -> bool:
        if self._get(f"bb:pending:{pid}") is None:
            return False
        self._del(f"bb:pending:{pid}")
        return True

# --------------------------------------------------------------------------- Vercel Blob
class BlobStore:
    name = "blob"

    def __init__(self):
        if vercel_blob is None:  # pragma: no cover
            raise RuntimeError("vercel-blob not installed")
        self.token = os.environ.get("BLOB_READ_WRITE_TOKEN", "")

    def _fetch(self, url: str) -> bytes:
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        return r.content

    def _find(self, prefix: str) -> dict | None:
        d = vercel_blob.list({"prefix": prefix, "limit": 5}, timeout=15)
        blobs = d.get("blobs", [])
        return blobs[0] if blobs else None

    def get_user(self, key: str) -> dict | None:
        b = self._find(f"users/{key}.json")
        if not b:
            return None
        try:
            return json.loads(self._fetch(b["url"]))
        except Exception:
            return None

    def put_user(self, rec: dict) -> None:
        vercel_blob.put(f"users/{rec['key']}.json", json.dumps(rec).encode(), timeout=15)

    def list_users(self) -> list[dict]:
        out = []
        cursor = None
        while True:
            opts = {"prefix": "users/", "limit": 1000, "mode": "expanded"}
            if cursor:
                opts["cursor"] = cursor
            d = vercel_blob.list(opts, timeout=15)
            for b in d.get("blobs", []):
                try:
                    out.append(json.loads(self._fetch(b["url"])))
                except Exception:
                    continue
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    def delete_user(self, key: str) -> bool:
        b = self._find(f"users/{key}.json")
        if not b:
            return False
        vercel_blob.delete(b["url"], timeout=15)
        return True

    def get_ticket(self, tid: str) -> dict | None:
        b = self._find(f"tickets/{tid}.json")
        if not b:
            return None
        try:
            return json.loads(self._fetch(b["url"]))
        except Exception:
            return None

    def put_ticket(self, rec: dict) -> None:
        vercel_blob.put(f"tickets/{rec['ticket_id']}.json", json.dumps(rec).encode(), timeout=15)

    def list_tickets(self, status: str | None = None) -> list[dict]:
        out = []
        try:
            d = vercel_blob.list({"prefix": "tickets/", "limit": 1000}, timeout=15)
        except Exception:
            d = {}
        for b in d.get("blobs", []):
            try:
                rec = json.loads(self._fetch(b["url"]))
            except Exception:
                continue
            if status and rec.get("status") != status:
                continue
            out.append(rec)
        return out

    def delete_ticket(self, tid: str) -> bool:
        b = self._find(f"tickets/{tid}.json")
        if not b:
            return False
        vercel_blob.delete(b["url"], timeout=15)
        return True



    def get_pending(self, pid: str) -> dict | None:
        b = self._find(f"pending/{pid}.json")
        if not b:
            return None
        try:
            return json.loads(self._fetch(b["url"]))
        except Exception:
            return None

    def put_pending(self, rec: dict) -> None:
        vercel_blob.put(f"pending/{rec['pending_id']}.json", json.dumps(rec).encode(), timeout=15)

    def list_pending(self, status: str | None = None) -> list[dict]:
        out = []
        try:
            d = vercel_blob.list({"prefix": "pending/", "limit": 1000}, timeout=15)
        except Exception:
            d = {}
        for b in d.get("blobs", []):
            try:
                rec = json.loads(self._fetch(b["url"]))
            except Exception:
                continue
            if status and rec.get("status") != status:
                continue
            out.append(rec)
        return out

    def delete_pending(self, pid: str) -> bool:
        b = self._find(f"pending/{pid}.json")
        if not b:
            return False
        vercel_blob.delete(b["url"], timeout=15)
        return True

# --------------------------------------------------------------------------- factory
_store: Any = None


def get_store() -> Any:
    global _store
    if _store is None:
        kv_url = os.environ.get("KV_REST_API_URL", "").strip()
        kv_tok = os.environ.get("KV_REST_API_TOKEN", "").strip()
        if kv_url and kv_tok and httpx is not None:
            _store = KVStore()
        elif os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip() and vercel_blob is not None:
            _store = BlobStore()
        else:
            _store = FileStore()
        if os.environ.get("VERCEL") and _store.name == "file":
            print("[store] WARNING: no KV/Blob credentials -> using ephemeral"
                  " file store; multi-user registrations will NOT persist "
                  "between serverless invocations.", file=sys.stderr)
    return _store


def store_name() -> str:
    return get_store().name
