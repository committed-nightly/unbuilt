"""Check unbuilt's claims against the builder that gets the last word.

Everything unbuilt says is a claim about what Docker does. Those claims are
cheap to assert and expensive to get wrong, so they are not left as comments:
each test here builds a real Dockerfile, inspects the real image, and asserts
that what unbuilt predicted is what happened.

Every fixture is `FROM scratch`, so these need a daemon but no network and no
registry pull. They skip, loudly, if there is no daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid

import pytest

from unbuilt import analyse, context
from unbuilt.cli import main

pytestmark = pytest.mark.docker


def _daemon_is_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=60
        ).returncode
        == 0
    )


needs_docker = pytest.mark.skipif(not _daemon_is_up(), reason="no Docker daemon")


@pytest.fixture
def build(tmp_path):
    """Write a Dockerfile, build it, return (ok, combined output, image tag)."""
    (tmp_path / "payload.txt").write_text("payload\n")
    built: list[str] = []

    def run(text: str, *args: str):
        (tmp_path / "Dockerfile").write_text(text)
        tag = f"unbuilt-test-{uuid.uuid4().hex[:12]}"
        built.append(tag)
        completed = subprocess.run(
            ["docker", "build", "-q", "-t", tag, *args, "."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return completed.returncode == 0, completed.stdout + completed.stderr, tag

    yield run
    for tag in built:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)


def config(tag: str) -> dict:
    out = subprocess.run(
        ["docker", "image", "inspect", tag, "--format", "{{json .Config}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return json.loads(out)


def env_of(tag: str) -> dict[str, str]:
    pairs = config(tag).get("Env") or []
    return dict(p.split("=", 1) for p in pairs if "=" in p)


# An unreachable stage whose body cannot possibly succeed. If the build goes
# green, the stage was skipped -- which is the whole claim.
SKIPPED = """\
FROM scratch AS dead
COPY no-such-file.txt /x

FROM scratch AS final
COPY payload.txt /payload
"""


@needs_docker
def test_an_unreachable_stage_is_never_run(build):
    ok, output, _ = build(SKIPPED)
    assert ok, output

    targeted_ok, targeted_output, _ = build(SKIPPED, "--target", "dead")
    assert not targeted_ok
    assert "no-such-file.txt" in targeted_output

    # ...and unbuilt names that stage as one the default build leaves out.
    result = analyse.run(SKIPPED)
    assert result.skipped_stages == ["dead"]
    assert [(f.kind, f.stage) for f in result.notes] == [("orphan-stage", "dead")]
    assert result.findings == []  # a --target away from being alive, so not a failure


@needs_docker
def test_the_legacy_builder_runs_the_stage_buildkit_skips(tmp_path):
    """unreachable-stage is a BuildKit fact, and the README says so.

    Skipping stages is what BuildKit added. The classic builder walks every
    instruction in the file, so the same Dockerfile fails there -- which is
    the one case where acting on the finding changes behaviour rather than
    just deleting dead weight.
    """
    (tmp_path / "payload.txt").write_text("payload\n")
    (tmp_path / "Dockerfile").write_text(SKIPPED)
    completed = subprocess.run(
        ["docker", "build", "-q", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        env=dict(os.environ, DOCKER_BUILDKIT="0"),
    )
    output = completed.stdout + completed.stderr
    if "buildkit" in output.lower() and completed.returncode == 0:
        pytest.skip("this daemon ignores DOCKER_BUILDKIT=0")
    assert completed.returncode != 0, output
    assert "no-such-file.txt" in output


DISCARDED = """\
ARG GLOBAL=from-global

FROM scratch AS base
ENV BASE_ENV=reaches-the-image
EXPOSE 8080
ENTRYPOINT ["/base-entry"]

FROM scratch AS builder
ENV BUILDER_ENV=does-not
EXPOSE 9999
ENTRYPOINT ["/builder-entry"]
COPY payload.txt /artifact

FROM base AS final
ENV LEAKED=${GLOBAL}
ARG GLOBAL
ENV REDECLARED=${GLOBAL}
COPY --from=builder /artifact /artifact
"""


@needs_docker
def test_an_artifact_only_stages_configuration_is_thrown_away(build):
    ok, output, tag = build(DISCARDED)
    assert ok, output
    environment, settings = env_of(tag), config(tag)

    # What the builder stage set is absent from the image, all three ways.
    assert "BUILDER_ENV" not in environment
    assert "9999/tcp" not in (settings.get("ExposedPorts") or {})
    assert settings["Entrypoint"] == ["/base-entry"]

    # What the FROM chain set is present, which is why this is not just "all
    # non-final stages are dead".
    assert environment["BASE_ENV"] == "reaches-the-image"
    assert "8080/tcp" in settings["ExposedPorts"]

    reported = {(f.kind, f.line) for f in analyse.run(DISCARDED).findings}
    assert ("discarded-setting", 9) in reported  # ENV BUILDER_ENV
    assert ("discarded-metadata", 10) in reported  # EXPOSE 9999
    assert ("discarded-metadata", 11) in reported  # ENTRYPOINT /builder-entry
    assert not any(f.stage == "base" for f in analyse.run(DISCARDED).findings)


@needs_docker
def test_a_global_arg_is_empty_until_the_stage_redeclares_it(build):
    ok, output, tag = build(DISCARDED)
    assert ok, output
    environment = env_of(tag)

    assert environment["LEAKED"] == ""  # declared globally, never re-declared
    assert environment["REDECLARED"] == "from-global"  # ARG GLOBAL, one line up

    findings = analyse.run(DISCARDED).findings
    empty = [f for f in findings if f.kind == "empty-expansion"]
    assert [f.line for f in empty] == [15]  # ENV LEAKED, and not ENV REDECLARED


@needs_docker
def test_passing_a_build_arg_the_file_never_declares_changes_nothing(build):
    text = "FROM scratch AS final\nENV V=${NEVER_DECLARED}\n"
    ok, output, tag = build(text, "--build-arg", "NEVER_DECLARED=hello")
    assert ok, output
    assert env_of(tag)["V"] == ""

    # unbuilt is quiet here by default -- the base image could have set it --
    # and says it under --strict. `scratch` cannot, but it cannot know that.
    assert analyse.run(text).findings == []
    assert [f.kind for f in analyse.run(text, strict=True).findings] == [
        "unknown-expansion"
    ]


@needs_docker
def test_the_last_env_wins_and_unbuilt_blames_the_first(build):
    text = "FROM scratch AS final\nENV PORT=3000\nENV PORT=8080\n"
    ok, output, tag = build(text)
    assert ok, output
    assert env_of(tag)["PORT"] == "8080"

    findings = analyse.run(text).findings
    assert [(f.kind, f.line) for f in findings] == [("superseded", 2)]


@needs_docker
def test_docker_refuses_an_unknown_target_so_unbuilt_must_not_report_success(
    build, tmp_path
):
    """The parity that sent this PR back, stated against the real builder.

    `docker build --target no-such-stage` does not fall back to another stage;
    it refuses to build anything and exits non-zero. unbuilt analyses the last
    stage instead and says so, which is a useful thing to do -- but it was
    then exiting 0, i.e. reporting a clean build of a build that Docker will
    not perform at all.
    """
    text = "FROM scratch AS final\nCOPY payload.txt /payload\n"
    ok, output, _ = build(text, "--target", "no-such-stage")
    assert not ok
    assert "no-such-stage" in output

    # Same file, same flag, no finding anywhere in it -- and still not a
    # success, because unbuilt could not analyse what was asked for.
    result = analyse.run(text, target="no-such-stage")
    assert result.findings == []
    assert not result.ok
    assert result.errors == [
        "no stage named no-such-stage; analysed the last stage (final) instead"
    ]

    (tmp_path / "Dockerfile").write_text(text)
    assert main([str(tmp_path / "Dockerfile"), "--target", "no-such-stage"]) != 0


@needs_docker
def test_a_copy_from_that_names_no_stage_is_resolved_as_an_image(build):
    text = "FROM scratch AS final\nCOPY --from=buildr /x /x\n"
    ok, output, _ = build(text)
    assert not ok
    # The point: the failure is a registry failure, not "unknown stage".
    assert "buildr" in output
    assert any(s in output for s in ("pull access denied", "not found", "failed to resolve"))

    findings = analyse.run(text).findings
    assert [f.kind for f in findings] == ["copy-from-unknown"]


# --- .dockerignore -------------------------------------------------------
#
# The matcher in context.py is a reimplementation of somebody else's matcher,
# which is the worst kind of code to leave unmeasured: it is right until
# BuildKit changes, and wrong silently. So the first test below does not check
# a handful of cases by hand -- it builds each pattern set for real, asks the
# image which files actually arrived, and asserts unbuilt predicted that exact
# set. A disagreement anywhere is a failure here.

#: A context with the awkward bits in it: nesting, a name that is a regex
#: metacharacter, and a directory people always exclude.
TREE = (
    "root.txt", "root.log", "secret.key",
    "a/mid.txt", "a/mid.log", "a/b/deep.txt", "a/b/deep.log",
    "keep/k.log", "keep/k.txt",
    "x/y/z/buried.txt",
    "node_modules/pkg/index.js",
    "weird/na+me.txt", "weird/pa(ren).txt", "weird/do.t.txt",
)

PATTERN_SETS = [
    "*.log",
    "**/*.log",
    "a",
    "a/",
    "/root.log",
    "a/**",
    "*\n!keep",
    "a\n!a/mid.txt",
    "a\n!a/b/deep.txt",
    "*.log\n!root.log\nroot.log",
    "**",
    "x/**/buried.txt",
    "*/y",
    "x/*/z",
    "**/z",
    "node_modules",
    "secret.ke?",
    "secret.[kx]ey",
    "# comment\n\n  secret.key  ",
    "weird/na+me.txt",
    "weird/pa(ren).txt",
    "weird/do.t.txt",
    "weird/*.txt\n!weird/do.t.txt",
    "a/b\nkeep/*.log\n!keep/k.log",
    "./a/mid.txt",
    "a/../root.txt",
    "**/deep.*",
    "k*/",
    "x\n!x/y\n!x/y/z\n!x/y/z/buried.txt",
    ".",
]


def _populate(root) -> None:
    for relative in TREE:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


@needs_docker
@pytest.mark.parametrize("patterns", PATTERN_SETS, ids=lambda p: p.replace("\n", "|"))
def test_the_matcher_agrees_with_buildkit_on_what_reaches_the_context(
    patterns, tmp_path
):
    """Build the context for real and compare it to what unbuilt predicted."""
    _populate(tmp_path)
    (tmp_path / ".dockerignore").write_text(patterns + "\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY . /ctx\n")

    destination = tmp_path.parent / (tmp_path.name + "-out")
    completed = subprocess.run(
        ["docker", "build", "-q", "-o", f"type=local,dest={destination}", "."],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    landed = {
        str(path.relative_to(destination / "ctx"))
        for path in (destination / "ctx").rglob("*")
        if path.is_file()
    }
    everything = set(TREE) | {"Dockerfile", ".dockerignore"}
    ignore = context.Ignore.parse(patterns)
    predicted = {path for path in everything if not ignore.excludes(path)}

    assert predicted == landed, (
        f"patterns {patterns!r}\n"
        f"  docker kept, unbuilt dropped: {sorted(landed - predicted)}\n"
        f"  unbuilt kept, docker dropped: {sorted(predicted - landed)}"
    )


@needs_docker
def test_a_pattern_with_nothing_left_to_match_copies_nothing_and_still_succeeds(
    build, tmp_path
):
    """The silent one, and the reason `dockerignored-copy` exists.

    An excluded *named* source fails the build, loudly. An excluded *pattern*
    does not: it matches nothing, copies nothing, creates nothing at the
    destination, and exits 0. Nothing in the output mentions it.
    """
    (tmp_path / ".dockerignore").write_text("*.key\n")
    (tmp_path / "secret.key").write_text("hunter2\n")
    text = "FROM scratch\nCOPY *.key /secrets/\n"
    ok, output, _ = build(text)
    assert ok, output

    destination = tmp_path.parent / "silent-out"
    subprocess.run(
        ["docker", "build", "-q", "-o", f"type=local,dest={destination}", "."],
        cwd=tmp_path, check=True, capture_output=True, timeout=300,
    )
    assert list(destination.rglob("*")) == []  # not even an empty /secrets

    result = analyse.run(text, build=context.open_context(tmp_path))
    assert [(f.kind, f.line) for f in result.findings] == [("dockerignored-copy", 2)]


@needs_docker
def test_an_excluded_named_source_fails_the_build(build, tmp_path):
    (tmp_path / ".dockerignore").write_text("dist\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("console.log(1)\n")
    text = "FROM scratch\nCOPY dist /dist\n"

    ok, output, _ = build(text)
    assert not ok
    assert "failed to compute cache key" in output

    result = analyse.run(text, build=context.open_context(tmp_path))
    assert [(f.kind, f.line) for f in result.findings] == [("dockerignored-copy", 2)]


@needs_docker
def test_a_negation_re_includes_under_an_excluded_directory(build, tmp_path):
    """git's rule is that you cannot do this. unbuilt must not inherit it.

    If unbuilt applied git's semantics it would call this `COPY` dead, which
    is exactly the false positive that kept `.dockerignore` out of the tool
    in the first place.
    """
    (tmp_path / ".dockerignore").write_text("dist\n!dist/keep.js\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "keep.js").write_text("kept\n")
    (tmp_path / "dist" / "drop.js").write_text("dropped\n")
    text = "FROM scratch\nCOPY dist /dist\n"

    ok, output, _ = build(text)
    assert ok, output

    destination = tmp_path.parent / "negation-out"
    subprocess.run(
        ["docker", "build", "-q", "-o", f"type=local,dest={destination}", "."],
        cwd=tmp_path, check=True, capture_output=True, timeout=300,
    )
    assert {p.name for p in (destination / "dist").iterdir()} == {"keep.js"}

    result = analyse.run(text, build=context.open_context(tmp_path))
    assert (result.findings, result.notes) == ([], [])  # it copies, so it is alive


@needs_docker
def test_copy_from_a_stage_does_not_read_the_context(build, tmp_path):
    """A `.dockerignore` rule matching the source path is irrelevant here."""
    (tmp_path / ".dockerignore").write_text("artifact\n")
    (tmp_path / "payload.txt").write_text("payload\n")
    text = (
        "FROM scratch AS build\n"
        "COPY payload.txt /artifact\n"
        "FROM scratch\n"
        "COPY --from=build /artifact /artifact\n"
    )
    ok, output, _ = build(text)
    assert ok, output

    result = analyse.run(text, build=context.open_context(tmp_path))
    assert (result.findings, result.notes) == ([], [])


@needs_docker
def test_a_per_dockerfile_ignore_file_replaces_the_plain_one(tmp_path):
    (tmp_path / "one.txt").write_text("1\n")
    (tmp_path / "two.txt").write_text("2\n")
    (tmp_path / ".dockerignore").write_text("one.txt\n")
    (tmp_path / "Dockerfile.foo.dockerignore").write_text("two.txt\n")
    (tmp_path / "Dockerfile.foo").write_text("FROM scratch\nCOPY . /ctx\n")

    destination = tmp_path.parent / "specific-out"
    subprocess.run(
        ["docker", "build", "-q", "-f", "Dockerfile.foo",
         "-o", f"type=local,dest={destination}", "."],
        cwd=tmp_path, check=True, capture_output=True, timeout=300,
    )
    landed = {p.name for p in (destination / "ctx").iterdir()}

    # one.txt survived: the specific file replaced `.dockerignore` rather
    # than adding to it.
    assert "one.txt" in landed and "two.txt" not in landed

    chosen = context.ignore_file_for(tmp_path, tmp_path / "Dockerfile.foo")
    assert chosen.name == "Dockerfile.foo.dockerignore"
    assert not context.load(tmp_path, tmp_path / "Dockerfile.foo").excludes("one.txt")


@needs_docker
@pytest.mark.parametrize("pattern", ["f[.txt", "f.txt\\", "!"])
def test_docker_refuses_a_malformed_pattern_so_unbuilt_must_not_guess(
    pattern, build, tmp_path
):
    """These do not merely behave oddly -- the build never starts."""
    (tmp_path / ".dockerignore").write_text(pattern + "\n")
    ok, output, _ = build("FROM scratch\nCOPY payload.txt /payload\n")
    assert not ok, output

    with pytest.raises(context.BadPattern):
        context.load(tmp_path)

    # And the CLI says so on stderr, exits 2, and still reports the file.
    assert main([str(tmp_path / "Dockerfile"), "--context", str(tmp_path)]) == 2


@needs_docker
def test_copy_source_patterns_have_no_double_star(build, tmp_path):
    """`COPY **/*.go` is `COPY */*.go`, which `.dockerignore`'s `**` is not.

    Letting the two matchers share an implementation would make unbuilt
    believe `COPY **/*.go` picks up `deep/c.go`, and then call a `.dockerignore`
    rule covering it the reason a file went missing.
    """
    for relative in ("a.go", "src/b.go", "src/deep/c.go"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package main\n")
    (tmp_path / ".dockerignore").write_text("")
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY **/*.go /out/\n")

    destination = tmp_path.parent / "glob-out"
    subprocess.run(
        ["docker", "build", "-q", "-o", f"type=local,dest={destination}", "."],
        cwd=tmp_path, check=True, capture_output=True, timeout=300,
    )
    assert {p.name for p in (destination / "out").iterdir()} == {"b.go"}

    resolved = context.open_context(tmp_path).resolve("**/*.go")
    assert resolved.matches == ("src/b.go",)


# --- the absent source, measured once it exists --------------------------
#
# A `COPY` source that is not in the context yet is the subtlest verdict
# unbuilt reaches, because the thing it is describing has not happened. The
# ground truth is still available, though: generate the source the way CI
# would, run the build, and see whether Docker agrees the line was dead.
#
# The first version of this check deferred to `.dockerignore` having *any*
# negation in it, which silently gave up on every repository that has one.
# These cases are why it now asks whether a negation could land under the
# source itself.

#: (ignore file, `COPY` source, what CI creates after unbuilt has run)
GENERATED_CASES = [
    # The negation names a sibling, a deeper sibling, an unrelated path: none
    # of them can ever re-include this source, so the verdict is knowable.
    ("dist\n!dist/keep.js", "dist/gen.js", ("dist/gen.js",)),
    ("dist\n!dist/sub/keep.js", "dist/gen.js", ("dist/gen.js",)),
    ("dist\n!unrelated/keep.js", "dist/gen.js", ("dist/gen.js",)),
    ("*\n!keep", "gen.js", ("gen.js",)),
    # The same miss one level up: an excluded directory CI fills in.
    ("build\n!other/keep", "build", ("build/out.txt",)),
    # No negation at all, which always worked.
    ("dist", "dist/gen.js", ("dist/gen.js",)),
    # And the other answer: the negation names this very source, or reaches
    # into it, so the file arrives and the build is fine.
    ("dist\n!dist/gen.js", "dist/gen.js", ("dist/gen.js",)),
    ("dist\n!dist/**", "dist/gen.js", ("dist/gen.js",)),
    ("build\n!build/out.txt", "build", ("build/out.txt",)),
]


def _build_fails(root) -> tuple[bool, str]:
    completed = subprocess.run(
        ["docker", "build", "-q", "."],
        cwd=root, capture_output=True, text=True, timeout=300,
    )
    return completed.returncode != 0, completed.stdout + completed.stderr


@needs_docker
@pytest.mark.parametrize(
    "patterns,source,generated",
    GENERATED_CASES,
    ids=lambda value: value.replace("\n", "|") if isinstance(value, str) else "",
)
def test_the_verdict_on_an_absent_source_agrees_with_buildkit(
    patterns, source, generated, tmp_path
):
    """Ask unbuilt while the source is missing, ask Docker once it is there."""
    (tmp_path / ".dockerignore").write_text(patterns + "\n")
    (tmp_path / "Dockerfile").write_text(f"FROM scratch\nCOPY {source} /app/\n")

    # unbuilt runs against the context as CI leaves it: source not yet built.
    result = analyse.run(
        (tmp_path / "Dockerfile").read_text(),
        build=context.open_context(tmp_path),
    )
    called_dead = [f.kind for f in result.findings] == ["dockerignored-copy"]

    for relative in generated:  # ... and now the build step that writes it.
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n")

    failed, output = _build_fails(tmp_path)
    assert called_dead == failed, (
        f"patterns {patterns!r}, COPY {source}\n"
        f"  unbuilt called it dead: {called_dead}\n"
        f"  docker build failed:    {failed}\n{output}"
    )


@needs_docker
@pytest.mark.parametrize("generated", [("build/keep",), ("build/other",)])
def test_the_remaining_deferral_is_a_real_coin_toss_not_a_preference(
    generated, tmp_path
):
    """Why the narrowed deferral still has to exist.

    `build` is excluded and `!build/keep` reaches inside it. Whether the
    build survives depends entirely on which files CI writes, and unbuilt is
    looking at the context before it writes any of them. Docker goes both
    ways on the same Dockerfile and the same ignore file; the only honest
    answer at that point is silence.
    """
    (tmp_path / ".dockerignore").write_text("build\n!build/keep\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY build /app\n")

    result = analyse.run(
        (tmp_path / "Dockerfile").read_text(),
        build=context.open_context(tmp_path),
    )
    assert result.findings == []  # Said nothing, and had to.

    path = tmp_path / generated[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("generated\n")

    failed, _ = _build_fails(tmp_path)
    assert failed == (generated[0] == "build/other")


# --- sources the builder fetches instead of reading ------------------------
#
# Every claim unbuilt makes here is a claim about which of two code paths
# BuildKit takes, and the two fail differently when the thing is not there:
# a context read says `"/x": not found`, a fetch says it could not resolve
# the host or that it has no ssh key. An unreachable host is enough to tell
# them apart, so these still need no network -- with no DNS at all the fetch
# cases fail earlier and in the same direction.

#: (source operand, is it read out of the build context?)
SOURCE_SHAPES = [
    ("https://example.invalid/o/r.git", False),
    ("https://example.invalid/o/r.git#main", False),
    ("HTTPS://EXAMPLE.INVALID/o/r.git", False),
    ("https://example.invalid/o/r", False),
    ("https://example.invalid/file.tar.gz", False),
    ("git://example.invalid/o/r.git", False),
    ("ssh://git@example.invalid/o/r.git", False),
    ("git@example.invalid:o/r.git", False),
    ("git@example.invalid:o/r", False),
    ("a@b:c", False),
    # The other half, and the surprising one. Each of these looks remote and
    # is a path in the build context.
    ("example.invalid/o/r.git", True),
    ("example.invalid/o/r.git#main", True),
    ("example.invalid:8080/o/r.git", True),
    ("git+ssh://example.invalid/o/r.git", True),
    ("git+http://example.invalid/o/r.git", True),
    ("my.user@example.invalid:o/r.git", True),
    ("u@my_host:o/r.git", True),
    ("dir/u@host:p", True),
    ("./u@host:p", True),
    ("nope.git", True),
]

#: What BuildKit says when it looked for the source in the context.
_CONTEXT_READ = ": not found"

#: ...and when it tried to fetch it. One of these, or it failed for a third
#: reason and the test should say so rather than pass.
_FETCHED = (
    "Could not resolve host",
    "unset ssh forward key",
    "no such host",
    "unable to look up",
)


@needs_docker
@pytest.mark.parametrize("source,local", SOURCE_SHAPES, ids=lambda v: str(v))
def test_is_remote_source_agrees_with_buildkit(source, local, tmp_path):
    """Nothing is in the context, so every case fails. *How* is the answer."""
    (tmp_path / "Dockerfile").write_text(f"FROM scratch\nADD {source} /dst\n")
    completed = subprocess.run(
        ["docker", "build", "-q", "."],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output

    if local:
        assert _CONTEXT_READ in output, output
    else:
        assert any(marker in output for marker in _FETCHED), output
    assert analyse.is_remote_source(source) is not local, output


@needs_docker
def test_a_dockerignored_dot_git_directory_fails_the_build(build, tmp_path):
    """logbook#47. The suffix guard dropped this instruction, so unbuilt was
    silent about a build that cannot start -- and `.git` in `.dockerignore`
    beside a `COPY .git` is a real pair of lines, not a constructed one."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".dockerignore").write_text(".git\n")

    ok, output, _ = build("FROM scratch\nCOPY .git /app/\n")
    assert not ok and "failed to compute cache key" in output, output

    result = analyse.run(
        (tmp_path / "Dockerfile").read_text(),
        build=context.open_context(tmp_path),
    )
    assert [f.kind for f in result.findings] == ["dockerignored-copy"]


@needs_docker
def test_the_same_copy_in_a_stage_nothing_builds_is_not_a_failure(build, tmp_path):
    """logbook#49, and the reason the fatal half follows `--target`.

    Identical file, identical ignore rule, identical `COPY`. Moving it into
    a stage the target does not build takes the build from `failed to
    compute cache key` to exit 0, because the refusal happens as a stage is
    converted and an unbuilt stage is never converted. unbuilt's exit code
    has to match the builder's in both directions.
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".dockerignore").write_text(".git\n")
    text = (
        "FROM scratch AS dead\nCOPY .git /app/\n"
        "\nFROM scratch AS final\nCOPY payload.txt /payload\n"
    )

    ok, output, _ = build(text)
    assert ok, output
    result = analyse.run(text, build=context.open_context(tmp_path))
    assert result.findings == []
    # Not a finding, and not silence either: a stage nothing builds is where
    # this mistake can sit in a repository without failing anything.
    assert [n.kind for n in result.notes] == ["orphan-stage", "dockerignored-copy"]

    ok, output, _ = build(text, "--target", "dead")
    assert not ok
    assert "failed to compute cache key" in output, output
    named = analyse.run(text, build=context.open_context(tmp_path), target="dead")
    assert [f.kind for f in named.findings] == ["dockerignored-copy"]


@needs_docker
def test_a_dead_pattern_in_such_a_stage_is_green_under_either_target(build, tmp_path):
    """Which is why the pattern half has no target to follow.

    The fatal half changes answer when you name the stage. This one cannot:
    a pattern with nothing left to match copies nothing and fails nothing,
    whichever stage it is in and whichever stage you ask for. So it stays a
    finding in both, where the fatal half drops to a note in one.
    """
    (tmp_path / ".dockerignore").write_text("*.key\n")
    (tmp_path / "secret.key").write_text("hunter2\n")
    text = (
        "FROM scratch AS dead\nCOPY *.key /secrets/\n"
        "\nFROM scratch AS final\nCOPY payload.txt /payload\n"
    )

    for args in ((), ("--target", "dead")):
        ok, output, _ = build(text, *args)
        assert ok, f"{args}: {output}"

    for target in (None, "dead"):
        result = analyse.run(
            text, build=context.open_context(tmp_path), target=target
        )
        kinds = [f.kind for f in result.findings]
        assert kinds == ["dockerignored-copy"], target


@needs_docker
def test_the_same_directory_with_no_rule_against_it_is_copied(build, tmp_path):
    """The other direction, which is what makes the finding above safe."""
    (tmp_path / "foo.git").mkdir()
    (tmp_path / "foo.git" / "HEAD").write_text("ref: refs/heads/main\n")

    ok, output, _ = build("FROM scratch\nCOPY foo.git /app/\n")
    assert ok, output

    result = analyse.run(
        (tmp_path / "Dockerfile").read_text(),
        build=context.open_context(tmp_path),
    )
    assert result.findings == []


@needs_docker
@pytest.mark.parametrize(
    "source,refusal",
    [
        ("https://example.invalid/o/r.git", "source can't be a git ref for COPY"),
        ("git@example.invalid:o/r.git", "source can't be a git ref for COPY"),
        ("git://example.invalid/o/r.git", "source can't be a git ref for COPY"),
        ("https://example.invalid/x.tar.gz", "source can't be a URL for COPY"),
        ("https://example.invalid/o/r", "source can't be a URL for COPY"),
    ],
    ids=lambda v: str(v),
)
def test_copy_cannot_fetch_and_unbuilt_quotes_the_refusal_verbatim(
    build, source, refusal, tmp_path
):
    """The detail text quotes BuildKit, so BuildKit gets to check the quote."""
    text = f"FROM scratch\nCOPY {source} /dst\n"
    ok, output, _ = build(text)
    assert not ok
    assert refusal in output, output

    findings = [f for f in analyse.run(text).findings if f.kind == "copy-remote-source"]
    assert len(findings) == 1
    assert refusal in findings[0].detail


@needs_docker
def test_a_copy_of_a_git_ref_in_a_stage_nothing_builds_is_not_a_failure(build):
    """Why this one check follows --target: the refusal is per converted stage."""
    text = (
        "FROM scratch AS dead\nCOPY https://example.invalid/o/r.git /dst\n"
        "\nFROM scratch AS final\nCOPY payload.txt /payload\n"
    )
    ok, output, _ = build(text)
    assert ok, output
    assert analyse.run(text).findings == []
    # Not a finding, and not nothing either: a stage nothing builds is where
    # this mistake can sit in a repository without failing anything.
    assert [n.kind for n in analyse.run(text).notes] == [
        "orphan-stage",
        "copy-remote-source",
    ]

    ok, output, _ = build(text, "--target", "dead")
    assert not ok
    assert "source can't be a git ref for COPY" in output, output
    assert [f.kind for f in analyse.run(text, target="dead").findings] == [
        "copy-remote-source"
    ]
