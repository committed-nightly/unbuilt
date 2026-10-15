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
        for finding in result.findings:
            lines.append(f"{path}:{finding.line}: {finding.kind}: {finding.detail}")
        return "\n".join(lines)

    for error in result.errors:
        lines.append(f"unbuilt: {path}: {error}")
    if result.errors and result.findings:
        lines.append("")

    if not result.findings:
        if not result.errors:
            plural = "s" if result.stage_count != 1 else ""
            lines.append(
                f"{path}: {result.stage_count} stage{plural}, building "
                f"{result.target}. Every instruction has an effect."
            )
        return "\n".join(lines)

    plural = "s" if result.stage_count != 1 else ""
    lines.append(
        f"{path}: {result.stage_count} stage{plural}, building {result.target}."
    )
    if result.skipped_stages:
        lines.append(f"Not built: {', '.join(result.skipped_stages)}.")
    lines.append("")

    for finding in result.findings:
        lines.append(f"  line {finding.line}  {finding.kind}  ({finding.stage})")
        lines.append(f"    {finding.instruction}")
        lines.append(_wrap(finding.detail, "    "))
        lines.append("")

    count = len(result.findings)
    lines.append(
        f"{count} instruction{'s' if count != 1 else ''} with no effect on this build."
    )
    return "\n".join(lines)


def to_json(result: Result, path: str) -> str:
    return json.dumps(
        {
            "file": path,
            "target": result.target,
            "stages": result.stage_count,
            "not_built": result.skipped_stages,
            "errors": result.errors,
            "findings": [
                {
                    "kind": f.kind,
                    "line": f.line,
                    "stage": f.stage,
                    "instruction": f.instruction,
                    "detail": f.detail,
                }
                for f in result.findings
            ],
        },
        indent=2,
        sort_keys=True,
    )
