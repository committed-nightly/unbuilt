"""The `.dockerignore` matcher and the context lookups built on it.

The claims these make about Docker are re-measured against a real builder in
`test_against_docker.py`. What is here is the behaviour that follows once
those claims are settled: parsing, precedence, and the decisions unbuilt
makes about a `COPY` it cannot fully read.
"""

from __future__ import annotations

import pytest

from unbuilt import analyse
from unbuilt.context import BadPattern, Context, Ignore, ignore_file_for, normalise
from unbuilt.parse import parse


def excluded(patterns: str, path: str) -> bool:
    return Ignore.parse(patterns).excludes(path)


class TestPatterns:
    def test_a_star_does_not_cross_a_slash(self):
        assert excluded("*.log", "top.log")
        assert not excluded("*.log", "a/mid.log")

    def test_double_star_spans_any_number_of_segments_including_none(self):
        assert excluded("**/*.log", "top.log")
        assert excluded("**/*.log", "a/b/deep.log")
        assert excluded("x/**/deep.txt", "x/y/z/deep.txt")

    def test_a_directory_pattern_takes_everything_under_it(self):
        assert excluded("a", "a/b/deep.log")
        assert excluded("a/", "a/b/deep.log")
        assert excluded("a*", "ab/c/f.txt")  # the parent match may be a glob

    def test_a_leading_slash_is_dropped_not_anchored_elsewhere(self):
        assert excluded("/top.log", "top.log")
        assert not excluded("/top.log", "a/top.log")

    def test_the_last_matching_pattern_wins(self):
        assert not excluded("*.log\n!top.log", "top.log")
        assert excluded("*.log\n!top.log\ntop.log", "top.log")

    def test_a_negation_re_includes_under_an_excluded_directory(self):
        """git cannot do this. Docker can, at any depth."""
        assert not excluded("a\n!a/mid.txt", "a/mid.txt")
        assert not excluded("a\n!a/b/deep.txt", "a/b/deep.txt")
        assert excluded("a\n!a/mid.txt", "a/other.txt")

    def test_character_classes_and_single_character_wildcards(self):
        assert excluded("secret.[kx]ey", "secret.key")
        assert excluded("secret.ke?", "secret.key")
        assert not excluded("secret.ke?", "secret.keys")

    def test_regex_metacharacters_in_a_name_are_literal(self):
        assert excluded("w/na+me.txt", "w/na+me.txt")
        assert not excluded("w/na+me.txt", "w/naame.txt")
        assert excluded("w/pa(ren).txt", "w/pa(ren).txt")
        assert not excluded("w/do.t.txt", "w/doXt.txt")

    def test_comments_blank_lines_and_surrounding_space(self):
        assert excluded("# a comment\n\n  secret.key  ", "secret.key")
        assert not excluded("#secret.key", "secret.key")

    def test_patterns_that_clean_away_to_nothing_are_ignored(self):
        for degenerate in (".", "/", "./"):
            assert not excluded(degenerate, "f.txt"), degenerate

    def test_dot_segments_are_cleaned_before_matching(self):
        assert excluded("./a/mid.txt", "a/mid.txt")
        assert excluded("a/../root.txt", "root.txt")

    def test_blame_names_the_line_that_did_it(self):
        ignore = Ignore.parse("# header\n*.log\n!keep.log")
        assert ignore.blame("x.log").line == 2
        assert ignore.blame("x.log").source == "*.log"
        assert ignore.blame("keep.log") is None  # re-included, so nobody is to blame

    @pytest.mark.parametrize("pattern", ["f[.txt", "f.txt\\", "!"])
    def test_a_pattern_docker_rejects_is_refused_rather_than_guessed(self, pattern):
        """`docker build` exits before reading the Dockerfile on each of these.

        Guessing would be worse than refusing: dropping a `!` line silently
        turns re-included files back into excluded ones, which is the exact
        direction that invents a dead `COPY`.
        """
        with pytest.raises(BadPattern):
            Ignore.parse(pattern)


class TestIgnoreFile:
    def test_a_per_dockerfile_ignore_file_replaces_the_plain_one(self, tmp_path):
        (tmp_path / ".dockerignore").write_text("one.txt\n")
        (tmp_path / "Dockerfile.foo.dockerignore").write_text("two.txt\n")
        chosen = ignore_file_for(tmp_path, tmp_path / "Dockerfile.foo")
        assert chosen.name == "Dockerfile.foo.dockerignore"

    def test_it_falls_back_when_there_is_no_specific_one(self, tmp_path):
        (tmp_path / ".dockerignore").write_text("one.txt\n")
        chosen = ignore_file_for(tmp_path, tmp_path / "Dockerfile.foo")
        assert chosen.name == ".dockerignore"


class TestSources:
    def sources(self, line: str):
        return analyse.copy_sources(parse(line).instructions[0])

    def test_the_last_word_is_the_destination(self):
        assert self.sources("COPY a.txt b.txt /dst/") == ["a.txt", "b.txt"]

    def test_the_json_form(self):
        assert self.sources('COPY ["a.txt", "/dst"]') == ["a.txt"]

    def test_flags_are_already_off_the_argument(self):
        assert self.sources("COPY --chown=1:1 a.txt /dst") == ["a.txt"]

    @pytest.mark.parametrize(
        "line",
        [
            "COPY ${BUILD_DIR}/app /app",  # unresolved expansion
            "ADD https://example.com/x.tar /x",  # fetched, never in the context
            "ADD git@github.com:o/r.git /src",
            "COPY only-one-word",
            'COPY ["a.txt", 7]',
            "COPY 'unbalanced /dst",
        ],
    )
    def test_an_argument_it_cannot_read_drops_the_whole_instruction(self, line):
        """Half-reading an argument list is how a working COPY gets called dead."""
        assert self.sources(line) is None

    def test_a_path_leaving_the_context_is_not_ours_to_judge(self):
        assert normalise("../outside.txt") is None
        assert normalise("a/../../out") is None
        assert normalise("a/../in.txt") == "in.txt"


@pytest.fixture
def tree(tmp_path):
    """A small context: src/{a,b}.go, dist/bundle.js, app.key, README.md."""

    def build(ignore: str = ""):
        for rel in ("src/a.go", "src/b.go", "dist/bundle.js", "app.key", "README.md"):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        (tmp_path / ".dockerignore").write_text(ignore)
        return Context(tmp_path, Ignore.parse(ignore))

    return build


class TestContextResolution:
    def test_a_pattern_resolves_one_directory_level_like_filepath_match(self, tree):
        context = tree()
        assert context.resolve("src/*.go").matches == ("src/a.go", "src/b.go")
        # No `**` in a COPY source: this reaches exactly one level down.
        assert context.resolve("**/*.go").matches == ("src/a.go", "src/b.go")

    def test_a_named_source_resolves_only_if_it_is_there(self, tree):
        context = tree()
        assert context.resolve("README.md").matches == ("README.md",)
        assert context.resolve("nope.md").matches == ()

    def test_survives_walks_into_an_excluded_directory_when_negations_exist(self, tree):
        assert not tree("dist").survives("dist")
        assert tree("dist\n!dist/bundle.js").survives("dist")

    def test_an_excluded_directory_with_no_negations_anywhere_is_settled(self, tree):
        context = tree("src")
        assert not context.survives("src")
        assert not context.ignore.has_negations


class TestVerdicts:
    """What `--context` turns into findings, with the context on disk."""

    def analyse(self, tree, ignore, dockerfile, strict=False):
        context = tree(ignore)
        result = analyse.run(dockerfile, build=context, strict=strict)
        return [(f.kind, f.line) for f in result.findings], [
            (n.kind, n.line) for n in result.notes
        ]

    def test_a_pattern_whose_every_match_is_excluded(self, tree):
        findings, _ = self.analyse(
            tree, "*.key", "FROM scratch\nCOPY *.key /secrets/\n"
        )
        assert findings == [("dockerignored-copy", 2)]

    def test_a_named_source_that_is_excluded(self, tree):
        findings, _ = self.analyse(tree, "dist", "FROM scratch\nCOPY dist /dist\n")
        assert findings == [("dockerignored-copy", 2)]

    def test_a_copy_that_still_copies_something_is_not_reported_at_all(self, tree):
        """Losing *some* of what a pattern matches is `.dockerignore` working.

        `COPY . /app` next to any sensible ignore file is this case, so a
        tool that said anything here would say it about every repository
        that uses the feature correctly.
        """
        findings, notes = self.analyse(
            tree, "src/b.go", "FROM scratch\nCOPY src/*.go /src/\n"
        )
        assert (findings, notes) == ([], [])

    def test_one_excluded_name_is_fatal_even_beside_sources_that_work(self, tree):
        """The build fails, so the other operands do not rescue the line."""
        findings, _ = self.analyse(
            tree, "app.key", "FROM scratch\nCOPY README.md app.key /dst/\n"
        )
        assert findings == [("dockerignored-copy", 2)]

    def test_but_one_excluded_pattern_beside_a_working_source_is_not(self, tree):
        """`COPY *.key src/*.go /dst/` builds, having copied the Go files."""
        findings, _ = self.analyse(
            tree, "*.key", "FROM scratch\nCOPY *.key src/*.go /dst/\n"
        )
        assert findings == []

    @pytest.mark.parametrize("source", [".", "./", "/"])
    def test_copying_the_whole_context_is_never_reported(self, tree, source):
        """`COPY . /app` beside a `.dockerignore` is the commonest pair of
        lines there is, and the context root is not a thing that goes missing.
        """
        findings, notes = self.analyse(
            tree, "*.key\ndist\nsrc", f"FROM scratch\nCOPY {source} /app\n"
        )
        assert (findings, notes) == ([], [])

    def test_an_untouched_copy_is_silent(self, tree):
        findings, notes = self.analyse(
            tree, "*.key", "FROM scratch\nCOPY src/*.go /src/\n"
        )
        assert (findings, notes) == ([], [])

    def test_copy_from_a_stage_never_consults_the_context(self, tree):
        """The context is not sent to a stage, so a matching rule is irrelevant."""
        findings, notes = self.analyse(
            tree,
            "dist",
            "FROM scratch AS build\nCOPY README.md /dist\n"
            "FROM scratch\nCOPY --from=build /dist /dist\n",
        )
        assert (findings, notes) == ([], [])

    def test_a_missing_source_waits_for_strict(self, tree):
        document = "FROM scratch\nCOPY nope*.txt /x/\n"
        assert self.analyse(tree, "", document) == ([], [])
        findings, _ = self.analyse(tree, "", document, strict=True)
        assert findings == [("missing-copy-source", 2)]

    def test_a_generated_source_under_a_negated_ignore_stays_quiet(self, tree):
        """Not on disk, and a negation could re-include part of it later.

        `build/` here is written by CI before `docker build` runs, so unbuilt
        is looking at a context that does not exist yet. With a negation in
        the file it cannot say what would survive, and says nothing.
        """
        findings, notes = self.analyse(
            tree, "build\n!build/keep", "FROM scratch\nCOPY build /app\n"
        )
        assert (findings, notes) == ([], [])

    def test_the_same_source_with_no_negations_is_decidable_without_the_disk(self, tree):
        """Nothing named `build` is on disk, and nothing can re-include it."""
        findings, _ = self.analyse(tree, "build", "FROM scratch\nCOPY build /app\n")
        assert findings == [("dockerignored-copy", 2)]

    def test_no_context_means_no_context_checks(self, tree):
        result = analyse.run("FROM scratch\nCOPY dist /dist\n", strict=True)
        assert result.findings == []
