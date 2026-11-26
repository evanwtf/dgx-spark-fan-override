#!/bin/sh
# Tests for scripts/check-versions.sh. It must pass on the real tree and fail
# when any one declaration differs or is missing.
set -u

here="$(cd "$(dirname "$0")" && pwd)"
repo="$here/.."
check="$here/check-versions.sh"
fails=0

files="packaging/nvfanread/DEBIAN/control nvfanread/dkms.conf \
	packaging/nvfanread/DEBIAN/postinst packaging/nvfanread/DEBIAN/prerm \
	scripts/build-nvfanread-deb.sh nvfanread/nvfanread.c"

fresh_copy() {
	dir="$(mktemp -d)"
	for f in $files; do
		mkdir -p "$dir/$(dirname "$f")"
		cp "$repo/$f" "$dir/$f"
	done
	echo "$dir"
}

expect() {
	# $1 = want (pass|fail), $2 = description, $3 = root
	if sh "$check" "$3" >/dev/null 2>&1; then got=pass; else got=fail; fi
	if [ "$got" = "$1" ]; then
		echo "ok - $2"
	else
		echo "NOT OK - $2 (wanted $1, got $got)"
		fails=$((fails + 1))
	fi
}

expect pass "real tree is consistent" "$repo"

# One mutation per declaration: each must make the check fail.
for case in control dkms postinst prerm build module; do
	dir="$(fresh_copy)"
	case "$case" in
	control) sed -i 's/^Version: .*/Version: 9.9.9/' "$dir/packaging/nvfanread/DEBIAN/control" ;;
	dkms) sed -i 's/^PACKAGE_VERSION=.*/PACKAGE_VERSION="9.9.9"/' "$dir/nvfanread/dkms.conf" ;;
	postinst) sed -i 's/^version=.*/version=9.9.9/' "$dir/packaging/nvfanread/DEBIAN/postinst" ;;
	prerm) sed -i 's/^version=.*/version=9.9.9/' "$dir/packaging/nvfanread/DEBIAN/prerm" ;;
	build) sed -i 's/^ver=.*/ver=9.9.9/' "$dir/scripts/build-nvfanread-deb.sh" ;;
	module) sed -i 's/^MODULE_VERSION(.*/MODULE_VERSION("9.9.9");/' "$dir/nvfanread/nvfanread.c" ;;
	esac
	expect fail "mismatch in $case is caught" "$dir"
	rm -rf "$dir"
done

# A missing declaration must fail, not pass silently.
dir="$(fresh_copy)"
sed -i '/^MODULE_VERSION/d' "$dir/nvfanread/nvfanread.c"
expect fail "missing MODULE_VERSION is caught" "$dir"
rm -rf "$dir"

if [ "$fails" -eq 0 ]; then
	echo "test-check-versions: all passed"
	exit 0
fi
echo "test-check-versions: $fails failed" >&2
exit 1
