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


def test_a_file_with_no_from_exits_two_and_claims_nothing_on_stdout(
    dockerfile, capsys
):
    # It builds nothing, so there is no report to give. An earlier version
    # printed "0 stages, building None" followed by "every instruction has an
    # effect on this build" -- two false claims -- and exited 0.
    assert main([str(dockerfile("RUN true\n"))]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "builds nothing" in captured.err


# A `--target` that names no stage is the case that sent this PR back: unbuilt
# analyses a different stage than the one asked for, so every number below is
# about a build nobody requested. It has to be loud in all three modes.
TYPO = ["--target", "no-such-stage"]


def test_an_unknown_target_exits_two_not_zero(dockerfile, capsys):
    assert main([str(dockerfile(CLEAN)), *TYPO]) == 2
    assert "no stage named no-such-stage" in capsys.readouterr().err


def test_an_unknown_target_is_not_silent_under_quiet(dockerfile, capsys):
    # The regression in full: -q is the mode documented for piping into CI,
    # and it was the one mode that printed nothing at all and exited 0.
    assert main([str(dockerfile(CLEAN)), *TYPO, "-q"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no stage named no-such-stage" in captured.err


def test_an_unknown_target_under_json_keeps_stdout_parseable(dockerfile, capsys):
    assert main([str(dockerfile(CLEAN)), *TYPO, "--json"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["errors"] == [
        "no stage named no-such-stage; analysed the last stage (f) instead"
    ]
    assert "no stage named no-such-stage" in captured.err


def test_an_error_outranks_a_finding_in_the_exit_code(dockerfile, capsys):
    # DIRTY on its own is exit 1. With a target that does not exist, the
    # finding is real but it is about a stage the caller did not ask for, so
    # "I could not do what you asked" is the answer that gets the exit code.
    assert main([str(dockerfile(DIRTY))]) == 1
    capsys.readouterr()
    assert main([str(dockerfile(DIRTY)), *TYPO]) == 2
    captured = capsys.readouterr()
    assert "superseded" in captured.out  # still reported, just not the verdict
    assert "no stage named no-such-stage" in captured.err


def test_a_real_target_is_still_quiet_on_stderr(dockerfile, capsys):
    # The guard against making every --target noisy: a stage that exists
    # produces no diagnostic at all.
    path = dockerfile("FROM alpine AS one\nRUN a\nFROM alpine AS two\nRUN b\n")
    assert main([str(path), "--target", "one"]) == 0
    assert capsys.readouterr().err == ""


#: The Dockerfile at the top of the README, verbatim. The block under it is
#: quoted output, so if this stops producing it the README is wrong.
README_LEAD = """\
FROM node:20-alpine AS build
WORKDIR /app
COPY . .
RUN npm ci && npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
ENV PORT=3000
ENV PORT=8080
COPY --from=builder /app/dist ./dist
EXPOSE ${PORT}
ENTRYPOINT ["node", "dist/server.js"]
"""


def test_the_readme_lead_example_still_prints_what_the_readme_says(
    dockerfile, capsys
):
    assert main([str(dockerfile(README_LEAD))]) == 1
    out = capsys.readouterr().out
    assert "2 stages, building runtime." in out
    assert "Not built for this target: build." in out
    assert "line 8  superseded  (runtime)" in out
    assert "line 10  copy-from-unknown  (runtime)" in out
    assert "the closest stage name is build" in out
    assert "2 instructions with no effect on this build." in out
    assert "line 1  orphan-stage  (build)" in out


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
