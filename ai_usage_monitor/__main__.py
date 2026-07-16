"""Command-line interface."""
import argparse
import sys

from . import sampler, server


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
    args = parser.parse_args(argv)
    if args.command == "sample":
        sampler.run_sample()
        return 0
    if args.command == "serve":
        server.serve(port=args.port, open_browser=args.open_browser)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
