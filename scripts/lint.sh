#!/bin/sh
# Repo linter: checkpatch.pl for kernel C, shellcheck for shell.
#
# Each tool is skipped with a notice if it is not installed, so this runs on a
# bare box (e.g. only checkpatch present via the kernel headers) and in CI
# (shellcheck installed; checkpatch absent). Exit status is non-zero if any
# tool that DID run reported a problem.
#
# checkpatch is a kernel developer tool tied to the kernel tree; run it locally
# via `make lint`. CI enforces shellcheck, the portable half.
set -u

cd "$(dirname "$0")/.." || exit 2
status=0

# ---- C: checkpatch.pl -------------------------------------------------------
# Locate checkpatch: $CHECKPATCH, then the running kernel's headers, then PATH.
CHECKPATCH="${CHECKPATCH:-}"
if [ -z "$CHECKPATCH" ]; then
	kbuild="/lib/modules/$(uname -r)/build/scripts/checkpatch.pl"
	if [ -x "$kbuild" ]; then
		CHECKPATCH="$kbuild"
	elif command -v checkpatch.pl >/dev/null 2>&1; then
		CHECKPATCH="$(command -v checkpatch.pl)"
	fi
fi

# Every C/header file we maintain. Globs pick up new files automatically.
c_files="$(ls nvfanread/*.c nvfanread/*.h nvfanread/tests/*.c \
	nvfancontrol/usr/src/nvfancontrol/*.c 2>/dev/null)"

if [ -n "$CHECKPATCH" ]; then
	echo "== checkpatch ($CHECKPATCH) =="
	# NEW_TYPEDEFS is intentional: nvfanread_proto.h defines dual-mode
	# (kernel + userspace) fixed-width aliases shared with the unit tests.
	# shellcheck disable=SC2086
	"$CHECKPATCH" --no-tree --terse --ignore NEW_TYPEDEFS -f $c_files || status=1
else
	echo "== checkpatch not found; skipping C lint (install kernel headers or set CHECKPATCH) =="
fi

# ---- Shell: shellcheck ------------------------------------------------------
# The maintainer scripts run as root during package install/removal, so these
# are the highest-value scripts to lint.
sh_files="nvfancontrol/DEBIAN/postinst \
	nvfancontrol/DEBIAN/prerm \
	nvfancontrol/DEBIAN/postrm \
	nvfancontrol/usr/sbin/nvfancontrol \
	scripts/lint.sh"

if command -v shellcheck >/dev/null 2>&1; then
	echo "== shellcheck =="
	# shellcheck disable=SC2086
	shellcheck $sh_files || status=1
else
	echo "== shellcheck not found; skipping shell lint (apt-get install shellcheck) =="
fi

if [ "$status" -eq 0 ]; then
	echo "== lint OK =="
fi
exit "$status"
