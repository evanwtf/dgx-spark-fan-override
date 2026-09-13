# Project status & handoff — 2026-09-13

Snapshot of live working context so this can be picked up on another machine
(e.g. a Mac) while the DGX is offline for the MOK-enrollment reboot. The Claude
session that produced this runs *on the DGX* and dies on that reboot.

## Goal
Expose DGX Spark fan RPM in **node_exporter** (Prometheus `node_hwmon_fan_rpm`)
via a **read-only** hwmon driver that reads the EC's fan telemetry over FF-A.
The box's tachometers are EC-owned; nothing in the OS exposes fan RPM today
(`sensors` shows temps only). Tracking issue: **#7**.

## ✅ WORKING — fan RPM in `sensors` and node_exporter
The goal is achieved end to end: **EC → hwmon → `sensors` → node_exporter →
Prometheus.** Confirmed 2026-09-13:
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

### Remaining
- **#6 persistence** — auto-load at boot + DKMS MOK auto-signing so the module
  (and node_exporter's fan metrics) survive reboots. Today the module is loaded
  manually and must be rebuilt+signed after each reboot.
- **#4 polish** (optional) — friendlier `sensors` labels via `/etc/sensors.d/`.

Manual load after a reboot (until #6 lands):
```sh
cd ~/git/dgx-spark-fan-override/nvfanread && make
"/lib/modules/$(uname -r)/build/scripts/sign-file" sha256 \
  ~/.mok/nvfanread-mok.priv ~/.mok/nvfanread-mok.der nvfanread.ko
sudo insmod nvfanread.ko && sensors nvfanread-*
```

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
- **Read-only `nvfanread` module** (the *decoder*): binds `arm-ffa-17`, issues only
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
