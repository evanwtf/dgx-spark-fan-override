"""Tests for scripts/release.py: the logic that decides and describes a release.

The release runs unattended on every merge to main, so most of these tests are
negative: the job is to refuse correctly, never to guess.
"""

from __future__ import annotations

import json
import subprocess
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


# ---- The PR lookup ----------------------------------------------------------

PR17_SHA = "15de9575d462ee8979014c877ff479ede84cbfca"


def graphql(
    number: int = 17,
    state: str = "MERGED",
    merge_oid: str | None = PR17_SHA,
    merged_at: str | None = "2026-11-26T03:23:29Z",
    last_edited_at: str | None = None,
    labels: Sequence[str] = (),
    body: str = "## Quick summary\n\nThe change.\n",
) -> str:
    """An answer to release._PR_QUERY, in the shape of the real one for #17
    (tests/fixtures/pr17_graphql.json)."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": number,
                        "state": state,
                        "body": body,
                        "mergedAt": merged_at,
                        "lastEditedAt": last_edited_at,
                        "mergeCommit": None
                        if merge_oid is None
                        else {"oid": merge_oid},
                        "labels": {
                            "totalCount": len(labels),
                            "nodes": [{"name": n} for n in labels],
                        },
                    }
                }
            }
        }
    )


class TestPrLookup:
    def test_real_pr17_shape(self) -> None:
        pr = release.pr_from_graphql((FIXTURES / "pr17_graphql.json").read_text())
        assert pr.number == 17
        assert pr.state == "MERGED"
        assert pr.merge_commit == PR17_SHA
        assert pr.labels == []
        assert pr.last_edited_at is None
        assert pr.body.startswith("## Quick summary")
        release.check_merged_as(pr, PR17_SHA)
        release.check_body_not_edited_after_merge(pr)

    def test_labels_are_read(self) -> None:
        pr = release.pr_from_graphql(graphql(labels=["semver:minor", "bug"]))
        assert pr.labels == ["semver:minor", "bug"]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "[]",
            "{}",
            '{"data": null}',
            '{"data": {"repository": null}}',
            '{"data": {"repository": {"pullRequest": null}}}',
            '{"errors": [{"message": "Bad credentials"}]}',
        ],
    )
    def test_malformed_is_refused(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            release.pr_from_graphql(text)

    def test_label_without_name_is_refused(self) -> None:
        text = graphql(labels=["semver:minor"]).replace('"name"', '"nom"')
        with pytest.raises(ReleaseError):
            release.pr_from_graphql(text)

    def test_unread_labels_are_refused(self) -> None:
        text = graphql(labels=["semver:minor"]).replace(
            '"totalCount": 1', '"totalCount": 101'
        )
        with pytest.raises(ReleaseError, match="101 labels"):
            release.pr_from_graphql(text)

    def test_failed_lookup_fails_the_release(self) -> None:
        def broken(args: Sequence[str]) -> str:
            raise ReleaseError("gh: HTTP 403")

        with pytest.raises(ReleaseError, match="403"):
            release.fetch_pr(17, REPO, broken)

    def test_lookup_passes_values_as_arguments(self) -> None:
        seen: list[Sequence[str]] = []

        def fake(args: Sequence[str]) -> str:
            seen.append(args)
            return graphql()

        release.fetch_pr(17, REPO, fake)
        (args,) = seen
        assert list(args[:3]) == ["gh", "api", "graphql"]
        assert "owner=evanwtf" in args
        assert "name=dgx-spark-fan-override" in args
        assert "number=17" in args

    def test_wrong_number_in_answer_is_refused(self) -> None:
        with pytest.raises(ReleaseError, match="#18"):
            release.fetch_pr(17, REPO, lambda args: graphql(number=18))

    def test_not_merged_is_refused(self) -> None:
        pr = release.pr_from_graphql(graphql(state="OPEN", merge_oid=None))
        with pytest.raises(ReleaseError, match="OPEN"):
            release.check_merged_as(pr, PR17_SHA)

    def test_other_merge_commit_is_refused(self) -> None:
        pr = release.pr_from_graphql(graphql(merge_oid="a" * 40))
        with pytest.raises(ReleaseError, match="not as this commit"):
            release.check_merged_as(pr, PR17_SHA)

    def test_body_edited_after_merge_is_refused(self) -> None:
        pr = release.pr_from_graphql(graphql(last_edited_at="2026-11-26T03:24:00Z"))
        with pytest.raises(ReleaseError, match="after the merge"):
            release.check_body_not_edited_after_merge(pr)

    def test_body_edited_before_merge_is_fine(self) -> None:
        pr = release.pr_from_graphql(graphql(last_edited_at="2026-11-26T03:00:00Z"))
        release.check_body_not_edited_after_merge(pr)

    def test_bad_time_is_refused(self) -> None:
        with pytest.raises(ReleaseError):
            release.pr_from_graphql(graphql(last_edited_at="yesterday"))

    def test_commit_pulls_real_shape(self) -> None:
        text = (FIXTURES / "pulls_15de957.json").read_text()
        assert release.commit_pulls_from_json(text) == [(17, PR17_SHA)]
        assert release.commit_pulls_from_json("[]") == []

    @pytest.mark.parametrize(
        "text",
        ["", "{}", '[{"number": 1}]', '[{"number": "1", "merge_commit_sha": null}]'],
    )
    def test_commit_pulls_malformed_is_refused(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            release.commit_pulls_from_json(text)


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
            ["nvfanread/README.md"],  # no .md file ships in either .deb
            ["tools/ec-probe/README.md", "docs/x.txt"],
        ],
    )
    def test_docs_only(self, paths: list[str]) -> None:
        assert release.is_docs_only(paths)

    @pytest.mark.parametrize(
        "paths",
        [
            [],  # an empty change set is not "docs only"; it releases
            ["README.md", "scripts/build-debs.sh"],
            [".github/workflows/release.yml"],
            ["packaging/nvfanread/DEBIAN/control"],
            ["nvfanread/nvfanread.c"],
            ["docsx/file"],
            ["README.mdx"],
            ["nvfanread/docs/x.c"],
        ],
    )
    def test_releases(self, paths: list[str]) -> None:
        assert not release.is_docs_only(paths)


# ---- Release notes from the PR body -----------------------------------------


class TestNotes:
    def test_real_pr17_body(self) -> None:
        body = release.pr_from_graphql(
            (FIXTURES / "pr17_graphql.json").read_text()
        ).body
        summary = release.quick_summary(body)
        assert summary.startswith("- On kernel 7.0.0-1025, a built-in kernel driver")
        assert summary.endswith("Closes #15")
        assert "## Cause" not in summary

    def test_stops_at_next_h2_but_not_h3(self) -> None:
        body = "## Quick summary\n\nOne.\n\n### Detail\n\nTwo.\n\n## Next\n\nNo."
        assert release.quick_summary(body) == "One.\n\n### Detail\n\nTwo."

    def test_stops_at_next_h1(self) -> None:
        body = "## Quick summary\n\nOne.\n\n# Big heading\n\nNo."
        assert release.quick_summary(body) == "One."

    def test_heading_inside_backtick_fence_does_not_end_it(self) -> None:
        body = (
            "## Quick summary\n\nRun:\n\n```\n## not a heading\n# nor this\n```\n\n"
            "## Next\n"
        )
        assert release.quick_summary(body) == (
            "Run:\n\n```\n## not a heading\n# nor this\n```"
        )

    def test_heading_inside_tilde_fence_does_not_end_it(self) -> None:
        body = (
            "## Quick summary\n\nRun:\n\n~~~sh\n# a comment\n~~~\n\nAfter.\n\n## Next\n"
        )
        assert (
            release.quick_summary(body) == "Run:\n\n~~~sh\n# a comment\n~~~\n\nAfter."
        )

    def test_backticks_do_not_close_a_tilde_fence(self) -> None:
        body = "## Quick summary\n\n~~~\n```\n## inside\n~~~\n\n## Next\n"
        assert release.quick_summary(body) == "~~~\n```\n## inside\n~~~"

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
            "## Quick summary\n\n# Next\n\nText.",
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


class TestNotesCommand:
    """`notes` against a real git commit (in a temp repo) and a saved answer."""

    @pytest.fixture
    def sha(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
        }
        for args in (
            ["init", "-q"],
            ["commit", "-q", "--allow-empty", "-m", "x (#17)"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
        monkeypatch.chdir(repo)
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        )
        return out.stdout.strip()

    def run(self, tmp_path: Path, sha: str, answer: str) -> tuple[int, Path]:
        pr_json = tmp_path / "pr.json"
        pr_json.write_text(answer)
        out = tmp_path / "notes.md"
        args = ["notes", "--pr", "17", "--rev", sha, "--repo", REPO]
        args += ["--pr-json", str(pr_json), "--out", str(out)]
        rc = release.main(args)
        return rc, out

    def test_writes_file_unchanged(self, tmp_path: Path, sha: str) -> None:
        tricky = "Uses `$(echo pwned)` and `` ` `` verbatim."
        answer = graphql(merge_oid=sha, body=f"## Quick summary\n\n{tricky}\n")
        rc, out = self.run(tmp_path, sha, answer)
        assert rc == 0
        assert (
            out.read_bytes()
            == (
                f"{tricky}\n\nPull request: https://github.com/{REPO}/pull/17\n"
            ).encode()
        )

    @pytest.mark.parametrize(
        "change",
        [
            {"body": "nothing here"},
            {"last_edited_at": "2026-11-27T00:00:00Z"},
            {"merge_oid": "b" * 40},
            {"state": "CLOSED", "merge_oid": None, "merged_at": None},
        ],
    )
    def test_refusals_write_nothing(
        self, tmp_path: Path, sha: str, change: dict[str, str | None]
    ) -> None:
        fields: dict[str, str | None] = {"merge_oid": sha, **change}
        rc, out = self.run(tmp_path, sha, graphql(**fields))  # type: ignore[arg-type]
        assert rc != 0
        assert not out.exists()
