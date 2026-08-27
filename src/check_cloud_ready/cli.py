"""check-cloud-ready CLI entry point.

This is intentionally a stub: only `--version` is supported for now.
Later tasks add format detection, assessments, scoring, and the
interactive CLI.
"""
import argparse

from check_cloud_ready import __version__


def main():
    ap = argparse.ArgumentParser(prog="check-cloud-ready")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.parse_args()
    return 0


if __name__ == "__main__":
    main()
