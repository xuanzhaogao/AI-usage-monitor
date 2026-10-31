"""SwiftBar menu-bar plugin: output formatting and install/uninstall.

SwiftBar runs a plugin script on an interval (encoded in the filename, e.g.
``aiusage.5m.sh`` = every 5 minutes). Everything the script prints before a
line containing only ``---`` becomes the menu-bar text; lines after it become
the dropdown. ``| key=value`` suffixes style a line or attach an action.
This module produces that text (pure ``format_menubar``) and installs a thin
wrapper plugin that calls ``python3 -m ai_usage_monitor menubar``.
"""
import glob
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import db, localmodel

PLUGIN_NAME = "aiusage.1m.sh"
# Any interval variant we may have written before (aiusage.5m.sh, etc.); used
# to clear stale copies so a changed refresh interval never leaves duplicates.
PLUGIN_GLOB = "aiusage.*.sh"
DASHBOARD_URL = "http://127.0.0.1:8377"
STALE_MINUTES = 30
SWIFTBAR_DOMAIN = "com.ameba.SwiftBar"

WINDOW_ORDER = ("5h", "7d", "month")
WINDOW_LABEL = {"5h": "5-hour", "7d": "7-day", "month": "Monthly"}
PROVIDER_ORDER = ("claude", "codex")
PROVIDER_LABEL = {"claude": "Claude", "codex": "Codex"}

# Informational lines are tinted (light-mode color, dark-mode color) to set
# them apart from the clickable items, which keep the system text color.
INFO_COLOR = "#2b5d9b,#8ab4f8"


def _info(text):
    """An informational dropdown line, tinted INFO_COLOR.

    SwiftBar (2.0.x) disables items without an action and macOS greys those
    out whatever their color=, so give it a harmless one: clicking just
    re-runs the plugin.
    """
    return "%s | color=%s refresh=true" % (text, INFO_COLOR)


def _parse_iso(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _humanize_reset(iso, now):
    dt = _parse_iso(iso)
    if dt is None:
        return "reset time unknown"
    seconds = (dt - now).total_seconds()
    if seconds <= 0:
        return "resetting now"
    minutes = int(seconds // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days > 0:
        return "in %dd %dh" % (days, hours)
    if hours > 0:
        return "in %dh %dm" % (hours, mins)
    return "in %dm" % mins


def _worst_percent(windows):
    pcts = [w["used_percent"] for w in windows.values()
            if w.get("used_percent") is not None]
    return max(pcts) if pcts else None


def newest_age_minutes(latest, now=None):
    now = now or datetime.now(timezone.utc)
    stamps = [w["ts"] for prov in latest.values() for w in prov.values()
              if w.get("ts")]
    if not stamps:
        return None
    newest = datetime.strptime(max(stamps), db.TS_FORMAT).replace(tzinfo=timezone.utc)
    return (now - newest).total_seconds() / 60


def _window_percent(latest, provider, window):
    info = latest.get(provider, {}).get(window)
    return info.get("used_percent") if info else None


def _local_model_lines(local, plugin_path):
    """Dropdown lines for the local model: state, plus Start/Stop when clickable."""
    status, models = local["status"], local["models"]
    state = status["state"]
    details = [{"running": "● running", "starting": "◐ starting",
                "stopped": "○ stopped"}[state]]
    if status.get("model"):
        details.append(localmodel.short_name(status["model"]))
    if status.get("memory_gb") is not None:
        details.append("%.0f GB" % status["memory_gb"])
    if state == "running" and not status.get("proxy"):
        details.append("⚠ proxy down")
    lines = [_info("Local model: " + " · ".join(details))]
    if not plugin_path:
        return lines
    action = 'bash="%s" param1=local-model %%s terminal=false refresh=true' % plugin_path
    if state == "stopped":
        lines.append("▶ Start local model")
        if not models:
            lines.append("--" + _info("No downloaded models"))
        for repo in models:
            lines.append("--%s | %s" % (localmodel.short_name(repo),
                                        action % ("param2=start param3=" + repo)))
    else:
        lines.append("⏹ Stop local model | " + action % "param2=stop")
    return lines


def format_menubar(latest, age_minutes, now=None, plugin_path=None, local=None):
    """Render the SwiftBar plugin output for one refresh.

    When ``plugin_path`` is given (SwiftBar exposes it as
    ``$SWIFTBAR_PLUGIN_PATH``), a "Refresh now" item is added that re-runs the
    plugin with a ``sample`` argument to fetch fresh data on demand.
    ``local`` ({"status": localmodel.status(), "models": [...]}) adds the
    local-model section; its Start/Stop items also need ``plugin_path``.
    """
    now = now or datetime.now(timezone.utc)
    stale = age_minutes is None or age_minutes > STALE_MINUTES
    any_error = any(w.get("error") for prov in latest.values()
                    for w in prov.values())

    # Menu-bar headline: "C·<Claude 5h>-<Claude 7d> X·<Codex's worst
    # (binding) window>". Nothing is colored; SwiftBar's default text color
    # follows light/dark mode.
    def number(pct):
        return "%d" % round(pct) if pct is not None else "?"

    if not latest:
        headline = "AI usage: no samples"
    else:
        headline = "C·%s-%s X·%s" % (
            number(_window_percent(latest, "claude", "5h")),
            number(_window_percent(latest, "claude", "7d")),
            number(_worst_percent(latest.get("codex", {}))))
    if stale or any_error:
        headline = "⚠ " + headline

    lines = [headline, "---"]

    if not latest:
        lines.append(_info("No samples yet — run: python3 -m ai_usage_monitor sample"))
    else:
        for provider in PROVIDER_ORDER:
            windows = latest.get(provider)
            if not windows:
                continue
            for window in WINDOW_ORDER:
                info = windows.get(window)
                if info is None:
                    continue
                name = "%s %s" % (PROVIDER_LABEL[provider], WINDOW_LABEL[window])
                if info.get("error"):
                    lines.append(_info("%s: ⚠ %s" % (name, info["error"])))
                else:
                    lines.append(_info("%s: %d%% · resets %s"
                                       % (name, round(info["used_percent"]),
                                          _humanize_reset(info.get("resets_at"), now))))

    if local is not None:
        lines.append("---")
        lines.extend(_local_model_lines(local, plugin_path))

    lines.append("---")
    if age_minutes is None:
        lines.append(_info("No samples recorded"))
    else:
        lines.append(_info("Last sample %d min ago" % round(age_minutes)))
    if plugin_path:
        lines.append('🔄 Refresh now | bash="%s" param1=sample terminal=false '
                     "refresh=true" % plugin_path)
    lines.append("Open dashboard | href=%s" % DASHBOARD_URL)
    return "\n".join(lines)


def render_menubar_plugin(python, repo_root, local_model_dir=None):
    """Return the SwiftBar wrapper script that calls the menubar subcommand."""
    local = ""
    if local_model_dir:
        local = 'export %s="%s"\n' % (localmodel.ENV_DIR, local_model_dir)
    return (
        "#!/bin/sh\n"
        "# <xbar.title>AI Usage Monitor</xbar.title>\n"
        "# <xbar.desc>Claude and Codex quota usage in the menu bar.</xbar.desc>\n"
        "# <xbar.dependencies>python3</xbar.dependencies>\n"
        'cd "%(repo)s" || exit 1\n'
        "%(local)s"
        'if [ "$1" = "sample" ]; then\n'
        '  exec "%(py)s" -m ai_usage_monitor sample\n'
        "fi\n"
        'if [ "$1" = "local-model" ]; then\n'
        '  exec "%(py)s" -m ai_usage_monitor "$@"\n'
        "fi\n"
        'exec "%(py)s" -m ai_usage_monitor menubar\n'
        % {"repo": repo_root, "py": python, "local": local}
    )


def swiftbar_plugin_dir():
    """Return SwiftBar's configured plugin directory, or None if unset."""
    result = subprocess.run(
        ["defaults", "read", SWIFTBAR_DOMAIN, "PluginDirectory"],
        capture_output=True, text=True)
    path = result.stdout.strip()
    return os.path.expanduser(path) if path else None


def install_menubar(local_model_dir=None):
    if local_model_dir:
        local_model_dir = os.path.abspath(os.path.expanduser(local_model_dir))
        if not os.path.isfile(os.path.join(local_model_dir, "start.sh")):
            print("No start.sh in %s." % local_model_dir, file=sys.stderr)
            return 1
    plugin_dir = swiftbar_plugin_dir()
    if not plugin_dir:
        print("SwiftBar plugin directory not set. Install SwiftBar "
              "(brew install --cask swiftbar), launch it, and choose a plugin "
              "folder, then re-run this command.", file=sys.stderr)
        return 1
    if not os.path.isdir(plugin_dir):
        print("SwiftBar plugin directory %s does not exist." % plugin_dir,
              file=sys.stderr)
        return 1
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Clear any prior interval variant (e.g. aiusage.5m.sh) so a changed
    # refresh rate doesn't leave two plugins running side by side.
    for stale in glob.glob(os.path.join(plugin_dir, PLUGIN_GLOB)):
        os.remove(stale)
    path = os.path.join(plugin_dir, PLUGIN_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_menubar_plugin(sys.executable, repo_root, local_model_dir))
    os.chmod(path, 0o755)
    subprocess.run(["open", "-g", "swiftbar://refreshallplugins"],
                   capture_output=True)
    print("Installed SwiftBar plugin: " + path)
    print("If it doesn't appear, open SwiftBar and refresh plugins.")
    return 0


def uninstall_menubar():
    plugin_dir = swiftbar_plugin_dir()
    if not plugin_dir:
        print("SwiftBar plugin directory not set; nothing to remove.")
        return 0
    removed = glob.glob(os.path.join(plugin_dir, PLUGIN_GLOB))
    for path in removed:
        os.remove(path)
    if not removed:
        print("Plugin not installed (no %s)." % os.path.join(plugin_dir, PLUGIN_GLOB))
        return 0
    subprocess.run(["open", "-g", "swiftbar://refreshallplugins"],
                   capture_output=True)
    for path in removed:
        print("Removed " + path)
    return 0
