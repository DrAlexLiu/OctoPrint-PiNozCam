# Native runtime CI

PiNozCam keeps native executables and models out of the plugin source archive.
The workflows below rebuild the executables from the checked-out source,
combine them with SHA-256-pinned model inputs, validate the resulting Wheel,
and retain it as a GitHub Actions artifact for 14 days.

| Workflow | Build host | Output |
| --- | --- | --- |
| `build-x86-runtime.yml` | GitHub x86-64 | x86-64 CPU Wheel |
| `build-gpu-x86-runtime.yml` | GitHub Ubuntu 22.04 x86-64 | x86-64 Vulkan GPU Wheel |
| `build-armhf-runtime.yml` | GitHub x86-64 cross-build plus QEMU | ARMHF CPU Wheel |
| `build-rockchip-runtime.yml` | GitHub ARM64 plus a pinned Ubuntu 20.04 cross-build container | RK3566, RK3576, and RK3588 Wheels |
| `build-gpu-aarch64-runtime.yml` | GitHub Ubuntu 22.04 ARM64 | Generic AArch64 CPU and AArch64 Vulkan GPU Wheels |
| `build-a733-runtime.yml` | GitHub ARM64 plus an ephemeral A733 self-hosted runner | A733 AWNN Wheel |

The generic AArch64 CPU Wheel and GPU fallback are built from the same pinned
ExecuTorch source on Ubuntu 22.04 ARM64. Rockchip uses one shared daemon
executable and three different
RKNN model files. Jetson builds both the CPU fallback and Vulkan executable;
Mesa lavapipe provides a software Vulkan protocol smoke test in CI. A733 uses
the pinned public `ZIFENG278/ai-sdk` input, but needs a real board for the final
AWNN/VIPLite inference test. Its build boundary is documented in
[`a733-ci.md`](a733-ci.md).

## Platform tags

The generic CPU Wheels are self-contained. The AArch64 and x86-64 GPU
executables use only the standard dynamic libraries permitted by their
`manylinux_2_35` baselines and load the machine's Vulkan implementation at
runtime. Both platform Wheels belong to the one `pinozcam-runner-gpu`
distribution and carry the same `nozcam-gpu.pte`; only the runner and CPU
fallback executable differ by architecture. The x86 runner uses the active
NVIDIA, AMD or Intel ICD. NVIDIA and AMD are hardware-qualified; Intel support
is provisional until the same artifact completes the real-hardware matrix.

Rockchip and A733 executables intentionally depend on board-provided vendor
libraries. Their CI Wheels therefore use the honest `linux_aarch64` tag rather
than claiming manylinux compatibility. PyPI does not accept this tag; durable
Rockchip and A733 binaries belong on the matching GitHub Release. Generic CPU
and Vulkan Wheels must still pass `auditwheel show`. All nine artifacts enter
the GitHub Release; the five portable CPU/GPU Wheels may enter PyPI later.

## What hosted CI proves

The workflows reject wrong ELF architectures, executable stacks, unexpected
dynamic dependencies, unsupported ABI floors, altered source/model inputs,
and incorrect Wheel contents. The CPU and software Vulkan jobs also execute a
real PTE inference through the production pipe protocol.

Hosted runners do not contain Rockchip or NVIDIA hardware. Before a release,
download the Actions artifact and run the same Wheel on RK3566, RK3576,
RK3588, and Jetson Orin hardware. The A733 workflow performs its AWNN inference
on the self-hosted board as part of the workflow itself.

## Validation record

The source-build workflows were exercised on 2026-08-09. These were QA
artifacts with development version numbers, not release assets.

| Target | Successful Actions run | Hardware qualification |
| --- | --- | --- |
| x86-64 CPU | [31337036007](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31337036007) | Installed artifact completed PING/INFO/INFER/SHUTDOWN with the production PTE on x86-64 |
| x86-64 Vulkan GPU | Pending first run of `build-gpu-x86-runtime.yml` | The production daemon and shared PTE completed the 34-image set on an RTX 4090 and Radeon R9700: box counts 34/34 identical, maximum score difference `1.1920929e-7`, box/severity/area differences zero. The exact Actions artifact still needs download-and-run qualification |
| ARMHF CPU | Pending first run of `build-armhf-runtime.yml` | Previously qualified local runner; the new source-built Actions artifact still needs exact-byte board qualification |
| A733 AWNN | [31339370137](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31339370137) | The ephemeral self-hosted A733 job built the runner and completed real AWNN inference |
| RK3566/RK3576/RK3588 | [31340003668](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31340003668) | The exact three downloaded Wheels completed real NPU inference on one board of each SoC family |
| Jetson Orin Vulkan | [31340384861](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31340384861) | The exact downloaded Wheel completed Vulkan inference on an NVIDIA Tegra Orin after the hosted lavapipe check |

The hardware smoke request uses a deterministic synthetic frame and validates
backend loading, device execution, the binary protocol, finite output, and
clean shutdown. It is a liveness and integration gate, not a substitute for
the separate image-set accuracy and CPU/accelerator parity qualification.

The initial local x86-64 image-set run measured median daemon inference times
of 10.035 ms on the RTX 4090 and 18.997 ms on the Radeon R9700. That Ubuntu 24
build has a GLIBC 2.38 floor and is deliberately rejected by the
`manylinux_2_35` gate.

The same source-build script was then run in Ubuntu 22.04. Its stripped Vulkan
daemon has SHA-256
`339eafa8c8429426a27bd9458258a69028933f81f285af8496971d10ddefce67`,
uses at most GLIBC 2.34 / GLIBCXX 3.4.29, and produced a Wheel that
`auditwheel show` classified as `manylinux_2_34_x86_64`. The installed Wheel
completed PING/INFO/INFER/SHUTDOWN on both GPUs. Across the 34-image set, box
counts, labels and alarm decisions matched 34/34; box, severity and area
differences were zero, and the maximum score difference was `1.1920929e-7`.
Median daemon times for that build were 10.231 ms on the RTX 4090 and 29.133 ms
on the Radeon R9700. The exact Actions artifact still needs download-and-run
qualification after the workflow's first branch run.

## Publishing

Actions artifacts are temporary QA outputs, not release downloads. After
hardware qualification, attach the verified Wheels and `SHA256SUMS` to the
immutable GitHub runtime release. All nine Wheels are distributed from that
fixed release. Later PyPI publication can add the three `pinozcam-runner` and
two `pinozcam-runner-gpu` platform Wheels without changing their distribution
names. The release manifest links each executable to the exact PiNozCam commit
used by CI.

Each platform workflow supports manual `workflow_dispatch` and reusable
`workflow_call`. `build-runtime-release.yml` invokes all six when an RC or
stable tag is pushed, verifies the nine-file set, and creates a draft Release.
GitHub only shows the manual Run workflow button after a workflow exists on the
default branch; tag-triggered runs use the workflow committed at that tag.

The selected installation contract and immutable-asset rules are documented in
[`runtime-release.md`](runtime-release.md).
