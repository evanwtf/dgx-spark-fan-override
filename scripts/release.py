"""Release logic for the release-on-merge workflow (.github/workflows/release.yml).

Every merge to main publishes a GitHub Release. The `v*` git tag is the only
record of the released version: nothing in the tree carries it, and the build
scripts stamp it into a staging copy. This CLI decides and describes a release:

  plan    decide whether this commit releases, and at which version
  notes   write the release notes from the PR's "## Quick summary" section
  verify  check that built .debs report the version, everywhere it is stamped

Each step refuses (non-zero exit) when it cannot answer. A stopped release is
a message; a release that quietly guesses its version or notes is not.

Stdlib only. Run with `uv run python scripts/release.py <command> --help`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Level = Literal["major", "minor", "patch"]
Runner = Callable[[Sequence[str]], str]


class ReleaseError(Exception):
    """The release cannot proceed. The message says why."""


# ---- Versions ---------------------------------------------------------------

_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_TAG_RE = re.compile(r"v" + _VERSION_RE.pattern)


@dataclasses.dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        m = _VERSION_RE.fullmatch(text)
        if m is None:
            raise ReleaseError(f"not a version (want X.Y.Z): {text!r}")
        return cls(int(m[1]), int(m[2]), int(m[3]))

    @classmethod
    def from_tag(cls, tag: str) -> Version | None:
        """The version of a `vX.Y.Z` tag, or None for any other tag name."""
        m = _TAG_RE.fullmatch(tag)
        if m is None:
            return None
        return cls(int(m[1]), int(m[2]), int(m[3]))

    def bump(self, level: str) -> Version:
        if level == "major":
            return Version(self.major + 1, 0, 0)
        if level == "minor":
            return Version(self.major, self.minor + 1, 0)
        if level == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ReleaseError(f"unknown bump level: {level!r}")

    @property
    def tag(self) -> str:
        return f"v{self}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


# The version to bump from while the repo has no vX.Y.Z tag. 1.0.0 is the
# version deployed before release automation (nvfanread and nvfancontrol
# 1.0.0, 2026-09-13), so the first automated release continues from it. Once
# any vX.Y.Z tag exists, the tags are the record and this constant is unused.
BASE_VERSION = Version(1, 0, 0)


def latest_version(tags: Iterable[str]) -> Version | None:
    """The highest vX.Y.Z tag by semver order. Other tag names are ignored."""
    versions = [v for v in (Version.from_tag(t) for t in tags) if v is not None]
    return max(versions, default=None)


def next_version(tags: Iterable[str], level: str) -> Version:
    """The version after the highest vX.Y.Z tag (or BASE_VERSION if none)."""
    if level not in ("major", "minor", "patch"):
        raise ReleaseError(f"unknown bump level: {level!r}")
    current = latest_version(tags)
    return (current or BASE_VERSION).bump(level)


# ---- Pull request and labels ------------------------------------------------

_SQUASH_RE = re.compile(r"\(#([0-9]+)\)")
_MERGE_RE = re.compile(r"Merge pull request #([0-9]+) from \S")


def pr_number_from_commit(subject: str, parent_count: int) -> int | None:
    """The PR a commit on main came from, read from its own subject.

    GitHub's commit-to-PR association is not reliably there when the
    workflow runs, so read what the merge itself wrote:

    - squash merge: "<title> (#N)" at the end of the subject (one parent)
    - merge commit: "Merge pull request #N from <branch>" (two parents)

    A direct push (one parent, no number) returns None: no PR, no labels.
    A merge commit with no number raises: it came from somewhere, and
    reading it as "no labels" could ship the wrong version.
    """
    if parent_count < 0:
        raise ReleaseError(f"bad parent count: {parent_count}")
    subject = subject.rstrip()
    if parent_count > 1:
        m = _MERGE_RE.match(subject)
        if m is not None:
            return int(m[1])
    tail = subject.rsplit(" ", 1)[-1] if " " in subject else subject
    m = _SQUASH_RE.fullmatch(tail)
    if m is not None and subject != tail:
        return int(m[1])
    if parent_count > 1:
        raise ReleaseError(
            f"merge commit with no pull request number in its subject: {subject!r}; "
            "refusing to guess its release labels"
        )
    return None


SKIP_LABEL = "skip-release"
_LEVEL_LABELS: list[tuple[str, Level]] = [
    ("semver:major", "major"),
    ("semver:minor", "minor"),
    ("semver:patch", "patch"),
]


def bump_level(labels: Iterable[str]) -> Level | Literal["skip"]:
    """skip-release wins; then major > minor > patch; no label means patch."""
    names = set(labels)
    if SKIP_LABEL in names:
        return "skip"
    for label, level in _LEVEL_LABELS:
        if label in names:
            return level
    return "patch"


def _load_json_object(text: str, what: str) -> dict[str, object]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ReleaseError(f"{what}: not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ReleaseError(f"{what}: want a JSON object, got {type(data).__name__}")
    return data


def labels_from_gh_json(text: str) -> list[str]:
    """Label names from `gh pr view N --json labels`. Strict: any odd shape
    raises, so a broken lookup never reads as "no labels"."""
    data = _load_json_object(text, "gh labels")
    raw = data.get("labels")
    if not isinstance(raw, list):
        raise ReleaseError(f"gh labels: 'labels' is not a list: {raw!r}")
    names: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str):
            raise ReleaseError(f"gh labels: label without a name: {item!r}")
        names.append(name)
    return names


def body_from_gh_json(text: str) -> str:
    """The PR body from `gh pr view N --json body`."""
    data = _load_json_object(text, "gh body")
    body = data.get("body")
    if not isinstance(body, str):
        raise ReleaseError(f"gh body: 'body' is not a string: {body!r}")
    return body


def run_command(args: Sequence[str], cwd: Path | None = None) -> str:
    """Run a command with an argument list (never a shell) and return stdout."""
    try:
        res = subprocess.run(
            list(args), cwd=cwd, check=False, capture_output=True, text=True
        )
    except OSError as e:
        raise ReleaseError(f"{args[0]}: {e}") from e
    if res.returncode != 0:
        raise ReleaseError(
            f"{' '.join(args[:3])} failed ({res.returncode}): {res.stderr.strip()}"
        )
    return res.stdout


def git_runner(repo: Path | None = None) -> Runner:
    def run(args: Sequence[str]) -> str:
        return run_command(["git", *args], cwd=repo)

    return run


def gh_runner(args: Sequence[str]) -> str:
    return run_command(args)


def fetch_labels(pr: int, repo: str, gh: Runner) -> list[str]:
    try:
        text = gh(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "labels"])
    except ReleaseError as e:
        raise ReleaseError(f"label lookup for #{pr} failed: {e}") from e
    return labels_from_gh_json(text)


def fetch_body(pr: int, repo: str, gh: Runner) -> str:
    text = gh(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "body"])
    return body_from_gh_json(text)


# ---- Docs-only changes ------------------------------------------------------


def is_docs_only(paths: Sequence[str]) -> bool:
    """True when every path is under docs/ or is a top-level *.md file.

    An empty change set is not docs-only. Workflows and scripts are not docs:
    a merge that changes only them still releases.
    """
    if not paths:
        return False
    return all(
        p.startswith("docs/") or ("/" not in p and p.endswith(".md")) for p in paths
    )


# ---- Release notes ----------------------------------------------------------

_SUMMARY_HEADING = "## Quick summary"


def quick_summary(body: str) -> str:
    """The text under '## Quick summary', up to the next '## ' heading.

    A '## ' line inside a fenced code block does not end the section. The text
    is returned exactly as written (CRLF normalized to LF), so backticks and
    shell syntax survive unchanged.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == _SUMMARY_HEADING:
            start = i + 1
            break
    if start is None:
        raise ReleaseError(f"PR body has no '{_SUMMARY_HEADING}' section")
    section: list[str] = []
    in_fence = False
    for line in lines[start:]:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            break
        section.append(line)
    text = "\n".join(section).strip("\n").rstrip()
    if not text.strip():
        raise ReleaseError(f"PR body's '{_SUMMARY_HEADING}' section is empty")
    return text


def release_notes(body: str, pr: int, repo: str) -> str:
    summary = quick_summary(body)
    return f"{summary}\n\nPull request: https://github.com/{repo}/pull/{pr}\n"


def commit_notes(message: str, sha: str, repo: str) -> str:
    """Notes for a direct push to main, which has no PR: the commit message."""
    text = message.replace("\r\n", "\n").strip()
    if not text:
        raise ReleaseError(f"commit {sha} has an empty message")
    return f"{text}\n\nCommit: https://github.com/{repo}/commit/{sha}\n"


# ---- Plan -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Plan:
    release: bool
    reason: str
    version: Version | None = None
    pr: int | None = None
    reused: bool = False


def _changed_paths(sha: str, parents: list[str], git: Runner) -> list[str]:
    if parents:
        out = git(["diff", "--name-only", "-z", parents[0], sha])
    else:
        out = git(["ls-tree", "-r", "--name-only", "-z", sha])
    return [p for p in out.split("\0") if p]


def make_plan(sha: str, repo: str, git: Runner, gh: Runner) -> Plan:
    """Decide whether commit `sha` releases, and at which version.

    Order matters:
    1. A vX.Y.Z tag already on this commit means an earlier run got that far:
       reuse it and finish publishing. Never bump twice.
    2. Read the PR number from the commit, then its labels (fail closed).
    3. skip-release or a docs-only change publishes nothing.
    4. Otherwise bump the highest vX.Y.Z tag by the label's level.
    """
    full = git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"]).strip()
    parents = git(["rev-list", "--parents", "-n", "1", full]).split()[1:]
    subject = git(["log", "-1", "--format=%s", full]).strip()
    pr = pr_number_from_commit(subject, len(parents))
    logger.info("commit %s, parents %d, PR %s", full[:12], len(parents), pr or "none")

    here = [
        v
        for v in (
            Version.from_tag(t) for t in git(["tag", "--points-at", full]).split()
        )
        if v is not None
    ]
    if len(here) > 1:
        raise ReleaseError(
            f"more than one version tag on {full[:12]}: "
            + ", ".join(v.tag for v in sorted(here))
        )
    if here:
        version = here[0]
        logger.info("%s already points at this commit: rerun, reusing it", version.tag)
        return Plan(True, f"rerun: reusing {version.tag}", version, pr, reused=True)

    labels: list[str] = []
    if pr is not None:
        labels = fetch_labels(pr, repo, gh)
    logger.info("labels: %s", ", ".join(labels) or "none")
    level = bump_level(labels)
    if level == "skip":
        return Plan(False, f"labelled {SKIP_LABEL}", pr=pr)

    paths = _changed_paths(full, parents, git)
    if is_docs_only(paths):
        return Plan(False, "docs-only change", pr=pr)

    tags = git(["tag", "--list", "v*"]).split()
    version = next_version(tags, level)
    latest = latest_version(tags)
    base = latest.tag if latest else f"{BASE_VERSION} (no tags yet)"
    return Plan(True, f"{level} bump from {base}", version, pr)


# ---- Verify built artifacts -------------------------------------------------

# Where each package carries its version. "{v}" is the version under test.
# Every declaration must exist and equal the version.
_DECLARATIONS: dict[str, list[tuple[str, str]]] = {
    "nvfanread": [
        ("DEBIAN/postinst", r"^version=(.*)$"),
        ("DEBIAN/prerm", r"^version=(.*)$"),
        ("usr/src/nvfanread-{v}/dkms.conf", r'^PACKAGE_VERSION="(.*)"$'),
        ("usr/src/nvfanread-{v}/nvfanread.c", r'^MODULE_VERSION\("(.*)"\);$'),
    ],
    "nvfancontrol": [
        ("DEBIAN/postinst", r"^module_version='(.*)'$"),
        ("DEBIAN/prerm", r"^module_version='(.*)'$"),
        ("DEBIAN/postrm", r"^module_version='(.*)'$"),
        ("usr/src/nvfancontrol/dkms.conf", r'^PACKAGE_VERSION="(.*)"$'),
    ],
}


def _check_tree(package: str, tree: Path, version: Version) -> list[str]:
    problems: list[str] = []
    for template, pattern in _DECLARATIONS[package]:
        member = template.format(v=version)
        path = tree / member
        if not path.is_file():
            problems.append(f"{package}: {member} is missing")
            continue
        found = re.findall(pattern, path.read_text(errors="replace"), re.MULTILINE)
        if not found:
            problems.append(f"{package}: {member} declares no version")
        for value in found:
            if value != str(version):
                problems.append(f"{package}: {member} has {value}, want {version}")
    return problems


def verify_debs(debs: Sequence[Path], version: Version) -> None:
    """Check every .deb reports `version`; both packages must be present."""
    problems: list[str] = []
    seen: set[str] = set()
    for deb in debs:
        package = run_command(["dpkg-deb", "-f", str(deb), "Package"]).strip()
        got = run_command(["dpkg-deb", "-f", str(deb), "Version"]).strip()
        if package not in _DECLARATIONS:
            problems.append(f"{deb.name}: unexpected package {package!r}")
            continue
        if package in seen:
            problems.append(f"{deb.name}: {package} given twice")
        seen.add(package)
        if got != str(version):
            problems.append(f"{package}: control Version {got}, want {version}")
        if f"_{version}_" not in deb.name:
            problems.append(f"{deb.name}: file name lacks _{version}_")
        with tempfile.TemporaryDirectory() as tmp:
            run_command(["dpkg-deb", "-R", str(deb), tmp])
            problems.extend(_check_tree(package, Path(tmp), version))
    for package in sorted(set(_DECLARATIONS) - seen):
        problems.append(f"{package}: no .deb given")
    if problems:
        raise ReleaseError("artifact check failed:\n  " + "\n  ".join(problems))


# ---- CLI --------------------------------------------------------------------


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def _cmd_plan(args: argparse.Namespace) -> None:
    gh: Runner = gh_runner
    if args.labels_json:
        labels_text = Path(args.labels_json).read_text()

        def gh_from_file(cmd: Sequence[str]) -> str:
            return labels_text

        gh = gh_from_file
    p = make_plan(args.rev, args.repo, git=git_runner(), gh=gh)
    if p.release:
        assert p.version is not None
        line = f"Releasing {p.version.tag}: {p.reason}."
    else:
        line = f"No release: {p.reason}. Publishing nothing."
    logger.info("%s", line)
    _append(args.step_summary, line + "\n")
    out = {
        "release": "true" if p.release else "false",
        "version": str(p.version) if p.version else "",
        "tag": p.version.tag if p.version else "",
        "pr": str(p.pr) if p.pr is not None else "",
        "reused": "true" if p.reused else "false",
    }
    _append(args.github_output, "".join(f"{k}={v}\n" for k, v in out.items()))


def _cmd_notes(args: argparse.Namespace) -> None:
    if args.pr is not None:
        if args.pr_json:
            body = body_from_gh_json(Path(args.pr_json).read_text())
        else:
            body = fetch_body(args.pr, args.repo, gh_runner)
        notes = release_notes(body, args.pr, args.repo)
    elif args.rev:
        message = git_runner()(["log", "-1", "--format=%B", args.rev])
        sha = git_runner()(["rev-parse", args.rev]).strip()
        notes = commit_notes(message, sha, args.repo)
    else:
        raise ReleaseError("notes: give --pr, or --rev for a direct push")
    Path(args.out).write_text(notes, encoding="utf-8")
    logger.info("wrote %d bytes of notes to %s", len(notes.encode()), args.out)


def _cmd_verify(args: argparse.Namespace) -> None:
    version = Version.parse(args.version)
    verify_debs([Path(d) for d in args.debs], version)
    logger.info("artifacts report %s: %s", version, ", ".join(args.debs))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="decide whether a commit releases, and its version")
    p.add_argument("--rev", required=True, help="the commit (e.g. $GITHUB_SHA)")
    p.add_argument("--repo", required=True, help="owner/name for the gh lookup")
    p.add_argument(
        "--labels-json",
        help="read `gh pr view N --json labels` output from this file instead of "
        "calling gh (dry runs and tests)",
    )
    p.add_argument("--github-output", help="append key=value outputs to this file")
    p.add_argument("--step-summary", help="append a one-line summary to this file")
    p.set_defaults(func=_cmd_plan)

    n = sub.add_parser("notes", help="write release notes from the PR's Quick summary")
    n.add_argument("--repo", required=True, help="owner/name, for the PR link")
    n.add_argument("--pr", type=int, help="the PR number (from `plan`)")
    n.add_argument(
        "--pr-json",
        help="read `gh pr view N --json body` output from this file instead of "
        "calling gh",
    )
    n.add_argument("--rev", help="for a direct push (no PR): use this commit's message")
    n.add_argument("--out", required=True, help="write the notes to this file")
    n.set_defaults(func=_cmd_notes)

    v = sub.add_parser("verify", help="check built .debs report a version")
    v.add_argument("--version", required=True, help="the version, X.Y.Z")
    v.add_argument("debs", nargs="+", help="the .deb files (both packages)")
    v.set_defaults(func=_cmd_verify)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.func(args)
    except ReleaseError as e:
        logger.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(main())
