"""Which names are in scope where, and which `${...}` references are empty.

The rule that catches people: an `ARG` declared before the first `FROM` is
**not** in scope inside any stage. It is in scope in `FROM` lines -- that is
what it is for -- and a stage that wants it has to say `ARG NAME` again.
Nothing warns you. The reference expands to the empty string and the build
succeeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parse import Instruction
from .stages import Stage

#: `${name}`, `${name:-default}`, `${name:+alt}`, `${name}`-style braces, and
#: bare `$name`. A `$$` or `\$` is an escape and matches nothing here.
_REFERENCE = re.compile(
    r"""
    (?<![\\$])\$
    (?:
        \{ (?P<braced>[A-Za-z_][A-Za-z0-9_]*) (?::[-+?][^}]*)? \}
      | (?P<bare>[A-Za-z_][A-Za-z0-9_]*)
    )
    """,
    re.VERBOSE,
)

#: The instructions the builder itself expands variables in. `RUN` is not one
#: of them -- there the shell does it, from an environment that build args are
#: injected into, which lands in the same place for our purposes but means a
#: `$1` or `$@` in a RUN is a shell thing and not a Dockerfile reference.
EXPANDS = frozenset(
    {
        "ADD",
        "COPY",
        "ENV",
        "EXPOSE",
        "FROM",
        "LABEL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
        "ONBUILD",
    }
)


@dataclass(frozen=True)
class Reference:
    name: str
    instruction: Instruction


def references(instruction: Instruction) -> list[Reference]:
    """Variable references in one instruction, including in heredoc bodies."""
    found: list[Reference] = []
    seen: set[str] = set()
    for text in (instruction.raw, *instruction.heredocs):
        for match in _REFERENCE.finditer(text):
            name = match.group("braced") or match.group("bare")
            if name not in seen:
                seen.add(name)
                found.append(Reference(name=name, instruction=instruction))
    return found


def assignments(argument: str) -> list[str]:
    """Names assigned by an `ENV`/`ARG` argument, in either supported form."""
    tokens = argument.split()
    if not tokens:
        return []
    # `ENV key value` -- the legacy space-separated form, one name only. It is
    # only that form if the first token has no `=` in it.
    if "=" not in tokens[0]:
        return [tokens[0]]
    names = []
    for token in tokens:
        name, sep, _ = token.partition("=")
        if sep and name:
            names.append(name)
    return names


def declared_globals(instructions: list[Instruction]) -> dict[str, Instruction]:
    """`ARG`s before the first `FROM`, which only `FROM` lines can see."""
    found: dict[str, Instruction] = {}
    for instruction in instructions:
        if instruction.keyword == "FROM":
            break
        if instruction.keyword == "ARG":
            for name in assignments(instruction.argument):
                found[name] = instruction
    return found


def names_in_stage(stage: Stage) -> set[str]:
    """Names the stage declares for itself, via `ARG` or `ENV`."""
    found: set[str] = set()
    for instruction in stage.instructions:
        if instruction.keyword in ("ARG", "ENV"):
            found.update(assignments(instruction.argument))
    return found


def inherited_names(stage: Stage, lineage: list[Stage]) -> set[str]:
    """Names `stage` starts with, from `ENV` set further up the FROM chain.

    `ARG` does not inherit -- a child stage re-declaring it is the only way --
    but `ENV` does, so a name an ancestor exported is genuinely in scope here.
    Ancestors therefore contribute their `ENV` names and nothing else.
    """
    scope: set[str] = set()
    for ancestor in lineage:
        if ancestor.index == stage.index:
            continue
        for instruction in ancestor.instructions:
            if instruction.keyword == "ENV":
                scope.update(assignments(instruction.argument))
    return scope


def scope_walk(stage: Stage, inherited: set[str]):
    """Yield (instruction, names in scope for it), declaration order honoured.

    This is positional on purpose. An `ARG NAME` three lines below a use does
    not reach back up to fill it in -- the reference above it is still empty,
    and a scope built from the whole stage at once would miss that. A name the
    instruction itself assigns counts, because `ENV A=1 B=${A}` resolves left
    to right within the one instruction.
    """
    scope = set(inherited)
    for instruction in stage.instructions:
        own: set[str] = set()
        if instruction.keyword in ("ARG", "ENV"):
            own = set(assignments(instruction.argument))
        yield instruction, scope | own
        scope |= own


def env_assignments(stage: Stage) -> list[tuple[str, Instruction]]:
    """Every `ENV` name this stage sets, in order, with the instruction."""
    out: list[tuple[str, Instruction]] = []
    for instruction in stage.instructions:
        if instruction.keyword == "ENV":
            for name in assignments(instruction.argument):
                out.append((name, instruction))
    return out
