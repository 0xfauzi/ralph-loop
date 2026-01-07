from __future__ import annotations

import argparse

from ralph_uv_example import greet


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ralph-uv-example")
    p.add_argument("name", nargs="?", default="world", help="Name to greet")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(greet(args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
