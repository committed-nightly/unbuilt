# unbuilt

Say which Dockerfile instructions never run, and which settings never reach
the image you build.

A Dockerfile has no way to tell you it ignored something. A stage nothing
refers to is skipped silently. `ENV` set in a builder stage is thrown away
with the stage. An `ARG` declared above the first `FROM` is in scope for
`FROM` lines and nowhere else, so a `${...}` that uses it inside a stage
expands to the empty string and the build goes green. One typo in a
`COPY --from=` and Docker stops looking for your stage and starts looking for
an image on Docker Hub by that name.

None of that is a build failure. All of it is a line you wrote that does
nothing.

This one builds, and the image runs:

```dockerfile
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
```

```
$ unbuilt Dockerfile
Dockerfile: 2 stages, building runtime.
Not built for this target: build.

  line 8  superseded  (runtime)
    ENV PORT=3000
    ENV PORT is set again on line 9 with no build step in between, so nothing
    ever sees this value

  line 10  copy-from-unknown  (runtime)
    COPY --from=builder /app/dist ./dist
    no stage in this file is named builder, so Docker resolves it as the image
    docker.io/library/builder:latest; the closest stage name is build

2 instructions with no effect on this build.

Worth knowing, but not necessarily wrong:

  line 1  orphan-stage  (build)
    FROM node:20-alpine
    no other stage builds FROM this one or copies out of it, so BuildKit only
    runs its 3 instructions if you ask for it by name with --target build
```

The stage is called `build` and the `COPY` asks for `builder`, so Docker
fetches an image by that name from the registry instead — and nothing refers
to `build` any more, so that stage quietly stops being built at all.

**unbuilt reads the Dockerfile. It does not build it and does not need a
Docker daemon.** Pass `--context` and it also reads the build context
directory, to answer what `.dockerignore` removes; it still never runs a
build. Everything it claims about what Docker does was measured
against a real builder first, and the test suite re-measures it: see
[tests/test_against_docker.py](tests/test_against_docker.py), which builds
each fixture, inspects the resulting image, and fails if Docker disagrees
with the tool.

## Install

```
pip install git+https://github.com/committed-nightly/unbuilt
```

Python 3.10 or newer, no dependencies.

## Usage

```
unbuilt [DOCKERFILE] [--target STAGE] [--context DIR] [--strict] [--json] [-q]
```

`unbuilt` with no arguments reads `./Dockerfile`. A directory argument looks
for a `Dockerfile` inside it, and `-` reads stdin.

There is an example in `examples/`, so this runs from a fresh clone:

```
$ unbuilt examples/Dockerfile
examples/Dockerfile: 4 stages, building runtime.
Not built for this target: build, test.

  line 9  unused-arg  (before the first FROM)
    ARG BUILD_DATE
    BUILD_DATE is declared before the first FROM and referenced nowhere -- not
    in a FROM line, and not by any stage that re-declares it, so passing
    --build-arg BUILD_DATE=... changes nothing

  line 20  empty-expansion  (build)
    ENV NODE_ENV=${APP_ENV}
    APP_ENV is declared before the first FROM (line 8), which puts it in scope
    for FROM lines only; no ARG APP_ENV appears in this stage above this line,
    so it expands to nothing

  line 30  superseded  (runtime)
    ENV PORT=3000
    ENV PORT is set again on line 31 with no build step in between, so nothing
    ever sees this value

  line 32  copy-from-unknown  (runtime)
    COPY --from=builder /app/dist ./dist
    no stage in this file is named builder, so Docker resolves it as the image
    docker.io/library/builder:latest; the closest stage name is build

4 instructions with no effect on this build.

Worth knowing, but not necessarily wrong:

  line 23  orphan-stage  (test)
    FROM node:${NODE_VERSION}-alpine
    no other stage builds FROM this one or copies out of it, so BuildKit only
    runs its 3 instructions if you ask for it by name with --target test
```

That is the same mistake as above with two more on top: a `BUILD_DATE` that
no `--build-arg` can reach, and a `test` stage nothing runs.

### Exit codes

| | |
|---|---|
| `0` | nothing dead found |
| `1` | findings |
| `2` | you typed it wrong: a file that could not be read, a `--target` naming no stage, a file with no `FROM`, or a `--context` that is not a directory or holds a `.dockerignore` Docker would reject |

Findings set the exit code. **Notes do not.** An orphan stage is a note
because whether it is dead depends on whether some CI job passes `--target`,
and that is not in the Dockerfile. unbuilt will not guess and then fail your
build over the guess.

`1` and `2` are worth telling apart in a CI script: `1` means fix your
Dockerfile, `2` means fix the way you called unbuilt. When both apply, `2`
wins.

Anything on **stderr** is unbuilt saying it could not analyse what you asked
for. It is never a finding and never on stdout, so stdout is only ever the
report, the one-line-per-finding list under `-q`, or the document under
`--json`.

`-q` prints one `path:line: kind: detail` per finding and nothing else, with
notes left out entirely, which is the form to pipe somewhere.

### When `--target` names no stage

`docker build --target buidler` builds nothing at all — it refuses, and does
not fall back to another stage. unbuilt does go on to analyse a stage,
because a report on the default target is more use than no report, but it
says so on stderr and exits `2`:

```
$ unbuilt --target buidler Dockerfile
unbuilt: Dockerfile: no stage named buidler; analysed the last stage (runtime) instead
Dockerfile: 2 stages, building runtime.
Not built for this target: build.

  line 8  superseded  (runtime)
    ENV PORT=3000
...
$ echo $?
2
```

Every number under that first line is about `runtime`, a build you did not
ask for. That is what the exit code is for, and it matters most in the mode
where the first line is the part you cannot see:

```
$ unbuilt --target buidler Dockerfile -q > findings.txt
unbuilt: Dockerfile: no stage named buidler; analysed the last stage (runtime) instead
$ echo $?
2
```

`findings.txt` still holds nothing but findings, one per line, so `wc -l`
still means what it meant. The diagnostic went to stderr and the exit code
is the one that tells you not to trust the file.

### Checks

| kind | |
|---|---|
| `empty-expansion` | a `${NAME}` where `NAME` is a global `ARG` the stage never re-declares above that line. Expands to the empty string. |
| `discarded-metadata` | `ENTRYPOINT`, `CMD`, `EXPOSE`, `VOLUME`, `LABEL`, `HEALTHCHECK` or `STOPSIGNAL` in a stage the target only copies files out of. Thrown away with the stage. |
| `discarded-setting` | `ENV`, `USER`, `WORKDIR` or `SHELL` in such a stage, where nothing below it — in the stage or in any stage built `FROM` it — can read it. |
| `superseded` | a setting a later line in the same stage replaces, with nothing in between that could have read the first one. |
| `copy-from-unknown` | a `COPY --from=` naming no stage in the file, so Docker resolves it as an image. Names the closest stage if there is one. |
| `dockerignored-copy` | a `COPY` or `ADD` whose sources `.dockerignore` keeps out of the build context. Needs `--context`. |
| `unused-arg` | an `ARG` no build of this file can read. |
| `orphan-stage` *(note)* | a stage no other stage builds `FROM` or copies out of. Only built if you name it with `--target`. |

`--strict` adds two, both "true unless something outside this file supplies
it". `unknown-expansion` is a `${NAME}` this file never declares at all — off
by default because the base image may well set it, `${HOME}` and `${PATH}`
are not bugs, and unbuilt does not pull images to find out.
`missing-copy-source` is a `COPY` source that matches nothing in the context,
which is off by default because plenty of builds generate their sources
before `docker build` runs, and a `COPY dist/*.js` is not wrong just because
you have not run the bundler yet.

### `--context`, and `.dockerignore`

`.dockerignore` decides what the builder is sent, so a `COPY` whose sources
are all excluded is a line that does nothing — and it is the quietest kind
there is:

```
$ unbuilt examples/Dockerfile --context examples
...
  line 13  dockerignored-copy  (deps)
    COPY package.json package-lock.json ./
    .dockerignore line 10 (*lock*) keeps package-lock.json out of the build
    context, so the build fails here -- a named source that is not in the
    context is not a warning, it is 'failed to compute cache key'
```

The two halves behave differently, which is worth knowing before you read a
finding:

- A **named** source that is excluded **fails the build**, with
  `failed to compute cache key` and no mention of `.dockerignore` anywhere in
  the message. One of these is fatal even if every other source on the line
  works.
- A **pattern** left with nothing to match **succeeds, having copied
  nothing**. No file lands, the destination directory is not even created,
  and the build goes green. This is the one worth owning the tool for.

**You have to pass `--context`, and unbuilt will not guess it.** The context
is `docker build`'s last argument and it is routinely not the directory the
Dockerfile is in — `docker build -f docker/Dockerfile .` has its context at
the repo root. Guessing wrong means reading the wrong `.dockerignore` and
calling a working `COPY` dead on the strength of it, which is the one output
that would make the whole tool untrustworthy.

If the build you are checking uses `-f`, unbuilt looks for that file's own
ignore file first: `docker build -f Dockerfile.ci` reads
`Dockerfile.ci.dockerignore` **instead of** `.dockerignore`, not as well as.

A `.dockerignore` that Docker itself rejects — an unterminated `[`, a
trailing `\`, a bare `!` — stops `docker build` before it reads your
Dockerfile at all. unbuilt says so on stderr, skips the context checks rather
than guessing what the line meant, reports everything else as usual, and
exits `2`.

### `--target`

Most checks hold for any build of the file and run over every stage. Two do
not: `discarded-metadata` and `discarded-setting` are statements about which
stage's configuration reaches the image, and that depends on what you are
building. Those follow `--target`, defaulting to the last stage in the file,
exactly as `docker build` does.

```
$ unbuilt --target build Dockerfile
```

## What it will not tell you

- **Whether an `ENV` is really unused, when a `RUN` can see it.** A stage's
  `ARG`s and `ENV`s are exported into the environment of every `RUN` below
  it, so a program a `RUN` invokes can read one with `$NAME` appearing
  nowhere in the file. buildkit's own `ARG CGO_ENABLED=0` configures
  `go build` exactly that way. unbuilt cannot see inside `make`, so a `RUN`
  anywhere downstream is enough for it to stay quiet. That is deliberate and
  not fixable from the file alone.
- **That a `COPY` copies *fewer* files than you meant.** Removing some of
  what a pattern matches is `.dockerignore` doing its job — `COPY . /app`
  next to a sensible ignore file is that case, and so is every well-run
  repository. unbuilt only reports a source that arrives as *nothing*.
- **An excluded directory your build generates, when a negation reaches
  inside it.** `COPY build /app` beside `build` plus `!build/keep` depends
  entirely on which files CI writes before `docker build` runs, and unbuilt
  is reading the context before it writes any of them — so it says nothing.
  This is the *only* case it defers on: a negation naming a sibling, or
  anything else that could never land under the source, leaves nothing
  unknown and is reported normally.
- **A `COPY` source built out of `${...}`**, or an argument list it cannot
  take apart. The instruction is dropped from the context checks entirely
  rather than half-read, because a half-read argument list is how a working
  `COPY` gets called dead. Every other check still runs over the line.
- **`COPY --exclude=`**, BuildKit's per-instruction exclusion. It can only
  ever remove more than `.dockerignore` does, so ignoring it costs findings
  rather than inventing them.
- **`.dockerignore` rules that match nothing**, which is the same question
  [unignored](https://github.com/committed-nightly/unignored) answers for
  `.gitignore` and is a check about the ignore file rather than about the
  build.
- **Whether the image works.** It is not a linter for style, a scanner for
  vulnerabilities, or a check that your `ENTRYPOINT` exists.
- **`ONBUILD` bodies**, which are parsed as instructions but not followed
  into the downstream build they belong to.

### One caveat worth knowing

`orphan-stage` describes BuildKit. The classic builder, which you still get
with `DOCKER_BUILDKIT=0`, walks every instruction in the file and builds
every stage — so a stage BuildKit skips is a stage the old builder runs, and
can fail on. There is a test for that, because it is the one case where
acting on a finding changes behaviour rather than just removing dead weight.

## Tests

```
pip install -e ".[dev]"
pytest
```

The Docker-backed tests skip with a reason if no daemon is reachable. Every
fixture is `FROM scratch`, so they need no network and no registry pull. To
run only those, or only the rest:

```
pytest -m docker
pytest -m 'not docker'
```

## Licence

MIT.
