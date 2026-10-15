"""The checks, and -- at least as important -- what they refuse to report.

Roughly half of these are negative: a tool that flags a correct line is worse
than no tool, because you stop reading its output.
"""

from __future__ import annotations

from unbuilt import analyse


def kinds(text: str, **kwargs) -> list[str]:
    return [f.kind for f in analyse.run(text, **kwargs).findings]


def at(text: str, line: int, **kwargs) -> list[str]:
    return [f.kind for f in analyse.run(text, **kwargs).findings if f.line == line]


# --- unreachable stages ----------------------------------------------------


def test_a_stage_nothing_needs_is_reported_once():
    text = "FROM alpine AS dead\nRUN a\nRUN b\nFROM alpine AS final\nRUN c\n"
    assert kinds(text) == ["unreachable-stage"]
    assert "2 instructions never run" in analyse.run(text).findings[0].detail


def test_a_stage_copied_from_is_not_unreachable():
    text = "FROM alpine AS src\nRUN a\nFROM alpine AS final\nCOPY --from=src /a /a\n"
    assert "unreachable-stage" not in kinds(text)


def test_a_base_of_the_target_is_not_unreachable():
    assert "unreachable-stage" not in kinds("FROM alpine AS base\nRUN a\nFROM base AS f\n")


def test_targeting_a_stage_makes_the_later_ones_unreachable():
    text = "FROM alpine AS one\nRUN a\nFROM alpine AS two\nRUN b\n"
    assert kinds(text, target="one") == ["unreachable-stage"]


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


def test_metadata_in_an_unreachable_stage_is_not_reported_twice():
    text = "FROM alpine AS dead\nEXPOSE 80\nENTRYPOINT [\"/x\"]\nFROM alpine AS f\n"
    assert kinds(text) == ["unreachable-stage"]


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


# --- unused ARGs -----------------------------------------------------------


def test_an_arg_nothing_mentions_is_reported():
    assert kinds("ARG UNUSED\nFROM alpine AS f\nRUN true\n") == ["unused-arg"]


def test_an_arg_a_run_mentions_is_used():
    assert kinds("FROM alpine AS f\nARG TOKEN\nRUN echo $TOKEN\n") == []


def test_an_arg_in_an_unreachable_stage_is_only_reported_as_the_stage():
    text = "FROM alpine AS dead\nARG UNUSED\nFROM alpine AS f\nRUN true\n"
    assert kinds(text) == ["unreachable-stage"]


# --- shape of the result ---------------------------------------------------


def test_findings_come_out_in_line_order():
    text = (
        "ARG UNUSED\nFROM alpine AS dead\nRUN a\n"
        "FROM alpine AS f\nENV P=1\nENV P=2\n"
    )
    lines = [f.line for f in analyse.run(text).findings]
    assert lines == sorted(lines)


def test_a_file_with_no_from_is_an_error_not_a_finding():
    result = analyse.run("RUN true\n")
    assert result.findings == []
    assert result.errors and "builds nothing" in result.errors[0]


def test_an_unknown_target_is_an_error_and_analysis_still_happens():
    result = analyse.run("FROM alpine AS f\nENV P=1\nENV P=2\n", target="nope")
    assert any("no stage named nope" in e for e in result.errors)
    assert [f.kind for f in result.findings] == ["superseded"]


def test_a_clean_dockerfile_reports_nothing():
    text = (
        "ARG TAG=3\nFROM alpine:${TAG} AS build\nWORKDIR /src\nCOPY . .\nRUN make\n"
        "FROM alpine:${TAG} AS final\nCOPY --from=build /src/out /usr/bin/out\n"
        "EXPOSE 80\nENTRYPOINT [\"/usr/bin/out\"]\n"
    )
    assert analyse.run(text).findings == []
    assert analyse.run(text).ok
