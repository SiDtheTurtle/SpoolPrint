"""SpoolPrint: print 3dfilamentprofiles.com spool and filament labels from the browser."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import segno
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
# Slots that print something about one particular spool, so not on filament labels.
SPOOL_ONLY_SLOTS = {"last_dried", "location", "purchaseDate", "purchasePrice", "purchaseNotes", "rfidTag", "shortCode"}
FILAMENT_SLOT_OPTIONS = {k: v for k, v in SLOT_OPTIONS.items() if k not in SPOOL_ONLY_SLOTS}
# Matches the site's own filament label: temperatures, two blank rows, no colour code.
DEFAULT_FILAMENT_LABEL_SETTINGS = {
    "slots": ["nozzleTemp", "bedTemp", "none", "none"],
    "show_rgb": False,
    "rgb_swatch": False,
    "rgb_field": "",
}
FILAMENT_URL = "https://spooldb.com/f/{id}"  # what the site's filament labels encode
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


app = FastAPI(title="SpoolPrint", docs_url=None, redoc_url=None, lifespan=lifespan)


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

def validate_label_settings(s: object, filament: bool = False) -> dict:
    """Return clean settings, or raise ApiError(400) naming the bad field.

    Filament labels may only use slots that make sense without a spool.
    """
    def bad(message: str):
        raise ApiError(400, "invalid_settings", message)

    allowed = FILAMENT_SLOT_OPTIONS if filament else SLOT_OPTIONS
    if not isinstance(s, dict):
        bad("Settings must be a JSON object.")
    slots = s.get("slots")
    if not isinstance(slots, list) or len(slots) != 4 or not all(v in allowed for v in slots):
        bad("slots must be a list of four slot values" + (" that apply to any label." if filament else "."))
    for key in ("show_rgb", "rgb_swatch"):
        if not isinstance(s.get(key), bool):
            bad(f"{key} must be true or false.")
    rgb_field = s.get("rgb_field")
    if rgb_field != "" and (rgb_field not in allowed or rgb_field == "none"):
        bad("rgb_field must be empty or a slot value" + (" that applies to any label." if filament else "."))
    if not filament and s.get("qr") not in QR_OPTIONS:
        bad("qr must be detail or edit.")
    defaults = DEFAULT_FILAMENT_LABEL_SETTINGS if filament else DEFAULT_LABEL_SETTINGS
    return {k: s[k] for k in defaults}


def read_settings_file() -> dict:
    stored = read_json(DATA_DIR / "settings.json")
    return stored if isinstance(stored, dict) else {}


def update_settings_file(key: str, value) -> None:
    """Replace one top-level section of settings.json, keeping the others."""
    stored = read_settings_file()
    stored[key] = value
    write_json(DATA_DIR / "settings.json", stored)


def load_label_settings(filament: bool = False) -> dict:
    defaults = DEFAULT_FILAMENT_LABEL_SETTINGS if filament else DEFAULT_LABEL_SETTINGS
    stored = read_settings_file().get("filament_label" if filament else "label") or {}
    try:
        return validate_label_settings({**defaults, **stored}, filament)
    except ApiError:
        return dict(defaults)


def label_params(settings: dict) -> dict:
    """Query parameters for the label API: the classic 40x30 mm layout as SVG."""
    params = {
        "format": "svg",
        "size": "1",
        "slots": ",".join(settings["slots"]),
        "show_rgb": str(settings["show_rgb"]).lower(),
        "rgb_swatch": str(settings["rgb_swatch"]).lower(),
        "qr": settings.get("qr", "detail"),
    }
    if settings["rgb_field"]:
        params["rgb_field"] = settings["rgb_field"]
    return params


# How the table opens, shared by every device. The page can still change the
# grouping for a visit; this is what it starts with.
DEFAULT_VIEW_SETTINGS = {"group": "filament", "hide_empty": True}
GROUP_OPTIONS = {"none": "None", "filament": "Filament", "material": "Material"}


def validate_view_settings(s: object) -> dict:
    if not isinstance(s, dict):
        raise ApiError(400, "invalid_settings", "View settings must be a JSON object.")
    if s.get("group") not in GROUP_OPTIONS:
        raise ApiError(400, "invalid_settings", f"group must be one of: {', '.join(GROUP_OPTIONS)}.")
    if not isinstance(s.get("hide_empty"), bool):
        raise ApiError(400, "invalid_settings", "hide_empty must be true or false.")
    return {"group": s["group"], "hide_empty": s["hide_empty"]}


def load_view_settings() -> dict:
    stored = read_settings_file().get("view") or {}
    try:
        return validate_view_settings({**DEFAULT_VIEW_SETTINGS, **stored})
    except ApiError:
        return dict(DEFAULT_VIEW_SETTINGS)


@app.get("/api/settings")
def get_settings():
    return {
        "label": load_label_settings(),
        "filament_label": load_label_settings(filament=True),
        "view": load_view_settings(),
        "defaults": {"label": DEFAULT_LABEL_SETTINGS, "filament_label": DEFAULT_FILAMENT_LABEL_SETTINGS,
                     "view": DEFAULT_VIEW_SETTINGS},
        "options": {"slots": SLOT_OPTIONS, "qr": QR_OPTIONS, "spool_only": sorted(SPOOL_ONLY_SLOTS),
                    "group": GROUP_OPTIONS},
    }


@app.put("/api/settings")
async def put_settings(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_settings", "Settings must be JSON.")
    if not isinstance(body, dict) or not ({"label", "filament_label", "view"} & set(body)):
        raise ApiError(400, "invalid_settings", "Send label, filament_label and/or view settings.")
    # Validate everything before saving anything.
    clean = {}
    if "label" in body:
        clean["label"] = validate_label_settings(body["label"])
    if "filament_label" in body:
        clean["filament_label"] = validate_label_settings(body["filament_label"], filament=True)
    if "view" in body:
        clean["view"] = validate_view_settings(body["view"])
    for key, value in clean.items():
        update_settings_file(key, value)
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

def label_cache_path(spool: dict, params: dict, stem: str | None = None, kind: str = "spool") -> Path:
    """
    One cache folder per kind and combination of label settings, so switching
    back to settings used before costs nothing. Within it, the file name changes
    when the source spool does, so a stale label is never served.
    """
    if kind == "spool":
        tag_source, version_source = params, str(spool.get("updated_at"))
    else:
        # A filament label also changes if a different spool becomes its source.
        tag_source = {"kind": kind, **params}
        version_source = f"{spool['short_code']}|{spool.get('updated_at')}"
    params_tag = hashlib.sha1(json.dumps(tag_source, sort_keys=True).encode()).hexdigest()[:10]
    version = hashlib.sha1(version_source.encode()).hexdigest()[:12]
    return DATA_DIR / "labels" / params_tag / f"{stem or spool['short_code']}-{version}.svg"


def filament_source_spool(spools: list[dict], filament_id: int) -> dict | None:
    """
    The spool whose label a filament label is made from. Prefer spools without
    calibrated overrides, so the label shows the filament's own values, and pick
    by code so the choice (and the cache) stays stable.
    """
    override_keys = ("preferred_nozzle_temp", "preferred_bed_temp", "flow_ratio", "k_value", "td_value")

    def has_overrides(s: dict) -> bool:
        props = s.get("spool_properties") or {}
        return any(props.get(k) not in (None, "") for k in override_keys)

    candidates = [s for s in spools if s.get("filament_id") == filament_id]
    return min(candidates, key=lambda s: (has_overrides(s), s.get("short_code") or ""), default=None)


def filament_label_path(spool: dict, filament_id: int) -> Path:
    params = label_params(load_label_settings(filament=True))
    return label_cache_path(spool, params, stem=f"f{filament_id}", kind="filament")


@app.post("/api/labels/cache-status")
async def labels_cache_status(request: Request):
    """Which spool and filament labels are cached, so the page can show the API cost of a batch."""
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_request", "Body must be JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "Body must be a JSON object.")
    codes = body.get("codes", [])
    filament_ids = body.get("filaments", [])
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes) or len(codes) > 1000:
        raise ApiError(400, "invalid_request", "codes must be a list of spool codes.")
    if (not isinstance(filament_ids, list) or len(filament_ids) > 1000
            or not all(isinstance(f, int) and not isinstance(f, bool) for f in filament_ids)):
        raise ApiError(400, "invalid_request", "filaments must be a list of filament ids.")
    if "codes" not in body and "filaments" not in body:
        raise ApiError(400, "invalid_request", "Send codes and/or filaments.")

    all_spools = (await load_spools())["spools"]
    spools = {s.get("short_code"): s for s in all_spools}
    params = label_params(load_label_settings())
    result = {"cached": [], "missing": [], "unknown": [],
              "filaments": {"cached": [], "missing": [], "unknown": []}}
    for code in codes:
        spool = spools.get(code)
        if spool is None:
            result["unknown"].append(code)
        else:
            result["cached" if label_cache_path(spool, params).exists() else "missing"].append(code)
    for fid in filament_ids:
        source = filament_source_spool(all_spools, fid)
        if source is None:
            result["filaments"]["unknown"].append(fid)
        else:
            result["filaments"]["cached" if filament_label_path(source, fid).exists() else "missing"].append(fid)
    return result


# ── filament labels ───────────────────────────────────────────────────────────
#
# The API only makes spool labels. A filament label is the same layout with a
# QR code for the filament instead of the spool, so we take a spool label of
# that filament and swap its QR block. This depends on the SVG's layout, which
# isn't a documented API: if it changes, we refuse rather than guess.

QR_BLOCK_RE = re.compile(r'<svg x="250" y="150" width="140" height="140" viewBox="0 0 (\d+) \1">.*?</svg>', re.S)


def qr_block(url: str) -> str:
    """A QR code drawn like the site's own: one horizontal stroke per run of dark modules."""
    qr = segno.make(url, error="L", boost_error=False, micro=False)
    runs = []
    for y, row in enumerate(qr.matrix):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                runs.append(f"M{start} {y}.5h{x - start}")
            else:
                x += 1
    n = len(qr.matrix)
    return (f'<svg x="250" y="150" width="140" height="140" viewBox="0 0 {n} {n}">'
            f'<path stroke="black" d="{"".join(runs)}"/></svg>')


def make_filament_label(spool_svg: bytes, filament_id: int) -> bytes:
    svg = spool_svg.decode("utf-8")
    matches = QR_BLOCK_RE.findall(svg)
    if len(matches) != 1:
        raise ApiError(502, "label_layout_changed",
                       "The spool label layout has changed, so SpoolPrint can't turn it into a filament label safely.")
    return QR_BLOCK_RE.sub(lambda _: qr_block(FILAMENT_URL.format(id=filament_id)), svg, count=1).encode("utf-8")


@app.get("/api/filament-label/{filament_id}")
async def get_filament_label(filament_id: int, refresh: bool = False):
    source = filament_source_spool((await load_spools())["spools"], filament_id)
    if source is None:
        raise ApiError(404, "unknown_filament", f"No spool of filament {filament_id} is in the spool list. Try Refresh.")

    params = label_params(load_label_settings(filament=True))
    path = filament_label_path(source, filament_id)
    async with app.state.label_locks.setdefault(f"f{filament_id}", asyncio.Lock()):
        if refresh or not path.exists():
            r = await upstream_get(f"/my/spools/{source['short_code']}/label", params)
            write_atomic(path, make_filament_label(r.content, filament_id))
            for old in path.parent.glob(f"f{filament_id}-*.svg"):
                if old != path:
                    old.unlink(missing_ok=True)
        body = path.read_bytes()

    return Response(body, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache", "Content-Security-Policy": LABEL_CSP})


@app.get("/api/label/{short_code}")
async def get_label(short_code: str, refresh: bool = False):
    if not SHORT_CODE_RE.fullmatch(short_code):
        raise ApiError(400, "invalid_short_code", "Spool codes are letters and digits only.")
    cached = await load_spools()
    spool = next((s for s in cached["spools"] if s.get("short_code") == short_code), None)
    if spool is None:
        raise ApiError(404, "unknown_spool", f"Spool {short_code} is not in the spool list. Try Refresh.")

    params = label_params(load_label_settings())
    path = label_cache_path(spool, params)
    labels_dir = path.parent

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
