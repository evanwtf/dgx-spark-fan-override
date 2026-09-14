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
the upstream [Z841973620/dgx-spark-fan-override](https://github.com/Z841973620/dgx-spark-fan-override);
the mailbox handshake here is derived from that project's `nvfancontrol.c`
(GPL-2.0), reduced to the read path only. The live RPM **decode offsets**
(command-7 reply bytes 7 and 9) come from the
[mathieu-lacage/dgx-spark-fan-override](https://github.com/mathieu-lacage/dgx-spark-fan-override)
fork, which validated them by reading real RPM on hardware. This module is
GPL-2.0. See the top-level `README` for full credits.

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

## Persistent install (DKMS `.deb`)

To auto-load at boot and rebuild+sign on kernel updates, install the DKMS
package. Under Secure Boot, first point DKMS's signing at a MOK you've enrolled
(see [`../docs/secure-boot-signing.md`](../docs/secure-boot-signing.md)):

```sh
# one-time: configure DKMS to sign with your enrolled MOK
sudo scripts/setup-signing.sh ~/.mok/nvfanread-mok.priv ~/.mok/nvfanread-mok.der

# build + install the package (needs dkms)
sudo apt-get install -y dkms
sh scripts/build-nvfanread-deb.sh
sudo dpkg -i dist/nvfanread_1.0.0_arm64.deb
```

The package registers the module with DKMS (built + signed on install and on
every kernel update), drops `/lib/modules-load.d/nvfanread.conf` for boot
auto-load, and installs `sensors` labels. Verify with `sensors nvfanread-*`. A
prebuilt `.deb` is also attached to GitHub Releases (and to each `package` CI
run's artifacts).

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
