#!/bin/bash
# Live test for nvfancontrol and nvfanread with the nvfan-mailbox lock, on the
# DGX Spark itself. NOT run in CI: it loads kernel modules and drives the fans.
#
# WARNING: this test writes the EC fan override. The override is sticky: it
# survives module unload. The test writes "max" exactly once, then "auto".
# A trap writes "auto" again on every exit path. If that fails, the test
# leaves nvfancontrol loaded and prints the manual command.
#
# Run it detached, so a lost SSH session cannot kill it halfway:
#
#   sudo systemd-run --unit=nvfan-livetest --collect bash scripts/live-test-nvfancontrol.sh <worktree>
#   journalctl -fu nvfan-livetest
#
# Arguments:
#   $1  the worktree that holds signed/ (default: this script's repo)
# The module paths default to <worktree>/signed/<module>-<release>.ko, where
# <release> is `uname -r` without "-nvidia". Override with NVFANREAD_KO,
# NVFANCONTROL_KO and NVFANHOLD_KO. The nvfanhold lock-holder test runs only
# if its .ko exists.
#
# At the end the new nvfanread stays loaded and nvfancontrol is unloaded. The
# test does not run modprobe: that would load the packaged nvfanread instead.
set -u

worktree="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
release="$(uname -r)"
short="${release%-nvidia}"
read_ko="${NVFANREAD_KO:-$worktree/signed/nvfanread-$short.ko}"
ctl_ko="${NVFANCONTROL_KO:-$worktree/signed/nvfancontrol-$short.ko}"
hold_ko="${NVFANHOLD_KO:-$worktree/signed/nvfanhold-$short.ko}"

ffa_device='/sys/bus/arm_ffa/devices/arm-ffa-17'
expected_uuid='884a63a0-3285-4120-83aa-eec008a0a546'
fan='/sys/devices/faux/nvfancontrol/fan'

log() {
	printf '%s live-test: %s\n' "$(date +%H:%M:%S)" "$*"
}

die() {
	log "FAIL: $*"
	exit 1
}

module_loaded() {
	grep -q "^$1 " /proc/modules
}

lock_held() {
	grep -q 'nvfan-mailbox' /proc/iomem
}

dmesg_count() {
	dmesg | grep -cE "$1"
}

bad_pattern='RESTORE VERIFY FAILED|OUTPUT TIMEOUT|poisoned|not idle|TRANSPORT FAILURE|SERVICE REJECTED|RESPONSE MISMATCH'


# Write "auto" until the attribute reads "auto". Used by the trap and at the
# end of the test.
restore_auto() {
	local i
	[ -e "$fan" ] || return 0
	if module_loaded nvfanhold; then
		rmmod nvfanhold || log "rmmod nvfanhold failed"
	fi
	for i in 1 2 3 4 5; do
		if printf 'auto\n' > "$fan" 2>/dev/null &&
		   [ "$(cat "$fan")" = 'auto' ]; then
			log "auto restored (try $i)"
			return 0
		fi
		log "auto write try $i failed: fan reads '$(cat "$fan" 2>/dev/null)'"
		sleep 2
	done
	log "COULD NOT RESTORE AUTO. nvfancontrol stays loaded. Run by hand:"
	log "  echo auto | sudo tee $fan && cat $fan"
	return 1
}

on_exit() {
	local rc=$?
	trap - EXIT INT TERM HUP PIPE
	restore_auto || rc=1
	if [ "$rc" -eq 0 ]; then
		log "PASS"
	else
		log "exit status $rc"
	fi
	exit "$rc"
}

on_signal() {
	log "interrupted"
	exit 130
}

trap on_exit EXIT
trap on_signal INT TERM HUP PIPE

# Read fan1 RPM (hwmon fan2_input). Retries a busy mailbox.
read_rpm() {
	local i v
	for i in 1 2 3 4 5; do
		if v="$(cat "$hwmon/fan2_input" 2>/dev/null)"; then
			printf '%s\n' "$v"
			return 0
		fi
		sleep 0.5
	done
	return 1
}

# ---- Preflight ------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "run as root"
for ko in "$read_ko" "$ctl_ko"; do
	[ -r "$ko" ] || die "missing module: $ko"
	vm="$(modinfo -F vermagic "$ko")"
	case "$vm" in
	"$release "*) ;;
	*) die "$ko vermagic '$vm' does not match $release" ;;
	esac
	[ -n "$(modinfo -F signer "$ko")" ] || die "$ko is not signed"
done
[ "$(tr -d '\r\n' < "$ffa_device/uuid")" = "$expected_uuid" ] ||
	die "$ffa_device has an unexpected UUID"
module_loaded nvfancontrol && die "nvfancontrol is already loaded; unload it first"
module_loaded nvfanhold && die "nvfanhold is already loaded; unload it first"
lock_held && die "the mailbox lock is already held before the test"
want_src="$(modinfo -F srcversion "$read_ko")"
[ -n "$want_src" ] || die "no srcversion in $read_ko"
owner_before="$(readlink "$ffa_device/driver" 2>/dev/null || echo '(none)')"
log "kernel $release; arm-ffa-17 driver: $owner_before"
log "nvfanread:    $read_ko (srcversion $want_src)"
log "nvfancontrol: $ctl_ko"

max_before="$(dmesg_count 'starting MAX request')"
auto_before="$(dmesg_count 'starting AUTO request')"
bad_before="$(dmesg_count "$bad_pattern")"

# ---- 1. Swap in the new nvfanread -----------------------------------------
if module_loaded nvfanread; then
	rmmod nvfanread || die "rmmod nvfanread failed; stopping before nvfancontrol loads"
fi
insmod "$read_ko" || die "insmod $read_ko failed"
got_src="$(cat /sys/module/nvfanread/srcversion 2>/dev/null)"
[ "$got_src" = "$want_src" ] ||
	die "loaded nvfanread srcversion '$got_src' is not the new build '$want_src'"

hwmon=''
for h in /sys/class/hwmon/hwmon*; do
	if [ "$(cat "$h/name" 2>/dev/null)" = 'nvfanread' ]; then
		hwmon="$h"
		break
	fi
done
[ -n "$hwmon" ] || die "no nvfanread hwmon device"
base_rpm="$(read_rpm)" || die "cannot read fan RPM from $hwmon"
log "nvfanread loaded: $hwmon; baseline fan1 $base_rpm RPM"

# ---- 2. Load nvfancontrol -------------------------------------------------
insmod "$ctl_ko" || die "insmod $ctl_ko failed"
[ -e "$fan" ] || die "$fan is missing after insmod"
[ "$(cat "$fan")" = 'ready' ] || die "fan reads '$(cat "$fan")', expected ready"
owner_now="$(readlink "$ffa_device/driver" 2>/dev/null || echo '(none)')"
[ "$owner_now" = "$owner_before" ] ||
	die "arm-ffa-17 owner changed: '$owner_before' -> '$owner_now'"
log "nvfancontrol loaded; fan reads ready; arm-ffa-17 owner unchanged"

# ---- 3. Bad input sends nothing -------------------------------------------
if printf 'bogus\n' > "$fan" 2>/dev/null; then
	die "write of 'bogus' was accepted"
fi
[ "$(cat "$fan")" = 'ready' ] || die "fan state changed after a bad write"
log "bad input refused"

# ---- 4. Lock holder: both modules refuse with EAGAIN, nothing sent ---------
if [ -r "$hold_ko" ]; then
	sleep 0.3	# let nvfanread's 200 ms result cache expire
	insmod "$hold_ko" hold_ms=2000 || die "insmod nvfanhold failed"
	lock_held || die "nvfan-mailbox is not in /proc/iomem while nvfanhold holds it"
	if err="$(cat "$hwmon/fan2_input" 2>&1)"; then
		die "nvfanread read succeeded while the lock was held ($err)"
	fi
	case "$err" in
	*'Resource temporarily unavailable'*) log "nvfanread: EAGAIN while held" ;;
	*) die "nvfanread failed with '$err', expected EAGAIN" ;;
	esac
	sleep 2.5
	rmmod nvfanhold || die "rmmod nvfanhold failed"
	lock_held && die "lock still held after nvfanhold released it"
	read_rpm >/dev/null || die "nvfanread does not recover after the lock is free"

	# nvfancontrol waits 8 s for the lock; hold it for 10 s.
	auto_n="$(dmesg_count 'starting AUTO request')"
	insmod "$hold_ko" hold_ms=10000 || die "insmod nvfanhold failed"
	if printf 'auto\n' > "$fan" 2>/dev/null; then
		die "auto write succeeded while the lock was held"
	fi
	[ "$(cat "$fan")" = 'error -11' ] ||
		die "fan reads '$(cat "$fan")', expected 'error -11'"
	[ "$(dmesg_count 'starting AUTO request')" -eq "$auto_n" ] ||
		die "an AUTO request was sent while the lock was held"
	rmmod nvfanhold || die "rmmod nvfanhold failed"
	lock_held && die "lock still held after nvfanhold unloaded"
	log "nvfancontrol: error -11 while held, nothing sent"
else
	log "SKIP lock-holder test: $hold_ko not found"
fi

# ---- 5. Contention: reads in flight while writing auto ---------------------
(
	end=$((SECONDS + 4))
	while [ "$SECONDS" -lt "$end" ]; do
		cat "$hwmon/fan2_input" >/dev/null 2>&1
		sleep 0.05
	done
) &
reader=$!
for i in 1 2 3; do
	printf 'auto\n' > "$fan" || die "auto write $i failed under read load: $(cat "$fan")"
	[ "$(cat "$fan")" = 'auto' ] || die "fan reads '$(cat "$fan")' after auto write $i"
	sleep 0.5
done
wait "$reader"
bad_now="$(dmesg_count "$bad_pattern")"
[ "$bad_now" -eq "$bad_before" ] ||
	die "$((bad_now - bad_before)) new mailbox error lines in dmesg"
log "contention: 3 auto writes succeeded with reads in flight"

# ---- 6. max, exactly once --------------------------------------------------
printf 'max\n' > "$fan" || die "max write failed: $(cat "$fan")"
[ "$(cat "$fan")" = 'max' ] || die "fan reads '$(cat "$fan")' after max"
log "max acknowledged; waiting 20 s for the fans to rise"
sleep 20
max_rpm="$(read_rpm)" || die "cannot read RPM at max"
log "fan1 at max: $max_rpm RPM (baseline $base_rpm)"
[ "$max_rpm" -gt $((base_rpm + 1000)) ] ||
	die "fan1 did not rise at max ($base_rpm -> $max_rpm RPM)"

# ---- 7. Restore auto and watch the fans fall back --------------------------
restore_auto || die "could not restore auto"
settled=''
for i in $(seq 1 30); do
	sleep 10
	rpm="$(read_rpm)" || continue
	log "after auto, $((i * 10)) s: fan1 $rpm RPM"
	if [ "$rpm" -le $((base_rpm + base_rpm / 5 + 300)) ]; then
		settled="$rpm"
		break
	fi
done
[ -n "$settled" ] ||
	die "fan1 did not return near baseline $base_rpm RPM within 300 s (auto was acknowledged; nvfancontrol left loaded)"

# ---- 8. Counts, lock, unload -----------------------------------------------
max_sent=$(($(dmesg_count 'starting MAX request') - max_before))
auto_sent=$(($(dmesg_count 'starting AUTO request') - auto_before))
log "requests sent: MAX $max_sent, AUTO $auto_sent"
[ "$(dmesg_count "$bad_pattern")" -eq "$bad_before" ] ||
	die "new mailbox error lines in dmesg during the test"
[ "$max_sent" -eq 1 ] || die "expected exactly 1 MAX request, saw $max_sent"
lock_held && die "the mailbox lock is held at the end"
[ "$(cat "$fan")" = 'auto' ] || die "fan does not read auto before unload"
rmmod nvfancontrol || die "rmmod nvfancontrol failed"
[ -e "$fan" ] && die "$fan still exists after unload"
log "nvfancontrol unloaded; nvfanread $want_src stays loaded"
