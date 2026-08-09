# Native runtime release design

## Decision

PiNozCam uses a hybrid runtime distribution:

- generic CPU Wheels and the Jetson Orin Vulkan Wheel use PyPI after passing a
  real manylinux audit;
- Rockchip and A733 Wheels use immutable GitHub Release assets because their
  honest platform tag is `linux_aarch64` and they depend on board-provided
  vendor libraries.

A Wheel remains the install format in both cases. PyPI or GitHub Release is
only its download location.

## Release assets

One runtime release contains:

| Runtime | Wheel platform tag | Production channel |
| --- | --- | --- |
| ARM 32-bit CPU | `manylinux2014_armv7l` | PyPI |
| ARM 64-bit CPU | `manylinux2014_aarch64` | PyPI |
| x86-64 CPU | `manylinux2014_x86_64` | PyPI |
| RK3566 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| RK3576 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| RK3588 NPU with CPU fallback | `linux_aarch64` | GitHub Release |
| A733 NPU with CPU fallback | `linux_aarch64` | GitHub Release, after license clearance |
| Jetson Orin Vulkan with CPU fallback | `manylinux_2_35_aarch64` | PyPI |

The GitHub Release contains the three Rockchip Wheels, the cleared A733 Wheel,
and `CHECKSUMS.txt`. GitHub attestations may be attached when the release
workflow generates them, but documentation must not claim an attestation exists
until the workflow has produced and verified it.

Hardware-specific Wheels use `linux_aarch64` when they intentionally depend on
vendor libraries supplied by the board. Hosting a Wheel on GitHub does not make
an inaccurate manylinux tag acceptable. Conversely, a valid-looking manylinux
filename alone does not prove compliance; the final Wheel must pass
`auditwheel show` and its native payload must be installed under platlib.

The RC5 generic CPU Wheels were accepted by PyPI, but an independent
`auditwheel show` check found their ELF executable under `.data/purelib` and
rejected the Wheel as an invalid binary layout. The source-built Jetson Wheel
has the same layout. Move the runtime packages to platlib and require
`auditwheel show` to pass before the stable CPU or Jetson upload. PyPI accepting
an upload is not a substitute for this check.

## Installation contract

The plugin installer must select exactly one runtime from the detected Python
ABI, CPU architecture, and SoC.

For CPU and Jetson, it emits an exact package-index requirement:

```text
pinozcam-runtime==1.1.0
pinozcam-runtime-jetson-orin==1.1.0
```

For Rockchip and A733, it emits one exact PEP 508 direct reference:

```text
pinozcam-runtime-rknn3566 @ https://github.com/DrAlexLiu/OctoPrint-PiNozCam/releases/download/1.1.0/pinozcam_runtime_rknn3566-1.1.0-py3-none-linux_aarch64.whl#sha256=<digest>
```

The URL must contain a fixed release tag and exact filename. The SHA-256
fragment must match the local release manifest. Do not use `latest`, mutable
branch archives, a nested `pip install`, or a custom downloader from
`setup.py`. The outer pip process then owns download, caching, error reporting,
and digest verification.

`CHECKSUMS.txt` is the source of truth for the expected filename and digest.
Tests must cover every platform/SoC selection, the exact URL, the exact package
name, and the digest fragment.

## Current integration gap

At the time this decision was recorded, `setup.py` still emits ordinary pinned
package-index requirements for every target. Hybrid installation is therefore
the selected release design, not yet the end-to-end behavior of the plugin
installer.

Before the first GitHub-only release:

1. Keep exact PyPI requirements for CPU and Jetson; use PEP 508 GitHub direct
   references only for Rockchip and A733.
2. Fix the CPU and Jetson platlib layout and add `auditwheel show` as a release
   gate.
3. Add selection and checksum regression tests for all eight Wheels.
4. Update the README and maintainer release checklist.
5. Perform clean tag-ZIP installs on every supported ABI/SoC.

## Build and qualification flow

The runtime workflows are manual (`workflow_dispatch`) and build from the
checked-out source revision:

1. GitHub-hosted x86-64 builds the x86 CPU runtime.
2. GitHub-hosted ARM64 builds the AArch64 CPU fallback used by the accelerator
   Wheels.
3. GitHub-hosted ARM64 builds the shared Rockchip runner and packages the three
   SoC-specific RKNN models.
4. GitHub-hosted ARM64 builds the Jetson Orin Vulkan runner; real Orin hardware
   performs the final GPU qualification.
5. An ephemeral A733 self-hosted runner builds and tests the AWNN/VIPLite
   runtime because the vendor toolchain and NPU are board-specific.
6. Download the workflow artifacts independently, verify their SHA-256 values,
   then execute the production pipe protocol on the matching hardware.
7. Upload compliant CPU and Jetson Wheels to PyPI. Attach only the verified
   Rockchip and cleared A733 Wheels plus their checksum manifest to the
   immutable GitHub Release.

The current source-build workflows cover x86-64 and all accelerator Wheels.
A dedicated source-build workflow for the generic ARMHF and AArch64 CPU Wheels
is still required before claiming that all eight release files are rebuilt by
GitHub Actions.

The measured workflow runs and hardware qualification results are recorded in
[`runtime-ci.md`](runtime-ci.md).

## A733 source and redistribution gate

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

The public repository solves source availability and reproducibility, but it
does not by itself grant redistribution permission. At the reviewed commit the
repository has no root license, and its VIPLite header contains proprietary and
confidential-use language. Obtain an explicit license grant or written
permission before publicly attaching an A733 runner Wheel. Until then, A733 CI
may validate the private/self-hosted build, but the artifact is not a public
release asset.

The A733 Wheel must not bundle `libNBGlinker.so` or `libVIPhal.so`. The runner
links to the board-provided copies, just as the Rockchip runner uses the
board-provided `librknnrt.so`. `libVIPhal.so` is the user-space HAL that talks
to `/dev/vipcore`, so replacing it with a Wheel-bundled copy can create a
user-space/kernel-driver ABI mismatch. Release qualification must verify the
device node, load the system libraries, and execute a real inference; failure
falls back to the CPU runtime.
