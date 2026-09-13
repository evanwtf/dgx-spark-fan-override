# ec-probe — EC mailbox diagnostic (development tool)

A throwaway kernel module for poking the DGX Spark EC mailbox (OEM1 command 17
over FF-A partition `arm-ffa-17`) and dumping the raw response, to debug why a
command succeeds or times out. **Not part of the shipped driver.**

It binds `arm-ffa-17` and creates `…/arm-ffa-17/probe`; reading it issues one EC
request built from module parameters and prints the service status, elapsed time,
and the shared-page bytes — whether the request succeeds or fails.

## Parameters
| param | default | meaning |
|---|---|---|
| `ec_cmd` | 7 | EC inner command (1=caps, 2/4=override reads, 5=write high slot, 7=telemetry) |
| `in_len` | 3 | frame[0] input length |
| `out_len` | 67 | frame[1] output length |
| `out_off` | 0 | frame[2] EC output start offset |
| `d0`,`d1`,`d2` | 0 | request data bytes at 0x13/0x14/0x15 (for write commands) |
| `mapmode` | 0 | ns_shm0 mapping: 0=WB, 1=WT, 2=WC |
| `dump` | 96 | bytes of the data region to dump |

> ⚠️ `ec_cmd=5` (and 3) are **writes** to the EC override slots. The default is a
> read (7). Don't pass write commands unless you mean to.

## Usage
Needs Secure Boot signing (see `docs/secure-boot-signing.md`) — `run.sh`
build+signs with the MOK at `~/.mok/` and loads:
```sh
sh run.sh ec_cmd=1 in_len=3 out_len=13          # read capabilities
sh run.sh ec_cmd=7 in_len=3 out_len=67          # read telemetry
sh run.sh ec_cmd=1 mapmode=2                     # try write-combine mapping
```

## What it found (2026-09-13)
Every command (1/2/4/7) returned service `status=5` at ~54ms regardless of cache
mapping — the EC wasn't answering the doorbell (see issue #10). Confirmed the
mailbox code is correct vs the `mathieu-lacage` fork; the remaining variable was
EC state (latched after power-button OOM force-offs → needs a cold power drain).
