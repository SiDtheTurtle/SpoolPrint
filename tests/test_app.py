"""Backend tests. The 3dfilamentprofiles API is faked, so no quota is used."""

import httpx
import pytest
from fastapi.testclient import TestClient

import app as niimbatch

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
        self.calls: list[httpx.Request] = []
        self.fail: tuple[int, dict | bytes] | None = None
        self.remaining = 1000

    def paths(self):
        return [r.url.path for r in self.calls]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.remaining -= 1
        headers = {"X-RateLimit-Limit": "1500", "X-RateLimit-Remaining": str(self.remaining)}
        if self.fail:
            status, body = self.fail
            if isinstance(body, bytes):
                return httpx.Response(status, content=body, headers=headers)
            return httpx.Response(status, json=body, headers=headers)
        path = request.url.path
        if path == "/api/v1/my/spools":
            return httpx.Response(200, json={"data": self.spools, "as_of": "x"}, headers=headers)
        if path.startswith("/api/v1/my/spools/") and path.endswith("/label"):
            return httpx.Response(200, content=SVG, headers={**headers, "Content-Type": "image/svg+xml"})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "nope"}}, headers=headers)


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(niimbatch, "API_KEY", "3dfp_test")
    monkeypatch.setattr(niimbatch, "DATA_DIR", tmp_path)
    monkeypatch.setattr(niimbatch, "UPSTREAM_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def client(api):
    with TestClient(niimbatch.app) as c:
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
    assert set(first) == {*niimbatch.SPOOL_FIELDS, "filament_id", *niimbatch.FILAMENT_FIELDS, "purchase_date"}


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
    assert r.json()["error"] == {"code": code, "message": niimbatch.FRIENDLY_ERRORS[code]}


def test_upstream_server_error_becomes_502(client, api):
    api.fail = (500, b"<html>oops</html>")
    r = client.get("/api/spools")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


def test_network_failure_becomes_502(client, api, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(niimbatch, "UPSTREAM_TRANSPORT", httpx.MockTransport(boom))
    r = client.get("/api/spools")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unreachable"


def test_missing_key_never_calls_api(client, api, monkeypatch):
    monkeypatch.setattr(niimbatch, "API_KEY", "")
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
    assert body["label"] == niimbatch.DEFAULT_LABEL_SETTINGS
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
    client.put("/api/settings", json={"label": niimbatch.DEFAULT_LABEL_SETTINGS})
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
    assert client.get("/api/settings").json()["label"] == niimbatch.DEFAULT_LABEL_SETTINGS


def test_legacy_flat_label_cache_removed_on_start(api, tmp_path):
    (tmp_path / "labels").mkdir()
    (tmp_path / "labels" / "AAA111-abc.svg").write_bytes(SVG)
    with TestClient(niimbatch.app):
        pass
    assert not (tmp_path / "labels" / "AAA111-abc.svg").exists()


# ── printer settings ─────────────────────────────────────────────────────────

B1 = {"density": 4, "direction": "top", "flip": True, "offset_x": -0.6, "offset_y": 0.25}


def test_unknown_printer_gets_defaults_unsaved(client, api):
    body = client.get("/api/printers/B1-H1234").json()
    assert body == {"name": "B1-H1234", "saved": False, "settings": niimbatch.DEFAULT_PRINTER_SETTINGS}


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
