# AGENTS.md

Guide for coding agents in this repo. Read [README.md](README.md) for what the
modules do and [docs/STATUS.md](docs/STATUS.md) for the current state.

## Policies

* Make minimal, focused changes; avoid broad refactors unless requested.
* Preserve existing architecture and patterns.
* Don't introduce new dependencies without justification.
* Update tests when behavior changes; update docs when user-visible
  behavior, configuration, or workflows change.

## Releases: every merge to main cuts one

- Each merge to `main` publishes a GitHub Release with both `.deb`s
  (`.github/workflows/release.yml`). Treat a merge as a release.
- **Squash merge is the only merge method.** The workflow reads the PR number
  from the squash subject, `<title> (#N)`. Keep that suffix when you edit the
  title at merge time, or the release fails.
- The PR label sets the bump: `semver:patch` (default), `semver:minor`,
  `semver:major`, or `skip-release`. A merge publishes nothing when nothing
  since the last release changed outside `docs/` and `*.md` files.
- A direct push to `main` publishes nothing. Its change ships with the next
  PR merge, under that merge's label and notes.
- Every PR body starts with `## Quick summary`. The release notes are that
  section, so a PR without it fails the release. Do not edit the body after
  the merge: the release refuses notes from a body edited after the merge.
- Do not push `v*` tags. The workflow owns them.
- Do not bump versions in the tree. Every version declaration is the
  placeholder `0.0.0`; the build scripts stamp the tag version into the
  package. `scripts/check-versions.sh` fails on a hand bump.

## Checks

Run these before a push. CI runs the same.

```sh
make test                          # C unit tests (protocol logic)
sh scripts/lint.sh                 # shellcheck, checkpatch if present, version pin
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q -rs               # release tooling; no test may skip
```

The required checks on `main` are `lint` and `unit-tests`.

## Safety

- Never run `insmod`, `rmmod`, `dpkg -i`, `dkms install`, or sysfs writes
  without an explicit request. `nvfancontrol` writes the EC fan override.
- Never commit key material (the MOK lives outside the repo).
- This repo is public: no hostnames, LAN IPs or secrets.

## Shared conventions

| Before doing this | Read |
| --- | --- |
| Changing CI or a workflow | `~/git/baseline/conventions/ci.md` ([GitHub](https://github.com/evanwtf/baseline/blob/main/conventions/ci.md)) |
| Changing the release flow | `~/git/baseline/conventions/releases.md` ([GitHub](https://github.com/evanwtf/baseline/blob/main/conventions/releases.md)) |
| Changing Python tooling | `~/git/baseline/conventions/python.md` ([GitHub](https://github.com/evanwtf/baseline/blob/main/conventions/python.md)) |
