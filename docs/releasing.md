# Releasing PiNozCam

PiNozCam uses two immutable distribution channels: generic CPU and Jetson Orin
Wheels use PyPI after a successful manylinux audit; Rockchip and A733 Wheels
use fixed GitHub Release asset URLs.

A tag and its assets are immutable. If a release candidate has a packaging
problem, fix it and create the next `rcN`; never move the old tag or replace an
already-published Wheel.

The installer contract and platform tags are defined in
[`runtime-release.md`](runtime-release.md). Do not release while that document's
`setup.py` direct-reference integration gap remains open.

## Artifacts

The GitHub tag archive contains the OctoPrint plugin and native runner source,
but no model or runner binary. A complete release has eight runtime Wheels:

| Distribution | Platform files | Channel |
| --- | --- | --- |
| `pinozcam-runtime` | ARM 32-bit, ARM 64-bit, x86-64 CPU | PyPI |
| `pinozcam-runtime-rknn3566` | RK3566 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-rknn3576` | RK3576 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-rknn3588` | RK3588 NPU plus ARM CPU fallback | GitHub Release |
| `pinozcam-runtime-a733` | A733 NPU plus ARM CPU fallback | GitHub Release after license clearance |
| `pinozcam-runtime-jetson-orin` | Jetson Orin Vulkan plus ARM CPU fallback | PyPI |

The GitHub Release also contains `CHECKSUMS.txt` for its hardware-specific
assets. Every direct-reference digest in the plugin source must match the
immutable release asset selected by `setup.py`.

## 1. Prepare a release candidate

1. Work on `release/1.1.0`; do not merge `master` during RC development.
2. Set `plugin_version` and `runtime_version` to the intended candidate.
3. Run each manual source-build workflow at the exact release commit.
4. Download the Actions artifacts into a new empty directory.
5. Verify filenames, Wheel metadata, ELF architecture, runtime manifests, and
   SHA-256 values. Require `auditwheel show` to pass for every CPU and Jetson
   Wheel; PyPI accepting a filename is not this check.
6. Do not publish the A733 Wheel unless its runner redistribution permission
   has been confirmed in writing.
7. Generate one `CHECKSUMS.txt` covering every asset that will be public.

The build workflows and recorded qualification runs are described in
[`runtime-ci.md`](runtime-ci.md).

## 2. Qualify the exact artifacts

Do not substitute a locally rebuilt executable after downloading the Actions
artifact. Install and test the exact Wheel bytes intended for the release.

The minimum matrix is:

- ARM 32-bit CPU, including a 64-bit kernel with 32-bit Python userspace;
- ARM 64-bit CPU;
- x86-64 CPU;
- RK3566, RK3576, and RK3588 NPU;
- A733 NPU, subject to the redistribution gate;
- Jetson Orin Vulkan.

For each target, require PING, INFO, INFER, and SHUTDOWN through the production
binary pipe protocol. Accelerator targets must execute on the real device, not
only a hosted build runner or software Vulkan implementation.

## 3. Create the pre-release

Commit the verified release state, create an annotated RC tag, and push only
the release branch and tag:

```bash
git tag -a 1.1.0rcN -m "PiNozCam 1.1.0rcN"
git push origin release/1.1.0
git push origin 1.1.0rcN
```

Create a GitHub pre-release and attach the exact qualified Rockchip Wheels,
the A733 Wheel only after license clearance, and their checksum manifest:

```bash
gh release create 1.1.0rcN \
  dist/pinozcam_runtime_rknn*.whl CHECKSUMS.txt \
  --repo DrAlexLiu/OctoPrint-PiNozCam \
  --verify-tag \
  --prerelease \
  --title "v1.1.0rcN" \
  --notes "Release candidate for PiNozCam 1.1.0."
```

Download the public assets into another empty directory and run
`sha256sum -c CHECKSUMS.txt`. This catches upload mistakes independently of the
build workspace.

Publish the audited generic CPU and Jetson Wheels through their production PyPI
Trusted Publishers. PyPI and GitHub must contain the same version recorded by
the plugin; never rebuild or replace a file under an existing version.

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
   pip output. CPU/Jetson must resolve the exact PyPI version; Rockchip/A733
   must download the fixed GitHub Release URL and verify its hash fragment.
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
6. Publish audited CPU and Jetson Wheels to PyPI.
7. Create the non-prerelease GitHub Release and upload the verified Rockchip
   Wheels, the cleared A733 Wheel, and `CHECKSUMS.txt`.
8. Download the public stable assets and repeat the clean-install smoke test.
