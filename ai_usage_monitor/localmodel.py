"""Local model (MLX Qwen) status and start/stop for the menu-bar dropdown.

The model itself is run by a separate project directory holding ``start.sh``
(model server on :8080 plus a LiteLLM proxy on :4000, model picked with the
``MODEL`` env var) and ``stop.sh``. This module only observes the processes
and shells out to those scripts; the directory comes from the
``AI_USAGE_LOCAL_MODEL_DIR`` env var, which ``install-menubar`` bakes into the
SwiftBar wrapper.
"""
import glob
import json
import os
import re
import socket
import subprocess

ENV_DIR = "AI_USAGE_LOCAL_MODEL_DIR"
SERVER_PORT = 8080
PROXY_PORT = 4000
# Matches both `mlx_lm.server --model X` and `python -m mlx_vlm.server --model X`.
SERVER_PATTERN = r"mlx_(lm|vlm)\.server --model"
# HF cache repos offered in the Start submenu.
MODEL_GLOB = "models--*--*Qwen3.8-27B*"
MODEL_PREFIX = "Qwen3.8-27B-"
# SwiftBar runs plugins with a minimal PATH; start.sh needs lsof from /usr/sbin.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def model_dir():
    path = os.environ.get(ENV_DIR)
    return path if path and os.path.isdir(path) else None


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _footprint_gb(pid):
    """Physical footprint in GB (RSS misses MLX's Metal allocations)."""
    try:
        out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Footprint:\s*([\d.]+)\s*([KMG])B", out)
    if not m:
        return None
    return float(m.group(1)) / {"K": 1024 ** 2, "M": 1024, "G": 1}[m.group(2)]


def _server_process():
    """Return (pid, model repo id) of the running model server, or (None, None)."""
    out = subprocess.run(["pgrep", "-fl", SERVER_PATTERN],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.match(r"(\d+) .*--model (\S+)", line)
        if m:
            return int(m.group(1)), m.group(2)
    return None, None


def status():
    """Snapshot of the local model: state is 'stopped', 'starting' or 'running'."""
    pid, model = _server_process()
    if pid is None:
        state = "stopped"
    else:
        # mlx_lm.server loads the weights before it starts listening.
        state = "running" if _port_open(SERVER_PORT) else "starting"
    return {
        "state": state,
        "model": model,
        "memory_gb": _footprint_gb(pid) if pid else None,
        "proxy": _port_open(PROXY_PORT),
    }


def _snapshot_complete(snapshot):
    if not os.path.isfile(os.path.join(snapshot, "config.json")):
        return False
    index = os.path.join(snapshot, "model.safetensors.index.json")
    if os.path.isfile(index):
        with open(index, encoding="utf-8") as f:
            shards = set(json.load(f).get("weight_map", {}).values())
    else:
        shards = {"model.safetensors"}
    # Shards still downloading have no snapshot symlink yet.
    return bool(shards) and all(os.path.isfile(os.path.join(snapshot, s))
                                for s in shards)


def hf_cache_dir():
    hub = os.environ.get("HF_HUB_CACHE")
    if hub:
        return hub
    home = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
    return os.path.join(home, "hub")


def available_models(cache=None):
    """Repo ids of fully downloaded models matching MODEL_GLOB, sorted."""
    repos = []
    for repo_dir in glob.glob(os.path.join(cache or hf_cache_dir(), MODEL_GLOB)):
        snapshots = glob.glob(os.path.join(repo_dir, "snapshots", "*"))
        if any(_snapshot_complete(s) for s in snapshots):
            org, name = os.path.basename(repo_dir)[len("models--"):].split("--", 1)
            repos.append("%s/%s" % (org, name))
    return sorted(repos)


def short_name(repo):
    """'lmstudio-community/Qwen3.8-27B-MLX-8bit' -> 'MLX-8bit'."""
    name = repo.split("/", 1)[-1]
    return name[len(MODEL_PREFIX):] if name.startswith(MODEL_PREFIX) else repo


def _run_script(name, env_extra=None):
    path = model_dir()
    if path is None:
        print("%s is not set to a directory" % ENV_DIR)
        return 1
    env = dict(os.environ, **(env_extra or {}))
    env["PATH"] = env.get("PATH", "") + ":" + SYSTEM_PATH
    return subprocess.run([os.path.join(path, name)], cwd=path, env=env).returncode


def start(model=None):
    return _run_script("start.sh", {"MODEL": model} if model else None)


def stop():
    return _run_script("stop.sh")
