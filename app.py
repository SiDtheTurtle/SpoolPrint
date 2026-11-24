"""NiimBatch: batch-print 3dfilamentprofiles.com spool labels to a Niimbot B1."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

API_BASE = "https://3dfilamentprofiles.com/api/v1"
API_KEY = os.environ.get("FILAMENTPROFILES_API_KEY", "").strip()
DATA_DIR = Path(os.environ.get("NB_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"

# Classic 40x30 mm layout, as SVG so the browser can rasterise it at printer resolution.
LABEL_PARAMS = {"format": "svg", "size": "1"}
LABEL_PARAMS_TAG = hashlib.sha1(json.dumps(LABEL_PARAMS, sort_keys=True).encode()).hexdigest()[:8]
SHORT_CODE_RE = re.compile(r"[A-Za-z0-9]{1,32}")
SPOOL_FIELDS = ("short_code", "location", "remaining_grams", "notes", "td_value", "updated_at", "spool_url")
FILAMENT_FIELDS = ("brand", "material", "material_type", "color", "rgb")

# Served with label SVGs so one opened directly can't run script on our origin.
LABEL_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"

# Tests swap this for an httpx.MockTransport so they never touch the real API.
UPSTREAM_TRANSPORT: httpx.AsyncBaseTransport | None = None

FRIENDLY_ERRORS = {
    "invalid_key": "The API key is missing, wrong or was regenerated. Update FILAMENTPROFILES_API_KEY in .env and restart.",
    "no_access": "The 3dfilamentprofiles.com account has no active API access. Check the subscription.",
    "blocked": "The 3dfilamentprofiles.com account has been blocked.",
    "rate_limited": "This month's API quota is used up. It resets at 00:00 UTC on the 1st.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Be gentle with the API, and never fetch the same label twice at once.
    app.state.upstream_slots = asyncio.Semaphore(4)
    app.state.label_locks = {}
    yield


app = FastAPI(title="NiimBatch", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


# ── storage helpers ───────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, obj: dict) -> None:
    write_atomic(path, json.dumps(obj, indent=1).encode())


# ── upstream API ──────────────────────────────────────────────────────────────

def record_quota(r: httpx.Response) -> None:
    limit, remaining = r.headers.get("X-RateLimit-Limit"), r.headers.get("X-RateLimit-Remaining")
    if limit is not None and remaining is not None:
        write_json(DATA_DIR / "quota.json", {"limit": int(limit), "remaining": int(remaining), "at": now_iso()})


async def upstream_get(path: str, params: dict | None = None) -> httpx.Response:
    if not API_KEY:
        raise ApiError(503, "no_api_key", "FILAMENTPROFILES_API_KEY is not set in .env.")
    async with app.state.upstream_slots:
        try:
            async with httpx.AsyncClient(
                base_url=API_BASE,
                transport=UPSTREAM_TRANSPORT,
                timeout=20,
                headers={"Authorization": f"Bearer {API_KEY}"},
            ) as client:
                r = await client.get(path, params=params)
        except httpx.HTTPError as e:
            detail = f"{e.__class__.__name__}: {e}" if str(e) else e.__class__.__name__
            raise ApiError(502, "upstream_unreachable", f"Could not reach 3dfilamentprofiles.com ({detail}). Try again.")
    record_quota(r)
    if r.is_success:
        return r
    try:
        err = r.json()["error"]
        code, message = err["code"], err["message"]
    except (ValueError, KeyError, TypeError):
        code, message = "upstream_error", f"Unexpected HTTP {r.status_code} from 3dfilamentprofiles.com."
    status = r.status_code if 400 <= r.status_code < 500 else 502
    raise ApiError(status, code, FRIENDLY_ERRORS.get(code, message))


# ── spools ────────────────────────────────────────────────────────────────────

async def load_spools(refresh: bool = False) -> dict:
    """The cached spool list, fetched from the API only when missing or on refresh."""
    path = DATA_DIR / "spools.json"
    if not refresh and (cached := read_json(path)) is not None:
        return cached
    r = await upstream_get("/my/spools")
    cached = {"fetched_at": now_iso(), "spools": r.json().get("data", [])}
    write_json(path, cached)
    return cached


def build_tree(spools: list[dict]) -> list[dict]:
    """Group spools under their filament, sorted by brand, material, type and colour."""
    groups: dict = {}
    for s in spools:
        fid = s.get("filament_id")
        if fid not in groups:
            groups[fid] = {"filament_id": fid, **{k: s.get(k) for k in FILAMENT_FIELDS}, "spools": []}
        groups[fid]["spools"].append({k: s.get(k) for k in SPOOL_FIELDS})
    for g in groups.values():
        g["spools"].sort(key=lambda s: s["short_code"] or "")
    return sorted(groups.values(), key=lambda g: tuple((g[k] or "").lower() for k in FILAMENT_FIELDS[:4]))


@app.get("/api/spools")
async def get_spools(refresh: bool = False):
    cached = await load_spools(refresh)
    return {
        "fetched_at": cached["fetched_at"],
        "spool_count": len(cached["spools"]),
        "filaments": build_tree(cached["spools"]),
    }


# ── labels ────────────────────────────────────────────────────────────────────

@app.get("/api/label/{short_code}")
async def get_label(short_code: str, refresh: bool = False):
    if not SHORT_CODE_RE.fullmatch(short_code):
        raise ApiError(400, "invalid_short_code", "Spool codes are letters and digits only.")
    cached = await load_spools()
    spool = next((s for s in cached["spools"] if s.get("short_code") == short_code), None)
    if spool is None:
        raise ApiError(404, "unknown_spool", f"Spool {short_code} is not in the spool list. Try Refresh.")

    # The file name changes whenever the spool or the label options change, so the
    # cache never serves a stale label, and costs one request per label otherwise.
    version = hashlib.sha1(f"{spool.get('updated_at')}|{LABEL_PARAMS_TAG}".encode()).hexdigest()[:12]
    labels_dir = DATA_DIR / "labels"
    path = labels_dir / f"{short_code}-{version}.svg"

    async with app.state.label_locks.setdefault(short_code, asyncio.Lock()):
        if refresh or not path.exists():
            r = await upstream_get(f"/my/spools/{short_code}/label", LABEL_PARAMS)
            write_atomic(path, r.content)
            for old in labels_dir.glob(f"{short_code}-*.svg"):
                if old != path:
                    old.unlink(missing_ok=True)
        body = path.read_bytes()

    return Response(body, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache", "Content-Security-Policy": LABEL_CSP})


# ── status ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    spools = read_json(DATA_DIR / "spools.json")
    return {
        "api_key_set": bool(API_KEY),
        "quota": read_json(DATA_DIR / "quota.json"),
        "spools_fetched_at": spools["fetched_at"] if spools else None,
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "api_key_set": bool(API_KEY)}


# Mounted last so /healthz and /api routes take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
