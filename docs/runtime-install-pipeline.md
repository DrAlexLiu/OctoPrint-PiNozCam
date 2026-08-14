# PiNozCam installation and native runtime pipeline

This document is the canonical installation description for PiNozCam 1.1.0
and later. It deliberately distinguishes the Python distribution, the native
runner executable, and the model file.

## Names used in this document

- **PiNozCam** is the OctoPrint plugin. Keep this capitalization in user-facing
  text and documentation.
- A **runtime distribution** is a versioned package such as
  `pinozcam-runtime-rknn3588` or `pinozcam-runtime-gpu`.
- A **runner** is the native executable inside a runtime distribution. It speaks
  the PiNozCam binary protocol over stdin/stdout.
- A **model** is the backend-specific model file shipped by that runtime. The
  plugin source archive never contains a runner or a model.

Do not call a runtime distribution a “runner package”, and do not use obsolete
names such as `pinozcam_runner`, `pinozcam-runtime-jetson-orin`, or mixed-case
variants of `PiNozCam` in new release metadata.

## What is installed

The user installs one plugin source archive. `setup.py` detects the host and
adds exactly one immutable runtime dependency:

| Host/backend | Runtime distribution | Native payload |
| --- | --- | --- |
| ARMHF, AArch64, or x86-64 CPU | `pinozcam-runtime` | CPU runner and CPU `.pte` model |
| RK3566 | `pinozcam-runtime-rknn3566` | RKNN runner, RKNN model, CPU fallback |
| RK3576 | `pinozcam-runtime-rknn3576` | RKNN runner, RKNN model, CPU fallback |
| RK3588 | `pinozcam-runtime-rknn3588` | RKNN runner, RKNN model, CPU fallback |
| Allwinner A733 | `pinozcam-runtime-a733` | AWNN runner, A733 model, CPU fallback |
| Vulkan-capable AArch64 or x86-64 host | `pinozcam-runtime-gpu` | Vulkan runner, GPU `.pte` model, CPU fallback |

The plugin package contains Python, templates, JavaScript, documentation, and
the protocol client. It does not contain native binaries or model weights.

## Installation flow

The normal flow is:

```text
OctoPrint Plugin Manager or tag ZIP
        |
        v
pip invokes setup.py in the OctoPrint virtual environment
        |
        v
setup.py detects ABI and available accelerator runtime
        |
        v
one exact runtime Wheel is selected from the matching GitHub Release
        |
        v
pip installs the plugin and that runtime together
        |
        v
OctoPrint restarts; PiNozCam starts the selected runner
```

For the 1.1.0 release candidate, the direct installation command is:

```bash
python -m pip install \
  --no-build-isolation --no-cache-dir \
  https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/refs/tags/1.1.0rc11.zip
```

`--no-build-isolation` is required for OctoPrint installations because the
plugin setup imports OctoPrint's packaging helpers. A newly created temporary
PEP 517 build environment does not contain those helpers, which produces:

```text
Could not import OctoPrint's setuptools.
```

This is an installation-environment requirement, not a detector or runner
failure. The plugin setup itself remains responsible for selecting the runtime.

## Runtime selection

Selection is fail-closed and happens during installation:

1. The Python interpreter ABI is checked first. A 32-bit userspace selects the
   ARMHF CPU runtime even when the Linux kernel is 64-bit.
2. On AArch64, the device tree is checked for RK3566, RK3576, or RK3588.
3. The installed Rockchip userspace runtime is checked before selecting RKNN.
4. The A733 AWNN runtime is selected only when its board runtime is present.
5. Jetson and other supported Vulkan hosts select `pinozcam-runtime-gpu` when
   Vulkan is available; otherwise the CPU runtime is selected.
6. An unsupported host does not receive a guessed native payload.

The selected runtime is pinned to the same immutable release version as the
plugin. Runtime distributions are not interchangeable: a Rockchip model is
not a CPU `.pte`, and a Vulkan runner is not an RKNN runner.

## GitHub Release and PyPI boundaries

GitHub Releases are the authoritative source for all nine runtime Wheels,
including hardware-specific RKNN and A733 distributions. The plugin's
`setup.py` uses fixed release asset URLs, never a mutable `latest` URL.

The portable CPU distribution (`pinozcam-runtime`) and the generic Vulkan
distribution (`pinozcam-runtime-gpu`) may additionally be mirrored on PyPI.
The bytes must match the corresponding GitHub Release assets. Hardware-specific
packages remain GitHub Release assets unless their platform tags and vendor
redistribution terms are explicitly suitable for PyPI.

## Uninstall and upgrade

Uninstalling the plugin removes the installed Python plugin and runtime
distribution through pip. It does not delete OctoPrint settings, print history,
evidence, or credentials stored under the OctoPrint data directory. An upgrade
should use the new tag and lets pip replace the old plugin/runtime package.

For a clean runtime replacement without touching settings:

```bash
python -m pip uninstall -y \
  OctoPrint-PiNozCam \
  pinozcam-runtime pinozcam-runtime-gpu \
  pinozcam-runtime-rknn3566 pinozcam-runtime-rknn3576 \
  pinozcam-runtime-rknn3588 pinozcam-runtime-a733

python -m pip install \
  --no-build-isolation --no-cache-dir \
  https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/refs/tags/1.1.0rc11.zip
```

Stop OctoPrint before a manual uninstall/reinstall and start it again after
the installation succeeds. Do not remove `~/.octoprint` or another OctoPrint
data directory as part of this procedure.

## Release checklist

For each release candidate:

1. Update `plugin_version` and `runtime_version` together.
2. Build the native runtime distributions in GitHub Actions.
3. Verify runner ELF architecture, model manifest, Wheel contents, and
   `CHECKSUMS.txt`.
4. Create one immutable GitHub Pre-release containing the nine runtime Wheels.
5. Install the exact tag on each available target board with
   `--no-build-isolation`.
6. Confirm the selected runtime distribution, OctoPrint service status, and a
   PING/INFO/INFER/SHUTDOWN protocol smoke test.
7. Publish the portable CPU and generic Vulkan distributions to PyPI only after
   their GitHub asset hashes match.

Never upload runner binaries or model files into the plugin source archive, and
never change an already published release asset. Fixes require a new release
candidate or patch version.
