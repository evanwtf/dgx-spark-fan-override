# Project status & handoff — 2026-09-13

Snapshot of live working context so this can be picked up on another machine
(e.g. a Mac) while the DGX is offline for the MOK-enrollment reboot. The Claude
session that produced this runs *on the DGX* and dies on that reboot.

## Goal
Expose DGX Spark fan RPM in **node_exporter** (Prometheus `node_hwmon_fan_rpm`)
via a **read-only** hwmon driver that reads the EC's fan telemetry over FF-A.
The box's tachometers are EC-owned; nothing in the OS exposes fan RPM today
(`sensors` shows temps only). Tracking issue: **#7**.

## Kernel 7.0 (2026-11-25, #15)
On `7.0.0-1025-nvidia` the built-in `nvidia-ffa-ec` driver binds `arm-ffa-17`
(Ubuntu commit `9c0c984e122c` added UUID `884a63a0-…`). It sets
`suppress_bind_attrs`, and the FF-A bus has no `driver_override`, so nvfanread
1.0.0 loaded but never bound, and fan RPM went away. `7.0.0-1019` and
`6.17.0-1032` did not list that UUID, so nvfanread bound there.

nvfanread no longer needs to bind. It finds the device on the FF-A bus by UUID
and sends through it; `nvidia-ffa-ec` keeps the binding. Where the device is
free (6.17, 7.0.0-1019) a small claim driver still binds it, so drivers that
bind and write the page without the mailbox lock (the `nvfancontrol` build that binds,
`ec-probe`) cannot attach. Each read also refuses with `-EAGAIN` if one of
them owns the device. Against other locking modules, the mailbox lock below
does the work.

The `sensors` name stays `nvfanread-virtual-0`. The node_exporter `chip` label
changes with the kernel, because 7.0.0-1025 moved FF-A devices under
`/sys/devices/platform/arm-ffa/`: `devices_arm_ffa_17` on 6.17 and 7.0.0-1019,
`arm_ffa_arm_ffa_17` on 7.0.0-1025+. Query by chip name to cover both:
`node_hwmon_fan_rpm * on(chip) group_left node_hwmon_chip_names{chip_name="nvfanread"}`.

The `telemetry` attribute moved to the hwmon device. The FF-A partition ID is
`0x8003` (shared by all EC services); `17` is only the bus index.

`nvfancontrol` (#16) no longer binds either. It finds the device the same way
and moves its `fan` attribute to `/sys/devices/faux/nvfancontrol/fan`. Because
neither module binds on 7.0.0-1025+, both now take a mailbox lock for each
request: `request_mem_region(0x933dd000, 0x1000, "nvfan-mailbox")`. The page
sits in a top-level non-busy `reserved` iomem region, so the request nests
under it, and a second request fails until the first is released. Install both
modules from the same release: builds without the lock (`nvfanread` 1.0.0 and
the #17 build; the `nvfancontrol` build that binds) can write the page at the same time.
`nvfancontrol` refuses to send while an `nvfanread` without the lock is loaded.
`-EAGAIN` means nothing was sent; an output timeout poisons the mailbox until
the module is unloaded. Live test: `scripts/live-test-nvfancontrol.sh`.

## ✅ WORKING — fan RPM in `sensors` and node_exporter
The goal is achieved end to end: **EC → hwmon → `sensors` → node_exporter →
Prometheus.** Confirmed 2026-09-13 on 6.17 (on 7.0.0-1025+ the `chip` label is
`arm_ffa_arm_ffa_17`; see above):
```
nvfanread-virtual-0:  fan0: 2700 RPM   fan1: 4050 RPM
node_hwmon_fan_rpm{chip="devices_arm_ffa_17",sensor="fan1"} 2700
node_hwmon_fan_rpm{chip="devices_arm_ffa_17",sensor="fan2"} 4050
```
Delivered in **PR #11** (merged to `main`): `nvfr_snapshot_fan_rpm()` decode + a
hwmon device (`fan1_input`/`fan2_input`); node_exporter's `--collector.hwmon`
picks it up automatically.

### The two blockers, both solved
1. **Secure Boot** — the module must be MOK-signed and the key enrolled (one-time,
   needs a physical monitor; no BMC). Done. Runbook: `docs/secure-boot-signing.md`.
2. **EC mailbox timed out** (status 5, every command) — the EC was **latched**
   after power-button OOM force-offs. A warm reboot does not reset it; a **cold
   AC power drain** (`poweroff` + unplug ~5 min) did. See #10. Our code was correct
   all along — byte-identical to the mathieu-lacage fork.

### Done — persistent install
- **#6** — packaged as a DKMS `.deb` (`packaging/nvfanread/` +
  `scripts/build-nvfanread-deb.sh`): builds + signs (with the enrolled MOK, via a
  DKMS framework drop-in written by `scripts/setup-signing.sh`) + installs on the
  target, auto-loads at boot (`/lib/modules-load.d/nvfanread.conf`), and rebuilds
  on kernel updates (`AUTOINSTALL=yes`). Installed and verified on the box.
- **#4** — friendlier `sensors` labels ("Fan 0" / "Fan 1") ship in the package.
- Downloadable `.deb` — built + uploaded by the `package` CI job on every
  push/PR; attached to GitHub Releases on tag.

Install (one-time signing setup, then the package):
```sh
sudo scripts/setup-signing.sh ~/.mok/nvfanread-mok.priv ~/.mok/nvfanread-mok.der
sudo apt-get install -y dkms
sh scripts/build-nvfanread-deb.sh
sudo dpkg -i dist/nvfanread_1.1.0_arm64.deb
```

### After a reboot — verify (all should just work, no manual steps)
```sh
lsmod | grep nvfanread                                    # auto-loaded via modules-load.d
sensors nvfanread-*                                       # Fan 0 / Fan 1 RPM
curl -s localhost:9100/metrics | grep node_hwmon_fan_rpm  # in node_exporter/Prometheus
dkms status nvfanread                                     # installed (rebuilds+re-signs on kernel updates)
```
Already deployed on this box (DKMS-installed, MOK-signed, auto-load) as of 2026-09-13.

### Credits / external resources
- Upstream protocol RE: [Z841973620/dgx-spark-fan-override](https://github.com/Z841973620/dgx-spark-fan-override).
- Validated RPM decode offsets: [mathieu-lacage/dgx-spark-fan-override](https://github.com/mathieu-lacage/dgx-spark-fan-override).
- Cold-EC-reset (power-drain) technique: vendor docs
  [ASUS](https://www.asus.com/support/faq/1050239/) and
  [MSI](https://www.msi.com/support/technical_details/NB_EC_RESET).

**Decode (already solved, from the fork):** in the command-7 reply,
`fan0_rpm = le16(reply[7])`, `fan1_rpm = le16(reply[9])` → in nvfanread's 64-byte
snapshot that's bytes `[4..5]` and `[6..7]`. Reference:
`mathieu-lacage/dgx-spark-fan-override` (GPL, same upstream) implements
`fan_caps`/`fan_telemetry`/`fan_rpm` read attributes — a good model for #3/#4.

Diagnostic harness used to reach this: **`tools/ec-probe/`** (parametrized probe;
sweep EC command / lengths / cache mode; build+sign+load via `run.sh`).

## What's done
- **Read-only `nvfanread` module** (the *decoder*): uses `arm-ffa-17` (bound it
  before 1.1.0), issues only
  EC **command 7** (telemetry, read-only — never an override write), exposes a
  `telemetry` sysfs attr that dumps the 64-byte snapshot + flags u16s in the fan
  RPM ranges. Built, CI-green. Pure protocol logic in `nvfanread_proto.h`.
- **Tests + CI + lint + release + docs** — all merged to `main` (PR #8):
  - unit tests (166 checks) on every push/PR; shellcheck lint job.
  - `scripts/lint.sh` = checkpatch (C, local) + shellcheck (shell, CI).
  - tag-triggered release workflow builds the DKMS `.deb` with a version gate
    (fixed a real bug: maintainer scripts were non-executable).
  - README rewritten in English (dropped the zh/en split).
- **MOK generated, signed, and ENROLLED** (Secure Boot solved; module loads and
  binds — dmesg "read-only telemetry ready"): key at `~/.mok/` on the DGX (`nvfanread-mok.priv` 600,
  `nvfanread-mok.der`), module signed (`signer: nvfanread module signing (evan MOK)`,
  sha256). Backed up in **1Password → Code Secrets → "nvfanread MOK — DGX Spark
  module signing"** (item `ogbrdqzcbfqsypinogximv3hmq`). Fingerprint
  `4F:9E:7D:1D:9A:77:56:56:9E:CE:05:DA:EC:7E:3E:76:BD:B1:D8:16`. **Never commit
  the private key** (`.gitignore` blocks key material).

- **EC mailbox diagnosed (#10):** every OEM1-cmd-17 command times out (status 5,
  ~54ms); confirmed our code is correct vs the working `mathieu-lacage` fork; the
  RPM decode is known (reply offsets 7 and 9). Root cause = EC latched → needs the
  cold reset above. Diagnostic harness preserved in `tools/ec-probe/`.

## Task chain (issues)
`#1 decode RPM offsets` → `#2 extract fn + tests` → `#3 hwmon driver (fanN_input)`
→ `#4 sensors + labels` → **`#5 node_exporter (GOAL)`**; plus `#6 package + DKMS
auto-sign`. `#9` = the Secure Boot signing blocker (gates #1). Nearly everything
is gated on **#1**, which needs the module *loaded* (= the enrollment above).

## Verified environment
- DGX Spark (GB10), aarch64; DGX OS / Ubuntu; kernel `6.17.0-1032-nvidia`.
- Secure Boot **enabled**; `sig_enforce=Y`; kernel `lockdown=integrity`.
- **No BMC/IPMI** (no remote KVM). Ports: **HDMI + 3× USB-C** (no USB-A).
  Enrollment therefore needs a physical monitor+keyboard — no headless path.
- nvidia modules load because Canonical-signed (not reusable). No user MOK
  enrolled; `/var/lib/shim-signed/mok/` empty. shim + `mmaa64.efi` present.
  `OsIndicationsSupported` bit0 set (`systemctl reboot --firmware-setup` works).
- FF-A partition `arm-ffa-17`, UUID `884a63a0-3285-4120-83aa-eec008a0a546`.
- EC path: OEM1 command 17 → EC inner command 7 → copies 64 bytes from
  `0x1188E2`; shared page `0x933dd000`. Fan ranges: fan0 1260–9000, fan1
  1890–13500 RPM. The **exact RPM byte offsets in the 64-byte snapshot are still
  unknown** — that's what #1 decodes once we can load + read.

## Repo / infra
- Repo: **`evanwtf/dgx-spark-fan-override`** (transferred from `evandhoffman`;
  local remote updated). Public.
- `main` is **protected**: PRs required, `lint` + `unit-tests` checks must pass;
  `enforce_admins=false` (admins can still push directly), `strict=false`.
- CI runs on GitHub-hosted `ubuntu-latest` (free for a public repo). The org has
  a self-hosted `[self-hosted, Linux, X64]` runner but it lacks a C toolchain,
  so we stayed on ubuntu-latest.

## Coordination / conventions
- Peer Claude session **"dgx spark apt repos"** (also on the DGX) halted its #354
  benchmark cleanly at 16/30 to give this reboot window; GPU is free. It will
  re-coordinate before relaunching so nothing collides with the kernel load.
  Both on-DGX sessions die on the reboot.
- **Never** add `Claude-Session:` or `Co-Authored-By: Claude` trailers to commits
  or PRs (standing user rule).

## Continue on the Mac
```sh
git clone git@github.com:evanwtf/dgx-spark-fan-override.git && cd dgx-spark-fan-override
# read: docs/STATUS.md (this file), docs/secure-boot-signing.md, issues #9/#1/#7
# then drive the enrollment before/after over `ssh dgx`
```
The MokManager step itself is manual at the monitor (no SSH/assistant possible
there by design). Everything before (stage key) and after (verify, insmod, read,
iterate) is doable over `ssh dgx` from the Mac.
