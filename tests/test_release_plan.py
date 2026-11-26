"""Tests for the `plan` step: real git repositories, a fake `gh`.

Each test builds a small git repo in a temp dir, so the git commands that
release.py runs are the real ones. Only the GitHub lookups are faked, in the
shapes recorded from the real API (tests/fixtures/).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import release
from release import ReleaseError

REPO = "evanwtf/dgx-spark-fan-override"


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
        },
    )
    return out.stdout.strip()


def commit(repo: Path, subject: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", subject)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "initial", {"nvfanread/nvfanread.c": "1\n", "README.md": "r\n"})
    return r


@dataclass
class FakePr:
    merge_commit: str | None
    labels: list[str] = field(default_factory=list)
    state: str = "MERGED"


@dataclass
class FakeGitHub:
    """Answers the two lookups release.py makes, like GitHub would."""

    prs: dict[int, FakePr] = field(default_factory=dict)
    calls: list[Sequence[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(args)
        if list(args[1:3]) == ["api", "graphql"]:
            number = int(next(a for a in args if a.startswith("number="))[7:])
            if number not in self.prs:
                raise ReleaseError(f"gh: Could not resolve to a PullRequest #{number}")
            pr = self.prs[number]
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "number": number,
                                "state": pr.state,
                                "body": "## Quick summary\n\nx\n",
                                "mergedAt": "2026-11-26T03:23:29Z"
                                if pr.merge_commit
                                else None,
                                "lastEditedAt": None,
                                "mergeCommit": {"oid": pr.merge_commit}
                                if pr.merge_commit
                                else None,
                                "labels": {
                                    "totalCount": len(pr.labels),
                                    "nodes": [{"name": n} for n in pr.labels],
                                },
                            }
                        }
                    }
                }
            )
        if args[1] == "api" and args[2].endswith("/pulls"):
            sha = args[2].split("/")[-2]
            return json.dumps(
                [
                    {"number": n, "merge_commit_sha": pr.merge_commit}
                    for n, pr in self.prs.items()
                    if pr.merge_commit == sha
                ]
            )
        raise AssertionError(f"unexpected gh call: {args}")


def merged(repo: Path, number: int, files: dict[str, str]) -> str:
    """Squash-merge a PR: one commit whose subject ends in (#N)."""
    return commit(repo, f"change {number} (#{number})", files)


def plan(repo: Path, sha: str, gh: release.Runner) -> release.Plan:
    return release.make_plan(sha, REPO, git=release.git_runner(repo), gh=gh)


class TestPlan:
    def test_first_release_no_label_is_1_0_1(self, repo: Path) -> None:
        sha = merged(repo, 3, {"nvfanread/nvfanread.c": "2\n"})
        p = plan(repo, sha, FakeGitHub({3: FakePr(sha)}))
        assert p.release
        assert str(p.version) == "1.0.1"
        assert p.pr == 3
        assert not p.reused

    def test_first_release_minor_is_1_1_0(self, repo: Path) -> None:
        sha = merged(repo, 4, {".github/w.yml": "x\n"})
        p = plan(repo, sha, FakeGitHub({4: FakePr(sha, ["semver:minor"])}))
        assert p.release
        assert str(p.version) == "1.1.0"

    def test_after_a_tag(self, repo: Path) -> None:
        git(repo, "tag", "v1.1.0")
        sha = merged(repo, 5, {"scripts/x.sh": "y\n"})
        p = plan(repo, sha, FakeGitHub({5: FakePr(sha, ["semver:patch"])}))
        assert str(p.version) == "1.1.1"

    def test_skip_label(self, repo: Path) -> None:
        sha = merged(repo, 6, {"nvfanread/nvfanread.c": "3\n"})
        gh = FakeGitHub({6: FakePr(sha, ["skip-release", "semver:major"])})
        p = plan(repo, sha, gh)
        assert not p.release
        assert "skip-release" in p.reason

    def test_docs_only(self, repo: Path) -> None:
        files = {
            "docs/STATUS.md": "s\n",
            "AGENTS.md": "a\n",
            "nvfanread/README.md": "r",
        }
        sha = merged(repo, 7, files)
        p = plan(repo, sha, FakeGitHub({7: FakePr(sha)}))
        assert not p.release
        assert "docs" in p.reason

    def test_workflow_only_releases(self, repo: Path) -> None:
        sha = merged(repo, 8, {".github/workflows/release.yml": "x\n"})
        assert plan(repo, sha, FakeGitHub({8: FakePr(sha)})).release

    def test_docs_only_judges_the_range_since_the_tag(self, repo: Path) -> None:
        # A code merge whose run was cancelled ships with the next merge,
        # even when that merge itself is docs-only.
        git(repo, "tag", "v1.0.1")
        code = merged(repo, 20, {"nvfanread/nvfanread.c": "new\n"})
        docs = merged(repo, 21, {"docs/a.md": "x\n"})
        gh = FakeGitHub({20: FakePr(code), 21: FakePr(docs)})
        p = plan(repo, docs, gh)
        assert p.release
        assert str(p.version) == "1.0.2"

    def test_docs_only_since_the_tag_skips(self, repo: Path) -> None:
        git(repo, "tag", "v1.0.1")
        a = merged(repo, 22, {"docs/a.md": "x\n"})
        b = merged(repo, 23, {"README.md": "y\n"})
        assert not plan(repo, b, FakeGitHub({22: FakePr(a), 23: FakePr(b)})).release

    def test_rename_from_code_to_docs_releases(self, repo: Path) -> None:
        # With rename detection, a move of nvfanread.c to docs/ would list only
        # the new path. --no-renames lists the deleted code file too.
        (repo / "docs").mkdir()
        git(repo, "mv", "nvfanread/nvfanread.c", "docs/old.c")
        sha = merged(repo, 24, {})
        assert plan(repo, sha, FakeGitHub({24: FakePr(sha)})).release

    def test_label_lookup_failure_fails(self, repo: Path) -> None:
        sha = merged(repo, 99, {"nvfanread/nvfanread.c": "4\n"})
        with pytest.raises(ReleaseError, match="#99"):
            plan(repo, sha, FakeGitHub({}))

    def test_pr_merged_as_another_commit_fails(self, repo: Path) -> None:
        sha = merged(repo, 30, {"nvfanread/nvfanread.c": "5\n"})
        gh = FakeGitHub({30: FakePr("c" * 40, ["semver:major"])})
        with pytest.raises(ReleaseError, match="not as this commit"):
            plan(repo, sha, gh)

    def test_pr_not_merged_fails(self, repo: Path) -> None:
        sha = merged(repo, 31, {"nvfanread/nvfanread.c": "6\n"})
        gh = FakeGitHub({31: FakePr(None, state="OPEN")})
        with pytest.raises(ReleaseError, match="OPEN"):
            plan(repo, sha, gh)

    def test_direct_push_publishes_nothing(self, repo: Path) -> None:
        sha = commit(repo, "hotfix", {"nvfanread/nvfanread.c": "5\n"})
        gh = FakeGitHub()
        p = plan(repo, sha, gh)
        assert not p.release
        assert p.pr is None
        assert "direct push" in p.reason
        # Asked GitHub, and did not look up any PR.
        assert [list(c[1:2]) for c in gh.calls] == [["api"]]
        assert gh.calls[0][2] == f"repos/{REPO}/commits/{sha}/pulls"

    def test_direct_push_code_ships_with_next_merge(self, repo: Path) -> None:
        git(repo, "tag", "v1.0.1")
        push = commit(repo, "hotfix", {"nvfanread/nvfanread.c": "5\n"})
        docs = merged(repo, 25, {"docs/a.md": "x\n"})
        gh = FakeGitHub({25: FakePr(docs)})
        assert not plan(repo, push, gh).release
        p = plan(repo, docs, gh)
        assert p.release
        assert str(p.version) == "1.0.2"

    def test_edited_squash_title_fails(self, repo: Path) -> None:
        # GitHub says PR #40 merged as this commit, but the subject lost (#40).
        sha = commit(repo, "a title someone edited", {"nvfanread/nvfanread.c": "7\n"})
        gh = FakeGitHub({40: FakePr(sha, ["skip-release"])})
        with pytest.raises(ReleaseError, match="squash title was edited"):
            plan(repo, sha, gh)

    def test_commit_pulls_lookup_failure_fails(self, repo: Path) -> None:
        sha = commit(repo, "hotfix", {"nvfanread/nvfanread.c": "8\n"})

        def broken(args: Sequence[str]) -> str:
            raise ReleaseError("gh: HTTP 502")

        with pytest.raises(ReleaseError, match="502"):
            plan(repo, sha, broken)

    def test_merge_commit_without_number_fails(self, repo: Path) -> None:
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, "side work", {"scripts/a.sh": "a\n"})
        git(repo, "checkout", "-q", "main")
        commit(repo, "main work", {"scripts/b.sh": "b\n"})
        git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
        sha = git(repo, "rev-parse", "HEAD")
        with pytest.raises(ReleaseError, match="merge commit"):
            plan(repo, sha, FakeGitHub())

    def test_merge_commit_docs_only_compares_first_parent(self, repo: Path) -> None:
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, "side docs", {"docs/x.md": "a\n"})
        git(repo, "checkout", "-q", "main")
        commit(repo, "main code", {"nvfanread/nvfanread.c": "x\n"})
        git(
            repo,
            "merge",
            "-q",
            "--no-ff",
            "-m",
            "Merge pull request #9 from o/s",
            "side",
        )
        sha = git(repo, "rev-parse", "HEAD")
        assert not plan(repo, sha, FakeGitHub({9: FakePr(sha)})).release

    def test_rerun_reuses_the_tag_on_this_commit(self, repo: Path) -> None:
        git(repo, "tag", "v1.0.1")
        sha = merged(repo, 10, {"nvfanread/nvfanread.c": "6\n"})
        git(repo, "tag", "v1.1.0", sha)
        git(repo, "tag", "nightly", sha)
        gh = FakeGitHub()
        p = plan(repo, sha, gh)
        assert p.release
        assert p.reused
        assert str(p.version) == "1.1.0"
        assert p.pr == 10
        assert gh.calls == []  # a rerun does not look anything up again

    def test_rerun_with_two_version_tags_on_commit_fails(self, repo: Path) -> None:
        sha = merged(repo, 11, {"nvfanread/nvfanread.c": "7\n"})
        git(repo, "tag", "v1.0.1", sha)
        git(repo, "tag", "v1.0.2", sha)
        with pytest.raises(ReleaseError, match="more than one"):
            plan(repo, sha, FakeGitHub())

    def test_rerun_of_an_older_merge_is_refused(self, repo: Path) -> None:
        # PR A (minor) merges; its run fails (say, no Quick summary). PR B
        # merges and publishes v1.0.1. A re-run of A's job must not publish
        # v1.1.0 on A, which is older code than v1.0.1.
        a = merged(repo, 50, {"nvfanread/nvfanread.c": "A\n"})
        b = merged(repo, 51, {"nvfanread/nvfanread.c": "B\n"})
        git(repo, "tag", "v1.0.1", b)
        gh = FakeGitHub({50: FakePr(a, ["semver:minor"]), 51: FakePr(b)})
        with pytest.raises(ReleaseError, match="not an ancestor"):
            plan(repo, a, gh)
        assert gh.calls == []  # refused before any lookup

    def test_commit_off_the_tagged_line_is_refused(self, repo: Path) -> None:
        base = git(repo, "rev-parse", "HEAD")
        b = merged(repo, 52, {"nvfanread/nvfanread.c": "B\n"})
        git(repo, "tag", "v1.0.1", b)
        git(repo, "checkout", "-q", "-b", "other", base)
        c = merged(repo, 53, {"nvfanread/nvfanread.c": "C\n"})
        with pytest.raises(ReleaseError, match="not an ancestor"):
            plan(repo, c, FakeGitHub({53: FakePr(c)}))

    def test_unknown_revision_fails(self, repo: Path) -> None:
        with pytest.raises(ReleaseError):
            plan(repo, "0" * 40, FakeGitHub())


class TestPlanCommand:
    def graphql_file(self, tmp_path: Path, sha: str, labels: list[str]) -> Path:
        path = tmp_path / "pr.json"
        gh = FakeGitHub({12: FakePr(sha, labels)})
        path.write_text(gh(["gh", "api", "graphql", "number=12"]))
        return path

    def test_writes_github_output_and_summary(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = merged(repo, 12, {"nvfanread/nvfanread.c": "8\n"})
        pr_json = self.graphql_file(tmp_path, sha, ["semver:minor"])
        out = tmp_path / "out"
        summary = tmp_path / "summary"
        monkeypatch.chdir(repo)
        args = [
            "plan",
            "--rev",
            sha,
            "--repo",
            REPO,
            "--pr-json",
            str(pr_json),
            "--github-output",
            str(out),
            "--step-summary",
            str(summary),
        ]
        rc = release.main(args)
        assert rc == 0
        lines = out.read_text().splitlines()
        assert "release=true" in lines
        assert "version=1.1.0" in lines
        assert "tag=v1.1.0" in lines
        assert "pr=12" in lines
        assert "reused=false" in lines
        assert "v1.1.0" in summary.read_text()

    def test_direct_push_writes_release_false(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = commit(repo, "hotfix", {"nvfanread/nvfanread.c": "9\n"})
        pulls = tmp_path / "pulls.json"
        pulls.write_text("[]")
        out = tmp_path / "out"
        summary = tmp_path / "summary"
        monkeypatch.chdir(repo)
        args = [
            "plan",
            "--rev",
            sha,
            "--repo",
            REPO,
            "--pulls-json",
            str(pulls),
            "--github-output",
            str(out),
            "--step-summary",
            str(summary),
        ]
        rc = release.main(args)
        assert rc == 0
        assert "release=false" in out.read_text().splitlines()
        assert "direct push" in summary.read_text()

    def test_failure_is_nonzero(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = merged(repo, 14, {"nvfanread/nvfanread.c": "9\n"})
        pr_json = tmp_path / "pr.json"
        pr_json.write_text("not json")
        out = tmp_path / "out"
        monkeypatch.chdir(repo)
        args = [
            "plan",
            "--rev",
            sha,
            "--repo",
            REPO,
            "--pr-json",
            str(pr_json),
            "--github-output",
            str(out),
        ]
        rc = release.main(args)
        assert rc != 0
        assert not out.exists()
