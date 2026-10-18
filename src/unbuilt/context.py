"""The build context: what `.dockerignore` removes before the builder looks.

This is the one part of unbuilt that reads the filesystem, and it is opt-in
(`--context DIR`) for that reason. Everything here was measured against
BuildKit rather than read out of the documentation, because the documentation
is wrong about at least one thing that matters -- see `Ignore` below.

Two different matchers live in this file and they are **not** the same, which
is the trap:

*`.dockerignore` patterns* use Docker's own matcher (`moby/patternmatcher`):
`*` does not cross a `/`, `**` spans any number of path segments including
zero, a pattern that matches a directory excludes everything beneath it, and
the last pattern to match wins.

*`COPY` source patterns* use Go's `filepath.Match` and nothing else. There is
no `**` there -- `COPY **/*.go` matches exactly one directory level, the same
as `COPY */*.go`. Assuming the two agreed was the first bug this module had.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

#: Stop walking a directory after this many entries. A context with a
#: `node_modules` in it is not worth a minute of stat calls, and the answer
#: when we give up is "something survives", i.e. no finding. Running out of
#: budget must never invent a dead line.
WALK_BUDGET = 50_000

#: Characters that make a path a pattern rather than a name. `[` counts
#: because `filepath.Match` reads character classes, which is why a
#: `.dockerignore` line of `secret.[kx]ey` really does hide `secret.key`.
GLOB_CHARS = "*?["


class BadPattern(ValueError):
    """A `.dockerignore` line this module will not pretend to understand."""


def _clean(pattern: str) -> str:
    """`filepath.Clean`, plus the leading `/` Docker drops.

    Patterns are relative to the context root whether or not they are written
    that way: `/secret.key` and `secret.key` are the same rule.
    """
    parts: list[str] = []
    for part in pattern.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append("..")
            continue
        parts.append(part)
    return "/".join(parts)


def _translate(pattern: str) -> str:
    """One `.dockerignore` pattern to a regex over a `/`-joined path.

    Mirrors `patternmatcher`'s own compiler: `**` eats segments, `*` and `?`
    stop at a `/`, and a `[...]` class is passed through. Anything else is a
    literal, which is the part worth being fussy about -- a `+` or a `(` in a
    filename is a regex metacharacter here and a plain character to Docker.
    """
    out = ["^"]
    i, end = 0, len(pattern)
    while i < end:
        char = pattern[i]
        if char == "*":
            if i + 1 < end and pattern[i + 1] == "*":
                i += 1
                if i + 1 < end and pattern[i + 1] == "/":
                    i += 1
                # `**` at the end takes the rest of the path; in the middle it
                # takes any number of whole segments, including none.
                out.append(".*" if i + 1 == end else "(.*/)?")
            else:
                out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                raise BadPattern(f"unterminated [ in {pattern!r}")
            body = pattern[i + 1 : close]
            if not body:
                raise BadPattern(f"empty [] in {pattern!r}")
            if body.startswith("^"):
                # `[^abc]` is a Go negation only when written `[^...]`, and
                # `patternmatcher` hands it to the regex engine meaning the
                # same thing. `!` is not a negation inside a class here.
                out.append("[^" + re.escape(body[1:]) + "]")
            else:
                out.append("[" + body.replace("\\", "\\\\") + "]")
            i = close
        elif char == "\\":
            if i + 1 >= end:
                raise BadPattern(f"trailing \\ in {pattern!r}")
            out.append(re.escape(pattern[i + 1]))
            i += 1
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return "".join(out)


@dataclass(frozen=True)
class Pattern:
    source: str
    """The line as written, for quoting back in a finding."""

    line: int
    negated: bool
    body: str
    """The line cleaned of its `!` and its leading `/`, as segments to match."""

    regex: re.Pattern[str]

    def hits(self, path: str) -> bool:
        """Does this pattern match `path`, or any directory above it?

        The parent half is how `.dockerignore`'s `node_modules` excludes
        `node_modules/x/y.js` without a `**` anywhere.
        """
        if self.regex.match(path):
            return True
        parts = path.split("/")
        return any(self.regex.match("/".join(parts[:n])) for n in range(1, len(parts)))

    def reaches_under(self, path: str) -> bool:
        """Could this pattern match something strictly *inside* `path`?

        Matching `path` itself does not count, and neither does matching a
        directory above it. Both of those hit `path` and every one of its
        children alike, so they cannot make a child's verdict differ from
        its parent's -- only a pattern that reaches *in* can do that.

        Answered without touching the disk, because the caller is asking
        about a path that is not there yet. `path` has known segment names;
        the pattern's segments are matched against them one at a time, with
        `**` free to absorb any number. If the pattern still has segments
        left once `path` is consumed, those segments describe something
        below `path` -- and every segment this module compiles can be
        satisfied by some name, so "left over" means "reaches under".
        """
        segments = self.body.split("/")
        if any(seg != "**" and "**" in seg for seg in segments):
            # A `**` glued to something else in one segment can cross a `/`
            # (`_translate` gives it `(.*/)?`), so segment-at-a-time counting
            # does not describe it. Nobody writes these; say "yes" and let
            # the caller stay quiet rather than guess.
            return True
        here = _skip_empty_stars(segments, {0})
        for name in path.split("/"):
            nxt: set[int] = set()
            for i in here:
                if i >= len(segments):
                    continue  # Pattern spent: it cannot reach this deep.
                if segments[i] == "**":
                    nxt.add(i)  # Absorbs this segment, and may absorb more.
                elif _ignore_segment_regex(segments[i]).match(name):
                    nxt.add(i + 1)
            here = _skip_empty_stars(segments, nxt)
            if not here:
                return False
        return any(i < len(segments) for i in here)


def _skip_empty_stars(segments: list[str], positions: set[int]) -> set[int]:
    """Positions reachable from `positions` by `**` matching no segments at all."""
    out = set(positions)
    stack = list(positions)
    while stack:
        i = stack.pop()
        if i < len(segments) and segments[i] == "**" and i + 1 not in out:
            out.add(i + 1)
            stack.append(i + 1)
    return out


@lru_cache(maxsize=512)
def _ignore_segment_regex(segment: str) -> re.Pattern[str]:
    """One `.dockerignore` segment, compiled to match a single path segment.

    Unlike `_segment_regex` this never sees a `**`: the caller handles those
    itself, because in a `.dockerignore` they span segments and here we are
    counting segments.
    """
    return re.compile(_translate(segment))


@dataclass
class Ignore:
    """A parsed `.dockerignore`.

    The rule people get wrong, and the reason this is measured in
    `tests/test_against_docker.py`: **a negation re-includes a path even when
    a parent directory is excluded.** git cannot do that; Docker can, at any
    depth. `node_modules` followed by `!node_modules/keep/a.txt` really does
    put `a.txt` in the context.
    """

    patterns: list[Pattern] = field(default_factory=list)
    path: Path | None = None

    @property
    def has_negations(self) -> bool:
        return any(p.negated for p in self.patterns)

    def negation_reaches_under(self, path: str) -> bool:
        """Could any negation re-include something inside `path`?

        The scoped form of `has_negations`, for deciding whether an excluded
        path that is not on disk yet has a knowable verdict. `excludes()`
        already answers for the path itself; what a caller cannot know about
        an absent path is whether it will turn out to be a directory with a
        re-included file in it. A negation that names a sibling -- or
        anything else that cannot land under this path -- leaves nothing to
        be uncertain about.
        """
        return any(p.negated and p.reaches_under(path) for p in self.patterns)

    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> Ignore:
        patterns: list[Pattern] = []
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.rstrip("\r").strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            body = _clean(line[1:].strip() if negated else line)
            if not body:
                # `!` on its own is the one Docker calls out by name
                # ("illegal exclusion pattern"); a line of `.` or `/` cleans
                # away to nothing and Docker is content to ignore it.
                if negated:
                    raise BadPattern("illegal exclusion pattern: !")
                continue
            patterns.append(
                Pattern(
                    source=line,
                    line=number,
                    negated=negated,
                    body=body,
                    regex=re.compile(_translate(body)),
                )
            )
        return cls(patterns=patterns, path=path)

    def excludes(self, path: str) -> bool:
        """Is `path` kept out of the context? Last matching pattern wins."""
        excluded = False
        for pattern in self.patterns:
            if pattern.hits(path):
                excluded = not pattern.negated
        return excluded

    def blame(self, path: str) -> Pattern | None:
        """The pattern that excluded `path`, for saying which line to go and fix."""
        winner = None
        for pattern in self.patterns:
            if pattern.hits(path):
                winner = pattern
        return winner if winner is not None and not winner.negated else None


def ignore_file_for(context: Path, dockerfile: Path | None) -> Path:
    """Which ignore file this build reads.

    `Dockerfile.foo.dockerignore` *replaces* `.dockerignore` for a build run
    with `-f Dockerfile.foo` -- it does not add to it. Measured, because
    "takes precedence over" could have meant either.
    """
    if dockerfile is not None:
        specific = context / (dockerfile.name + ".dockerignore")
        if specific.is_file():
            return specific
    return context / ".dockerignore"


def load(context: Path, dockerfile: Path | None = None) -> Ignore:
    """Read the ignore file for this build. A missing one ignores nothing."""
    path = ignore_file_for(context, dockerfile)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Ignore(patterns=[], path=None)
    return Ignore.parse(text, path=path)


@lru_cache(maxsize=512)
def _segment_regex(segment: str) -> re.Pattern[str]:
    """One `filepath.Match` segment, compiled.

    `**` is flattened to `*` deliberately. Go's `filepath.Match` has no `**`,
    so in a `COPY` source the two are the same pattern and both stop at a `/`
    -- `COPY **/*.go` reaches exactly one directory down.
    """
    return re.compile(_translate(segment.replace("**", "*")))


@dataclass(frozen=True)
class Source:
    """One `COPY`/`ADD` source, resolved against the context on disk."""

    written: str
    """The argument as it appears in the Dockerfile."""

    normalised: str
    """Relative to the context root, which is what `.dockerignore` matches."""

    is_pattern: bool
    matches: tuple[str, ...]
    """Context-relative paths the source resolves to. Empty means no such file."""


def normalise(source: str) -> str | None:
    """A `COPY` source as a context-relative path, or None if it is not one.

    `..` out of the context is not a path Docker will accept, so there is
    nothing to say about it.
    """
    cleaned = _clean(source)
    if not cleaned or cleaned.startswith(".."):
        return None
    return cleaned


class Context:
    """A build context directory, answering questions about one `COPY`."""

    def __init__(self, root: Path, ignore: Ignore) -> None:
        self.root = root
        self.ignore = ignore

    def resolve(self, source: str) -> Source | None:
        """What this source names on disk, before `.dockerignore` runs."""
        normalised = normalise(source)
        if normalised is None:
            return None
        is_pattern = any(c in normalised for c in GLOB_CHARS)
        if not is_pattern:
            exists = (self.root / normalised).exists()
            return Source(source, normalised, False, (normalised,) if exists else ())
        return Source(source, normalised, True, tuple(self._expand(normalised)))

    def _expand(self, pattern: str) -> list[str]:
        """Walk the glob a segment at a time, as `filepath.Match` does."""
        found = [""]
        for segment in pattern.split("/"):
            nxt: list[str] = []
            for prefix in found:
                directory = self.root / prefix if prefix else self.root
                try:
                    entries = sorted(e.name for e in directory.iterdir())
                except OSError:
                    continue
                matcher = _segment_regex(segment)
                for name in entries:
                    if matcher.match(name):
                        nxt.append(f"{prefix}/{name}" if prefix else name)
            found = nxt
            if not found:
                return []
        return found

    def survives(self, path: str) -> bool:
        """Does anything at or under `path` reach the builder?

        A directory is not settled by its own verdict: a negation can
        re-include a file inside an excluded directory, so an excluded
        directory still has to be walked when the ignore file has negations
        in it at all.
        """
        full = self.root / path
        if not full.is_dir():
            return not self.ignore.excludes(path)
        if self.ignore.excludes(path) and not self.ignore.has_negations:
            return False  # Without negations, exclusion is hereditary.
        return self._any_file_survives(full, path)

    def _any_file_survives(self, directory: Path, prefix: str) -> bool:
        budget = WALK_BUDGET
        stack = [(directory, prefix)]
        while stack:
            current, rel = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                budget -= 1
                if budget <= 0:
                    return True  # Out of budget: assume alive, never invent a finding.
                child = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir() and not entry.is_symlink():
                    if self.ignore.excludes(child) and not self.ignore.has_negations:
                        continue
                    stack.append((entry, child))
                elif not self.ignore.excludes(child):
                    return True
        return False


def open_context(root: Path, dockerfile: Path | None = None) -> Context:
    return Context(root, load(root, dockerfile))
