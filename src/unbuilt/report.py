"""Output. Grouped by line, because that is the order you fix things in."""

from __future__ import annotations

import json
import textwrap

from .analyse import Result

#: Width that keeps a finding readable in a split terminal.
WRAP = 78


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(
        text, width=WRAP, initial_indent=indent, subsequent_indent=indent
    )


def text(result: Result, path: str, quiet: bool = False) -> str:
    lines: list[str] = []

    if quiet:
        # Findings only. `notes` are not failures, so they stay out of the
        # output you pipe into something that counts lines.
        for finding in result.findings:
            lines.append(f"{path}:{finding.line}: {finding.kind}: {finding.detail}")
        return "\n".join(lines)

    for error in result.errors:
        lines.append(f"unbuilt: {path}: {error}")
    if result.errors:
        lines.append("")

    plural = "s" if result.stage_count != 1 else ""
    lines.append(
        f"{path}: {result.stage_count} stage{plural}, building {result.target}."
    )
    if result.skipped_stages:
        lines.append(
            f"Not built for this target: {', '.join(result.skipped_stages)}."
        )
    lines.append("")

    def block(finding) -> None:
        lines.append(f"  line {finding.line}  {finding.kind}  ({finding.stage})")
        lines.append(f"    {finding.instruction}")
        lines.append(_wrap(finding.detail, "    "))
        lines.append("")

    for finding in result.findings:
        block(finding)

    count = len(result.findings)
    if count:
        lines.append(
            f"{count} instruction{'s' if count != 1 else ''} with no effect on "
            f"this build."
        )
    else:
        lines.append("Every instruction has an effect on this build.")

    if result.notes:
        lines.append("")
        lines.append("Worth knowing, but not necessarily wrong:")
        lines.append("")
        for note in result.notes:
            block(note)

    return "\n".join(lines).rstrip()


def _finding(finding) -> dict:
    return {
        "kind": finding.kind,
        "line": finding.line,
        "stage": finding.stage,
        "instruction": finding.instruction,
        "detail": finding.detail,
    }


def to_json(result: Result, path: str) -> str:
    return json.dumps(
        {
            "file": path,
            "target": result.target,
            "stages": result.stage_count,
            "not_built": result.skipped_stages,
            "errors": result.errors,
            "findings": [_finding(f) for f in result.findings],
            "notes": [_finding(f) for f in result.notes],
        },
        indent=2,
        sort_keys=True,
    )
