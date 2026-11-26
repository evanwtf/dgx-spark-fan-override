"""Release logic for the release-on-merge workflow (.github/workflows/release.yml).

Every merge to main publishes a GitHub Release. The `v*` git tag is the only
record of the released version: nothing in the tree carries it, and the build
scripts stamp it into a staging copy. This CLI decides and describes a release:

  plan    decide whether this commit releases, and at which version
          (a direct push to main, with no PR, publishes nothing)
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
from datetime import datetime
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


def _load_json(text: str, what: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ReleaseError(f"{what}: not JSON: {e}") from e


def _field(obj: object, key: str, what: str) -> object:
    if not isinstance(obj, dict) or key not in obj:
        raise ReleaseError(f"{what}: no {key!r} in {obj!r:.200}")
    return obj[key]


def _str_field(obj: object, key: str, what: str) -> str:
    value = _field(obj, key, what)
    if not isinstance(value, str):
        raise ReleaseError(f"{what}: {key!r} is not a string: {value!r:.200}")
    return value


def _opt_time(obj: object, key: str, what: str) -> datetime | None:
    value = _field(obj, key, what)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseError(f"{what}: {key!r} is not a time: {value!r}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise ReleaseError(f"{what}: {key!r} is not a time: {value!r}") from e


# ---- GitHub lookups ---------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    merge_commit: str | None
    merged_at: datetime | None
    last_edited_at: datetime | None
    labels: list[str]
    body: str


# One GraphQL query, so every field comes from the same moment. `gh pr view`
# does not expose lastEditedAt; GraphQL does.
_PR_QUERY = """\
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      state
      body
      mergedAt
      lastEditedAt
      mergeCommit { oid }
      labels(first: 100) { totalCount nodes { name } }
    }
  }
}"""


def pr_from_graphql(text: str) -> PullRequest:
    """Parse the `gh api graphql` answer to _PR_QUERY. Strict: any odd shape
    raises, so a broken lookup never reads as "no labels"."""
    what = "gh pull request lookup"
    data = _load_json(text, what)
    if isinstance(data, dict) and data.get("errors"):
        raise ReleaseError(f"{what}: {data['errors']!r:.300}")
    repo = _field(_field(data, "data", what), "repository", what)
    pr = _field(repo, "pullRequest", what)
    if pr is None:
        raise ReleaseError(f"{what}: no such pull request")
    number = _field(pr, "number", what)
    if not isinstance(number, int) or isinstance(number, bool):
        raise ReleaseError(f"{what}: 'number' is not an int: {number!r}")
    merge = _field(pr, "mergeCommit", what)
    merge_commit = None if merge is None else _str_field(merge, "oid", what)
    labels_obj = _field(pr, "labels", what)
    total = _field(labels_obj, "totalCount", what)
    nodes = _field(labels_obj, "nodes", what)
    if not isinstance(nodes, list) or not isinstance(total, int):
        raise ReleaseError(f"{what}: bad labels: {labels_obj!r:.200}")
    if total != len(nodes):
        raise ReleaseError(f"{what}: {total} labels, only {len(nodes)} read")
    return PullRequest(
        number=number,
        state=_str_field(pr, "state", what),
        merge_commit=merge_commit,
        merged_at=_opt_time(pr, "mergedAt", what),
        last_edited_at=_opt_time(pr, "lastEditedAt", what),
        labels=[_str_field(n, "name", what) for n in nodes],
        body=_str_field(pr, "body", what),
    )


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ReleaseError(f"repo must be owner/name, got {repo!r}")
    return owner, name


def fetch_pr(number: int, repo: str, gh: Runner) -> PullRequest:
    owner, name = _split_repo(repo)
    args = ["gh", "api", "graphql", "-f", f"query={_PR_QUERY}"]
    args += ["-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"]
    try:
        text = gh(args)
    except ReleaseError as e:
        raise ReleaseError(f"lookup of #{number} failed: {e}") from e
    pr = pr_from_graphql(text)
    if pr.number != number:
        raise ReleaseError(f"asked for #{number}, GitHub answered #{pr.number}")
    return pr


def check_merged_as(pr: PullRequest, sha: str) -> None:
    """The PR must be merged, and its merge commit must be this commit.

    A number in a subject can point at any PR. Only a PR whose merge commit is
    this commit owns this commit's labels.
    """
    if pr.state != "MERGED":
        raise ReleaseError(f"#{pr.number} is {pr.state}, not MERGED")
    if pr.merge_commit != sha:
        raise ReleaseError(
            f"#{pr.number} merged as {pr.merge_commit}, not as this commit {sha}"
        )


def check_body_not_edited_after_merge(pr: PullRequest) -> None:
    """Refuse notes from a body edited after the merge: the notes must say
    what was reviewed and merged."""
    if pr.last_edited_at is None or pr.merged_at is None:
        return
    if pr.last_edited_at > pr.merged_at:
        raise ReleaseError(
            f"#{pr.number} body was edited at {pr.last_edited_at.isoformat()}, "
            f"after the merge at {pr.merged_at.isoformat()}; refusing its notes"
        )


def commit_pulls_from_json(text: str) -> list[tuple[int, str | None]]:
    """(number, merge_commit_sha) of each PR in the answer to
    `gh api repos/{repo}/commits/{sha}/pulls`."""
    what = "gh commit pulls lookup"
    data = _load_json(text, what)
    if not isinstance(data, list):
        raise ReleaseError(f"{what}: want a list, got {data!r:.200}")
    out: list[tuple[int, str | None]] = []
    for item in data:
        number = _field(item, "number", what)
        merge_sha = _field(item, "merge_commit_sha", what)
        if not isinstance(number, int) or not (
            merge_sha is None or isinstance(merge_sha, str)
        ):
            raise ReleaseError(f"{what}: bad entry {item!r:.200}")
        out.append((number, merge_sha))
    return out


def fetch_commit_pulls(sha: str, repo: str, gh: Runner) -> list[tuple[int, str | None]]:
    _split_repo(repo)
    try:
        text = gh(["gh", "api", f"repos/{repo}/commits/{sha}/pulls"])
    except ReleaseError as e:
        raise ReleaseError(f"lookup of the PRs for {sha} failed: {e}") from e
    return commit_pulls_from_json(text)


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


# ---- Docs-only changes ------------------------------------------------------


def is_docs_only(paths: Sequence[str]) -> bool:
    """True when every path is under docs/ or is a *.md file at any depth.

    No .md file ships in either .deb, so a change to one changes no package.
    An empty change set is not docs-only. Workflows and scripts are not docs:
    a merge that changes only them still releases.
    """
    if not paths:
        return False
    return all(p.startswith("docs/") or p.endswith(".md") for p in paths)


# ---- Release notes ----------------------------------------------------------

_SUMMARY_HEADING = "## Quick summary"
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def quick_summary(body: str) -> str:
    """The text under '## Quick summary', up to the next level-1 or level-2
    heading ('# ' or '## ').

    A heading inside a fenced code block (``` or ~~~) does not end the
    section. The text is returned exactly as written (CRLF normalized to LF),
    so backticks and shell syntax survive unchanged.
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
    fence: str | None = None  # the opening fence while inside a code block
    for line in lines[start:]:
        m = _FENCE_RE.match(line)
        if fence is None and m is not None:
            fence = m[1]
        elif fence is not None:
            # A fence closes on the same character, at least as long.
            if m is not None and m[1][0] == fence[0] and len(m[1]) >= len(fence):
                fence = None
        elif line.startswith(("# ", "## ")) or line.rstrip() in ("#", "##"):
            break
        section.append(line)
    text = "\n".join(section).strip("\n").rstrip()
    if not text.strip():
        raise ReleaseError(f"PR body's '{_SUMMARY_HEADING}' section is empty")
    return text


def release_notes(body: str, pr: int, repo: str) -> str:
    summary = quick_summary(body)
    return f"{summary}\n\nPull request: https://github.com/{repo}/pull/{pr}\n"


# ---- Plan -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Plan:
    release: bool
    reason: str
    version: Version | None = None
    pr: int | None = None
    reused: bool = False


def _paths(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


def make_plan(sha: str, repo: str, git: Runner, gh: Runner) -> Plan:
    """Decide whether commit `sha` releases, and at which version.

    Order matters:
    1. A vX.Y.Z tag already on this commit means an earlier run got that far:
       reuse it and finish publishing. Never bump twice.
    2. The highest vX.Y.Z tag must be an ancestor of this commit. A rerun of
       an old merge must not publish a higher version on older code.
    3. Find the PR from the commit subject. It must be merged as this commit.
       No PR (a direct push) publishes nothing. A missing (#N) on a commit
       that GitHub says a PR merged fails: the squash title was edited.
    4. skip-release, or a docs-only change since the highest tag, publishes
       nothing.
    5. Otherwise bump the highest vX.Y.Z tag by the label's level.
    """
    full = git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"]).strip()
    parents = git(["rev-list", "--parents", "-n", "1", full]).split()[1:]
    subject = git(["log", "-1", "--format=%s", full]).strip()
    pr_number = pr_number_from_commit(subject, len(parents))
    logger.info(
        "commit %s, parents %d, PR %s", full[:12], len(parents), pr_number or "none"
    )

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
        return Plan(
            True, f"rerun: reusing {version.tag}", version, pr_number, reused=True
        )

    tags = git(["tag", "--list", "v*"]).split()
    latest = latest_version(tags)
    if latest is not None:
        tagged = git(["rev-parse", f"{latest.tag}^{{commit}}"]).strip()
        base = git(["merge-base", tagged, full]).strip()
        if base != tagged:
            raise ReleaseError(
                f"{latest.tag} is not an ancestor of {full[:12]}: this commit is "
                f"older than the latest release (or off its line); refusing to "
                f"publish a version above {latest} on it"
            )

    if pr_number is None:
        for number, merge_sha in fetch_commit_pulls(full, repo, gh):
            if merge_sha == full:
                raise ReleaseError(
                    f"#{number} merged as {full[:12]}, but the subject has no "
                    f"'(#{number})': the squash title was edited at merge time; "
                    "refusing to guess its release labels"
                )
        return Plan(
            False,
            "direct push to main, not a PR merge; its change ships with the "
            "next PR merge",
        )

    pr = fetch_pr(pr_number, repo, gh)
    check_merged_as(pr, full)
    logger.info("labels: %s", ", ".join(pr.labels) or "none")
    level = bump_level(pr.labels)
    if level == "skip":
        return Plan(False, f"labelled {SKIP_LABEL}", pr=pr_number)

    # With a tag, judge everything since it: a code merge whose run was
    # cancelled still ships with the next merge, even a docs-only one.
    if latest is not None:
        since = latest.tag
        paths = _paths(git(["diff", "--name-only", "--no-renames", "-z", since, full]))
    elif parents:
        since = parents[0][:12]
        paths = _paths(
            git(["diff", "--name-only", "--no-renames", "-z", parents[0], full])
        )
    else:
        since = "the root"
        paths = _paths(git(["ls-tree", "-r", "--name-only", "-z", full]))
    if is_docs_only(paths):
        return Plan(False, f"docs-only change since {since}", pr=pr_number)

    version = next_version(tags, level)
    base_text = latest.tag if latest else f"{BASE_VERSION} (no tags yet)"
    return Plan(True, f"{level} bump from {base_text}", version, pr_number)


# ---- Verify built artifacts -------------------------------------------------

# Where each package carries its version. "{v}" is the version under test.
# Every declaration must exist and equal the version.
_DECLARATIONS: dict[str, list[tuple[str, str]]] = {
    "nvfanread": [
        ("DEBIAN/postinst", r"^version=(.*)$"),
        ("DEBIAN/prerm", r"^version=(.*)$"),
        ("usr/src/nvfanread-{v}/dkms.conf", r'^PACKAGE_VERSION="(.*)"$'),
        ("usr/src/nvfanread-{v}/nvfanread.c", r'MODULE_VERSION\("([^"]*)"\)'),
    ],
    "nvfancontrol": [
        ("DEBIAN/postinst", r"^module_version='(.*)'$"),
        ("DEBIAN/prerm", r"^module_version='(.*)'$"),
        ("DEBIAN/postrm", r"^module_version='(.*)'$"),
        ("usr/src/nvfancontrol/dkms.conf", r'^PACKAGE_VERSION="(.*)"$'),
    ],
}
# Any MODULE_VERSION in any shipped .c file must also carry the version.
_MODULE_VERSION_RE = re.compile(r'MODULE_VERSION\("([^"]*)"\)')


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
    for path in sorted(tree.rglob("*.c")):
        member = path.relative_to(tree).as_posix()
        for value in _MODULE_VERSION_RE.findall(path.read_text(errors="replace")):
            if value != str(version):
                problems.append(
                    f"{package}: {member} has MODULE_VERSION {value}, want {version}"
                )
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


def _gh_from_files(pr_json: str | None, pulls_json: str | None) -> Runner:
    """A gh stand-in for dry runs: answers from saved `gh api` output files,
    and fails any lookup it has no file for."""

    def run(args: Sequence[str]) -> str:
        if list(args[1:3]) == ["api", "graphql"] and pr_json:
            return Path(pr_json).read_text()
        if (
            list(args[1:2]) == ["api"]
            and str(args[2]).endswith("/pulls")
            and pulls_json
        ):
            return Path(pulls_json).read_text()
        raise ReleaseError(f"dry run: no saved answer for {' '.join(args[:3])}")

    return run


def _cmd_plan(args: argparse.Namespace) -> None:
    gh: Runner = gh_runner
    if args.pr_json or args.pulls_json:
        gh = _gh_from_files(args.pr_json, args.pulls_json)
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
    gh: Runner = gh_runner
    if args.pr_json:
        gh = _gh_from_files(args.pr_json, None)
    sha = git_runner()(["rev-parse", "--verify", f"{args.rev}^{{commit}}"]).strip()
    pr = fetch_pr(args.pr, args.repo, gh)
    check_merged_as(pr, sha)
    check_body_not_edited_after_merge(pr)
    notes = release_notes(pr.body, args.pr, args.repo)
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
    p.add_argument("--repo", required=True, help="owner/name for the gh lookups")
    p.add_argument(
        "--pr-json",
        help="dry run: answer the PR lookup from this saved `gh api graphql` "
        "output instead of calling gh",
    )
    p.add_argument(
        "--pulls-json",
        help="dry run: answer the commit-to-PR lookup from this saved "
        "`gh api repos/O/R/commits/SHA/pulls` output",
    )
    p.add_argument("--github-output", help="append key=value outputs to this file")
    p.add_argument("--step-summary", help="append a one-line summary to this file")
    p.set_defaults(func=_cmd_plan)

    n = sub.add_parser("notes", help="write release notes from the PR's Quick summary")
    n.add_argument("--repo", required=True, help="owner/name, for the lookup and link")
    n.add_argument("--pr", type=int, required=True, help="the PR number (from plan)")
    n.add_argument("--rev", required=True, help="the commit the PR merged as")
    n.add_argument(
        "--pr-json",
        help="dry run: answer the PR lookup from this saved `gh api graphql` output",
    )
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
