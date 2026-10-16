"""The stage graph: what gets built, and what the built image inherits from.

Two different questions get confused constantly, and most of unbuilt's
findings come from keeping them apart:

*Is this stage built at all?* A stage is built if the target needs it, either
as an ancestor (`FROM other`) or as a source of files (`COPY --from=other`,
`RUN --mount=from=other`). BuildKit skips everything else outright.

*Does this stage's configuration reach the image?* Only stages on the
target's `FROM` chain contribute `ENV`, `ENTRYPOINT`, `EXPOSE` and the rest.
A stage you only copy files out of contributes its files and nothing else --
its `ENV` lines are real while that stage builds, and then they are thrown
away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parse import Instruction

#: Instructions that write image configuration and have no other effect. In a
#: stage whose configuration is discarded, these are dead outright.
METADATA_ONLY = frozenset(
    {"ENTRYPOINT", "CMD", "EXPOSE", "VOLUME", "LABEL", "HEALTHCHECK", "STOPSIGNAL"}
)

#: Instructions that write image configuration *and* change how the rest of
#: the stage builds. Discarded the same way, but only dead if nothing later in
#: the stage could have used them.
METADATA_AND_BUILD = frozenset({"ENV", "USER", "WORKDIR", "SHELL", "ARG"})

#: Instructions a later `ENV`/`USER`/`WORKDIR`/`SHELL` could plausibly affect.
BUILD_STEPS = frozenset({"RUN", "COPY", "ADD"})


@dataclass
class Stage:
    index: int
    """0-based position in the file. `COPY --from=0` is legal and means this."""

    name: str | None
    """The `AS name` label, lower-cased by the builder. None if unnamed."""

    base: str
    """The `FROM` argument verbatim, which may be a stage name or an image."""

    line: int
    instructions: list[Instruction] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or f"stage {self.index}"


@dataclass
class Graph:
    stages: list[Stage]
    target: Stage | None
    """The stage `docker build` would produce. The last one unless --target."""

    built: set[int]
    """Indices of stages the builder will actually run."""

    lineage: list[int]
    """Target first, then its base stage, and so on. These set the config."""

    unknown_target: str | None = None
    """Set when --target named a stage that does not exist."""

    referenced: set[int] = field(default_factory=set)
    """Stages some other stage names, by `FROM` or by `--from`.

    A stage outside this set is only ever built if you name it with `--target`.
    That is different from "not built for this target", which in a Dockerfile
    written as a library of stages is true of nearly all of them and worth
    saying about none of them.
    """

    def by_ref(self, ref: str) -> Stage | None:
        """Resolve a stage reference the way the builder does: name, or index."""
        lowered = ref.lower()
        for stage in self.stages:
            if stage.name == lowered:
                return stage
        if ref.isdigit():
            index = int(ref)
            if 0 <= index < len(self.stages):
                return self.stages[index]
        return None


def _split_stages(instructions: list[Instruction]) -> list[Stage]:
    stages: list[Stage] = []
    for instruction in instructions:
        if instruction.keyword == "FROM":
            tokens = instruction.argument.split()
            name = None
            if len(tokens) >= 3 and tokens[-2].upper() == "AS":
                name = tokens[-1].lower()
            stages.append(
                Stage(
                    index=len(stages),
                    name=name,
                    base=tokens[0] if tokens else "",
                    line=instruction.line,
                )
            )
        elif stages:
            stages[-1].instructions.append(instruction)
        # Instructions before the first FROM are global ARGs; vars.py has them.
    return stages


def dependencies(stage: Stage, graph_stages: list[Stage]) -> set[int]:
    """Stage indices this stage pulls files out of."""
    found: set[int] = set()
    lookup = {s.name: s.index for s in graph_stages if s.name}
    for index, s in enumerate(graph_stages):
        lookup.setdefault(str(index), s.index)

    for instruction in stage.instructions:
        refs = []
        if instruction.keyword in ("COPY", "ADD") and "from" in instruction.flags:
            refs.append(instruction.flags["from"])
        if instruction.keyword == "RUN" and "mount" in instruction.flags:
            for part in instruction.flags["mount"].split(","):
                key, _, value = part.partition("=")
                if key.strip() == "from":
                    refs.append(value.strip())
        for ref in refs:
            resolved = lookup.get(ref.lower(), lookup.get(ref))
            if resolved is not None:
                found.add(resolved)
    return found


def from_chain(all_stages: list[Stage], stage: Stage) -> list[Stage]:
    """`stage`, then what it was built `FROM`, and so on up to a real image."""
    by_name = {s.name: s for s in all_stages if s.name}
    chain: list[Stage] = []
    cursor: Stage | None = stage
    while cursor is not None and cursor not in chain:
        chain.append(cursor)
        parent = by_name.get(cursor.base.lower())
        cursor = parent if parent is not None and parent.index < cursor.index else None
    return chain


def descendants(all_stages: list[Stage], stage: Stage) -> list[Stage]:
    """Stages built `FROM` this one, transitively.

    These inherit its `ENV`, `WORKDIR`, `USER` and `SHELL`, so their build
    steps can read a value set here. n8n's `toolchain` stage sets
    `ENV NODE_GYP=...` and three stages below it run `node "$NODE_GYP"`.
    """
    found: list[Stage] = []
    queue = [stage]
    while queue:
        current = queue.pop()
        for candidate in all_stages:
            if candidate.index <= current.index or candidate in found:
                continue
            if candidate.base.lower() == (current.name or ""):
                found.append(candidate)
                queue.append(candidate)
    return found


def build(instructions: list[Instruction], target: str | None = None) -> Graph:
    """Work out what a `docker build` of this file would and would not run."""
    stages = _split_stages(instructions)
    if not stages:
        return Graph(stages=[], target=None, built=set(), lineage=[])

    by_name = {s.name: s for s in stages if s.name}
    unknown_target = None
    if target is None:
        chosen: Stage | None = stages[-1]
    else:
        chosen = by_name.get(target.lower())
        if chosen is None and target.isdigit() and int(target) < len(stages):
            chosen = stages[int(target)]
        if chosen is None:
            unknown_target = target
            chosen = stages[-1]

    # The FROM chain: these stages' ENV, ENTRYPOINT and friends reach the image.
    lineage: list[int] = []
    cursor: Stage | None = chosen
    while cursor is not None and cursor.index not in lineage:
        lineage.append(cursor.index)
        parent = by_name.get(cursor.base.lower())
        # A stage cannot be its own ancestor, and a FROM naming a later stage
        # is not a back-reference the builder honours.
        cursor = parent if parent is not None and parent.index < cursor.index else None

    # Everything the target transitively needs, by either kind of edge.
    built: set[int] = set()
    queue = [chosen.index]
    while queue:
        index = queue.pop()
        if index in built:
            continue
        built.add(index)
        stage = stages[index]
        parent = by_name.get(stage.base.lower())
        if parent is not None and parent.index < stage.index:
            queue.append(parent.index)
        queue.extend(dependencies(stage, stages))

    referenced: set[int] = set()
    for stage in stages:
        parent = by_name.get(stage.base.lower())
        if parent is not None and parent.index < stage.index:
            referenced.add(parent.index)
        referenced |= dependencies(stage, stages)

    return Graph(
        stages=stages,
        target=chosen,
        built=built,
        lineage=lineage,
        unknown_target=unknown_target,
        referenced=referenced,
    )
