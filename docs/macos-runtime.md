# macOS arm64 runtime CI

## Hosted build boundary

`build-macos-runtime.yml` runs entirely on GitHub's `macos-14` runners,
which are Apple silicon. Nothing about this target can be cross-produced
from the Linux runners: a Mach-O executable is a different container from
an ELF one, macOS ships no static libSystem, and Apple's linker spells the
same options differently. That is why this target has its own
`bootstrap_macos_arm64.sh` and `build_macos.sh` rather than a branch inside
the shared scripts, whose output must keep reproducing byte for byte for
armhf and aarch64.

The model is not rebuilt here. The workflow downloads the pinned
`nozcam-cpu.pte` published with 1.1.0rc5 and checks its SHA-256
(`1057e47d…`) before use, so every platform runs the identical file and no
result difference can be blamed on a different model.

## Runner version is a deliberate choice, not a default

`macos-14` sets `minos 14.0` in the produced binary and
`macosx_14_0_arm64` in the Wheel tag. macOS is backward compatible with an
older deployment target, so a Wheel built on 14 installs and runs on
everything newer. Building on the newest available runner would raise that
floor to the runner's own version and lock out every older Mac — the same
trade the Linux daemons avoid by linking statically rather than inheriting
the build host's glibc.

## Verified on hardware, not inferred

Measured on an M4 Mac mini running **macOS 26.5**, using the exact Wheel
published for 1.1.0rc11 — downloaded from the GitHub Release and confirmed
by SHA-256 (`9ac604e0…`) to be the file the runner produced, not a local
rebuild:

| check | result |
|---|---|
| binary | Mach-O arm64, `minos 14.0` |
| dynamic libraries | `libSystem.B.dylib`, `libc++.1.dylib`, nothing else |
| `smoke_test.py` | PASS |
| runner contract suite | 17/17 PASS |
| 34 calibration frames | **36.6 ms median, 1641 img/min** |

A binary built on macOS 14 therefore runs on macOS 26. That was an open
question until this run: every earlier macOS measurement had used a
locally compiled daemon.

### The CI build and a local build are bit-identical

The same 34 frames through the CI Wheel and through a daemon compiled on
the Mac itself, under a different macOS release and a different Xcode:

```
box counts      34/34
severity        34/34
total_area      34/34
max |Δscore|    0
max |Δbox|      0
```

This follows from the model being entirely int8: integer multiply-
accumulate has no floating-point associativity, so retiling or reordering
the accumulation cannot change the result. The same property is already on
record for XNNPACK across A72, A76 and x86, and for Ascend across two CANN
compiler versions.

⚠️ It holds for **all-int8 only**. A build carrying fp32 islands, or an
fp16 model, will diverge between toolchains — that is exactly what happens
on Linux between the 32-bit and 64-bit ABIs.

## Speed in context

Same `.pte`, same preprocessing, measured end to end through the daemon:

| host | backend | median | img/min |
|---|---|---:|---:|
| Mac mini M4 | XNNPACK CPU | 36.6 ms | 1641 |
| Orange Pi AI Pro | Ascend NPU | 33.4 ms | 1795 |
| Orange Pi AI Pro | XNNPACK CPU (3× A55) | 487.4 ms | 123 |

An M4's plain CPU lands within touching distance of a dedicated edge NPU,
and is 13× the same-generation ARM CPU beside it.

## Three Linux assumptions this target had to separate out

Each was a real build failure, in the order they appeared.

**No CPU pinning.** Darwin has no `sched_setaffinity` and no `cpu_set_t`;
its scheduler does not expose pinning at all. `nozcam_daemon.cpp` guards
both helpers with `__linux__`, and on macOS `ApplyAffinity` returns
`ENOTSUP` while `CurrentCpuList` returns empty. The runner contract already
treats affinity as a scheduling hint whose failure warns rather than fails,
so this satisfies it. The Linux bodies are untouched inside the guard.

**GNU-only link options.** `-static` (no static libSystem), `-z
noexecstack` (Mach-O has no `PT_GNU_STACK` to mark) and `--whole-archive`
(Apple spells it per-archive, `-force_load`). `build_macos.sh` scrapes the
same flags out of `executor_runner`'s `flags.make` and `link.txt` as the
shared script does, and substitutes only these.

**torchgen must exist before cmake configures.** `Codegen.cmake` probes for
it with `execute_process` and bakes the resulting path into the build.
Installing it afterwards leaves an empty root and the build fails much
later with a missing `/packaged/ATen/native/native_functions.yaml` —
pointing at a path that never existed rather than at the ordering.

## The failure a local build cannot find

The first tagged attempt failed only on this target. macOS runners ship
Homebrew Python, which is PEP 668 externally-managed and refuses
`pip install` into itself:

```
error: externally-managed-environment
```

The step was copied from `build-x86-runtime.yml`, where it works, because
Ubuntu runners have no such guard. It cannot reproduce on a developer Mac
either, where `python3` is usually pyenv or a virtualenv. Packaging and the
reinstall check now run from a virtualenv created in the step;
`smoke_test.py` is untouched because it imports only the standard library.

## Adding a target touches more than the build

Adding this one raised the Wheel count from nine to ten, and that number
was hardcoded in three places that each fail differently:

- `assemble_release.py` required exactly nine and now reports
  `len(TARGETS)`.
- The publish workflows' allow-list matched `manylinux`/`linux` tags only
  and rejected `macosx_14_0_arm64` as an "unsupported public Linux Wheel
  tag"; the CPU upload also expected exactly three Wheels.
- ⚠️ `build-runtime-release.yml`'s `bundle` job lists its dependencies and
  downloads explicitly. Omitting the new job there does **not** fail: it
  assembles the nine Wheels it did fetch, passes the contract check and the
  draft gate's file count, and publishes a release silently missing the
  target.
