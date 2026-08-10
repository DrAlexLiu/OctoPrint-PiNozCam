# Native runtime release design

## Decision

PiNozCam uses a hybrid runtime distribution:

- generic CPU Wheels and the AArch64/x86-64 Vulkan Wheels use PyPI after
  passing a real manylinux audit;
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
| AArch64 Vulkan GPU with CPU fallback | `manylinux_2_35_aarch64` | PyPI |
| x86-64 Vulkan GPU with CPU fallback | `manylinux_2_35_x86_64` | PyPI |

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
rejected the Wheel as an invalid binary layout. The runtime packagers now use
platlib and both hosted CPU/GPU builds run `auditwheel show`. PyPI accepting an
upload remains insufficient without this gate.

## Installation contract

The plugin installer must select exactly one runtime from the detected Python
ABI, CPU architecture, and SoC.

For CPU and Vulkan, it emits an exact package-index requirement:

```text
pinozcam-runtime==1.1.0
pinozcam-runtime-gpu-aarch64==1.1.0
pinozcam-runtime-gpu-x86-64==1.1.0
```

While the plugin still pins the immutable `1.1.0rc5` runtime set, setup keeps
requesting its historical `pinozcam-runtime-jetson-orin` name and the loader
accepts that module after trying the new canonical name. The compatibility
entry must not be used for rc6 or stable artifacts.

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

Before the first hybrid release:

1. Keep exact PyPI requirements for CPU and both Vulkan architectures; use
   PEP 508 GitHub direct references only for Rockchip and A733.
2. Keep the AArch64 Vulkan distribution named
   `pinozcam-runtime-gpu-aarch64`; the backend remains Vulkan and Jetson Orin
   is the first qualified device family.
3. Keep native payloads in platlib and require `auditwheel show` as a release
   gate for every CPU and GPU Wheel.
4. Add selection and checksum regression tests for all nine Wheels.
5. Update the README and maintainer release checklist.
6. Perform clean tag-ZIP installs on every supported ABI/SoC.

## Build and qualification flow

The runtime workflows are manual (`workflow_dispatch`) and build from the
checked-out source revision:

1. GitHub-hosted x86-64 builds the x86 CPU runtime.
2. GitHub-hosted ARM64 builds the AArch64 CPU fallback used by the accelerator
   Wheels.
3. GitHub-hosted ARM64 builds the shared Rockchip runner and packages the three
   SoC-specific RKNN models.
4. GitHub-hosted ARM64 builds the AArch64 Vulkan runner; real Orin hardware
   performs the final GPU qualification.
5. GitHub-hosted x86-64 builds the x86 Vulkan runner; the same artifact is
   qualified with NVIDIA and AMD ICDs.
6. An ephemeral A733 self-hosted runner builds and tests the AWNN/VIPLite
   runtime because the vendor toolchain and NPU are board-specific.
7. Download the workflow artifacts independently, verify their SHA-256 values,
   then execute the production pipe protocol on the matching hardware.
8. Upload compliant CPU and Vulkan Wheels to PyPI. Attach only the verified
   Rockchip and cleared A733 Wheels plus their checksum manifest to the
   immutable GitHub Release.

The source-build definitions now cover all nine Wheels: x86-64 CPU/GPU and
ARMHF on GitHub x86 hosts, generic AArch64 CPU/GPU on GitHub ARM64, Rockchip on
a GitHub ARM64/cross-build pair, and A733 with its ephemeral self-hosted board.
The new ARMHF workflow must complete its first hosted run and its exact output
must pass the real-board qualification before this becomes release evidence.

The AArch64 Vulkan runner contains no Jetson-specific dependency. Jetson Orin
is the currently qualified platform. Grace Hopper/GH200 and other AArch64
Vulkan systems are candidates, but must not be listed as supported until their
installed Vulkan ICD exposes the required Vulkan features and the production
PTE completes a real inference. A missing or incompatible Vulkan backend must
fall back to the generic AArch64 CPU runtime.

The x86-64 Vulkan runner is likewise vendor-neutral and uses the same Vulkan
PTE on supported NVIDIA and AMD drivers. This portability does not add an AMD
code path to NVIDIA execution. Its performance tradeoff is instead that the
delegate cannot use TensorRT/CUDA-specific graph fusion, kernel selection, or
memory planning, so Jetson Vulkan can be slower than a TensorRT-only engine.

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
