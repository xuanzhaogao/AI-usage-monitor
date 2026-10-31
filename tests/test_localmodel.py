import json

from ai_usage_monitor import localmodel


def make_snapshot(cache, repo, shards, present):
    org, name = repo.split("/")
    snap = cache / ("models--%s--%s" % (org, name)) / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    (snap / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"w%d" % i: s for i, s in enumerate(shards)}}))
    for s in present:
        (snap / s).write_text("weights")


def test_available_models_lists_only_complete_downloads(tmp_path):
    shards = ["model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"]
    make_snapshot(tmp_path, "lmstudio-community/Qwen3.8-27B-MLX-8bit", shards, shards)
    make_snapshot(tmp_path, "lmstudio-community/Qwen3.8-27B-MLX-6bit", shards, shards[:1])
    make_snapshot(tmp_path, "unsloth/Qwen3.5-9B-MLX", shards, shards)
    assert localmodel.available_models(str(tmp_path)) == [
        "lmstudio-community/Qwen3.8-27B-MLX-8bit"]


def test_short_name():
    assert localmodel.short_name("lmstudio-community/Qwen3.8-27B-MLX-6bit") == "MLX-6bit"
    assert localmodel.short_name("mlx-community/Qwen3.8-27B-4bit") == "4bit"
    assert localmodel.short_name("someone/Other") == "someone/Other"


class Result:
    def __init__(self, stdout):
        self.stdout = stdout


def test_status_running(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return Result("75494 /venv/bin/python .venv/bin/mlx_lm.server --model "
                          "lmstudio-community/Qwen3.8-27B-MLX-8bit --host 127.0.0.1\n")
        return Result("python [75494]: 64-bit    Footprint: 38 GB (16384 bytes per page)\n")
    monkeypatch.setattr(localmodel.subprocess, "run", fake_run)
    monkeypatch.setattr(localmodel, "_port_open", lambda port: True)
    assert localmodel.status() == {
        "state": "running", "model": "lmstudio-community/Qwen3.8-27B-MLX-8bit",
        "memory_gb": 38.0, "proxy": True}


def test_status_starting_until_the_server_listens(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return Result("123 python -m mlx_vlm.server --model org/Qwen3.8-27B-MLX-6bit\n")
        return Result("python [123]: 64-bit    Footprint: 512 MB\n")
    monkeypatch.setattr(localmodel.subprocess, "run", fake_run)
    monkeypatch.setattr(localmodel, "_port_open", lambda port: False)
    s = localmodel.status()
    assert s["state"] == "starting"
    assert s["memory_gb"] == 0.5


def test_status_stopped(monkeypatch):
    monkeypatch.setattr(localmodel.subprocess, "run", lambda cmd, **k: Result(""))
    monkeypatch.setattr(localmodel, "_port_open", lambda port: False)
    assert localmodel.status() == {"state": "stopped", "model": None,
                                   "memory_gb": None, "proxy": False}


def test_start_runs_start_script_with_model(tmp_path, monkeypatch):
    (tmp_path / "start.sh").write_text("")
    calls = {}

    class Done:
        returncode = 0

    def fake_run(cmd, cwd=None, env=None):
        calls.update(cmd=cmd, cwd=cwd, env=env)
        return Done()

    monkeypatch.setenv(localmodel.ENV_DIR, str(tmp_path))
    monkeypatch.setattr(localmodel.subprocess, "run", fake_run)
    assert localmodel.start("org/Qwen3.8-27B-MLX-6bit") == 0
    assert calls["cmd"] == [str(tmp_path / "start.sh")]
    assert calls["cwd"] == str(tmp_path)
    assert calls["env"]["MODEL"] == "org/Qwen3.8-27B-MLX-6bit"
    assert "/usr/sbin" in calls["env"]["PATH"]


def test_start_without_dir_fails(monkeypatch):
    monkeypatch.delenv(localmodel.ENV_DIR, raising=False)
    assert localmodel.start() == 1
