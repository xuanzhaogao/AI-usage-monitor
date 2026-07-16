"""Install/uninstall the macOS launchd sampling agent."""
import os
import subprocess
import sys
from xml.sax.saxutils import escape

from . import db

PLIST_LABEL = "com.ai-usage-monitor"


def plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % PLIST_LABEL)


def render_plist(python, repo_root, data_dir):
    python, repo_root, data_dir = escape(python), escape(repo_root), escape(data_dir)
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>ai_usage_monitor</string>
    <string>sample</string>
  </array>
  <key>WorkingDirectory</key><string>{repo_root}</string>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{data_dir}/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{data_dir}/launchd.err.log</string>
</dict>
</plist>
""".format(label=PLIST_LABEL, python=python, repo_root=repo_root, data_dir=data_dir)


def install_agent():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(db.data_dir(), exist_ok=True)
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_plist(sys.executable, repo_root, db.data_dir()))
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    result = subprocess.run(["launchctl", "load", path],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("launchctl load failed: " + result.stderr.strip(), file=sys.stderr)
        return 1
    print("Installed launchd agent %s (samples every 10 minutes)." % PLIST_LABEL)
    print("Plist: " + path)
    return 0


def uninstall_agent():
    path = plist_path()
    if not os.path.exists(path):
        print("agent not installed (no plist at %s)" % path)
        return 0
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    os.remove(path)
    print("Removed " + path)
    return 0


def agent_loaded():
    result = subprocess.run(["launchctl", "list", PLIST_LABEL], capture_output=True)
    return result.returncode == 0
