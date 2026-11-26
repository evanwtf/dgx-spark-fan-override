"""Tests for scripts/release.py: the logic that decides and describes a release.

The release runs unattended on every merge to main, so most of these tests are
negative: the job is to refuse correctly, never to guess.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

import release
from release import ReleaseError, Version

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "evanwtf/dgx-spark-fan-override"


# ---- PR number from the merge commit ----------------------------------------


class TestPrNumber:
    def test_squash_subject(self) -> None:
        subject = (
            "nvfanread 1.1.0: read fans on kernel 7.0.0-1025+ without binding "
            "the FF-A device (#17)"
        )
        assert release.pr_number_from_commit(subject, 1) == 17

    def test_direct_push_has_no_number(self) -> None:
        assert release.pr_number_from_commit("docs: fix a typo", 1) is None

    def test_root_commit_has_no_number(self) -> None:
        assert release.pr_number_from_commit("initial commit", 0) is None

    def test_merge_commit_without_number_is_refused(self) -> None:
        with pytest.raises(ReleaseError, match="merge commit"):
            release.pr_number_from_commit("Merge branch 'x' into main", 2)

    def test_merge_commit_shape_is_read(self) -> None:
        subject = "Merge pull request #12 from evanwtf/some-branch"
        assert release.pr_number_from_commit(subject, 2) == 12

    def test_merge_shape_on_single_parent_is_not_a_pr(self) -> None:
        # Only a real merge commit (2 parents) writes this subject.
        subject = "Merge pull request #12 from evanwtf/some-branch"
        assert release.pr_number_from_commit(subject, 1) is None

    def test_hash_not_at_the_end_is_ignored(self) -> None:
        assert release.pr_number_from_commit("fix #15 in the parser", 1) is None
        assert release.pr_number_from_commit("revert (#15) partly", 1) is None

    def test_bare_hash_at_end_is_ignored(self) -> None:
        assert release.pr_number_from_commit("closes #15", 1) is None

    def test_only_the_trailing_number_counts(self) -> None:
        subject = "Follow-up to (#3) for #4 (#21)"
        assert release.pr_number_from_commit(subject, 1) == 21

    def test_trailing_whitespace_is_tolerated(self) -> None:
        assert release.pr_number_from_commit("thing (#9)  ", 1) == 9

    def test_negative_parent_count_is_refused(self) -> None:
        with pytest.raises(ReleaseError):
            release.pr_number_from_commit("thing (#9)", -1)


# ---- Bump level from labels -------------------------------------------------


class TestBumpLevel:
    @pytest.mark.parametrize(
        ("labels", "level"),
        [
            ([], "patch"),
            (["bug", "documentation"], "patch"),
            (["semver:patch"], "patch"),
            (["semver:minor"], "minor"),
            (["semver:major"], "major"),
            (["semver:minor", "semver:patch"], "minor"),
            (["semver:patch", "semver:major", "semver:minor"], "major"),
            (["skip-release"], "skip"),
            (["semver:major", "skip-release"], "skip"),
        ],
    )
    def test_levels(self, labels: list[str], level: str) -> None:
        assert release.bump_level(labels) == level

    def test_label_match_is_exact(self) -> None:
        assert release.bump_level(["semver:Major", "skip-release-not"]) == "patch"


class TestLabelsFromGhJson:
    def test_real_shape_no_labels(self) -> None:
        text = (FIXTURES / "pr17.json").read_text()
        assert release.labels_from_gh_json(text) == []

    def test_real_shape_with_a_label(self) -> None:
        # Shape copied from `gh pr view 76 -R evanwtf/monitor --json labels`.
        text = json.dumps(
            {
                "labels": [
                    {
                        "color": "5319E7",
                        "description": "Merging this publishes no release",
                        "id": "LA_kwDOT1I5MM8AAAACwVacCg",
                        "name": "skip-release",
                    }
                ]
            }
        )
        assert release.labels_from_gh_json(text) == ["skip-release"]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "[]",
            "{}",
            '{"labels": null}',
            '{"labels": "semver:major"}',
            '{"labels": [{"id": "x"}]}',
            '{"labels": [{"name": 3}]}',
            '{"labels": ["semver:major"]}',
        ],
    )
    def test_malformed_is_refused(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            release.labels_from_gh_json(text)

    def test_failed_lookup_fails_the_release(self) -> None:
        def broken(args: Sequence[str]) -> str:
            raise ReleaseError("gh: HTTP 403")

        with pytest.raises(ReleaseError, match="403"):
            release.fetch_labels(17, REPO, broken)

    def test_lookup_passes_number_and_repo_as_arguments(self) -> None:
        seen: list[Sequence[str]] = []

        def fake(args: Sequence[str]) -> str:
            seen.append(args)
            return '{"labels": [{"name": "semver:minor"}]}'

        assert release.fetch_labels(17, REPO, fake) == ["semver:minor"]
        assert seen == [["gh", "pr", "view", "17", "--repo", REPO, "--json", "labels"]]


# ---- Next version from tags -------------------------------------------------


class TestVersion:
    def test_parse_and_format(self) -> None:
        v = Version.parse("1.2.3")
        assert (v.major, v.minor, v.patch) == (1, 2, 3)
        assert str(v) == "1.2.3"
        assert v.tag == "v1.2.3"

    @pytest.mark.parametrize(
        "text",
        ["", "1.2", "1.2.3.4", "v1.2.3", "1.2.x", "01.2.3", "1.2.3-rc1", " 1.2.3"],
    )
    def test_malformed_is_refused(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            Version.parse(text)

    def test_semver_order_not_lexical(self) -> None:
        assert Version.parse("0.0.10") > Version.parse("0.0.9")
        assert Version.parse("1.10.0") > Version.parse("1.9.9")


class TestNextVersion:
    def test_base_is_the_hand_made_release(self) -> None:
        assert Version(1, 0, 0) == release.BASE_VERSION

    @pytest.mark.parametrize(
        ("level", "expected"),
        [("patch", "1.0.1"), ("minor", "1.1.0"), ("major", "2.0.0")],
    )
    def test_no_tags_bumps_from_base(self, level: str, expected: str) -> None:
        assert str(release.next_version([], level)) == expected

    def test_base_is_ignored_once_a_tag_exists(self) -> None:
        assert str(release.next_version(["v1.1.0"], "patch")) == "1.1.1"
        # Even a tag below the base wins: the tags are the record.
        assert str(release.next_version(["v0.0.3"], "patch")) == "0.0.4"

    def test_highest_by_semver(self) -> None:
        tags = ["v0.0.9", "v0.0.10", "v0.0.2"]
        assert str(release.next_version(tags, "patch")) == "0.0.11"

    def test_minor_and_major_reset_lower_parts(self) -> None:
        assert str(release.next_version(["v1.2.3"], "minor")) == "1.3.0"
        assert str(release.next_version(["v1.2.3"], "major")) == "2.0.0"

    def test_non_matching_tags_are_ignored(self) -> None:
        tags = ["nightly", "v9", "v9.9", "v9.9.9-rc1", "release-9.9.9", "v1.0.4"]
        assert str(release.next_version(tags, "patch")) == "1.0.5"

    def test_only_non_matching_tags_means_base(self) -> None:
        assert str(release.next_version(["nightly", "v2"], "minor")) == "1.1.0"

    @pytest.mark.parametrize("level", ["skip", "", "Patch", "huge"])
    def test_bad_level_is_refused(self, level: str) -> None:
        with pytest.raises(ReleaseError):
            release.next_version([], level)


# ---- Docs-only changes publish nothing --------------------------------------


class TestDocsOnly:
    @pytest.mark.parametrize(
        "paths",
        [
            ["README.md"],
            ["docs/STATUS.md"],
            ["docs/deep/nested/file.png", "AGENTS.md", "CLAUDE.md"],
        ],
    )
    def test_docs_only(self, paths: list[str]) -> None:
        assert release.is_docs_only(paths)

    @pytest.mark.parametrize(
        "paths",
        [
            [],  # an empty change set is not "docs only"; it releases
            ["nvfanread/README.md"],  # a .md below the top level is not docs
            ["README.md", "scripts/build-debs.sh"],
            [".github/workflows/release.yml"],
            ["packaging/nvfanread/DEBIAN/control"],
            ["nvfanread/nvfanread.c"],
            ["docsx/file"],
            ["README.mdx"],
        ],
    )
    def test_releases(self, paths: list[str]) -> None:
        assert not release.is_docs_only(paths)


# ---- Release notes from the PR body -----------------------------------------


class TestNotes:
    def test_real_pr17_body(self) -> None:
        body = json.loads((FIXTURES / "pr17.json").read_text())["body"]
        summary = release.quick_summary(body)
        assert summary.startswith("- On kernel 7.0.0-1025, a built-in kernel driver")
        assert summary.endswith("Closes #15")
        assert "## Cause" not in summary

    def test_stops_at_next_h2_but_not_h3(self) -> None:
        body = "## Quick summary\n\nOne.\n\n### Detail\n\nTwo.\n\n## Next\n\nNo."
        assert release.quick_summary(body) == "One.\n\n### Detail\n\nTwo."

    def test_heading_inside_code_fence_does_not_end_it(self) -> None:
        body = "## Quick summary\n\nRun:\n\n```\n## not a heading\n```\n\n## Next\n"
        assert release.quick_summary(body) == "Run:\n\n```\n## not a heading\n```"

    def test_crlf_body_from_web_editor(self) -> None:
        body = "## Quick summary\r\n\r\nLine one.\r\n\r\n## Next\r\n"
        assert release.quick_summary(body) == "Line one."

    def test_backticks_and_shell_text_survive_byte_for_byte(self) -> None:
        tricky = "Run `rm -rf $HOME` and $(whoami) and \\n and 'quotes' \"too\"."
        body = f"## Quick summary\n\n{tricky}\n\n## Next\n"
        assert release.quick_summary(body) == tricky

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "No headings at all.",
            "## Summary\n\nWrong heading.",
            "## Quick summary\n\n\n## Next\n\nText.",
            "## Quick summary\n   \n",
            "### Quick summary\n\nWrong level.",
        ],
    )
    def test_missing_or_empty_is_refused(self, body: str) -> None:
        with pytest.raises(ReleaseError):
            release.quick_summary(body)

    def test_notes_link_the_pr(self) -> None:
        body = "## Quick summary\n\nThe change.\n"
        notes = release.release_notes(body, 17, REPO)
        assert notes == (
            "The change.\n\n"
            "Pull request: https://github.com/evanwtf/dgx-spark-fan-override/pull/17\n"
        )

    def test_body_from_gh_json(self) -> None:
        text = (FIXTURES / "pr17.json").read_text()
        assert release.body_from_gh_json(text).startswith("## Quick summary")

    @pytest.mark.parametrize("text", ["", "[]", "{}", '{"body": null}', "x"])
    def test_body_from_bad_json_is_refused(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            release.body_from_gh_json(text)

    def test_notes_command_writes_file_unchanged(self, tmp_path: Path) -> None:
        tricky = "Uses `$(echo pwned)` and `` ` `` verbatim."
        pr_json = tmp_path / "pr.json"
        pr_json.write_text(json.dumps({"body": f"## Quick summary\n\n{tricky}\n"}))
        out = tmp_path / "notes.md"
        rc = release.main(
            [
                "notes",
                "--pr",
                "5",
                "--repo",
                REPO,
                "--pr-json",
                str(pr_json),
                "--out",
                str(out),
            ]
        )
        assert rc == 0
        assert (
            out.read_bytes()
            == (
                f"{tricky}\n\nPull request: https://github.com/{REPO}/pull/5\n"
            ).encode()
        )

    def test_notes_command_refuses_missing_summary(self, tmp_path: Path) -> None:
        pr_json = tmp_path / "pr.json"
        pr_json.write_text(json.dumps({"body": "nothing here"}))
        out = tmp_path / "notes.md"
        rc = release.main(
            [
                "notes",
                "--pr",
                "5",
                "--repo",
                REPO,
                "--pr-json",
                str(pr_json),
                "--out",
                str(out),
            ]
        )
        assert rc != 0
        assert not out.exists()

    def test_direct_push_notes_from_commit_message(self) -> None:
        notes = release.commit_notes("Fix the thing\n\nLonger text.\n", "abc123", REPO)
        assert notes == (
            "Fix the thing\n\nLonger text.\n\n"
            "Commit: https://github.com/evanwtf/dgx-spark-fan-override/commit/abc123\n"
        )

    def test_direct_push_with_empty_message_is_refused(self) -> None:
        with pytest.raises(ReleaseError):
            release.commit_notes("  \n", "abc123", REPO)
