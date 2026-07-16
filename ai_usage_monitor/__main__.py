"""Command-line interface."""
import argparse
import sys

from . import sampler


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_usage_monitor",
        description="Monitor Claude and Codex quota usage over time.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample", help="fetch quota status once and append it to the local DB")
    args = parser.parse_args(argv)
    if args.command == "sample":
        sampler.run_sample()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
