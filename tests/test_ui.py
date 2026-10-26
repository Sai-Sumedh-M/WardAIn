import json


def test_ui_served_as_html(gw_http):
    r = gw_http.get("/ui")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "WardAIn" in r.text


def test_scorecard_requires_admin(gw_http):
    assert gw_http.get("/admin/scorecard").status_code == 401
    assert gw_http.get("/admin/scorecard", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_scorecard_missing_returns_404(gw_http):
    r = gw_http.get("/admin/scorecard", headers={"X-Admin-Token": "test-admin"})
    assert r.status_code == 404
    assert r.json() == {"error": "no_scorecard"}


def test_scorecard_served_from_results_dir(tmp_settings, tools_http, tmp_path):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    (tmp_path / "scorecard.json").write_text(json.dumps({"metrics": [], "scenarios": []}))
    app = create_app(replace(tmp_settings, RESULTS_DIR=str(tmp_path)), tools_http=tools_http)
    r = TestClient(app).get("/admin/scorecard", headers={"X-Admin-Token": "test-admin"})
    assert r.status_code == 200 and r.json() == {"metrics": [], "scenarios": []}
