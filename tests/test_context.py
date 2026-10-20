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


def reaches(pattern: str, path: str) -> bool:
    (only,) = Ignore.parse(pattern).patterns
    return only.reaches_under(path)


class TestNegationScope:
    """Which negations could re-include something *inside* a given path.

    This is what decides whether an excluded source that is not on disk yet
    has a knowable verdict. `excludes()` settles the path itself whatever is
    on disk; the only thing an absent path hides is whether it is a directory
    with a re-included file in it, and only a negation that reaches *in* can
    put one there.
    """

    def test_a_negation_naming_a_sibling_does_not_reach_in(self):
        """The whole bug: `!dist/keep.js` says nothing about `dist/gen.js`."""
        assert not reaches("dist/keep.js", "dist/gen.js")
        assert not reaches("dist/sub/keep.js", "dist/gen.js")
        assert not reaches("unrelated/keep.js", "dist/gen.js")

    def test_a_negation_naming_a_child_reaches_in(self):
        assert reaches("build/keep", "build")
        assert reaches("build/a/b/deep.txt", "build")

    def test_matching_the_path_itself_is_not_reaching_under_it(self):
        """It hits the path and every child alike, so no child can differ."""
        assert not reaches("build", "build")

    def test_nor_is_matching_a_directory_above_it(self):
        assert not reaches("dist", "dist/gen.js")

    def test_a_root_anchored_negation_cannot_be_inside_a_directory(self):
        assert not reaches("keep.js", "dist")

    def test_double_star_reaches_everywhere_it_is_allowed_to(self):
        assert reaches("**", "dist")
        assert reaches("**/keep.js", "dist/sub")
        assert reaches("dist/**", "dist")
        assert not reaches("dist/**", "other")

    def test_a_glob_segment_reaches_in_when_it_can_match_the_name(self):
        assert reaches("a/*/keep", "a/c")
        assert not reaches("a/b/keep", "a/c")

    def test_an_embedded_double_star_is_deferred_to_rather_than_counted(self):
        """`a**b` can cross a `/`, so counting segments does not describe it."""
        assert reaches("a**b", "anything")

    def test_the_ignore_level_question_takes_only_the_negations(self):
        ignore = Ignore.parse("dist\n!dist/keep.js\nbuild\n!build/keep")
        assert not ignore.negation_reaches_under("dist/gen.js")
        assert ignore.negation_reaches_under("build")
        # `dist` and `build` are excludes, not negations: they are not asked.
        assert not ignore.negation_reaches_under("elsewhere")


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
            "COPY only-one-word",
            'COPY ["a.txt", 7]',
            "COPY 'unbalanced /dst",
        ],
    )
    def test_an_argument_it_cannot_read_drops_the_whole_instruction(self, line):
        """Half-reading an argument list is how a working COPY gets called dead."""
        assert self.sources(line) is None

    @pytest.mark.parametrize(
        "line",
        [
            "ADD https://example.com/x.tar /x",  # fetched, never in the context
            "ADD git@github.com:o/r.git /src",
            "ADD git://example.com/o/r.git /src",
        ],
    )
    def test_a_fetched_source_is_not_in_the_context_to_check(self, line):
        """Empty, not None: nothing to check is a different claim from cannot read."""
        assert self.sources(line) == []

    def test_a_fetched_source_does_not_take_its_neighbours_with_it(self):
        """Measured: the local operand of a mixed ADD is still a context read."""
        assert self.sources("ADD local.txt https://e.com/o/r.git /dst/") == [
            "local.txt"
        ]

    def test_a_path_leaving_the_context_is_not_ours_to_judge(self):
        assert normalise("../outside.txt") is None
        assert normalise("a/../../out") is None
        assert normalise("a/../in.txt") == "in.txt"


class TestRemoteShapes:
    """Which source operands the builder fetches instead of reading.

    Every case here is built for real in
    `test_against_docker.py::test_is_remote_source_agrees_with_buildkit`.
    These are the same table, asserted without a daemon so the rule stays
    pinned where that half of the suite skips.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "https://example.com/o/r.git",
            "https://example.com/o/r.git#main",
            "HTTPS://EXAMPLE.COM/o/r.git",  # the scheme is case-insensitive
            "git://example.com/o/r.git",
            "ssh://git@example.com/o/r.git",
            "git@example.com:o/r.git",
            "git@example.com:o/r",  # scp-style needs no .git suffix
            "a@b:c",
            "a@b-c.example.com:d",
        ],
    )
    def test_a_git_ref_is_remote(self, source):
        assert analyse.is_git_ref(source)
        assert analyse.is_remote_source(source)

    @pytest.mark.parametrize(
        "source",
        [
            "https://example.com/file.tar.gz",
            "https://example.com/o/r",  # no .git: a download, not a clone
            "https://example.com/o/r#main",  # a fragment is not enough either
            "http://example.com/x",
        ],
    )
    def test_a_url_is_remote_but_is_not_a_git_ref(self, source):
        assert not analyse.is_git_ref(source)
        assert analyse.is_remote_source(source)

    @pytest.mark.parametrize(
        "source",
        [
            ".git",  # the whole of logbook#47: a real directory
            "foo.git",
            "./foo.git",
            "sub/thing.git",
            "example.com/o/r.git",  # no scheme, so a path with dots in it
            "example.com/o/r.git#main",
            "example.com:8080/o/r.git",
            "git+ssh://example.com/o/r.git",  # a scheme this BuildKit lacks
            "git+http://example.com/o/r.git",
            "has-colon:file.txt",
            "my.user@example.com:o/r.git",  # a dot in the user part
            "u@my_host:o/r.git",  # an underscore in the host
            "dir/u@host:p",  # the shape has to start the operand
            "./u@host:p",
            "plain.txt",
        ],
    )
    def test_everything_else_is_read_out_of_the_context(self, source):
        assert not analyse.is_remote_source(source)
        assert not analyse.is_git_ref(source)


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

    def test_a_negation_that_cannot_reach_the_source_does_not_buy_it_silence(self, tree):
        """The build fails unconditionally, so staying quiet was a miss.

        `!dist/keep.js` names a sibling of `dist/gen.js`. It cannot
        re-include it at any point in the future, whatever CI generates, so
        there is nothing here unbuilt does not know. Deferring to any
        negation anywhere in the file went blind to this entire fatal class
        for every repository with one negation in it.
        """
        findings, _ = self.analyse(
            tree, "dist\n!dist/keep.js", "FROM scratch\nCOPY dist/gen.js /app/\n"
        )
        assert findings == [("dockerignored-copy", 2)]

    def test_a_generated_directory_beside_an_unrelated_negation_is_decidable(self, tree):
        """Same miss one level up: `build` is a directory nothing reaches into."""
        findings, _ = self.analyse(
            tree, "build\n!other/keep", "FROM scratch\nCOPY build /app\n"
        )
        assert findings == [("dockerignored-copy", 2)]

    def test_the_blame_names_the_excluding_line_not_the_negation(self, tree):
        result = analyse.run(
            "FROM scratch\nCOPY dist/gen.js /app/\n",
            build=tree("# header\ndist\n!dist/keep.js"),
        )
        assert "dist" in result.findings[0].detail
        assert "keep.js" not in result.findings[0].detail

    def test_a_negation_reaching_into_the_source_still_defers(self, tree):
        """The deferral is narrowed, not removed. `!dist/keep.js` could land
        inside `dist/sub` if CI generates `dist/sub/` -- it cannot, in fact,
        but a negation one level deeper could, and that is the shape of case
        this stays quiet for."""
        findings, notes = self.analyse(
            tree, "dist\n!dist/sub/keep.js", "FROM scratch\nCOPY dist/sub /app\n"
        )
        assert (findings, notes) == ([], [])

    def test_no_context_means_no_context_checks(self, tree):
        result = analyse.run("FROM scratch\nCOPY dist /dist\n", strict=True)
        assert result.findings == []
