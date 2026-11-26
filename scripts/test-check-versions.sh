#!/bin/sh
# Tests for scripts/check-versions.sh. It must pass on the real tree and fail
# when any one declaration differs from the placeholder or is missing.
set -u

here="$(cd "$(dirname "$0")" && pwd)"
repo="$here/.."
check="$here/check-versions.sh"
fails=0

files="packaging/nvfanread/DEBIAN/control nvfanread/dkms.conf \
	packaging/nvfanread/DEBIAN/postinst packaging/nvfanread/DEBIAN/prerm \
	scripts/build-nvfanread-deb.sh nvfanread/nvfanread.c \
	nvfancontrol/DEBIAN/control nvfancontrol/DEBIAN/postinst \
	nvfancontrol/DEBIAN/prerm nvfancontrol/DEBIAN/postrm \
	nvfancontrol/usr/src/nvfancontrol/dkms.conf scripts/build-debs.sh \
	nvfancontrol/usr/src/nvfancontrol/nvfancontrol.c"

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

expect pass "real tree has the placeholder everywhere" "$repo"

# One mutation per declaration: each must make the check fail.
for case in control dkms postinst prerm build module \
	    nfc-control nfc-postinst nfc-prerm nfc-postrm nfc-dkms nfc-build; do
	dir="$(fresh_copy)"
	case "$case" in
	control) sed -i 's/^Version: .*/Version: 9.9.9/' "$dir/packaging/nvfanread/DEBIAN/control" ;;
	dkms) sed -i 's/^PACKAGE_VERSION=.*/PACKAGE_VERSION="9.9.9"/' "$dir/nvfanread/dkms.conf" ;;
	postinst) sed -i 's/^version=.*/version=9.9.9/' "$dir/packaging/nvfanread/DEBIAN/postinst" ;;
	prerm) sed -i 's/^version=.*/version=9.9.9/' "$dir/packaging/nvfanread/DEBIAN/prerm" ;;
	build) sed -i 's/^ver=.*/ver="${1:-9.9.9}"/' "$dir/scripts/build-nvfanread-deb.sh" ;;
	module) sed -i 's/^MODULE_VERSION(.*/MODULE_VERSION("9.9.9");/' "$dir/nvfanread/nvfanread.c" ;;
	nfc-control) sed -i 's/^Version: .*/Version: 9.9.9/' "$dir/nvfancontrol/DEBIAN/control" ;;
	nfc-postinst) sed -i "s/^module_version=.*/module_version='9.9.9'/" "$dir/nvfancontrol/DEBIAN/postinst" ;;
	nfc-prerm) sed -i "s/^module_version=.*/module_version='9.9.9'/" "$dir/nvfancontrol/DEBIAN/prerm" ;;
	nfc-postrm) sed -i "s/^module_version=.*/module_version='9.9.9'/" "$dir/nvfancontrol/DEBIAN/postrm" ;;
	nfc-dkms) sed -i 's/^PACKAGE_VERSION=.*/PACKAGE_VERSION="9.9.9"/' "$dir/nvfancontrol/usr/src/nvfancontrol/dkms.conf" ;;
	nfc-build) sed -i 's/^ver=.*/ver="${1:-9.9.9}"/' "$dir/scripts/build-debs.sh" ;;
	esac
	expect fail "non-placeholder in $case is caught" "$dir"
	rm -rf "$dir"
done

# All declarations equal but not the placeholder (a hand bump of every file)
# must still fail: the tag is the only source of the released version.
dir="$(fresh_copy)"
for f in $files; do
	sed -i 's/0\.0\.0/1.2.3/g' "$dir/$f"
done
expect fail "a consistent hand bump is caught" "$dir"
rm -rf "$dir"

# A missing declaration must fail, not pass silently.
dir="$(fresh_copy)"
sed -i '/^MODULE_VERSION/d' "$dir/nvfanread/nvfanread.c"
expect fail "missing MODULE_VERSION is caught" "$dir"
rm -rf "$dir"

dir="$(fresh_copy)"
sed -i '/^module_version=/d' "$dir/nvfancontrol/DEBIAN/postrm"
expect fail "missing nvfancontrol postrm module_version is caught" "$dir"
rm -rf "$dir"

# A second MODULE_VERSION, in nvfanread.c or in another shipped .c file,
# must be checked too, not only the first one found.
dir="$(fresh_copy)"
printf 'MODULE_VERSION("9.9.9");\n' >> "$dir/nvfanread/nvfanread.c"
expect fail "a second MODULE_VERSION in nvfanread.c is caught" "$dir"
rm -rf "$dir"

dir="$(fresh_copy)"
printf 'MODULE_VERSION("9.9.9");\n' >> "$dir/nvfancontrol/usr/src/nvfancontrol/nvfancontrol.c"
expect fail "a MODULE_VERSION in nvfancontrol.c is caught" "$dir"
rm -rf "$dir"

dir="$(fresh_copy)"
printf 'MODULE_VERSION("9.9.9");\n' > "$dir/nvfanread/extra.c"
expect fail "a MODULE_VERSION in a new nvfanread .c file is caught" "$dir"
rm -rf "$dir"

if [ "$fails" -eq 0 ]; then
	echo "test-check-versions: all passed"
	exit 0
fi
echo "test-check-versions: $fails failed" >&2
exit 1
