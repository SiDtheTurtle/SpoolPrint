"""Backend tests. The 3dfilamentprofiles API is faked, so no quota is used."""

import httpx2
import pytest
from fastapi.testclient import TestClient

import app as spoolprint

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="30mm"/>'


def spool(code, fid, color, updated="2026-10-14T18:02:11.000Z", brand="Bambu Lab", material="PLA"):
    return {
        "short_code": code, "filament_id": fid, "brand": brand, "material": material,
        "material_type": "Basic", "color": color, "rgb": "#000000", "location": "Shelf A",
        "remaining_grams": 740, "notes": None, "td_value": 1.2, "updated_at": updated,
        "spool_url": f"https://spooldb.com/s/{code}",
    }


class FakeApi:
    """Records calls and answers like the real API."""

    def __init__(self):
        self.spools = [
            spool("BBB222", 101, "Charcoal Black"),
            spool("AAA111", 101, "Charcoal Black"),
            spool("CCC333", 202, "Jade White"),
            spool("DDD444", 303, "Blue", brand="Anycubic"),
        ]
        self.calls: list[httpx2.Request] = []
        self.fail: tuple[int, dict | bytes] | None = None
        self.remaining = 1000
        self.label_svg = SVG

    def paths(self):
        return [r.url.path for r in self.calls]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        self.remaining -= 1
        headers = {"X-RateLimit-Limit": "1500", "X-RateLimit-Remaining": str(self.remaining)}
        if self.fail:
            status, body = self.fail
            if isinstance(body, bytes):
                return httpx2.Response(status, content=body, headers=headers)
            return httpx2.Response(status, json=body, headers=headers)
        path = request.url.path
        if path == "/api/v1/my/spools":
            return httpx2.Response(200, json={"data": self.spools, "as_of": "x"}, headers=headers)
        if path.startswith("/api/v1/my/spools/") and path.endswith("/label"):
            return httpx2.Response(200, content=self.label_svg, headers={**headers, "Content-Type": "image/svg+xml"})
        return httpx2.Response(404, json={"error": {"code": "not_found", "message": "nope"}}, headers=headers)


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(spoolprint, "API_KEY", "3dfp_test")
    monkeypatch.setattr(spoolprint, "DATA_DIR", tmp_path)
    monkeypatch.setattr(spoolprint, "UPSTREAM_TRANSPORT", httpx2.MockTransport(fake.handler))
    return fake


@pytest.fixture
def client(api):
    with TestClient(spoolprint.app) as c:
        yield c


def test_spools_are_a_flat_list_with_filament_and_purchase_date(client, api):
    api.spools[0]["spool_properties"] = {"purchase_date": "2026-09-30", "purchase_price": 19.99}
    body = client.get("/api/spools").json()
    assert body["spool_count"] == 4
    first = body["spools"][0]
    assert first["short_code"] == "BBB222"
    assert first["filament_id"] == 101
    assert first["color"] == "Charcoal Black"
    assert first["purchase_date"] == "2026-09-30"
    assert "purchase_price" not in first
    assert body["spools"][1]["purchase_date"] is None
    assert set(first) == {*spoolprint.SPOOL_FIELDS, "filament_id", *spoolprint.FILAMENT_FIELDS, "purchase_date"}


def test_spool_list_is_cached_until_refresh(client, api):
    client.get("/api/spools")
    client.get("/api/spools")
    assert api.paths() == ["/api/v1/my/spools"]
    client.get("/api/spools?refresh=true")
    assert len(api.calls) == 2


def test_sends_bearer_key(client, api):
    client.get("/api/spools")
    assert api.calls[0].headers["Authorization"] == "Bearer 3dfp_test"


def test_label_is_fetched_once_then_cached(client, api):
    r1 = client.get("/api/label/AAA111")
    r2 = client.get("/api/label/AAA111")
    assert r1.status_code == r2.status_code == 200
    assert r1.headers["content-type"] == "image/svg+xml"
    assert "default-src 'none'" in r1.headers["content-security-policy"]
    assert r1.content == SVG
    label_calls = [c for c in api.calls if c.url.path.endswith("/label")]
    assert len(label_calls) == 1
    assert dict(label_calls[0].url.params) == {
        "format": "svg", "size": "1", "slots": "nozzleTemp,bedTemp,flowRatio,tdValue",
        "show_rgb": "true", "rgb_swatch": "false", "qr": "detail",
    }


def test_label_refetched_when_spool_changes(client, api, tmp_path):
    client.get("/api/label/AAA111")
    api.spools[1] = spool("AAA111", 101, "Charcoal Black", updated="2026-11-20T09:00:00.000Z")
    client.get("/api/spools?refresh=true")
    client.get("/api/label/AAA111")
    assert len([c for c in api.calls if c.url.path.endswith("/label")]) == 2
    assert len(list((tmp_path / "labels").glob("*/AAA111-*.svg"))) == 1


def test_label_refresh_forces_fetch(client, api):
    client.get("/api/label/AAA111")
    client.get("/api/label/AAA111?refresh=true")
    assert len([c for c in api.calls if c.url.path.endswith("/label")]) == 2


def test_unknown_spool_does_not_call_label_api(client, api):
    r = client.get("/api/label/ZZZ999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_spool"
    assert not any(c.url.path.endswith("/label") for c in api.calls)


def test_invalid_short_code_rejected(client, api):
    r = client.get("/api/label/AB$12")
    assert r.status_code == 400
    assert api.calls == []


@pytest.mark.parametrize("status,code", [(401, "invalid_key"), (402, "no_access"), (429, "rate_limited")])
def test_upstream_errors_pass_through_with_friendly_message(client, api, status, code):
    api.fail = (status, {"error": {"code": code, "message": "raw"}})
    r = client.get("/api/spools")
    assert r.status_code == status
    assert r.json()["error"] == {"code": code, "message": spoolprint.FRIENDLY_ERRORS[code]}


def test_upstream_server_error_becomes_502(client, api):
    api.fail = (500, b"<html>oops</html>")
    r = client.get("/api/spools")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


def test_network_failure_becomes_502(client, api, monkeypatch):
    def boom(request):
        raise httpx2.ConnectError("down")
    monkeypatch.setattr(spoolprint, "UPSTREAM_TRANSPORT", httpx2.MockTransport(boom))
    r = client.get("/api/spools")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unreachable"


def test_missing_key_never_calls_api(client, api, monkeypatch):
    monkeypatch.setattr(spoolprint, "API_KEY", "")
    r = client.get("/api/spools")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "no_api_key"
    assert api.calls == []


def test_status_reports_quota_and_fetch_time(client, api):
    assert client.get("/api/status").json()["quota"] is None
    client.get("/api/spools")
    status = client.get("/api/status").json()
    assert status["quota"]["limit"] == 1500
    assert status["quota"]["remaining"] == 999
    assert status["spools_fetched_at"]


# ── label settings ───────────────────────────────────────────────────────────

CUSTOM = {
    "slots": ["nozzleTemp", "bedTemp", "dryingInfo", "location"],
    "show_rgb": True, "rgb_swatch": True, "rgb_field": "", "qr": "edit",
}


def label_calls(api):
    return [c for c in api.calls if c.url.path.endswith("/label")]


def test_settings_default_and_options(client, api):
    body = client.get("/api/settings").json()
    assert body["label"] == spoolprint.DEFAULT_LABEL_SETTINGS
    assert body["options"]["slots"]["dryingInfo"] == "Drying Info (Time & Temp)"
    assert api.calls == []


def test_saved_settings_shape_label_request(client, api):
    assert client.put("/api/settings", json={"label": CUSTOM}).status_code == 200
    assert client.get("/api/settings").json()["label"] == CUSTOM
    client.get("/api/label/AAA111")
    params = dict(label_calls(api)[0].url.params)
    assert params["slots"] == "nozzleTemp,bedTemp,dryingInfo,location"
    assert params["rgb_swatch"] == "true"
    assert params["qr"] == "edit"
    assert "rgb_field" not in params


def test_rgb_field_is_sent_when_set(client, api):
    client.put("/api/settings", json={"label": {**CUSTOM, "rgb_field": "shortCode"}})
    client.get("/api/label/AAA111")
    assert dict(label_calls(api)[0].url.params)["rgb_field"] == "shortCode"


def test_changing_settings_refetches_and_switching_back_is_free(client, api):
    client.get("/api/label/AAA111")
    client.put("/api/settings", json={"label": CUSTOM})
    client.get("/api/label/AAA111")
    client.put("/api/settings", json={"label": spoolprint.DEFAULT_LABEL_SETTINGS})
    client.get("/api/label/AAA111")
    assert len(label_calls(api)) == 2


@pytest.mark.parametrize("bad", [
    {**CUSTOM, "slots": ["nozzleTemp", "bedTemp", "dryingInfo"]},
    {**CUSTOM, "slots": ["nozzleTemp", "bedTemp", "dryingInfo", "bogus"]},
    {**CUSTOM, "show_rgb": "yes"},
    {**CUSTOM, "rgb_field": "none"},
    {**CUSTOM, "rgb_field": "bogus"},
    {**CUSTOM, "qr": "somewhere"},
    "not an object",
])
def test_invalid_settings_rejected_and_not_saved(client, api, bad):
    r = client.put("/api/settings", json={"label": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_settings"
    assert client.get("/api/settings").json()["label"] == spoolprint.DEFAULT_LABEL_SETTINGS


def test_legacy_flat_label_cache_removed_on_start(api, tmp_path):
    (tmp_path / "labels").mkdir()
    (tmp_path / "labels" / "AAA111-abc.svg").write_bytes(SVG)
    with TestClient(spoolprint.app):
        pass
    assert not (tmp_path / "labels" / "AAA111-abc.svg").exists()


# ── printer settings ─────────────────────────────────────────────────────────

B1 = {"density": 4, "direction": "top", "flip": True, "offset_x": -0.6, "offset_y": 0.25}


def test_unknown_printer_gets_defaults_unsaved(client, api):
    body = client.get("/api/printers/B1-H1234").json()
    assert body == {"name": "B1-H1234", "saved": False, "settings": spoolprint.DEFAULT_PRINTER_SETTINGS}


def test_printer_settings_saved_per_printer_and_snapped_to_dots(client, api):
    r = client.put("/api/printers/B1-H1234", json=B1)
    assert r.status_code == 200
    assert r.json()["settings"]["offset_x"] == -0.625  # nearest whole dot
    assert client.get("/api/printers/B1-H1234").json()["saved"] is True
    assert client.get("/api/printers/B1-H1234").json()["settings"]["density"] == 4
    assert client.get("/api/printers/B1-OTHER").json()["saved"] is False


def test_label_and_printer_settings_do_not_overwrite_each_other(client, api):
    client.put("/api/printers/B1-H1234", json=B1)
    client.put("/api/settings", json={"label": CUSTOM})
    assert client.get("/api/printers/B1-H1234").json()["saved"] is True
    client.put("/api/printers/B1-H1234", json={**B1, "density": 2})
    assert client.get("/api/settings").json()["label"] == CUSTOM


@pytest.mark.parametrize("bad", [
    {**B1, "density": 6}, {**B1, "density": "3"}, {**B1, "density": True},
    {**B1, "direction": "right"}, {**B1, "flip": 1},
    {**B1, "offset_x": 5.5}, {**B1, "offset_y": "1"}, [],
])
def test_invalid_printer_settings_rejected(client, api, bad):
    r = client.put("/api/printers/B1-H1234", json=bad)
    assert r.status_code == 400
    assert client.get("/api/printers/B1-H1234").json()["saved"] is False


def test_invalid_printer_name_rejected(client, api):
    assert client.get("/api/printers/bad%2Fname").status_code in (400, 404)
    assert client.put("/api/printers/" + "x" * 65, json=B1).status_code == 400


# ── batch cache status ───────────────────────────────────────────────────────

def test_cache_status_reports_cached_missing_and_unknown(client, api):
    client.get("/api/label/AAA111")
    calls_before = len(api.calls)
    r = client.post("/api/labels/cache-status", json={"codes": ["AAA111", "BBB222", "NOPE99"]})
    assert r.json() == {"cached": ["AAA111"], "missing": ["BBB222"], "unknown": ["NOPE99"],
                        "filaments": {"cached": [], "missing": [], "unknown": []}}
    assert len(api.calls) == calls_before  # checking costs nothing


def test_cache_status_follows_label_settings(client, api):
    client.get("/api/label/AAA111")
    client.put("/api/settings", json={"label": CUSTOM})
    r = client.post("/api/labels/cache-status", json={"codes": ["AAA111"]})
    assert r.json()["missing"] == ["AAA111"]


@pytest.mark.parametrize("bad", [{}, {"codes": "AAA111"}, {"codes": [1, 2]}, {"filaments": ["x"]}, []])
def test_cache_status_rejects_bad_body(client, api, bad):
    assert client.post("/api/labels/cache-status", json=bad).status_code == 400


# ── filament labels ──────────────────────────────────────────────────────────

import re
from pathlib import Path

import zxingcpp
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"
SPOOL_LABEL = (FIXTURES / "spool_label.svg").read_bytes()        # QR with circle corner markers
FILAMENT_LABEL = (FIXTURES / "filament_label.svg").read_bytes()  # a real filament label from the site


def decode_qr(svg: bytes) -> str | None:
    """Rebuild the QR block's module grid from its path and scan it."""
    block = re.search(r'<svg x="250"[^>]*viewBox="0 0 (\d+) \1"[^>]*>(.*?)</svg>', svg.decode(), re.S)
    n = int(block.group(1))
    d = re.search(r' d="([^"]+)"', block.group(2)).group(1)
    scale, quiet = 8, 4
    img = Image.new("L", ((n + 2 * quiet) * scale,) * 2, 255)
    for x, y, w in re.findall(r"M(\d+) (\d+)\.5h(\d+)", d):
        x, y, w = int(x), int(y), int(w)
        img.paste(0, ((x + quiet) * scale, (y + quiet) * scale, (x + w + quiet) * scale, (y + quiet + 1) * scale))
    found = zxingcpp.read_barcodes(img)
    return found[0].text if found else None


def filament_label_calls(api):
    return [c for c in api.calls if c.url.path.endswith("/label")]


def test_site_filament_labels_point_at_spooldb():
    # The fixture is the site's own filament label for filament 129.
    assert decode_qr(FILAMENT_LABEL) == "https://spooldb.com/f/129"


def test_filament_label_swaps_qr_and_keeps_layout(client, api):
    api.label_svg = SPOOL_LABEL
    r = client.get("/api/filament-label/101")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    svg = r.content
    assert decode_qr(svg) == "https://spooldb.com/f/101"
    assert b"<circle" not in svg                       # spool-style corner markers gone
    assert b">PLA Basic</text>" in svg                 # rest of the label untouched
    assert svg.count(b'<svg x="250" y="150"') == 1
    call = filament_label_calls(api)[0]
    assert call.url.path == "/api/v1/my/spools/AAA111/label"  # lowest code of filament 101
    assert dict(call.url.params) == {
        "format": "svg", "size": "1", "slots": "nozzleTemp,bedTemp,none,none",
        "show_rgb": "false", "rgb_swatch": "false", "qr": "detail",
    }


def test_filament_label_is_cached_and_follows_its_settings(client, api):
    api.label_svg = SPOOL_LABEL
    client.get("/api/filament-label/101")
    client.get("/api/filament-label/101")
    assert len(filament_label_calls(api)) == 1
    client.put("/api/settings", json={"filament_label": {
        "slots": ["nozzleTemp", "bedTemp", "dryingInfo", "none"], "show_rgb": True, "rgb_swatch": True, "rgb_field": ""}})
    client.get("/api/filament-label/101")
    assert len(filament_label_calls(api)) == 2
    # Spool label settings are separate, so the spool label cache is unaffected.
    client.get("/api/label/AAA111")
    client.get("/api/label/AAA111")
    assert len(filament_label_calls(api)) == 3


def test_filament_label_prefers_spool_without_overrides(client, api):
    api.label_svg = SPOOL_LABEL
    api.spools[1]["spool_properties"] = {"preferred_nozzle_temp": 250}  # AAA111
    client.get("/api/spools?refresh=true")
    client.get("/api/filament-label/101")
    assert filament_label_calls(api)[0].url.path == "/api/v1/my/spools/BBB222/label"


def test_unknown_filament_does_not_call_label_api(client, api):
    r = client.get("/api/filament-label/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_filament"
    assert filament_label_calls(api) == []


def test_changed_label_layout_is_refused_and_not_cached(client, api):
    api.label_svg = SVG  # no QR block where we expect one
    r = client.get("/api/filament-label/101")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "label_layout_changed"
    client.get("/api/filament-label/101")
    assert len(filament_label_calls(api)) == 2


@pytest.mark.parametrize("bad", [
    {"slots": ["nozzleTemp", "bedTemp", "location", "none"], "show_rgb": False, "rgb_swatch": False, "rgb_field": ""},
    {"slots": ["nozzleTemp", "bedTemp", "none", "none"], "show_rgb": True, "rgb_swatch": False, "rgb_field": "shortCode"},
])
def test_filament_label_settings_reject_spool_only_values(client, api, bad):
    r = client.put("/api/settings", json={"label": CUSTOM, "filament_label": bad})
    assert r.status_code == 400
    # Nothing is saved when any part is invalid.
    assert client.get("/api/settings").json()["label"] == spoolprint.DEFAULT_LABEL_SETTINGS


def test_settings_put_needs_a_section(client, api):
    assert client.put("/api/settings", json={}).status_code == 400


def test_cache_status_covers_filaments(client, api):
    api.label_svg = SPOOL_LABEL
    client.get("/api/filament-label/101")
    r = client.post("/api/labels/cache-status", json={"codes": ["CCC333"], "filaments": [101, 202, 999]})
    assert r.json() == {"cached": [], "missing": ["CCC333"], "unknown": [],
                        "filaments": {"cached": [101], "missing": [202], "unknown": [999]}}


# ── view settings ────────────────────────────────────────────────────────────

def test_view_settings_default_to_filament_grouping(client, api):
    s = client.get("/api/settings").json()
    assert s["view"] == {"group": "filament", "hide_empty": True}
    assert s["defaults"]["view"] == s["view"]
    assert list(s["options"]["group"]) == ["none", "filament", "material"]


def test_view_settings_save_without_touching_label_settings(client, api):
    r = client.put("/api/settings", json={"view": {"group": "material", "hide_empty": False}})
    assert r.status_code == 200
    assert r.json()["view"] == {"group": "material", "hide_empty": False}
    assert r.json()["label"] == spoolprint.DEFAULT_LABEL_SETTINGS
    assert client.get("/api/settings").json()["view"]["group"] == "material"


@pytest.mark.parametrize("bad", [
    {"group": "brand", "hide_empty": True},
    {"group": "none", "hide_empty": "yes"},
    {"group": "none"},
    ["none"],
])
def test_invalid_view_settings_are_rejected(client, api, bad):
    assert client.put("/api/settings", json={"view": bad}).status_code == 400
    assert client.get("/api/settings").json()["view"] == {"group": "filament", "hide_empty": True}
