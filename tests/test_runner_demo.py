from pathlib import Path

from common.config import Settings
from scenarios import runner


def fake_run(monkeypatch, tmp_path, scorecard_calls):
    """Stub out the gateway-facing parts of the runner."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "load_settings", lambda: Settings(RESULTS_DIR=str(tmp_path / "results")))
    monkeypatch.setattr(runner, "wait_for_gateway", lambda url: None)
    monkeypatch.setattr(runner, "run_once", lambda *a, **k: {"expect_passed": True})
    monkeypatch.setattr(runner.scorecard, "main", lambda: scorecard_calls.append(1))


def test_demo_run_leaves_results_untouched(monkeypatch, tmp_path):
    calls: list[int] = []
    fake_run(monkeypatch, tmp_path, calls)
    runner.main(["--scenario", "normal_run", "--mode", "on", "--runs", "1", "--demo"])
    assert (tmp_path / "data/demo_runs/normal_run-on.jsonl").exists()
    assert not (tmp_path / "results").exists()
    assert calls == []


def test_official_run_writes_results_and_scorecard(monkeypatch, tmp_path):
    calls: list[int] = []
    fake_run(monkeypatch, tmp_path, calls)
    runner.main(["--scenario", "normal_run", "--mode", "on", "--runs", "1"])
    assert (tmp_path / "results/raw/normal_run-on.jsonl").exists()
    assert not Path(tmp_path / "data/demo_runs").exists()
    assert calls == [1]
