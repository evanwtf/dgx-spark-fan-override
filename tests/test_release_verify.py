"""Tests for the build scripts and the `verify` step that checks built .debs.

These run the real build scripts (dpkg-deb only, no install), so they prove
that a release build stamps the version it was given everywhere it matters.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import release
from release import ReleaseError

ROOT = Path(__file__).resolve().parent.parent


def build(version: str | None, outdir: Path) -> list[Path]:
    args = [] if version is None else [version]
    for script in ("build-debs.sh", "build-nvfanread-deb.sh"):
        subprocess.run(
            ["sh", str(ROOT / "scripts" / script), *args, str(outdir)]
            if args
            else ["sh", str(ROOT / "scripts" / script)],
            check=True,
            capture_output=True,
            cwd=ROOT,
        )
    return sorted(outdir.glob("*.deb"))


def tracked_diff() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--stat", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture(scope="module")
def debs_110(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    out = tmp_path_factory.mktemp("debs")
    return build("1.1.0", out)


def test_builds_both_packages_with_version_in_name(debs_110: list[Path]) -> None:
    names = [d.name for d in debs_110]
    assert names == ["nvfancontrol_1.1.0_arm64.deb", "nvfanread_1.1.0_arm64.deb"]


def test_verify_passes_at_built_version(debs_110: list[Path]) -> None:
    release.verify_debs(debs_110, release.Version.parse("1.1.0"))


def test_verify_fails_at_other_version(debs_110: list[Path]) -> None:
    with pytest.raises(ReleaseError, match=r"1\.1\.1"):
        release.verify_debs(debs_110, release.Version.parse("1.1.1"))


def test_verify_command_exit_codes(debs_110: list[Path]) -> None:
    paths = [str(d) for d in debs_110]
    assert release.main(["verify", "--version", "1.1.0", *paths]) == 0
    assert release.main(["verify", "--version", "1.1.1", *paths]) != 0


def test_verify_fails_when_a_package_is_missing(debs_110: list[Path]) -> None:
    with pytest.raises(ReleaseError, match="nvfancontrol"):
        release.verify_debs(debs_110[1:], release.Version.parse("1.1.0"))


def test_verify_fails_on_unknown_package(debs_110: list[Path], tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "DEBIAN").mkdir(parents=True)
    (stage / "DEBIAN" / "control").write_text(
        "Package: other\nVersion: 1.1.0\nArchitecture: all\n"
        "Maintainer: x <x@example.invalid>\nDescription: x\n"
    )
    other = tmp_path / "other.deb"
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(stage), str(other)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ReleaseError, match="other"):
        release.verify_debs([*debs_110, other], release.Version.parse("1.1.0"))


@pytest.mark.parametrize(
    ("package", "member", "old", "new"),
    [
        ("nvfanread", "DEBIAN/control", "Version: 1.1.0", "Version: 1.1.1"),
        (
            "nvfanread",
            "usr/src/nvfanread-1.1.0/dkms.conf",
            'PACKAGE_VERSION="1.1.0"',
            'PACKAGE_VERSION="0.0.0"',
        ),
        (
            "nvfanread",
            "usr/src/nvfanread-1.1.0/nvfanread.c",
            'MODULE_VERSION("1.1.0");',
            'MODULE_VERSION("0.0.0");',
        ),
        ("nvfanread", "DEBIAN/postinst", "version=1.1.0", "version=0.0.0"),
        ("nvfanread", "DEBIAN/prerm", "version=1.1.0", "version=0.0.0"),
        (
            "nvfancontrol",
            "usr/src/nvfancontrol/dkms.conf",
            'PACKAGE_VERSION="1.1.0"',
            'PACKAGE_VERSION="0.0.0"',
        ),
        (
            "nvfancontrol",
            "DEBIAN/postrm",
            "module_version='1.1.0'",
            "module_version='0.0.0'",
        ),
    ],
)
def test_one_wrong_stamp_fails(
    debs_110: list[Path],
    tmp_path: Path,
    package: str,
    member: str,
    old: str,
    new: str,
) -> None:
    """Rebuild one .deb with a single declaration changed: verify must catch it."""
    src = next(d for d in debs_110 if d.name.startswith(package + "_"))
    tree = tmp_path / "tree"
    subprocess.run(
        ["dpkg-deb", "-R", str(src), str(tree)], check=True, capture_output=True
    )
    path = tree / member
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    bad = tmp_path / src.name
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(tree), str(bad)],
        check=True,
        capture_output=True,
    )
    others = [d for d in debs_110 if d != src]
    with pytest.raises(ReleaseError):
        release.verify_debs([*others, bad], release.Version.parse("1.1.0"))


def test_default_build_is_placeholder_and_tree_is_unchanged(tmp_path: Path) -> None:
    before = tracked_diff()
    debs = build(None, tmp_path)  # default outdir is dist/ in the repo
    assert debs == []  # nothing went to tmp_path
    dist = ROOT / "dist"
    built = sorted(dist.glob("*_0.0.0_arm64.deb"))
    assert [d.name for d in built] == [
        "nvfancontrol_0.0.0_arm64.deb",
        "nvfanread_0.0.0_arm64.deb",
    ]
    release.verify_debs(built, release.Version.parse("0.0.0"))
    assert tracked_diff() == before


def test_release_build_leaves_tree_unchanged(tmp_path: Path) -> None:
    before = tracked_diff()
    build("3.4.5", tmp_path)
    assert tracked_diff() == before


@pytest.mark.parametrize("bad", ["v1.1.0", "1.1", "1.1.0-rc1", "x"])
def test_build_scripts_refuse_a_bad_version(bad: str, tmp_path: Path) -> None:
    for script in ("build-debs.sh", "build-nvfanread-deb.sh"):
        res = subprocess.run(
            ["sh", str(ROOT / "scripts" / script), bad, str(tmp_path)],
            capture_output=True,
            cwd=ROOT,
        )
        assert res.returncode != 0, script
    assert list(tmp_path.glob("*.deb")) == []


def test_dpkg_deb_is_present() -> None:
    # The verify step needs dpkg-deb; CI (ubuntu-latest) has it. Fail loudly
    # rather than skip if it is missing.
    assert shutil.which("dpkg-deb")


@pytest.mark.parametrize(
    ("package", "member"),
    [
        ("nvfanread", "usr/src/nvfanread-1.1.0/nvfanread.c"),
        ("nvfancontrol", "usr/src/nvfancontrol/nvfancontrol.c"),
    ],
)
def test_a_second_module_version_is_checked(
    debs_110: list[Path], tmp_path: Path, package: str, member: str
) -> None:
    """Every MODULE_VERSION in every shipped .c file counts, not only the first."""
    src = next(d for d in debs_110 if d.name.startswith(package + "_"))
    tree = tmp_path / "tree"
    subprocess.run(
        ["dpkg-deb", "-R", str(src), str(tree)], check=True, capture_output=True
    )
    path = tree / member
    path.write_text(path.read_text() + 'MODULE_VERSION("0.0.0");\n')
    bad = tmp_path / src.name
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(tree), str(bad)],
        check=True,
        capture_output=True,
    )
    others = [d for d in debs_110 if d != src]
    with pytest.raises(ReleaseError, match=r"MODULE_VERSION 0\.0\.0"):
        release.verify_debs([*others, bad], release.Version.parse("1.1.0"))
