"""The checks, and -- at least as important -- what they refuse to report.

Roughly half of these are negative: a tool that flags a correct line is worse
than no tool, because you stop reading its output.
"""

from __future__ import annotations

from unbuilt import analyse


def kinds(text: str, **kwargs) -> list[str]:
    return [f.kind for f in analyse.run(text, **kwargs).findings]


def notes(text: str, **kwargs) -> list[str]:
    return [f.kind for f in analyse.run(text, **kwargs).notes]


def at(text: str, line: int, **kwargs) -> list[str]:
    return [f.kind for f in analyse.run(text, **kwargs).findings if f.line == line]


# --- orphan stages ---------------------------------------------------------
#
# An orphan is a note, not a finding: whether it is dead depends on whether
# some CI job names it with --target, which is not in the file.


def test_a_stage_nothing_refers_to_is_a_note_not_a_finding():
    text = "FROM alpine AS lonely\nRUN a\nRUN b\nFROM alpine AS final\nRUN c\n"
    assert kinds(text) == []
    assert notes(text) == ["orphan-stage"]
    assert "--target lonely" in analyse.run(text).notes[0].detail


def test_a_stage_copied_from_is_not_an_orphan():
    text = "FROM alpine AS src\nRUN a\nFROM alpine AS final\nCOPY --from=src /a /a\n"
    assert notes(text) == []


def test_a_base_of_another_stage_is_not_an_orphan():
    assert notes("FROM alpine AS base\nRUN a\nFROM base AS f\nRUN b\n") == []


def test_the_target_itself_is_never_an_orphan():
    assert notes("FROM alpine AS only\nRUN a\n") == []


def test_an_alias_stage_with_no_instructions_is_not_worth_mentioning():
    # `FROM alpine AS base` with nothing in it is how stage-library
    # Dockerfiles name their inputs. There is nothing in it to be dead.
    assert notes("FROM alpine AS base\nFROM alpine AS final\nRUN a\n") == []


def test_a_stage_referenced_only_by_an_orphan_is_still_not_an_orphan():
    # The chain is real even if nothing asks for its end. Saying otherwise
    # would cascade one note into a report about half the file.
    text = (
        "FROM alpine AS build\nRUN a\n"
        "FROM alpine AS test\nCOPY --from=build /a /a\n"
        "FROM alpine AS final\nRUN b\n"
    )
    assert notes(text) == ["orphan-stage"]
    assert analyse.run(text).notes[0].stage == "test"
    assert analyse.run(text).skipped_stages == ["build", "test"]


def test_targeting_an_earlier_stage_does_not_turn_later_ones_into_findings():
    text = "FROM alpine AS one\nRUN a\nFROM alpine AS two\nRUN b\n"
    assert kinds(text, target="one") == []
    assert analyse.run(text, target="one").skipped_stages == ["two"]


# --- configuration that does not reach the image ---------------------------

ARTIFACT_ONLY = """\
FROM alpine AS builder
RUN make
ENV RUNTIME_FLAG=yes
EXPOSE 9999
ENTRYPOINT ["/builder"]
FROM alpine AS final
COPY --from=builder /out /out
"""


def test_metadata_in_an_artifact_only_stage_is_discarded():
    assert at(ARTIFACT_ONLY, 4) == ["discarded-metadata"]
    assert at(ARTIFACT_ONLY, 5) == ["discarded-metadata"]


def test_env_in_an_artifact_only_stage_with_nothing_to_read_it_is_discarded():
    assert at(ARTIFACT_ONLY, 3) == ["discarded-setting"]


def test_env_a_later_run_in_the_same_stage_could_use_is_not_reported():
    text = (
        "FROM alpine AS builder\nENV CFLAGS=-O2\nRUN make\n"
        "FROM alpine AS final\nCOPY --from=builder /out /out\n"
    )
    assert at(text, 2) == []


def test_env_a_later_copy_only_uses_if_it_names_a_variable():
    used = (
        "FROM alpine AS b\nENV DEST=/opt\nCOPY x ${DEST}\n"
        "FROM alpine AS f\nCOPY --from=b /opt /opt\n"
    )
    unused = (
        "FROM alpine AS b\nENV DEST=/opt\nCOPY x /literal\n"
        "FROM alpine AS f\nCOPY --from=b /opt /opt\n"
    )
    assert at(used, 2) == []
    assert at(unused, 2) == ["discarded-setting"]


def test_an_env_any_later_instruction_names_is_not_discarded():
    # grafana's Dockerfile, reduced: the ENV is read by the WORKDIR under it.
    # An earlier version only looked at RUN/COPY/ADD and called this dead.
    text = (
        "FROM alpine AS assets\nENV GF_PATHS_HOME=/usr/share/grafana\n"
        "WORKDIR $GF_PATHS_HOME\nCOPY x ./bin/\n"
        "FROM alpine AS f\nCOPY --from=assets /usr/share/grafana /g\n"
    )
    assert at(text, 2) == []


def test_an_env_read_only_by_a_metadata_instruction_still_counts():
    text = (
        "FROM alpine AS b\nENV PORT=8080\nEXPOSE ${PORT}\n"
        "FROM alpine AS f\nCOPY --from=b /x /x\n"
    )
    # EXPOSE itself is discarded here, but it does read PORT, so PORT is not.
    assert at(text, 2) == []
    assert at(text, 3) == ["discarded-metadata"]


def test_an_env_a_descendant_stage_reads_is_not_discarded():
    # n8n's Dockerfile, reduced: `toolchain` sets NODE_GYP and the stage built
    # FROM it runs `node "$NODE_GYP"`. Looking only inside the one stage calls
    # that dead.
    text = (
        "FROM alpine AS toolchain\nENV NODE_GYP=/usr/bin/node-gyp\n"
        "FROM toolchain AS native\nRUN node \"$NODE_GYP\" rebuild\n"
        "FROM alpine AS f\nCOPY --from=native /build /build\n"
    )
    assert at(text, 2) == []


def test_any_run_downstream_is_enough_to_keep_an_env():
    # Deliberately conservative, and the reason is not fixable: a RUN gets
    # every ARG and ENV in its environment, so the program it invokes can read
    # UNUSED_FLAG without the Dockerfile naming it. unbuilt cannot see inside
    # `make`, so it does not claim the line is dead.
    text = (
        "FROM alpine AS toolchain\nENV UNUSED_FLAG=1\n"
        "FROM toolchain AS native\nRUN make\n"
        "FROM alpine AS f\nCOPY --from=native /build /build\n"
    )
    assert at(text, 2) == []


def test_an_env_with_no_run_anywhere_downstream_is_discarded():
    text = (
        "FROM alpine AS toolchain\nENV UNUSED_FLAG=1\n"
        "FROM toolchain AS native\nCOPY in /out\n"
        "FROM alpine AS f\nCOPY --from=native /out /out\n"
    )
    assert at(text, 2) == ["discarded-setting"]


def test_a_workdir_whose_stage_has_no_descendants_and_no_build_steps():
    # outline's Dockerfile, reduced: `base` is an ARG and a WORKDIR, nothing
    # is built FROM it, and every COPY --from=base uses an absolute path.
    text = (
        "ARG APP_PATH=/opt/app\nFROM scratch AS base\nARG APP_PATH\n"
        "WORKDIR $APP_PATH\n"
        "FROM alpine AS runner\nCOPY --from=base /opt/app/build ./build\n"
    )
    assert at(text, 4) == ["discarded-setting"]


def test_workdir_before_a_build_step_is_never_reported():
    # WORKDIR changes where a COPY lands, variables or not.
    text = (
        "FROM alpine AS b\nWORKDIR /app\nCOPY x y\n"
        "FROM alpine AS f\nCOPY --from=b /app /app\n"
    )
    assert at(text, 2) == []


def test_metadata_in_the_targets_own_lineage_is_kept():
    text = "FROM alpine AS base\nEXPOSE 80\nENV A=1\nFROM base AS final\nRUN true\n"
    assert kinds(text) == []


def test_discarded_config_is_only_claimed_about_stages_this_target_builds():
    # `lonely` is not built for this target, so unbuilt makes no claim about
    # what its EXPOSE reaches -- `--target lonely` and it reaches the image.
    text = "FROM alpine AS lonely\nEXPOSE 80\nENTRYPOINT [\"/x\"]\nFROM alpine AS f\nRUN a\n"
    assert kinds(text) == []
    assert kinds(text, target="lonely") == []


# --- ARG scope -------------------------------------------------------------


def test_a_global_arg_used_in_a_stage_without_redeclaring_is_empty():
    text = "ARG V=1\nFROM alpine AS final\nENV X=${V}\n"
    assert kinds(text) == ["empty-expansion"]


def test_redeclaring_the_arg_in_the_stage_puts_it_in_scope():
    assert kinds("ARG V=1\nFROM alpine AS final\nARG V\nENV X=${V}\n") == []


def test_an_arg_below_the_use_does_not_reach_back_up_to_fill_it():
    # Scope is positional. Docker builds this file with LEAKED="" and
    # REDECLARED set, so only the first line is a finding.
    text = "ARG V=1\nFROM alpine AS f\nENV LEAKED=${V}\nARG V\nENV OK=${V}\n"
    assert at(text, 3) == ["empty-expansion"]
    assert at(text, 5) == []


def test_a_name_assigned_earlier_in_the_same_env_is_in_scope():
    assert kinds("ARG V=1\nFROM alpine AS f\nENV V=2 W=${V}\n") == []


def test_a_global_arg_in_a_from_line_is_in_scope_and_counts_as_used():
    assert kinds("ARG TAG=3\nFROM alpine:${TAG} AS final\nRUN true\n") == []


def test_an_ancestors_env_is_in_scope_down_the_from_chain():
    text = "FROM alpine AS base\nENV V=1\nFROM base AS final\nENV W=${V}\n"
    assert kinds(text) == []


def test_an_ancestors_arg_is_not_in_scope_in_the_child():
    # ARG does not inherit, so this one is genuinely empty -- but the name is
    # not a global, so only --strict has an opinion about it.
    text = "FROM alpine AS base\nARG V=1\nFROM base AS final\nENV W=${V}\n"
    assert kinds(text) == []
    assert "unknown-expansion" in kinds(text, strict=True)


def test_an_undeclared_name_is_quiet_without_strict():
    assert kinds("FROM alpine AS final\nENV X=${WHO_KNOWS}\n") == []


def test_strict_leaves_run_alone_because_that_is_the_shells_dollar_sign():
    text = "FROM alpine AS final\nRUN echo $1 $@ $HOME\n"
    assert kinds(text, strict=True) == []


def test_an_escaped_dollar_is_not_a_reference():
    # `\$V` is a literal, so there is no expansion to call empty -- and V ends
    # up genuinely unreferenced, which is the other finding here.
    assert kinds("ARG V=1\nFROM alpine AS final\nENV X=\\$V\n") == ["unused-arg"]


def test_a_default_in_the_braces_is_still_a_reference_to_the_name():
    assert kinds("ARG V=1\nFROM alpine AS final\nENV X=${V:-fallback}\n") == [
        "empty-expansion"
    ]


def test_a_reference_inside_a_heredoc_counts():
    text = "ARG V=1\nFROM alpine AS final\nRUN <<EOF\necho ${V}\nEOF\n"
    assert kinds(text) == ["empty-expansion"]


# --- superseded settings ---------------------------------------------------


def test_a_second_entrypoint_in_a_stage_kills_the_first():
    text = "FROM alpine AS f\nENTRYPOINT [\"/a\"]\nRUN true\nENTRYPOINT [\"/b\"]\n"
    assert at(text, 2) == ["superseded"]


def test_env_set_twice_with_nothing_between_is_reported():
    assert at("FROM alpine AS f\nENV P=1\nENV P=2\n", 2) == ["superseded"]


def test_env_set_twice_around_a_run_is_not_reported():
    assert at("FROM alpine AS f\nENV P=1\nRUN use-it\nENV P=2\n", 2) == []


def test_a_different_key_is_not_a_supersede():
    assert kinds("FROM alpine AS f\nENV A=1\nENV B=2\n") == []


def test_the_legacy_space_separated_env_form_is_understood():
    assert at("FROM alpine AS f\nENV P 1\nENV P=2\n", 2) == ["superseded"]


def test_env_with_several_pairs_on_one_line():
    assert at("FROM alpine AS f\nENV A=1 B=2\nENV B=3\n", 2) == ["superseded"]


# --- COPY --from -----------------------------------------------------------


def test_a_bare_from_that_is_not_a_stage_is_reported_with_the_near_miss():
    text = "FROM alpine AS build\nRUN a\nFROM alpine AS f\nCOPY --from=builder /a /a\n"
    findings = [f for f in analyse.run(text).findings if f.kind == "copy-from-unknown"]
    assert len(findings) == 1
    assert "the closest stage name is build" in findings[0].detail


def test_a_tagged_or_registry_image_is_left_alone():
    for ref in ("nginx:alpine", "ghcr.io/x/y", "alpine@sha256:abc", "x.io/y"):
        text = f"FROM alpine AS f\nCOPY --from={ref} /a /a\n"
        assert "copy-from-unknown" not in kinds(text), ref


def test_a_real_stage_name_is_left_alone():
    text = "FROM alpine AS build\nRUN a\nFROM alpine AS f\nCOPY --from=build /a /a\n"
    assert "copy-from-unknown" not in kinds(text)


# --- COPY of something only ADD can fetch ----------------------------------


def only_remote(text: str, **kwargs) -> list:
    return [
        f
        for f in analyse.run(text, **kwargs).findings
        if f.kind == "copy-remote-source"
    ]


def test_a_copy_of_a_git_ref_is_the_build_failing():
    text = "FROM alpine AS f\nCOPY https://example.com/o/r.git /src\n"
    findings = only_remote(text)
    assert len(findings) == 1
    assert "only ADD can fetch one" in findings[0].detail
    assert "source can't be a git ref for COPY" in findings[0].detail


def test_a_copy_of_a_url_quotes_the_other_refusal():
    text = "FROM alpine AS f\nCOPY https://example.com/x.tar.gz /src\n"
    findings = only_remote(text)
    assert len(findings) == 1
    assert "source can't be a URL for COPY" in findings[0].detail


def test_from_does_not_excuse_it():
    """Measured: the refusal is on the operand's shape, --from or no --from."""
    text = (
        "FROM alpine AS b\nRUN a\n"
        "FROM alpine AS f\nCOPY --from=b https://example.com/o/r.git /src\n"
    )
    assert len(only_remote(text)) == 1


def test_add_is_how_you_do_it_and_is_not_reported():
    for source in ("https://example.com/o/r.git", "https://example.com/x.tar.gz"):
        text = f"FROM alpine AS f\nADD {source} /src\n"
        assert only_remote(text) == [], source


def test_a_local_path_that_looks_remote_is_not_reported():
    """logbook#47 from the other side: these are ordinary context reads."""
    for source in (".git", "foo.git", "example.com/o/r.git", "git+ssh://h/o/r.git"):
        text = f"FROM alpine AS f\nCOPY {source} /src\n"
        assert only_remote(text) == [], source


def test_a_stage_this_target_does_not_build_is_a_note_not_a_failure():
    """Measured: an orphan stage is never converted, so the refusal never fires.

    A note rather than silence, because a stage nothing builds is the only
    place this mistake can sit in a repository without failing something.
    """
    text = (
        "FROM alpine AS dead\nCOPY https://example.com/o/r.git /src\n"
        "FROM alpine AS f\nRUN a\n"
    )
    assert only_remote(text) == []
    assert notes(text) == ["orphan-stage", "copy-remote-source"]
    assert "does not build dead" in analyse.run(text).notes[1].detail

    # ...and naming the stage makes it the failure it is.
    assert len(only_remote(text, target="dead")) == 1
    assert "copy-remote-source" not in notes(text, target="dead")


def test_several_remote_sources_on_one_line_report_once():
    text = (
        "FROM alpine AS f\n"
        "COPY https://example.com/a.git https://example.com/b.git /src/\n"
    )
    findings = only_remote(text)
    assert len(findings) == 1
    assert "(and 1 more)" in findings[0].detail


def test_a_source_it_cannot_read_is_not_guessed_at():
    text = "FROM alpine AS f\nCOPY ${REPO_URL} /src\n"
    assert only_remote(text) == []


# --- unused ARGs -----------------------------------------------------------


def test_an_arg_nothing_mentions_is_reported():
    assert kinds("ARG UNUSED\nFROM alpine AS f\nRUN true\n") == ["unused-arg"]


def test_an_arg_a_run_mentions_is_used():
    assert kinds("FROM alpine AS f\nARG TOKEN\nRUN echo $TOKEN\n") == []


def test_a_stage_arg_with_a_run_below_it_is_left_alone():
    # The RUN gets NAME in its environment, so the program it invokes can read
    # it with nothing in the Dockerfile naming it. buildkit's own
    # `ARG CGO_ENABLED=0` works precisely this way.
    assert kinds("FROM alpine AS f\nARG CGO_ENABLED=0\nRUN go build\n") == []


def test_a_stage_arg_with_no_run_below_it_is_reported():
    assert kinds("FROM alpine AS f\nRUN go build\nARG LATE=1\n") == ["unused-arg"]


def test_a_global_arg_is_judged_without_the_run_let_off():
    # Globals are injected into no stage's environment, so a RUN cannot be
    # quietly reading one.
    assert kinds("ARG UNUSED\nFROM alpine AS f\nRUN go build\n") == ["unused-arg"]


def test_build_args_buildkit_reads_for_itself_are_not_unused():
    text = "FROM alpine AS f\nARG BUILDKIT_SBOM_SCAN_STAGE=true\n"
    assert kinds(text) == []


# --- shape of the result ---------------------------------------------------


def test_findings_come_out_in_line_order():
    text = (
        "ARG UNUSED\nFROM alpine AS f\nENV P=1\nENV P=2\n"
        "ENTRYPOINT [\"/a\"]\nENTRYPOINT [\"/b\"]\n"
    )
    lines = [f.line for f in analyse.run(text).findings]
    assert len(lines) == 3
    assert lines == sorted(lines)


def test_a_file_with_no_from_is_an_error_not_a_finding():
    result = analyse.run("RUN true\n")
    assert result.findings == []
    assert result.errors and "builds nothing" in result.errors[0]


def test_an_unknown_target_is_an_error_and_analysis_still_happens():
    result = analyse.run("FROM alpine AS f\nENV P=1\nENV P=2\n", target="nope")
    assert any("no stage named nope" in e for e in result.errors)
    assert [f.kind for f in result.findings] == ["superseded"]


def test_a_stage_library_dockerfile_is_quiet():
    """The shape that drove the design: many stages, built one at a time.

    grafana's and buildkit's Dockerfiles look like this. An earlier version
    reported every stage the default target did not need, which was 20-odd
    findings per file and all of them useless. Nothing here is a finding; the
    per-platform entry points are notes.
    """
    text = (
        "ARG GO=1.23\n"
        "FROM golang:${GO} AS build-base\nRUN apk add git\n"
        "FROM build-base AS build\nCOPY . .\nRUN go build -o /out/app\n"
        "FROM alpine AS release-linux\nCOPY --from=build /out/app /app\n"
        "FROM alpine AS release-windows\nCOPY --from=build /out/app /app.exe\n"
        "FROM release-linux AS final\nENTRYPOINT [\"/app\"]\n"
    )
    result = analyse.run(text)
    assert result.findings == []
    assert [f.stage for f in result.notes] == ["release-windows"]


def test_a_clean_dockerfile_reports_nothing():
    text = (
        "ARG TAG=3\nFROM alpine:${TAG} AS build\nWORKDIR /src\nCOPY . .\nRUN make\n"
        "FROM alpine:${TAG} AS final\nCOPY --from=build /src/out /usr/bin/out\n"
        "EXPOSE 80\nENTRYPOINT [\"/usr/bin/out\"]\n"
    )
    assert analyse.run(text).findings == []
    assert analyse.run(text).ok
