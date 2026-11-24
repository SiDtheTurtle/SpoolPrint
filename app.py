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

# Slot values the label API accepts, with the names it prints (from the API docs).
SLOT_OPTIONS = {
    "nozzleTemp": "Nozzle Temp",
    "bedTemp": "Bed Temp",
    "chamber_temp": "Chamber Temp",
    "flowRatio": "Flow Ratio",
    "flowInfo": "Flow Info (Max Vol Spd & Flow Ratio)",
    "max_volumetric_speed": "Max Volumetric Speed",
    "kValue": "Pressure Advance/K Value",
    "tdValue": "Transmission Distance (TD)",
    "dryingInfo": "Drying Info (Time & Temp)",
    "dryingTemp": "Drying Temp",
    "dryingTime": "Drying Time",
    "last_dried": "Last Dried Date",
    "spoolWeight": "Empty Spool Weight",
    "shrinkage": "Shrinkage",
    "ironing_info": "Ironing Info (Speed & Flow)",
    "ironing_flow": "Ironing Flow",
    "ironing_speed": "Ironing Speed",
    "ams": "AMS Compatible",
    "amsModels": "AMS Models",
    "filamentId": "Filament ID",
    "sku": "SKU",
    "shortCode": "Spool ID",
    "location": "Location",
    "rfidTag": "RFID Tag",
    "purchaseDate": "Purchase Date",
    "purchasePrice": "Purchase Price",
    "purchaseNotes": "Purchase Notes",
    "none": "(empty)",
}
QR_OPTIONS = {"detail": "Spool page", "edit": "Spool edit form"}
DEFAULT_LABEL_SETTINGS = {
    "slots": ["nozzleTemp", "bedTemp", "flowRatio", "tdValue"],
    "show_rgb": True,
    "rgb_swatch": False,
    "rgb_field": "",  # "" keeps the colour code; otherwise a slot value printed in the colour area
    "qr": "detail",
}
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
    # Labels used to be cached flat in labels/; they now live in one folder per settings.
    for legacy in (DATA_DIR / "labels").glob("*.svg"):
        legacy.unlink(missing_ok=True)
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


def flatten_spool(s: dict) -> dict:
    """The fields the page needs; sorting and grouping happen in the browser."""
    return {
        **{k: s.get(k) for k in SPOOL_FIELDS},
        "filament_id": s.get("filament_id"),
        **{k: s.get(k) for k in FILAMENT_FIELDS},
        "purchase_date": (s.get("spool_properties") or {}).get("purchase_date"),
    }


@app.get("/api/spools")
async def get_spools(refresh: bool = False):
    cached = await load_spools(refresh)
    return {
        "fetched_at": cached["fetched_at"],
        "spool_count": len(cached["spools"]),
        "spools": [flatten_spool(s) for s in cached["spools"]],
    }


# ── label settings ────────────────────────────────────────────────────────────

def validate_label_settings(s: object) -> dict:
    """Return clean settings, or raise ApiError(400) naming the bad field."""
    def bad(message: str):
        raise ApiError(400, "invalid_settings", message)

    if not isinstance(s, dict):
        bad("Settings must be a JSON object.")
    slots = s.get("slots")
    if not isinstance(slots, list) or len(slots) != 4 or not all(v in SLOT_OPTIONS for v in slots):
        bad("slots must be a list of four slot values.")
    for key in ("show_rgb", "rgb_swatch"):
        if not isinstance(s.get(key), bool):
            bad(f"{key} must be true or false.")
    rgb_field = s.get("rgb_field")
    if rgb_field != "" and (rgb_field not in SLOT_OPTIONS or rgb_field == "none"):
        bad("rgb_field must be empty or a slot value.")
    if s.get("qr") not in QR_OPTIONS:
        bad("qr must be detail or edit.")
    return {k: s[k] for k in DEFAULT_LABEL_SETTINGS}


def read_settings_file() -> dict:
    stored = read_json(DATA_DIR / "settings.json")
    return stored if isinstance(stored, dict) else {}


def update_settings_file(key: str, value) -> None:
    """Replace one top-level section of settings.json, keeping the others."""
    stored = read_settings_file()
    stored[key] = value
    write_json(DATA_DIR / "settings.json", stored)


def load_label_settings() -> dict:
    merged = {**DEFAULT_LABEL_SETTINGS, **(read_settings_file().get("label") or {})}
    try:
        return validate_label_settings(merged)
    except ApiError:
        return dict(DEFAULT_LABEL_SETTINGS)


def label_params(settings: dict) -> dict:
    """Query parameters for the label API: the classic 40x30 mm layout as SVG."""
    params = {
        "format": "svg",
        "size": "1",
        "slots": ",".join(settings["slots"]),
        "show_rgb": str(settings["show_rgb"]).lower(),
        "rgb_swatch": str(settings["rgb_swatch"]).lower(),
        "qr": settings["qr"],
    }
    if settings["rgb_field"]:
        params["rgb_field"] = settings["rgb_field"]
    return params


@app.get("/api/settings")
def get_settings():
    return {
        "label": load_label_settings(),
        "defaults": DEFAULT_LABEL_SETTINGS,
        "options": {"slots": SLOT_OPTIONS, "qr": QR_OPTIONS},
    }


@app.put("/api/settings")
async def put_settings(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_settings", "Settings must be JSON.")
    label = validate_label_settings(body.get("label") if isinstance(body, dict) else None)
    update_settings_file("label", label)
    return get_settings()


# ── printer settings, one set per printer ─────────────────────────────────────

DEFAULT_PRINTER_SETTINGS = {"density": 3, "direction": "top", "flip": False, "offset_x": 0.0, "offset_y": 0.0}
PRINTER_NAME_RE = re.compile(r"[A-Za-z0-9 _.:-]{1,64}")
DOTS_PER_MM = 8
MAX_OFFSET_MM = 5


def validate_printer_settings(s: object) -> dict:
    def bad(message: str):
        raise ApiError(400, "invalid_settings", message)

    if not isinstance(s, dict):
        bad("Printer settings must be a JSON object.")
    density = s.get("density")
    if not isinstance(density, int) or isinstance(density, bool) or not 1 <= density <= 5:
        bad("density must be a whole number from 1 to 5.")
    if s.get("direction") not in ("top", "left"):
        bad("direction must be top or left.")
    if not isinstance(s.get("flip"), bool):
        bad("flip must be true or false.")
    clean = {"density": density, "direction": s["direction"], "flip": s["flip"]}
    for key in ("offset_x", "offset_y"):
        v = s.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or abs(v) > MAX_OFFSET_MM:
            bad(f"{key} must be a number of mm from -{MAX_OFFSET_MM} to {MAX_OFFSET_MM}.")
        clean[key] = round(v * DOTS_PER_MM) / DOTS_PER_MM  # whole printer dots
    return clean


def check_printer_name(name: str) -> None:
    if not PRINTER_NAME_RE.fullmatch(name):
        raise ApiError(400, "invalid_printer_name", "Printer names are letters, digits, spaces and _ . : - only.")


@app.get("/api/printers/{name}")
def get_printer_settings(name: str):
    check_printer_name(name)
    saved = (read_settings_file().get("printers") or {}).get(name)
    if saved is not None:
        try:
            return {"name": name, "saved": True, "settings": validate_printer_settings(saved)}
        except ApiError:
            pass
    return {"name": name, "saved": False, "settings": dict(DEFAULT_PRINTER_SETTINGS)}


@app.put("/api/printers/{name}")
async def put_printer_settings(name: str, request: Request):
    check_printer_name(name)
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_settings", "Settings must be JSON.")
    clean = validate_printer_settings(body)
    printers = read_settings_file().get("printers") or {}
    printers[name] = clean
    update_settings_file("printers", printers)
    return {"name": name, "saved": True, "settings": clean}


# ── labels ────────────────────────────────────────────────────────────────────

@app.get("/api/label/{short_code}")
async def get_label(short_code: str, refresh: bool = False):
    if not SHORT_CODE_RE.fullmatch(short_code):
        raise ApiError(400, "invalid_short_code", "Spool codes are letters and digits only.")
    cached = await load_spools()
    spool = next((s for s in cached["spools"] if s.get("short_code") == short_code), None)
    if spool is None:
        raise ApiError(404, "unknown_spool", f"Spool {short_code} is not in the spool list. Try Refresh.")

    # One cache folder per combination of label settings, so switching back to
    # settings used before costs nothing. Within it, the file name changes when
    # the spool does, so a stale label is never served.
    params = label_params(load_label_settings())
    params_tag = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10]
    version = hashlib.sha1(str(spool.get("updated_at")).encode()).hexdigest()[:12]
    labels_dir = DATA_DIR / "labels" / params_tag
    path = labels_dir / f"{short_code}-{version}.svg"

    async with app.state.label_locks.setdefault(short_code, asyncio.Lock()):
        if refresh or not path.exists():
            r = await upstream_get(f"/my/spools/{short_code}/label", params)
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
