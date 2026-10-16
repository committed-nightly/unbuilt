"""unbuilt's command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyse, report

#: Exit 1 means "found instructions with no effect", like a linter. Exit 2 is
#: argparse's, i.e. you typed it wrong, and also ours for a file we cannot read
#: or cannot analyse as asked -- a `--target` naming no stage, or a file with
#: no `FROM`.
#:
#: Errors beat findings, rather than the other way round or merging into one
#: code. If `--target` named a stage that does not exist, unbuilt analysed a
#: different stage than the one you asked for, so the findings are about a
#: build you did not request. "I could not do what you asked" is the more
#: useful thing to say, and it is the one a CI script can act on differently:
#: exit 1 means fix your Dockerfile, exit 2 means fix your unbuilt invocation.
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

    # Errors go to stderr in every mode, so that whatever stdout is for in
    # this mode stays only that: one line per finding under -q, a parseable
    # document under --json, the readable report otherwise. They stay in the
    # --json payload as well, because that is where a consumer of that mode
    # looks, and dropping them to make the channels tidy would be the bug
    # this is fixing.
    for line in report.error_lines(result, label):
        print(line, file=sys.stderr)

    out = (
        report.to_json(result, label)
        if args.json
        else report.text(result, label, quiet=args.quiet)
    )
    if out:
        print(out)

    if result.ok:
        return 0
    return EXIT_USAGE if result.errors else EXIT_FINDINGS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
