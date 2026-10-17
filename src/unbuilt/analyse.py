"""The checks. Each one answers "will this line have any effect on the build?"

A finding here means unbuilt believes the line does nothing: it is skipped,
discarded, overwritten before anything reads it, or expands to nothing. Lines
that merely look suspicious are not findings. The point of the tool is that
you can act on its output without checking it.
"""

from __future__ import annotations

import difflib
import json
import shlex
from dataclasses import dataclass, field

from . import context as context_module
from . import stages as stages_module
from . import vars as vars_module
from .parse import Instruction, parse

#: Instructions whose only effect is on the image's configuration, so a second
#: one in the same stage makes the first unreachable with nothing in between
#: able to have observed it.
SINGLE_VALUED = ("ENTRYPOINT", "CMD", "HEALTHCHECK", "STOPSIGNAL")

#: Build args BuildKit reads for itself. Declaring one is the whole point --
#: `ARG BUILDKIT_SBOM_SCAN_STAGE=true` turns on SBOM scanning for the stage
#: and is referenced by nothing, which an earlier version of this check
#: reported as dead in buildkit's own Dockerfile, two lines under a comment
#: saying what it does.
RESERVED_ARGS = frozenset(
    {
        "BUILDKIT_SBOM_SCAN_STAGE",
        "BUILDKIT_SBOM_SCAN_CONTEXT",
        "BUILDKIT_SYNTAX",
        "BUILDKIT_CACHE_MOUNT_NS",
        "BUILDKIT_CONTEXT_KEEP_GIT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
    }
)


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
    `COPY` lands as, where it lands, what shell a `RUN` uses -- so a build
    step below one of those counts, variables or not.

    `ENV` is read by name, so it is checked by name, and against *every* later
    instruction rather than just the build steps. Checking only build steps is
    how an earlier version decided grafana's `ENV GF_PATHS_HOME` was dead with
    `WORKDIR $GF_PATHS_HOME` on the line directly below it. A `RUN` always
    counts, because there the value is in the shell's environment whether or
    not anything spells it out.
    """
    if setting.keyword != "ENV":
        return later.keyword in stages_module.BUILD_STEPS
    if later.keyword == "RUN":
        return True
    names = set(vars_module.assignments(setting.argument))
    return any(r.name in names for r in vars_module.references(later))


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
                # Below it in this stage, and anywhere in a stage built FROM
                # this one -- those inherit the value and can read it. Looking
                # only at this stage called n8n's NODE_GYP dead with three
                # `node "$NODE_GYP"` lines in stages below it.
                later = list(stage.instructions[position + 1 :])
                for child in stages_module.descendants(graph.stages, stage):
                    later.extend(child.instructions)
                if any(_could_consume(instruction, i) for i in later):
                    continue
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


#: `ADD` sources that are not paths in the build context, so `.dockerignore`
#: never sees them. A remote source is fetched, not sent.
_REMOTE_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")


def copy_sources(instruction: Instruction) -> list[str] | None:
    """The source operands of a `COPY`/`ADD`, or None if they cannot be read.

    None is the important return. A source built out of `${...}`, or a
    quoted path this cannot take apart, is one unbuilt must stay quiet about
    -- the whole instruction is dropped rather than half-checked, because a
    half-read argument list is how you report that a `COPY` copies nothing
    when the part you could not read was the part that worked.
    """
    argument = instruction.argument.strip()
    if not argument:
        return None

    if argument.startswith("["):
        try:
            words = json.loads(argument)
        except ValueError:
            return None
        if not all(isinstance(w, str) for w in words):
            return None
    else:
        try:
            words = shlex.split(argument)
        except ValueError:
            return None

    if len(words) < 2:
        return None  # No destination: not a form the builder accepts.
    sources = words[:-1]
    if any("$" in s for s in sources):
        return None
    if any(s.startswith(_REMOTE_PREFIXES) or s.endswith(".git") for s in sources):
        return None
    return sources


def _blame(ignore: context_module.Ignore, path: str) -> str:
    """Name the rule and the file it is in, so there is a line to go and fix.

    The filename is not decoration: a build run with `-f Dockerfile.ci` reads
    `Dockerfile.ci.dockerignore` *instead of* `.dockerignore`, and being told
    to look in the wrong one of the two is worse than not being told.
    """
    name = ignore.path.name if ignore.path is not None else ".dockerignore"
    pattern = ignore.blame(path)
    if pattern is None:
        return f"a rule in {name}"
    return f"{name} line {pattern.line} ({pattern.source})"


def _context_copies(
    graph: stages_module.Graph, build: context_module.Context, strict: bool
) -> list[Finding]:
    """`COPY` and `ADD` read against the context the builder would be sent.

    Target-independent, like `copy-from-unknown`: a source `.dockerignore`
    removes is removed from every build of the file.

    `--from=` is skipped outright and that is not an oversight. A
    `COPY --from=build /app/dist` reads the *stage's* filesystem, which the
    context never touches, so a `.dockerignore` line matching `/app/dist`
    has nothing to do with it. Measured, because reporting those would have
    been the loudest possible false positive.
    """
    findings: list[Finding] = []
    ignore = build.ignore

    for stage in graph.stages:
        for instruction in stage.instructions:
            if instruction.keyword not in ("COPY", "ADD"):
                continue
            if "from" in instruction.flags or instruction.raw_heredocs:
                continue
            sources = copy_sources(instruction)
            if sources is None:
                continue

            # (source as written, which rule did it, pattern?)
            dead: list[tuple[str, str, bool]] = []
            missing: list[str] = []
            alive = 0
            for written in sources:
                source = build.resolve(written)
                if source is None:
                    alive += 1  # `..` out of the context: not ours to judge.
                    continue
                verdict, blamed = _source_verdict(build, source)
                if verdict == "dead":
                    dead.append((written, _blame(ignore, blamed), source.is_pattern))
                elif verdict == "missing":
                    missing.append(written)
                else:
                    alive += 1

            # A *named* source with nothing behind it fails the build, and it
            # does that whatever the other operands do -- so one dead name is
            # enough, where one dead pattern is not.
            fatal = [entry for entry in dead if not entry[2]]
            if fatal:
                findings.append(_dockerignored(instruction, stage, fatal))
            elif dead and not (alive or missing):
                findings.append(_dockerignored(instruction, stage, dead))
            elif missing and not alive and strict:
                findings.append(_missing_sources(instruction, stage, missing))

    return findings


def _source_verdict(
    build: context_module.Context, source: context_module.Source
) -> tuple[str, str]:
    """Whether one source reaches the builder as anything, and what to blame.

    "dead" means *nothing* arrives. A source that loses some of its files and
    keeps others is alive, and deliberately so: removing some of what a
    `COPY` would otherwise pick up is the entire purpose of `.dockerignore`,
    so a tool that remarked on it would fire on every repository that uses
    the file correctly. `COPY . /app` beside a sensible `.dockerignore` is
    the single most common pair of lines in the ecosystem.
    """
    if source.is_pattern:
        if not source.matches:
            return "missing", source.normalised
        gone = [m for m in source.matches if not build.survives(m)]
        if len(gone) == len(source.matches):
            return "dead", gone[0]
        return "alive", source.normalised

    if build.ignore.excludes(source.normalised):
        if source.matches:  # It is on disk, so its kind is known.
            alive = build.survives(source.normalised)
            return ("alive" if alive else "dead"), source.normalised
        # Not on disk: it may be generated before the build runs. Without
        # negations an excluded path stays excluded whatever it turns out to
        # be, so the verdict holds; with them, a file inside it could be
        # re-included and this has no way to know.
        if build.ignore.has_negations:
            return "alive", source.normalised
        return "dead", source.normalised
    return ("alive" if source.matches else "missing"), source.normalised


def _dockerignored(
    instruction: Instruction, stage: stages_module.Stage, dead
) -> Finding:
    written, blamed, is_pattern = dead[0]
    which = f"{written} (and {len(dead) - 1} more)" if len(dead) > 1 else written
    if is_pattern:
        # Measured: a pattern left with nothing to match is not an error.
        # The build goes green having created nothing at the destination.
        outcome = (
            "so this instruction matches nothing and the build copies no files "
            "here, without failing"
        )
    else:
        outcome = (
            "so the build fails here -- a named source that is not in the "
            "context is not a warning, it is 'failed to compute cache key'"
        )
    return Finding(
        kind="dockerignored-copy",
        line=instruction.line,
        stage=stage.label,
        detail=f"{blamed} keeps {which} out of the build context, {outcome}",
        instruction=_truncate(instruction.raw),
    )


def _missing_sources(
    instruction: Instruction, stage: stages_module.Stage, missing: list[str]
) -> Finding:
    first = missing[0]
    which = f"{first} (and {len(missing) - 1} more)" if len(missing) > 1 else first
    return Finding(
        kind="missing-copy-source",
        line=instruction.line,
        stage=stage.label,
        detail=(
            f"nothing in the context matches {which}, so this copies nothing -- "
            f"unless your build generates it, which is why this is --strict only"
        ),
        instruction=_truncate(instruction.raw),
    )


def _unused_args(
    instructions: list[Instruction], graph: stages_module.Graph
) -> list[Finding]:
    """ARGs no build of this file can read.

    The trap here cost a rewrite. A stage-level `ARG NAME` is exported into
    the environment of every `RUN` below it, so the program the `RUN` invokes
    can read it with `$NAME` appearing nowhere in the Dockerfile --
    buildkit's own `ARG CGO_ENABLED=0` configures `go build` exactly that
    way. So a stage `ARG` is only provably unused with no `RUN` beneath it.

    A global `ARG` is injected into no stage's environment, so for those the
    plain "nothing references it" answer holds.
    """
    used: set[str] = set()
    for instruction in instructions:
        for reference in vars_module.references(instruction):
            used.add(reference.name)

    # A bare `ARG NAME` inside a stage is a declaration, not a reference, but
    # it is what pulls a global's value -- and so any --build-arg for it --
    # into that stage. airflow declares AIRFLOW_IMAGE_README_URL globally and
    # re-declares it 2000 lines later, and that counts as using it.
    redeclared: set[str] = set()
    for stage in graph.stages:
        redeclared |= vars_module.names_in_stage(stage)

    findings = []
    for instruction in instructions:
        if instruction.keyword != "ARG":
            continue
        owner = next((s for s in graph.stages if instruction in s.instructions), None)

        if owner is None:
            if any(
                name in redeclared
                for name in vars_module.assignments(instruction.argument)
            ):
                continue
            reason = (
                "is declared before the first FROM and referenced nowhere -- not "
                "in a FROM line, and not by any stage that re-declares it"
            )
        else:
            position = owner.instructions.index(instruction)
            if any(i.keyword == "RUN" for i in owner.instructions[position + 1 :]):
                continue  # A RUN below it gets the value in its environment.
            reason = (
                "is declared, referenced nowhere, and has no RUN below it in this "
                "stage to pick it up out of the environment"
            )

        for name in vars_module.assignments(instruction.argument):
            if name in used or name.upper() in RESERVED_ARGS:
                continue
            findings.append(
                Finding(
                    kind="unused-arg",
                    line=instruction.line,
                    stage=owner.label if owner else "before the first FROM",
                    detail=(
                        f"{name} {reason}, so passing --build-arg {name}=... "
                        f"changes nothing"
                    ),
                    instruction=_truncate(instruction.raw),
                )
            )
    return findings


def run(
    text: str,
    target: str | None = None,
    strict: bool = False,
    build: context_module.Context | None = None,
) -> Result:
    """Analyse Dockerfile source as `docker build --target=<target>` would.

    `build` is an opened build context, and the `.dockerignore` checks only
    run when one is given. unbuilt will not go looking for a context on its
    own: `docker build -f docker/Dockerfile .` has its context at the repo
    root, not next to the file, so the directory holding the Dockerfile is a
    guess -- and a wrong guess reads the wrong `.dockerignore` and reports a
    `COPY` as dead on the strength of it.
    """
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
    if build is not None:
        result.findings.extend(_context_copies(graph, build, strict))
    result.findings.sort(key=lambda f: (f.line, f.kind))
    result.notes.sort(key=lambda f: (f.line, f.kind))
    result.skipped_stages = [
        s.label for s in graph.stages if s.index not in graph.built
    ]
    return result
