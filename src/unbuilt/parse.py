"""Turn a Dockerfile into a flat list of instructions.

This is a parser for the *shape* of a Dockerfile, not for what the commands
inside it mean. It resolves the three things that otherwise make naive
line-by-line reading wrong -- line continuations, parser directives, and
BuildKit heredocs -- and then hands back one record per instruction with the
line number it started on, because every finding unbuilt reports has to point
at a line a person can go and look at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The escape character a Dockerfile uses unless a directive says otherwise.
DEFAULT_ESCAPE = "\\"

#: `# escape=x` and `# syntax=...`. Directives are only directives before the
#: first instruction or comment that is not one; after that they are comments.
_DIRECTIVE = re.compile(r"^#\s*(?P<name>[a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*(?P<value>.*?)\s*$")

#: `<<EOF`, `<<-EOF`, `<<"EOF"`, `<<'EOF'`. BuildKit's heredoc form. The body
#: that follows is data, and parsing it as instructions invents findings.
_HEREDOC = re.compile(r"<<-?\s*(?P<quote>[\"']?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")

#: `--flag=value` at the head of an instruction, e.g. COPY's `--from`.
_FLAG = re.compile(r"^--(?P<name>[a-zA-Z][a-zA-Z0-9-]*)(?:=(?P<value>\S*))?$")


@dataclass(frozen=True)
class Instruction:
    """One instruction, with its continuations already joined up."""

    keyword: str
    """Upper-cased, e.g. ``COPY``. Dockerfile keywords are case-insensitive."""

    argument: str
    """Everything after the keyword, with ``--flags`` removed."""

    flags: dict[str, str]
    """``--name=value`` flags that preceded the argument."""

    line: int
    """1-based line the instruction started on."""

    raw: str
    """The joined source, flags included. What a reader would quote back."""

    heredocs: tuple[str, ...] = ()
    """Bodies of any heredocs attached to this instruction."""


@dataclass
class Parsed:
    instructions: list[Instruction] = field(default_factory=list)
    directives: dict[str, str] = field(default_factory=dict)
    escape: str = DEFAULT_ESCAPE


def _logical_lines(lines: list[str], escape: str) -> list[tuple[int, str, list[str]]]:
    """Join continuations. Returns (line number, joined text, heredoc bodies).

    Comment lines inside a continuation are dropped, which is what the builder
    does, and is the reason a commented-out line in the middle of a `RUN` does
    not comment out the rest of it.
    """
    out: list[tuple[int, str, list[str]]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        start = i
        parts: list[str] = []
        while i < len(lines):
            text = lines[i].rstrip("\n")
            continues = text.rstrip().endswith(escape) and not text.rstrip().endswith(escape * 2)
            if continues:
                text = text.rstrip()[: -len(escape)]
            parts.append(text.strip())
            i += 1
            if not continues:
                break
            # Skip comment-only and blank lines inside the continuation.
            while i < len(lines) and (
                not lines[i].strip() or lines[i].strip().startswith("#")
            ):
                i += 1

        joined = " ".join(p for p in parts if p)

        # Consume any heredoc bodies this instruction opened, in order.
        bodies: list[str] = []
        for match in _HEREDOC.finditer(joined):
            tag = match.group("tag")
            body: list[str] = []
            while i < len(lines):
                if lines[i].strip() == tag:
                    i += 1
                    break
                body.append(lines[i].rstrip("\n"))
                i += 1
            bodies.append("\n".join(body))

        out.append((start + 1, joined, bodies))
    return out


def parse(text: str) -> Parsed:
    """Parse Dockerfile source. Never raises: an unparseable line is skipped."""
    lines = text.splitlines()
    result = Parsed()

    # Parser directives come first, and stop at the first line that is not one.
    for line in lines:
        if not line.strip():
            continue
        if not line.lstrip().startswith("#"):
            break
        match = _DIRECTIVE.match(line.strip())
        if not match:
            break
        result.directives[match.group("name").lower()] = match.group("value")

    declared = result.directives.get("escape", "").strip()
    if declared in ("\\", "`"):
        result.escape = declared

    for number, joined, bodies in _logical_lines(lines, result.escape):
        head, _, rest = joined.partition(" ")
        if not head:
            continue

        flags: dict[str, str] = {}
        tokens = rest.split()
        consumed = 0
        for token in tokens:
            match = _FLAG.match(token)
            if not match:
                break
            flags[match.group("name").lower()] = match.group("value") or ""
            consumed += 1

        result.instructions.append(
            Instruction(
                keyword=head.upper(),
                argument=" ".join(tokens[consumed:]),
                flags=flags,
                line=number,
                raw=joined,
                heredocs=tuple(bodies),
            )
        )

    return result
