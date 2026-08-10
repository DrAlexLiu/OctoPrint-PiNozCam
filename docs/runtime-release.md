# Native runtime release design

## Decision

Every PiNozCam runtime Wheel is attached to the matching immutable GitHub
Release. This gives the tag archive, all nine native payloads, and one checksum
manifest a single versioned release boundary.

The portable `pinozcam-runtime` and `pinozcam-runtime-gpu` distributions may also
be published to PyPI later. Rockchip and A733 Wheels remain GitHub Release
assets because their honest platform tag is `linux_aarch64` and they depend on
board-provided vendor libraries.

A Wheel remains the install format in both cases. PyPI or GitHub Release is
only its download location.

## Release assets

One runtime release contains:

| Runtime | Wheel platform tag | Production channel |
| --- | --- | --- |
| ARM 32-bit CPU | `manylinux2014_armv7l` | GitHub Release; PyPI later |
| ARM 64-bit CPU | `manylinux2014_aarch64` | GitHub Release; PyPI later |
| x86-64 CPU | `manylinux2014_x86_64` | GitHub Release; PyPI later |
| RK3566 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| RK3576 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| RK3588 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| A733 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| AArch64 Vulkan GPU with CPU fallback | `manylinux_2_35_aarch64` | GitHub Release; PyPI later |
| x86-64 Vulkan GPU with CPU fallback | `manylinux_2_35_x86_64` | GitHub Release; PyPI later |

The GitHub Release contains all nine Wheels and `CHECKSUMS.txt`. GitHub
attestations may be attached when the release workflow generates them, but
documentation must not claim an attestation exists until the workflow has
produced and verified it.

Hardware-specific Wheels use `linux_aarch64` when they intentionally depend on
vendor libraries supplied by the board. Hosting a Wheel on GitHub does not make
an inaccurate manylinux tag acceptable. Conversely, a valid-looking manylinux
filename alone does not prove compliance; the final Wheel must pass
`auditwheel show` and its native payload must be installed under platlib.

The RC5 generic CPU Wheels were accepted by PyPI, but an independent
`auditwheel show` check found their ELF executable under `.data/purelib` and
rejected the Wheel as an invalid binary layout. The runtime packagers now use
platlib and both hosted CPU/GPU builds run `auditwheel show`. PyPI accepting an
upload remains insufficient without this gate.

## Installation contract

The plugin installer must select exactly one runtime from the detected Python
ABI, CPU architecture, and SoC.

For RC7, every target emits an exact PEP 508 direct reference. The portable CPU
and GPU project names are shared across their platform Wheels:

```text
pinozcam-runtime @ https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases/download/1.1.0rc8/pinozcam_runtime-1.1.0rc8-py3-none-manylinux2014_aarch64.whl
pinozcam-runtime-gpu @ https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases/download/1.1.0rc8/pinozcam_runtime_gpu-1.1.0rc8-py3-none-manylinux_2_35_aarch64.whl
```

Hardware-specific targets use the same fixed-tag form:

```text
pinozcam-runtime-rknn3566 @ https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases/download/1.1.0rc8/pinozcam_runtime_rknn3566-1.1.0rc8-py3-none-linux_aarch64.whl
```

The URL must contain a fixed release tag and exact filename. Do not use
`latest`, mutable branch archives, a nested `pip install`, or a custom
downloader from `setup.py`. The outer pip process owns download, caching, and
error reporting.

`CHECKSUMS.txt` is generated from the nine verified Wheel bytes and is the
source of truth for release-asset verification. The RC7 setup dependency uses a
fixed tag and filename but does not embed the digest because the Wheel is built
from that same tag. The release workflow and post-download checks enforce the
digest set. Tests cover every platform/SoC selection, exact URL, package name,
and filename.

## Release requirements

Before publishing a candidate:

1. Keep all nine setup dependencies on the same fixed release tag as the
   plugin.
2. Keep all three CPU Wheels under `pinozcam-runtime` and both Vulkan Wheels
   under `pinozcam-runtime-gpu`; Wheel platform tags select the host ABI while
   the backend remains Vulkan.
3. Keep native payloads in platlib and require `auditwheel show` as a release
   gate for every CPU and GPU Wheel.
4. Run selection and checksum regression tests for all nine Wheels.
5. Perform clean tag-ZIP installs on every supported ABI/SoC.

## Build and qualification flow

The platform workflows are reusable and manually dispatchable. The release
workflow invokes all six from the tagged source revision:

1. GitHub-hosted x86-64 builds the x86 CPU runtime.
2. GitHub-hosted ARM64 builds the AArch64 CPU fallback used by the accelerator
   Wheels.
3. GitHub-hosted ARM64 builds the shared Rockchip runner and packages the three
   SoC-specific RKNN models.
4. GitHub-hosted ARM64 builds the AArch64 Vulkan runner; real Orin hardware
   performs the final GPU qualification.
5. GitHub-hosted x86-64 builds the x86 Vulkan runner; the same artifact is
   qualified with NVIDIA and AMD ICDs. Intel uses the same artifact but remains
   provisional until it completes the hardware matrix.
6. GitHub-hosted ARM64 fetches the pinned public A733 SDK inputs, builds the
   AWNN/VIPLite daemon in a manylinux 2.28 container, and performs static,
   dependency, packaging, and CPU-fallback protocol checks.
7. Download the workflow artifacts independently, verify their SHA-256 values,
   then execute the production pipe protocol on the matching hardware.
8. Assemble all nine Wheels and their checksum manifest into one immutable
   GitHub Release. Publishing the five portable CPU/GPU Wheels to PyPI is a
   later, separately approved delivery step.

The source-build definitions now cover all nine Wheels: x86-64 CPU/GPU and
ARMHF on GitHub x86 hosts, generic AArch64 CPU/GPU on GitHub ARM64, Rockchip on
a GitHub ARM64/cross-build pair, and A733 on GitHub ARM64 with a pinned
manylinux container.
The new ARMHF workflow must complete its first hosted run and its exact output
must pass the real-board qualification before this becomes release evidence.

The AArch64 Vulkan runner contains no Jetson-specific dependency. Jetson Orin
is the currently qualified platform. Grace Hopper/GH200 and other AArch64
Vulkan systems are candidates, but must not be listed as supported until their
installed Vulkan ICD exposes the required Vulkan features and the production
PTE completes a real inference. A missing or incompatible Vulkan backend must
fall back to the generic AArch64 CPU runtime.

The x86-64 Vulkan runner is likewise vendor-neutral and uses the same Vulkan
PTE on compatible NVIDIA, AMD and Intel drivers. NVIDIA and AMD are qualified;
Intel is provisional. This portability does not add AMD or Intel code paths to
NVIDIA execution. Its performance tradeoff is instead that the delegate cannot
use TensorRT/CUDA-specific graph fusion, kernel selection, or memory planning,
so Jetson Vulkan can be slower than a TensorRT-only engine.

The measured workflow runs and hardware qualification results are recorded in
[`runtime-ci.md`](runtime-ci.md).

## A733 source and system-runtime boundary

[Radxa's A733 documentation](https://docs.radxa.com/cubie/a7a/app-dev/npu-dev/cubie-vpm-run)
identifies `vpm_run` as a VIPLite application, points to
[`ZIFENG278/ai-sdk`](https://github.com/ZIFENG278/ai-sdk), and builds the A733
target with:

```bash
make AI_SDK_PLATFORM=a733
```

PiNozCam pins the reviewed SDK input to commit
`fc90006d0f6569da2f6726c2d8395877686f5aca`. A moving branch must never be used
as a release build input.

The A733 Wheel must not bundle `libNBGlinker.so` or `libVIPhal.so`. The runner
links to the board-provided copies, just as the Rockchip runner uses the
board-provided `librknnrt.so`. `libVIPhal.so` is the user-space HAL that talks
to `/dev/vipcore`, so replacing it with a Wheel-bundled copy can create a
user-space/kernel-driver ABI mismatch. The hosted release workflow does not
access a device node or execute an AWNN inference. On an installed board,
failure to load the system runtime or initialize the NPU falls back to the CPU
runtime.
