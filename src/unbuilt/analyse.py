"""The checks. Each one answers "will this line have any effect on the build?"

A finding here means unbuilt believes the line does nothing: it is skipped,
discarded, overwritten before anything reads it, or expands to nothing. Lines
that merely look suspicious are not findings. The point of the tool is that
you can act on its output without checking it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from . import stages as stages_module
from . import vars as vars_module
from .parse import Instruction, parse

#: Instructions whose only effect is on the image's configuration, so a second
#: one in the same stage makes the first unreachable with nothing in between
#: able to have observed it.
SINGLE_VALUED = ("ENTRYPOINT", "CMD", "HEALTHCHECK", "STOPSIGNAL")


@dataclass(frozen=True)
class Finding:
    kind: str
    line: int
    stage: str
    detail: str
    """One sentence, in the imperative of what the builder does, not advice."""

    instruction: str
    """The source, truncated for display."""


@dataclass
class Result:
    target: str | None
    stage_count: int

    findings: list[Finding] = field(default_factory=list)
    """Instructions with no effect. These set the exit code."""

    notes: list[Finding] = field(default_factory=list)
    """True, and not necessarily a problem, so they do not fail anything.

    An orphan stage lives here. Whether it is dead depends on whether some CI
    job passes `--target`, and that is not in the Dockerfile. unbuilt will not
    guess and then exit non-zero about the guess.
    """

    errors: list[str] = field(default_factory=list)
    skipped_stages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings and not self.errors


def _truncate(text: str, width: int = 68) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"


def _orphan_stages(graph: stages_module.Graph) -> list[Finding]:
    """Stages nothing in the file refers to.

    Deliberately *not* "every stage this target does not need". Plenty of
    Dockerfiles are written as a library of stages that CI builds one at a
    time with `--target`, and telling someone that 20 of their 23 stages are
    unused because you happened to pick the last one is noise. A stage no
    other stage names is a different claim: unless you ask for it by name, it
    is never built by anything, and in a file where every other stage is
    wired up that is usually a rename somebody half-finished.
    """
    findings = []
    for stage in graph.stages:
        if stage.index in graph.referenced:
            continue
        if graph.target is not None and stage.index == graph.target.index:
            continue
        if not stage.instructions:
            continue  # An alias stage, e.g. `FROM alpine AS base`. Nothing lost.
        count = len(stage.instructions)
        body = f"{count} instruction{'s' if count != 1 else ''}"
        named = f"--target {stage.name}" if stage.name else "--target"
        findings.append(
            Finding(
                kind="orphan-stage",
                line=stage.line,
                stage=stage.label,
                detail=(
                    f"no other stage builds FROM this one or copies out of it, so "
                    f"BuildKit only runs its {body} if you ask for it by name with "
                    f"{named}"
                ),
                instruction=_truncate(f"FROM {stage.base}"),
            )
        )
    return findings


def _could_consume(setting: Instruction, later: Instruction) -> bool:
    """Could `later` read the value `setting` establishes?

    `USER`, `WORKDIR` and `SHELL` change how any build step behaves -- what a
    `COPY` lands as, where it lands, what shell a `RUN` uses -- so any of them
    counts. `ENV` is different: a `RUN` always has it in the environment, but a
    `COPY` or `ADD` only reads it if the path actually names a variable. That
    distinction is the whole reason unbuilt can call out an `ENV` in a builder
    stage, which is the mistake this check was written for.
    """
    if later.keyword not in stages_module.BUILD_STEPS:
        return False
    if setting.keyword != "ENV":
        return True
    if later.keyword == "RUN":
        return True
    return bool(vars_module.references(later))


def _discarded_config(graph: stages_module.Graph) -> list[Finding]:
    """Settings in a stage that is built, but only for the files it holds."""
    findings = []
    in_lineage = set(graph.lineage)
    for stage in graph.stages:
        if stage.index not in graph.built or stage.index in in_lineage:
            continue
        target_label = graph.target.label if graph.target else "the target"
        for position, instruction in enumerate(stage.instructions):
            if instruction.keyword in stages_module.METADATA_ONLY:
                findings.append(
                    Finding(
                        kind="discarded-metadata",
                        line=instruction.line,
                        stage=stage.label,
                        detail=(
                            f"{target_label} copies files out of this stage but is not "
                            f"built from it, so this {instruction.keyword} is thrown "
                            f"away with the rest of the stage's configuration"
                        ),
                        instruction=_truncate(instruction.raw),
                    )
                )
            elif instruction.keyword in ("ENV", "USER", "WORKDIR", "SHELL"):
                later = stage.instructions[position + 1 :]
                if any(_could_consume(instruction, i) for i in later):
                    continue  # A later build step in this stage could use it.
                findings.append(
                    Finding(
                        kind="discarded-setting",
                        line=instruction.line,
                        stage=stage.label,
                        detail=(
                            f"{target_label} only copies files out of this stage, and "
                            f"nothing later in the stage can read this "
                            f"{instruction.keyword}, so it has no effect anywhere"
                        ),
                        instruction=_truncate(instruction.raw),
                    )
                )
    return findings


def _superseded(graph: stages_module.Graph) -> list[Finding]:
    """Settings a later line in the same stage replaces before anything reads."""
    findings = []
    for stage in graph.stages:
        if not stage.instructions:
            continue

        for keyword in SINGLE_VALUED:
            matching = [i for i in stage.instructions if i.keyword == keyword]
            for instruction in matching[:-1]:
                findings.append(
                    Finding(
                        kind="superseded",
                        line=instruction.line,
                        stage=stage.label,
                        detail=(
                            f"a later {keyword} in this stage (line "
                            f"{matching[-1].line}) replaces it, and {keyword} has no "
                            f"effect during the build for anything to have used it"
                        ),
                        instruction=_truncate(instruction.raw),
                    )
                )

        # ENV is only dead if nothing between the two assignments could read it.
        assignments = vars_module.env_assignments(stage)
        positions = {id(i): n for n, i in enumerate(stage.instructions)}
        for index, (name, instruction) in enumerate(assignments):
            later = [
                other
                for other_name, other in assignments[index + 1 :]
                if other_name == name
            ]
            if not later:
                continue
            start, end = positions[id(instruction)], positions[id(later[0])]
            between = stage.instructions[start + 1 : end]
            if any(i.keyword in stages_module.BUILD_STEPS for i in between):
                continue
            findings.append(
                Finding(
                    kind="superseded",
                    line=instruction.line,
                    stage=stage.label,
                    detail=(
                        f"ENV {name} is set again on line {later[0].line} with no build "
                        f"step in between, so nothing ever sees this value"
                    ),
                    instruction=_truncate(instruction.raw),
                )
            )
    return findings


def _expansions(
    graph: stages_module.Graph,
    globals_: dict[str, Instruction],
    strict: bool,
) -> list[Finding]:
    """References that expand to the empty string."""
    findings = []
    for stage in graph.stages:
        # Every stage, built or not: an empty expansion is empty whichever
        # target you pick.
        chain = stages_module.from_chain(graph.stages, stage)
        inherited = vars_module.inherited_names(stage, chain)

        for instruction, scope in vars_module.scope_walk(stage, inherited):
            for reference in vars_module.references(instruction):
                if reference.name in scope:
                    continue
                if reference.name in globals_:
                    findings.append(
                        Finding(
                            kind="empty-expansion",
                            line=instruction.line,
                            stage=stage.label,
                            detail=(
                                f"{reference.name} is declared before the first FROM "
                                f"(line {globals_[reference.name].line}), which puts it "
                                f"in scope for FROM lines only; no ARG "
                                f"{reference.name} appears in this stage above this "
                                f"line, so it expands to nothing"
                            ),
                            instruction=_truncate(instruction.raw),
                        )
                    )
                elif strict and instruction.keyword in vars_module.EXPANDS:
                    findings.append(
                        Finding(
                            kind="unknown-expansion",
                            line=instruction.line,
                            stage=stage.label,
                            detail=(
                                f"{reference.name} is not declared anywhere in this "
                                f"file; it expands to nothing unless the base image "
                                f"sets it"
                            ),
                            instruction=_truncate(instruction.raw),
                        )
                    )
    return findings


def _copy_sources(graph: stages_module.Graph) -> list[Finding]:
    """`--from` references that are not stages, so the builder pulls an image."""
    findings = []
    names = [s.name for s in graph.stages if s.name]
    for stage in graph.stages:
        # Target-independent: the reference is wrong in any build.
        for instruction in stage.instructions:
            if instruction.keyword not in ("COPY", "ADD"):
                continue
            reference = instruction.flags.get("from")
            if not reference or graph.by_ref(reference) is not None:
                continue
            # A registry path or a tag means an external image was meant.
            if any(c in reference for c in ":/@."):
                continue
            close = difflib.get_close_matches(reference.lower(), names, n=1, cutoff=0.6)
            hint = f"; the closest stage name is {close[0]}" if close else ""
            findings.append(
                Finding(
                    kind="copy-from-unknown",
                    line=instruction.line,
                    stage=stage.label,
                    detail=(
                        f"no stage in this file is named {reference}, so Docker resolves "
                        f"it as the image docker.io/library/{reference}:latest"
                        f"{hint}"
                    ),
                    instruction=_truncate(instruction.raw),
                )
            )
    return findings


def _unused_args(
    instructions: list[Instruction], graph: stages_module.Graph
) -> list[Finding]:
    used: set[str] = set()
    for instruction in instructions:
        for reference in vars_module.references(instruction):
            used.add(reference.name)

    findings = []
    for instruction in instructions:
        if instruction.keyword != "ARG":
            continue
        owner = next(
            (s for s in graph.stages if instruction in s.instructions),
            None,
        )
        for name in vars_module.assignments(instruction.argument):
            if name in used:
                continue
            findings.append(
                Finding(
                    kind="unused-arg",
                    line=instruction.line,
                    stage=owner.label if owner else "before the first FROM",
                    detail=(
                        f"{name} is declared and never referenced, so passing "
                        f"--build-arg {name}=... changes nothing"
                    ),
                    instruction=_truncate(instruction.raw),
                )
            )
    return findings


def run(text: str, target: str | None = None, strict: bool = False) -> Result:
    """Analyse Dockerfile source as `docker build --target=<target>` would."""
    parsed = parse(text)
    graph = stages_module.build(parsed.instructions, target=target)

    result = Result(
        target=graph.target.label if graph.target else None,
        stage_count=len(graph.stages),
    )
    if not graph.stages:
        result.errors.append("no FROM instruction: this file builds nothing")
        return result
    if graph.unknown_target:
        result.errors.append(
            f"no stage named {graph.unknown_target}; analysed the last stage "
            f"({graph.target.label if graph.target else '?'}) instead"
        )

    globals_ = vars_module.declared_globals(parsed.instructions)
    result.notes.extend(_orphan_stages(graph))
    result.findings.extend(_discarded_config(graph))
    result.findings.extend(_superseded(graph))
    result.findings.extend(_expansions(graph, globals_, strict))
    result.findings.extend(_copy_sources(graph))
    result.findings.extend(_unused_args(parsed.instructions, graph))
    result.findings.sort(key=lambda f: (f.line, f.kind))
    result.notes.sort(key=lambda f: (f.line, f.kind))
    result.skipped_stages = [
        s.label for s in graph.stages if s.index not in graph.built
    ]
    return result
