"""Parsing. Getting any of this wrong invents findings, so it is tested first."""

from __future__ import annotations

from unbuilt.parse import parse


def keywords(text: str) -> list[str]:
    return [i.keyword for i in parse(text).instructions]


def test_keywords_are_upper_cased():
    assert keywords("from alpine\nRuN true\n") == ["FROM", "RUN"]


def test_continuations_are_joined():
    parsed = parse("FROM alpine\nRUN apt-get update \\\n && apt-get install -y git\n")
    assert len(parsed.instructions) == 2
    assert parsed.instructions[1].argument == "apt-get update && apt-get install -y git"
    assert parsed.instructions[1].line == 2


def test_comment_inside_a_continuation_does_not_end_the_instruction():
    # The builder drops the comment and keeps going. A parser that stops here
    # would see the third line as an instruction of its own.
    parsed = parse("FROM alpine\nRUN one \\\n# why\n  two\nCMD [\"x\"]\n")
    assert [i.keyword for i in parsed.instructions] == ["FROM", "RUN", "CMD"]
    assert parsed.instructions[1].argument == "one two"


def test_a_doubled_escape_does_not_continue():
    parsed = parse("FROM alpine\nRUN echo a\\\\\nRUN echo b\n")
    assert [i.keyword for i in parsed.instructions] == ["FROM", "RUN", "RUN"]


def test_escape_directive_changes_the_continuation_character():
    parsed = parse("# escape=`\nFROM alpine\nRUN one `\n two\n")
    assert parsed.escape == "`"
    assert parsed.instructions[1].argument == "one two"


def test_syntax_directive_is_recorded():
    parsed = parse("# syntax=docker/dockerfile:1\nFROM alpine\n")
    assert parsed.directives["syntax"] == "docker/dockerfile:1"


def test_a_directive_after_a_plain_comment_is_just_a_comment():
    parsed = parse("# hello\n# escape=`\nFROM alpine\nRUN one `\n two\n")
    assert parsed.escape == "\\"


def test_flags_are_split_off_the_argument():
    instruction = parse("FROM alpine\nCOPY --from=build --chown=1:1 /a /b\n").instructions[1]
    assert instruction.flags == {"from": "build", "chown": "1:1"}
    assert instruction.argument == "/a /b"


def test_a_flag_shaped_token_after_the_argument_stays_in_the_argument():
    instruction = parse("FROM alpine\nRUN ls --color=auto\n").instructions[1]
    assert instruction.flags == {}
    assert instruction.argument == "ls --color=auto"


def test_heredoc_body_is_not_parsed_as_instructions():
    text = "FROM alpine\nRUN <<EOF\nFROM not-a-stage\nRUN not-an-instruction\nEOF\nCMD [\"x\"]\n"
    parsed = parse(text)
    assert [i.keyword for i in parsed.instructions] == ["FROM", "RUN", "CMD"]
    assert "not-a-stage" in parsed.instructions[1].heredocs[0]


def test_quoted_heredoc_tag_is_recognised():
    parsed = parse("FROM alpine\nRUN <<'EOF'\nFROM nope\nEOF\n")
    assert [i.keyword for i in parsed.instructions] == ["FROM", "RUN"]


def test_blank_and_comment_only_files_produce_nothing():
    assert parse("\n\n# just a comment\n").instructions == []
