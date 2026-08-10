# Releasing PiNozCam

For the end-to-end installation boundary, package naming, and clean reinstall
procedure, see [`runtime-install-pipeline.md`](runtime-install-pipeline.md).

PiNozCam RCs use one immutable distribution boundary: the GitHub Release holds
all nine runtime Wheels and their checksum manifest. The five portable CPU and
GPU Wheels may also be published to PyPI later through a separate approved
workflow.

A tag and its assets are immutable. If a release candidate has a packaging
problem, fix it and create the next `rcN`; never move the old tag or replace an
already-published Wheel.

The installer contract and platform tags are defined in
[`runtime-release.md`](runtime-release.md).

## Artifacts

The GitHub tag archive contains the OctoPrint plugin and native runner source,
but no model or runner binary. A complete release has nine runtime Wheels:

| Distribution | Platform files | Channel |
| --- | --- | --- |
| `pinozcam-runtime` | ARM 32-bit, ARM 64-bit, x86-64 CPU | GitHub Release and PyPI |
| `pinozcam-runtime-rknn3566` | RK3566 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-rknn3576` | RK3576 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-rknn3588` | RK3588 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-a733` | A733 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-gpu` | AArch64 and x86-64 Vulkan Wheels, each with its CPU fallback | GitHub Release and PyPI |

The GitHub Release also contains `CHECKSUMS.txt` for all nine assets. The
release workflow verifies the exact filename, Wheel metadata, source revision,
and digest before it creates the draft release.

## 1. Prepare a release candidate

1. Work on `release/1.1.0`; do not merge `master` during RC development.
2. Set `plugin_version` and `runtime_version` to the intended candidate.
3. Run each manual source-build workflow at the exact release commit.
4. Download the Actions artifacts into a new empty directory.
5. Verify filenames, Wheel metadata, ELF architecture, runtime manifests, and
   SHA-256 values. Require `auditwheel show` to pass for every CPU and Vulkan
   Wheel; PyPI accepting a filename is not this check.
6. Generate one `CHECKSUMS.txt` covering all nine Wheels.

The build workflows and recorded qualification runs are described in
[`runtime-ci.md`](runtime-ci.md).

## 2. Qualify the exact artifacts

Do not substitute a locally rebuilt executable after downloading the Actions
artifact. Install and test the exact Wheel bytes intended for the release.

The minimum matrix is:

- ARM 32-bit CPU, including a 64-bit kernel with 32-bit Python userspace;
- ARM 64-bit CPU;
- x86-64 CPU;
- x86-64 Vulkan on qualified NVIDIA and AMD drivers, plus provisional Intel;
- RK3566, RK3576, and RK3588 NPU;
- A733 hosted build, ELF/dependency checks, Wheel verification, and CPU
  fallback protocol inference;
- AArch64 Vulkan on a qualified Jetson Orin.

For each hardware-qualified target, require PING, INFO, INFER, and SHUTDOWN
through the production binary pipe protocol. The A733 exception is documented
in [`a733-ci.md`](a733-ci.md): its release workflow does not execute the AWNN
runner on a real device.

## 3. Create the pre-release

Commit the verified release state, create an annotated RC tag, and push only
the release branch and tag:

```bash
git tag -a 1.1.0rcN -m "PiNozCam 1.1.0rcN"
git push origin release/1.1.0
git push origin 1.1.0rcN
```

The tag-triggered `Build nine-Wheel runtime release` workflow builds, verifies,
and attaches all nine Wheels plus `CHECKSUMS.txt` to a draft pre-release. Do
not manually substitute a local Wheel. For emergency manual recovery only:

```bash
gh release create 1.1.0rcN \
  dist/*.whl CHECKSUMS.txt \
  --repo DrAlexLiu/OctoPrint-PiNozCam \
  --verify-tag \
  --prerelease \
  --title "v1.1.0rcN" \
  --notes "Release candidate for PiNozCam 1.1.0."
```

Download the public assets into another empty directory and run
`sha256sum -c CHECKSUMS.txt`. This catches upload mistakes independently of the
build workspace.

PyPI publication is not part of RC7 creation. A later manual workflow may
publish only the three `pinozcam-runtime` and two `pinozcam-runtime-gpu` Wheels.

## 4. End-to-end clean-install test

Install the public tag ZIP, not a working tree:

```text
https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/refs/tags/1.1.0rcN.zip
```

For every target:

1. Confirm the printer is not printing or paused.
2. Record the PiNozCam settings independently of credentials.
3. Stop OctoPrint.
4. Uninstall `OctoPrint-PiNozCam` and old `pinozcam-runtime*` distributions.
   This does not delete OctoPrint's `config.yaml`.
5. Install the tag ZIP with `--no-cache-dir --no-build-isolation` and preserve
   pip output. Every target must download the exact fixed-tag GitHub Release
   Wheel selected by `setup.py`.
6. Restart OctoPrint and confirm the plugin version and selected backend.
7. Call `/plugin/pinozcam/check` and run an authenticated Speed Test.
8. Require HTTP 200, `backendError=false`, and a successful inference.
9. Confirm user-controlled settings are unchanged.

## 5. Promote to stable

Do not rename RC artifacts. After the candidate passes:

1. Build fresh runtime Wheels as version `1.1.0` from the final source commit.
2. Repeat checksum verification and the complete hardware matrix.
3. Set the plugin and runtime references to the final immutable asset names and
   digests.
4. Manually merge the verified release commit to `master`.
5. Create the annotated `1.1.0` tag on that exact merged commit.
6. Create the non-prerelease GitHub Release containing all nine verified Wheels
   and `CHECKSUMS.txt`.
7. Optionally publish the five audited portable CPU/GPU Wheels to PyPI through
   the separate manual Trusted Publishing workflow.
8. Download the public stable assets and repeat the clean-install smoke test.
