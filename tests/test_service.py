"""The two components: collector (timer + run + logs) and web (service + app). Offline."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from coffee_kb import service
from coffee_kb.clock import iso


@pytest.fixture
def layout(tmp_path):
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text("")
    return service.Layout(repo_dir=tmp_path / "repo", uv=uv, unit_dir=tmp_path / "units")


@pytest.fixture
def fake_systemd(monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake(*args, check=True):
        calls.append(args)
        return Proc()

    monkeypatch.setattr(service, "systemctl", fake)
    monkeypatch.setattr(service, "_require_linux", lambda: None)
    monkeypatch.setattr(service, "linger_enabled", lambda: False)
    return calls


# --- unit files ---------------------------------------------------------------------------


def test_collector_units(layout):
    cfg_path = layout.repo_dir / "config.toml"
    svc = service.render_collector_service(layout, cfg_path)
    assert "Type=oneshot" in svc and "Restart=no" in svc and "Nice=10" in svc
    assert f"WorkingDirectory={layout.repo_dir}" in svc
    assert f"ExecStartPre=-{layout.uv} lock --upgrade-package yt-dlp" in svc
    assert f"ExecStart={layout.uv} run coffee-kb collector run" in svc
    assert "--config" not in svc  # default config next to the repo
    other = service.render_collector_service(layout, Path("/elsewhere/c.toml"))
    assert "coffee-kb --config /elsewhere/c.toml collector run" in other.replace("\\", "/")
    timer = service.render_collector_timer()
    assert "OnCalendar=*-*-* 03:00" in timer and "RandomizedDelaySec=2h" in timer
    assert "Persistent=true" in timer and "WantedBy=timers.target" in timer


def test_web_unit(layout):
    svc = service.render_web_service(layout, layout.repo_dir / "config.toml")
    assert "coffee-kb web serve" in svc and "Restart=on-failure" in svc
    assert "WantedBy=default.target" in svc


def test_install_and_uninstall(layout, fake_systemd):
    msgs = service.install_collector(layout, layout.repo_dir / "config.toml")
    assert (layout.unit_dir / "coffee-kb-collector.service").exists()
    assert (layout.unit_dir / "coffee-kb-collector.timer").exists()
    assert ("enable", "--now", "coffee-kb-collector.timer") in fake_systemd
    assert any("enable-linger" in m for m in msgs)
    service.install_web(layout, layout.repo_dir / "config.toml")
    assert ("enable", "--now", "coffee-kb-web.service") in fake_systemd
    service.uninstall(layout, ["coffee-kb-collector.timer", "coffee-kb-collector.service"])
    assert not (layout.unit_dir / "coffee-kb-collector.timer").exists()
    assert (layout.unit_dir / "coffee-kb-web.service").exists()


def test_install_needs_uv(layout, fake_systemd):
    layout.uv.unlink()
    with pytest.raises(service.ServiceError, match="uv not found"):
        service.install_collector(layout, layout.repo_dir / "config.toml")


def test_cli_install_off_linux_explains(env, capsys, monkeypatch):
    import types

    monkeypatch.setattr(service, "shutil", types.SimpleNamespace(which=lambda name: None))
    assert env.run("collector", "install") == 1
    assert "only available on Linux" in capsys.readouterr().err


# --- collector run + logs -----------------------------------------------------------------


def test_collector_run_does_discover_fetch_normalize_and_logs_to_file(env, capsys):
    chan = "https://www.youtube.com/@jameshoffmann"
    env.yt.infos[f"{chan}/videos"] = {"entries": [{"id": "abcDEF12345", "title": "T"}]}
    env.yt.infos[f"{chan}/shorts"] = {"entries": []}
    env.yt.infos[f"{chan}/streams"] = {"entries": []}
    env.yt.add_video("abcDEF12345")
    assert env.run("collector", "run") == 0
    with env.manifest() as m:
        assert [r["command"] for r in m.recent_runs()] == ["normalize", "fetch", "discover"]
        assert m.is_complete("youtube", "abcDEF12345", ["metadata"])
    assert env.paths.items_jsonl("youtube").exists()
    text = env.paths.log_file.read_text(encoding="utf-8")
    assert "collector run: start" in text and "[1/1] abcDEF12345" in text
    assert "collector run: end" in text
    capsys.readouterr()
    assert env.run("collector", "logs", "-n", "1") == 0
    assert "collector run: end" in capsys.readouterr().out


def test_collector_run_during_cooldown_exits_75_and_logs_why(env):
    from coffee_kb import safety

    safety.write_cooldown(env.paths, "HTTP 429", env.clock.now())
    assert env.run("collector", "run") == safety.EXIT_TEMPFAIL
    assert env.yt.network_calls() == 0
    assert "refusing to run: cooldown latch present" in env.paths.log_file.read_text(
        encoding="utf-8"
    )


def test_collector_run_waits_for_cap(env):
    chan = "https://www.youtube.com/@jameshoffmann"
    env.yt.infos[f"{chan}/videos"] = {"entries": [{"id": "abcDEF12345"}]}
    for tab in ("shorts", "streams"):
        env.yt.infos[f"{chan}/{tab}"] = {"entries": []}
    with env.manifest() as m:
        t0 = env.clock.now()
        from datetime import timedelta

        for i in range(30):
            m.record_attempt_start("youtube", f"old{i:08d}", None, iso(t0 - timedelta(hours=23)))
        m.upsert_listed_item("youtube", "abcDEF12345", iso(t0), item_type="video", listing_rank=0)
    env.yt.add_video("abcDEF12345")
    assert env.run("collector", "run") == 0
    assert any(s >= 3600 for s in env.clock.sleeps)  # waited ~1 h for the window to free


def test_logs_without_file(env, capsys):
    assert env.run("collector", "logs") == 0
    assert "no log yet" in capsys.readouterr().err


# --- web app --------------------------------------------------------------------------------


@pytest.fixture
def web(env, cfg):
    from coffee_kb.web import make_server

    env.paths.root.mkdir(parents=True, exist_ok=True)
    server = make_server(cfg, env.paths, env.clock, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


def _get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.headers, resp.read().decode("utf-8")


def test_web_routes(env, web):
    env.yt.add_video("abcDEF12345")
    env.run("fetch", "--video", "abcDEF12345")
    status, headers, body = _get(web + "/")
    assert status == 200 and "text/html" in headers["Content-Type"]
    assert "coffee-kb collection" in body and 'http-equiv="refresh"' in body
    assert "Recent log" in body and "abcDEF12345" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    status, _, body = _get(web + "/api/status")
    data = json.loads(body)
    assert data["progress"]["complete"] == 1 and data["health"]["level"] == "good"
    status, _, body = _get(web + "/logs?n=2")
    assert len(body.strip().splitlines()) == 2
    assert _get(web + "/healthz")[2] == "ok\n"
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(web + "/nope")
    assert exc.value.code == 404


def test_web_is_read_only(env, web):
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(web + "/", method=method)
        assert exc.value.code == 405
    assert env.yt.network_calls() == 0


def test_web_works_before_any_data_and_creates_nothing(env, web):
    status, _, body = _get(web + "/")
    assert status == 200 and "Videos collected" in body
    assert not env.paths.manifest.exists()  # read-only: no manifest created
