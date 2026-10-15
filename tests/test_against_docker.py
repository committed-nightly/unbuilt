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

from unbuilt import analyse

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
def test_a_copy_from_that_names_no_stage_is_resolved_as_an_image(build):
    text = "FROM scratch AS final\nCOPY --from=buildr /x /x\n"
    ok, output, _ = build(text)
    assert not ok
    # The point: the failure is a registry failure, not "unknown stage".
    assert "buildr" in output
    assert any(s in output for s in ("pull access denied", "not found", "failed to resolve"))

    findings = analyse.run(text).findings
    assert [f.kind for f in findings] == ["copy-from-unknown"]
