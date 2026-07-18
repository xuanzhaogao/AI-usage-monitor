"""Command-line interface."""
import argparse
import os
import sys

from . import db, launchd, menubar, sampler, server


def format_status(latest, loaded):
    lines = ["launchd agent: " +
             ("loaded" if loaded else "NOT loaded (run install-agent)")]
    if not latest:
        lines.append("no samples recorded yet — run: python3 -m ai_usage_monitor sample")
        return "\n".join(lines)
    for provider in sorted(latest):
        for window in ("5h", "7d", "month"):
            info = latest[provider].get(window)
            if info is None:
                continue
            if info["error"]:
                lines.append("%s %-3s: error: %s (at %s)"
                             % (provider, window, info["error"], info["ts"]))
            else:
                lines.append("%s %-3s: %.1f%% used, resets %s (sampled %s)"
                             % (provider, window, info["used_percent"],
                                info["resets_at"] or "?", info["ts"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_usage_monitor",
        description="Monitor Claude and Codex quota usage over time.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample", help="fetch quota status once and append it to the local DB")
    p_serve = sub.add_parser("serve", help="serve the dashboard on 127.0.0.1")
    p_serve.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p_serve.add_argument("--open", action="store_true", dest="open_browser",
                         help="open the dashboard in the default browser")
    sub.add_parser("install-agent", help="install the launchd agent (samples every 10 min)")
    sub.add_parser("uninstall-agent", help="unload and remove the launchd agent")
    sub.add_parser("status", help="print the latest readings and agent state")
    sub.add_parser("menubar", help="print SwiftBar menu-bar plugin output")
    sub.add_parser("install-menubar", help="install the SwiftBar menu-bar plugin")
    sub.add_parser("uninstall-menubar", help="remove the SwiftBar menu-bar plugin")
    args = parser.parse_args(argv)

    if args.command == "sample":
        sampler.run_sample()
        return 0
    if args.command == "serve":
        server.serve(port=args.port, open_browser=args.open_browser)
        return 0
    if args.command == "install-agent":
        return launchd.install_agent()
    if args.command == "uninstall-agent":
        return launchd.uninstall_agent()
    if args.command == "status":
        conn = db.connect()
        try:
            latest = db.query_latest(conn)
        finally:
            conn.close()
        print(format_status(latest, launchd.agent_loaded()))
        return 0
    if args.command == "menubar":
        conn = db.connect()
        try:
            latest = db.query_latest(conn)
        finally:
            conn.close()
        print(menubar.format_menubar(
            latest, menubar.newest_age_minutes(latest),
            plugin_path=os.environ.get("SWIFTBAR_PLUGIN_PATH")))
        return 0
    if args.command == "install-menubar":
        return menubar.install_menubar()
    if args.command == "uninstall-menubar":
        return menubar.uninstall_menubar()
    return 2


if __name__ == "__main__":
    sys.exit(main())
