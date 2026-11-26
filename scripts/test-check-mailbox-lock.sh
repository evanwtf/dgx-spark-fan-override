#!/bin/sh
# Tests for scripts/check-mailbox-lock.sh. It must pass on the real tree and
# fail when either module locks a different page, uses a different lock name
# or bound, or stops taking the lock.
set -u

here="$(cd "$(dirname "$0")" && pwd)"
repo="$here/.."
check="$here/check-mailbox-lock.sh"
fails=0
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

files="nvfanread/nvfanread.c nvfanread/nvfanread_proto.h \
	nvfancontrol/usr/src/nvfancontrol/nvfancontrol.c"

fresh_copy() {
	dir="$(mktemp -d -p "$tmp")"
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

# $1 = description, $2 = file, $3 = sed expression
mutate() {
	dir="$(fresh_copy)"
	sed -i "$3" "$dir/$2"
	if cmp -s "$repo/$2" "$dir/$2"; then
		echo "NOT OK - $1 (the mutation changed nothing)"
		fails=$((fails + 1))
		return
	fi
	expect fail "$1 is caught" "$dir"
}

ctl=nvfancontrol/usr/src/nvfancontrol/nvfancontrol.c
rd=nvfanread/nvfanread.c
proto=nvfanread/nvfanread_proto.h

expect pass "real tree is consistent" "$repo"

mutate "address mismatch in nvfancontrol" "$ctl" \
	's/^#define ESPI_NS_SHM_PA .*/#define ESPI_NS_SHM_PA 0x933de000ULL/'
mutate "address mismatch in nvfanread" "$rd" \
	's/^#define ESPI_NS_SHM_PA .*/#define ESPI_NS_SHM_PA 0x933de000ULL/'
mutate "size mismatch" "$ctl" \
	's/^#define ESPI_NS_SHM_SIZE .*/#define ESPI_NS_SHM_SIZE 0x2000U/'
mutate "lock name mismatch" "$ctl" \
	's/^#define MAILBOX_LOCK_NAME .*/#define MAILBOX_LOCK_NAME "nvfancontrol"/'
mutate "retry budget mismatch" "$proto" \
	's/^#define NVFR_LOCK_RETRY_BUDGET_US .*/#define NVFR_LOCK_RETRY_BUDGET_US 1LL/'
mutate "attempt cap mismatch" "$ctl" \
	's/^#define LOCK_MAX_ATTEMPTS .*/#define LOCK_MAX_ATTEMPTS 5U/'
mutate "missing lock name define" "$proto" \
	'/^#define NVFR_MAILBOX_LOCK_NAME/d'
mutate "nvfancontrol stops taking the lock" "$ctl" \
	's/request_mem_region(/request_mem_region_x(/'
mutate "nvfanread stops releasing the lock" "$rd" \
	's/release_mem_region(/release_mem_region_x(/'

if [ "$fails" -eq 0 ]; then
	echo "test-check-mailbox-lock: all passed"
	exit 0
fi
echo "test-check-mailbox-lock: $fails failed" >&2
exit 1
