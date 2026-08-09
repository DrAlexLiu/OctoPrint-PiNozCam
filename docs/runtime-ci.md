# Native runtime CI

PiNozCam keeps native executables and models out of the plugin source archive.
The workflows below rebuild the executables from the checked-out source,
combine them with SHA-256-pinned model inputs, validate the resulting Wheel,
and retain it as a GitHub Actions artifact for 14 days.

| Workflow | Build host | Output |
| --- | --- | --- |
| `build-x86-runtime.yml` | GitHub x86-64 | x86-64 CPU Wheel |
| `build-rockchip-runtime.yml` | GitHub ARM64 plus a pinned Ubuntu 20.04 cross-build container | RK3566, RK3576, and RK3588 Wheels |
| `build-jetson-runtime.yml` | GitHub Ubuntu 22.04 ARM64 | Jetson Orin Vulkan Wheel |
| `build-a733-runtime.yml` | GitHub ARM64 plus an ephemeral A733 self-hosted runner | A733 AWNN Wheel |

The CPU fallback is built from the same pinned ExecuTorch source on Ubuntu
22.04 ARM64. Rockchip uses one shared daemon executable and three different
RKNN model files. Jetson builds both the CPU fallback and Vulkan executable;
Mesa lavapipe provides a software Vulkan protocol smoke test in CI. A733 needs
a real board because its AWNN/VIPLite build inputs are not available from a
public, versioned download source.

## Platform tags

The generic CPU Wheels are self-contained. The Jetson executable uses only the
standard dynamic libraries permitted by its `manylinux_2_35_aarch64` baseline
and loads the board's Vulkan implementation at runtime.

Rockchip and A733 executables intentionally depend on board-provided vendor
libraries. Their CI Wheels therefore use the honest `linux_aarch64` tag rather
than claiming manylinux compatibility. PyPI does not accept this tag; durable
Rockchip and A733 binaries belong on the matching GitHub Release unless a
future implementation removes or legally bundles those vendor dependencies.

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
| A733 AWNN | [31339370137](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31339370137) | The ephemeral self-hosted A733 job built the runner and completed real AWNN inference |
| RK3566/RK3576/RK3588 | [31340003668](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31340003668) | The exact three downloaded Wheels completed real NPU inference on one board of each SoC family |
| Jetson Orin Vulkan | [31340384861](https://github.com/DrAlexLiu/OctoPrint-PiNozCam/actions/runs/31340384861) | The exact downloaded Wheel completed Vulkan inference on an NVIDIA Tegra Orin after the hosted lavapipe check |

The hardware smoke request uses a deterministic synthetic frame and validates
backend loading, device execution, the binary protocol, finite output, and
clean shutdown. It is a liveness and integration gate, not a substitute for
the separate image-set accuracy and CPU/accelerator parity qualification.

## Publishing

Actions artifacts are temporary QA outputs, not release downloads. After
hardware qualification, attach the verified Wheels and `SHA256SUMS` to the
immutable GitHub runtime release. The release manifest links each executable
to the exact PiNozCam commit used by CI.

These workflows are intentionally manual (`workflow_dispatch`). GitHub only
shows the Run workflow button after a workflow exists on the default branch.
During development on a non-default branch, a temporary branch-only `push`
trigger may be used for validation and must be removed from the final commit.

The experimental workflows do not by themselves change `setup.py` or the
current package-index install policy. Wiring GitHub Release URLs into plugin
installation is a separate release decision.
