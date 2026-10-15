"""unbuilt's command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyse, report

#: Exit 1 means "found instructions with no effect", like a linter. Exit 2 is
#: argparse's, i.e. you typed it wrong, and also ours for a file we cannot read.
EXIT_FINDINGS = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unbuilt",
        description=(
            "Say which Dockerfile instructions never run, and which settings "
            "never reach the image you build. unbuilt reads the file; it does "
            "not build it and does not need a Docker daemon."
        ),
    )
    parser.add_argument(
        "dockerfile",
        nargs="?",
        type=Path,
        default=Path("Dockerfile"),
        help="the file to read (default: ./Dockerfile; - for stdin)",
    )
    parser.add_argument(
        "--target",
        metavar="STAGE",
        help=(
            "analyse the build of this stage, as docker build --target does "
            "(default: the last stage in the file)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "also report references to names this file never declares, which "
            "are empty unless the base image sets them"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print the findings only, one per line, nothing else",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if str(args.dockerfile) == "-":
        source, label = sys.stdin.read(), "<stdin>"
    else:
        path = args.dockerfile
        if path.is_dir():
            path = path / "Dockerfile"
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            print(f"unbuilt: {path}: {error.strerror}", file=sys.stderr)
            return EXIT_USAGE
        label = str(path)

    result = analyse.run(source, target=args.target, strict=args.strict)
    out = (
        report.to_json(result, label)
        if args.json
        else report.text(result, label, quiet=args.quiet)
    )
    if out:
        print(out)
    return EXIT_FINDINGS if result.findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
