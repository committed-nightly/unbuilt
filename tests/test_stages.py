"""The stage graph: built-or-not, and whose configuration reaches the image."""

from __future__ import annotations

from unbuilt import stages
from unbuilt.parse import parse

MULTI = """\
FROM alpine AS base
FROM base AS middle
FROM alpine AS helper
FROM middle AS final
COPY --from=helper /a /a
"""


def graph(text: str, target: str | None = None) -> stages.Graph:
    return stages.build(parse(text).instructions, target=target)


def built_labels(g: stages.Graph) -> set[str]:
    return {g.stages[i].label for i in g.built}


def test_default_target_is_the_last_stage():
    assert graph(MULTI).target.label == "final"


def test_from_chain_is_the_lineage_and_a_copy_source_is_not():
    g = graph(MULTI)
    assert [g.stages[i].label for i in g.lineage] == ["final", "middle", "base"]
    assert built_labels(g) == {"final", "middle", "base", "helper"}


def test_a_stage_nobody_needs_is_not_built():
    g = graph(MULTI + "\nFROM alpine AS orphan\nRUN true\n")
    # `orphan` is last, so it becomes the target and everything else drops out.
    assert g.target.label == "orphan"
    assert built_labels(g) == {"orphan"}


def test_targeting_an_earlier_stage_drops_the_later_ones():
    g = graph(MULTI, target="middle")
    assert built_labels(g) == {"middle", "base"}
    assert "helper" not in built_labels(g)


def test_target_names_are_case_insensitive():
    assert graph(MULTI, target="FINAL").target.label == "final"


def test_an_unknown_target_is_reported_and_falls_back_to_the_last_stage():
    g = graph(MULTI, target="nope")
    assert g.unknown_target == "nope"
    assert g.target.label == "final"


def test_a_stage_can_be_referenced_by_index():
    g = graph("FROM alpine\nFROM alpine AS final\nCOPY --from=0 /a /a\n")
    assert len(g.built) == 2


def test_run_mount_from_counts_as_needing_a_stage():
    g = graph(
        "FROM alpine AS cache\nFROM alpine AS final\n"
        "RUN --mount=type=bind,from=cache,target=/c true\n"
    )
    assert built_labels(g) == {"cache", "final"}


def test_a_from_naming_a_later_stage_is_not_a_back_reference():
    # Stage order matters to the builder: `later` is not in scope yet here.
    g = graph("FROM later AS first\nRUN true\nFROM alpine AS later\n")
    assert g.target.label == "later"
    assert built_labels(g) == {"later"}


def test_a_file_with_no_from_has_no_stages():
    assert graph("ARG X=1\n").stages == []


def test_instructions_land_in_the_stage_they_follow():
    g = graph("FROM alpine AS one\nRUN a\nFROM alpine AS two\nRUN b\n")
    assert [i.argument for i in g.stages[0].instructions] == ["a"]
    assert [i.argument for i in g.stages[1].instructions] == ["b"]
