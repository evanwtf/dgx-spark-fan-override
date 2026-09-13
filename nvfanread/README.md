# nvfanread — read-only DGX Spark fan telemetry

A small kernel module that **reads** DGX Spark fan telemetry from the EC and
never changes fan behavior. It is a clean-room, read-only companion to the
override driver in this repo: it binds the same FF-A partition (`arm-ffa-17`)
but issues **only EC inner command 7** — the documented thermal/fan telemetry
snapshot — and never touches an override slot (EC commands 3/5).

It surfaces real fan RPM through a **hwmon** device (`fan1_input` / `fan2_input`),
so `sensors` and node_exporter's hwmon collector pick it up automatically. A
`telemetry` debug attribute also dumps the raw 64-byte snapshot (hex + decoded
RPM + candidates). A short snapshot cache means reading both fans is one EC
transaction.

## Provenance

The EC/FF-A protocol (partition UUID, shared-page address, OEM1 command 17,
the EC inner-command table, and the fan RPM ranges) was reverse-engineered by
the upstream project this repo is forked from; see the top-level `README`. The
mailbox handshake here is derived from that project's `nvfancontrol.c`
(GPL-2.0), reduced to the read path only. This module is GPL-2.0.

## Safety

- **Read-only by construction.** The request builder can emit only command 7;
  there is no code path to an override write. This is asserted by a unit test.
- Refuses a non-idle mailbox, snapshots the shared page, and restores it around
  every request. Requests are serialized under a mutex.
- Loading the module issues **no** EC request; a request happens only when the
  `telemetry` sysfs attribute is read.

## Layout

| File | Purpose |
|---|---|
| `nvfanread.c` | Kernel module: FF-A / shared-page I/O and the `telemetry` sysfs attribute |
| `nvfanread_proto.h` | Pure protocol logic (frame build, validation, RPM classification), shared with the tests |
| `tests/test_proto.c` | Userspace unit tests for the pure logic |
| `Makefile` | `all`, `test`, `lint`, `check`, `clean` |

## Build and test

```sh
make            # build the kernel module (needs matching kernel headers)
make test       # run userspace unit tests (no kernel/hardware needed)
make lint       # repo-wide lint: checkpatch (C) + shellcheck (shell)
make check      # build the module AND run the unit tests
```

`make test` needs only a C compiler — it exercises the protocol logic without a
kernel or hardware, and is what CI runs. The FF-A / shared-page I/O in
`nvfanread.c` cannot be unit-tested without loading the module; it is validated
at load time on real hardware.

## Usage (once loaded)

```sh
sudo insmod nvfanread.ko
sensors                                             # fan RPM via hwmon
cat /sys/class/hwmon/hwmon*/fan1_input              # fan0 RPM (direct)
cat /sys/class/hwmon/hwmon*/fan2_input              # fan1 RPM (direct)
cat /sys/bus/arm_ffa/devices/arm-ffa-17/telemetry   # raw snapshot + decoded RPM (debug)
sudo rmmod nvfanread
```

The module registers a hwmon device named `nvfanread` exposing `fan1_input`
(fan0) and `fan2_input` (fan1) in RPM. The `telemetry` debug attribute dumps the
64-byte snapshot as hex, the decoded RPM, and any little-endian u16 in the fan
RPM ranges (fan0 1260–9000, fan1 1890–13500) as candidates.
