"""The command line: exit codes, output modes, and failing usefully."""

from __future__ import annotations

import json

import pytest

from unbuilt.cli import main

DIRTY = "FROM alpine AS f\nENV P=1\nENV P=2\n"
CLEAN = "FROM alpine AS f\nENV P=1\nRUN true\n"


@pytest.fixture
def dockerfile(tmp_path):
    def write(text: str, name: str = "Dockerfile"):
        path = tmp_path / name
        path.write_text(text)
        return path

    return write


def test_findings_exit_one(dockerfile, capsys):
    assert main([str(dockerfile(DIRTY))]) == 1
    assert "superseded" in capsys.readouterr().out


def test_a_clean_file_exits_zero_and_says_so(dockerfile, capsys):
    assert main([str(dockerfile(CLEAN))]) == 0
    assert "Every instruction has an effect on this build." in capsys.readouterr().out


def test_a_missing_file_exits_two_with_a_message_on_stderr(tmp_path, capsys):
    assert main([str(tmp_path / "nope")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No such file" in captured.err


def test_a_directory_argument_looks_for_a_dockerfile_inside_it(dockerfile, tmp_path):
    dockerfile(DIRTY)
    assert main([str(tmp_path)]) == 1


def test_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(DIRTY))
    assert main(["-"]) == 1
    assert "<stdin>" in capsys.readouterr().out


def test_json_is_valid_and_carries_the_findings(dockerfile, capsys):
    main(["--json", str(dockerfile(DIRTY))])
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "f"
    assert payload["stages"] == 1
    assert [f["kind"] for f in payload["findings"]] == ["superseded"]
    assert payload["findings"][0]["line"] == 2
    assert payload["notes"] == []


def test_json_keeps_notes_separate_from_findings(dockerfile, capsys):
    path = dockerfile("FROM alpine AS lonely\nRUN a\nFROM alpine AS f\nRUN b\n")
    main(["--json", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"] == []
    assert [n["kind"] for n in payload["notes"]] == ["orphan-stage"]
    assert payload["not_built"] == ["lonely"]


def test_quiet_is_one_grep_able_line_per_finding(dockerfile, capsys):
    path = dockerfile(DIRTY)
    main(["--quiet", str(path)])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{path}:2: superseded: ")


def test_quiet_prints_nothing_at_all_for_a_clean_file(dockerfile, capsys):
    assert main(["--quiet", str(dockerfile(CLEAN))]) == 0
    assert capsys.readouterr().out == ""


def test_target_changes_which_stages_are_left_out(dockerfile, capsys):
    path = dockerfile("FROM alpine AS one\nEXPOSE 80\nFROM alpine AS two\nRUN b\n")
    assert main([str(path)]) == 0
    assert "Not built for this target: one." in capsys.readouterr().out

    assert main(["--target", "one", str(path)]) == 0
    assert "Not built for this target: two." in capsys.readouterr().out


def test_notes_are_printed_but_do_not_set_the_exit_code(dockerfile, capsys):
    path = dockerfile("FROM alpine AS lonely\nRUN a\nFROM alpine AS f\nRUN b\n")
    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "Worth knowing, but not necessarily wrong:" in out
    assert "orphan-stage" in out


def test_quiet_leaves_notes_out_entirely(dockerfile, capsys):
    path = dockerfile("FROM alpine AS lonely\nRUN a\nFROM alpine AS f\nRUN b\n")
    assert main(["--quiet", str(path)]) == 0
    assert capsys.readouterr().out == ""


def test_an_unreadable_dockerfile_does_not_traceback(dockerfile):
    path = dockerfile(DIRTY)
    path.chmod(0o000)
    try:
        assert main([str(path)]) == 2
    finally:
        path.chmod(0o644)


def test_a_file_with_no_from_reports_the_error_and_exits_zero(dockerfile, capsys):
    # Nothing was found to be dead, so this is not a findings exit. The error
    # still gets printed.
    assert main([str(dockerfile("RUN true\n"))]) == 0
    assert "builds nothing" in capsys.readouterr().out


def test_the_examples_dockerfile_is_the_one_the_readme_describes(capsys):
    # The README quotes this run. If the example or the tool changes, this is
    # the test that notices before a reader does.
    assert main(["examples/Dockerfile"]) == 1
    out = capsys.readouterr().out
    assert "Not built for this target: build, test." in out
    assert "4 instructions with no effect on this build." in out
    for kind in (
        "unused-arg",
        "empty-expansion",
        "superseded",
        "copy-from-unknown",
        "orphan-stage",
    ):
        assert kind in out
