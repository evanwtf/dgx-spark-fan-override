"""Tests for the `plan` step: real git repositories, a fake `gh`.

Each test builds a small git repo in a temp dir, so the git commands that
release.py runs are the real ones. Only the GitHub lookup is faked.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
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


def gh_labels(mapping: dict[int, list[str]]) -> release.Runner:
    """A fake gh that answers `gh pr view N --json labels` from a dict."""

    def run(args: Sequence[str]) -> str:
        assert list(args[:3]) == ["gh", "pr", "view"]
        number = int(args[3])
        if number not in mapping:
            raise ReleaseError(f"gh: no pull request #{number}")
        return json.dumps({"labels": [{"name": n} for n in mapping[number]]})

    return run


def plan(repo: Path, sha: str, gh: release.Runner) -> release.Plan:
    return release.make_plan(sha, REPO, git=release.git_runner(repo), gh=gh)


class TestPlan:
    def test_first_release_no_label_is_1_0_1(self, repo: Path) -> None:
        sha = commit(repo, "change code (#3)", {"nvfanread/nvfanread.c": "2\n"})
        p = plan(repo, sha, gh_labels({3: []}))
        assert p.release
        assert str(p.version) == "1.0.1"
        assert p.pr == 3
        assert not p.reused

    def test_first_release_minor_is_1_1_0(self, repo: Path) -> None:
        sha = commit(repo, "ci: release on merge (#4)", {".github/w.yml": "x\n"})
        p = plan(repo, sha, gh_labels({4: ["semver:minor"]}))
        assert p.release
        assert str(p.version) == "1.1.0"

    def test_after_a_tag(self, repo: Path) -> None:
        git(repo, "tag", "v1.1.0")
        sha = commit(repo, "more (#5)", {"scripts/x.sh": "y\n"})
        p = plan(repo, sha, gh_labels({5: ["semver:patch"]}))
        assert str(p.version) == "1.1.1"

    def test_skip_label(self, repo: Path) -> None:
        sha = commit(repo, "change (#6)", {"nvfanread/nvfanread.c": "3\n"})
        p = plan(repo, sha, gh_labels({6: ["skip-release", "semver:major"]}))
        assert not p.release
        assert "skip-release" in p.reason

    def test_docs_only(self, repo: Path) -> None:
        sha = commit(repo, "docs (#7)", {"docs/STATUS.md": "s\n", "AGENTS.md": "a\n"})
        p = plan(repo, sha, gh_labels({7: []}))
        assert not p.release
        assert "docs" in p.reason

    def test_workflow_only_releases(self, repo: Path) -> None:
        sha = commit(repo, "ci (#8)", {".github/workflows/release.yml": "x\n"})
        assert plan(repo, sha, gh_labels({8: []})).release

    def test_label_lookup_failure_fails(self, repo: Path) -> None:
        sha = commit(repo, "change (#99)", {"nvfanread/nvfanread.c": "4\n"})
        with pytest.raises(ReleaseError, match="#99"):
            plan(repo, sha, gh_labels({}))

    def test_direct_push_takes_patch_without_lookup(self, repo: Path) -> None:
        sha = commit(repo, "hotfix", {"nvfanread/nvfanread.c": "5\n"})

        def no_gh(args: Sequence[str]) -> str:
            raise AssertionError("gh must not be called for a direct push")

        p = plan(repo, sha, no_gh)
        assert p.release
        assert p.pr is None
        assert str(p.version) == "1.0.1"

    def test_merge_commit_without_number_fails(self, repo: Path) -> None:
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, "side work", {"scripts/a.sh": "a\n"})
        git(repo, "checkout", "-q", "main")
        commit(repo, "main work", {"scripts/b.sh": "b\n"})
        git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
        sha = git(repo, "rev-parse", "HEAD")
        with pytest.raises(ReleaseError, match="merge commit"):
            plan(repo, sha, gh_labels({}))

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
        assert not plan(repo, sha, gh_labels({9: []})).release

    def test_rerun_reuses_the_tag_on_this_commit(self, repo: Path) -> None:
        git(repo, "tag", "v1.0.1")
        sha = commit(repo, "change (#10)", {"nvfanread/nvfanread.c": "6\n"})
        git(repo, "tag", "v1.1.0", sha)
        git(repo, "tag", "nightly", sha)

        def no_gh(args: Sequence[str]) -> str:
            raise AssertionError("a rerun must not look up labels again")

        p = plan(repo, sha, no_gh)
        assert p.release
        assert p.reused
        assert str(p.version) == "1.1.0"
        assert p.pr == 10

    def test_rerun_with_two_version_tags_on_commit_fails(self, repo: Path) -> None:
        sha = commit(repo, "change (#11)", {"nvfanread/nvfanread.c": "7\n"})
        git(repo, "tag", "v1.0.1", sha)
        git(repo, "tag", "v1.0.2", sha)
        with pytest.raises(ReleaseError, match="more than one"):
            plan(repo, sha, gh_labels({11: []}))

    def test_unknown_revision_fails(self, repo: Path) -> None:
        with pytest.raises(ReleaseError):
            plan(repo, "0" * 40, gh_labels({}))


class TestPlanCommand:
    def test_writes_github_output_and_summary(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = commit(repo, "change (#12)", {"nvfanread/nvfanread.c": "8\n"})
        labels = tmp_path / "labels.json"
        labels.write_text(json.dumps({"labels": [{"name": "semver:minor"}]}))
        out = tmp_path / "out"
        summary = tmp_path / "summary"
        monkeypatch.chdir(repo)
        rc = release.main(
            [
                "plan",
                "--rev",
                sha,
                "--repo",
                REPO,
                "--labels-json",
                str(labels),
                "--github-output",
                str(out),
                "--step-summary",
                str(summary),
            ]
        )
        assert rc == 0
        lines = out.read_text().splitlines()
        assert "release=true" in lines
        assert "version=1.1.0" in lines
        assert "tag=v1.1.0" in lines
        assert "pr=12" in lines
        assert "reused=false" in lines
        assert "v1.1.0" in summary.read_text()

    def test_skip_writes_release_false(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = commit(repo, "docs (#13)", {"docs/a.md": "x\n"})
        labels = tmp_path / "labels.json"
        labels.write_text('{"labels": []}')
        out = tmp_path / "out"
        summary = tmp_path / "summary"
        monkeypatch.chdir(repo)
        rc = release.main(
            [
                "plan",
                "--rev",
                sha,
                "--repo",
                REPO,
                "--labels-json",
                str(labels),
                "--github-output",
                str(out),
                "--step-summary",
                str(summary),
            ]
        )
        assert rc == 0
        assert "release=false" in out.read_text().splitlines()
        assert "nothing" in summary.read_text()

    def test_failure_is_nonzero(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = commit(repo, "change (#14)", {"nvfanread/nvfanread.c": "9\n"})
        labels = tmp_path / "labels.json"
        labels.write_text("not json")
        out = tmp_path / "out"
        monkeypatch.chdir(repo)
        rc = release.main(
            [
                "plan",
                "--rev",
                sha,
                "--repo",
                REPO,
                "--labels-json",
                str(labels),
                "--github-output",
                str(out),
            ]
        )
        assert rc != 0
        assert not out.exists() or "release=true" not in out.read_text()
