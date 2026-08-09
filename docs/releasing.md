# Releasing PiNozCam

This document is the maintainer checklist for a release candidate and the
eventual stable release. A tag is immutable: if a candidate has a packaging
bug, fix it and create the next `rcN`; never move the old tag or replace an
already-published Wheel.

## Artifacts

The GitHub tag archive contains the OctoPrint plugin and native runner source,
but no runner binary or model. `setup.py` selects one runtime distribution for
the detected platform and pins its exact version. The outer pip process resolves
that dependency from its configured package index, normally production PyPI.

There are eight Wheels in six Python projects:

| Project | Platform files |
| --- | --- |
| `pinozcam-runtime` | ARM 32-bit, ARM 64-bit, x86-64 CPU |
| `pinozcam-runtime-rknn3566` | RK3566 NPU plus ARM CPU fallback |
| `pinozcam-runtime-rknn3576` | RK3576 NPU plus ARM CPU fallback |
| `pinozcam-runtime-rknn3588` | RK3588 NPU plus ARM CPU fallback |
| `pinozcam-runtime-a733` | A733 NPU plus ARM CPU fallback |
| `pinozcam-runtime-jetson-orin` | Jetson Orin Vulkan plus ARM CPU fallback |

`CHECKSUMS.txt` records the eight files for the selected runtime version. Its
digests must match the files published to PyPI and the runtime GitHub Release.
The plugin and runtime versions are intentionally independent: a plugin-only
fix can reuse an already published and verified runtime release.

## 1. Prepare the candidate

1. Work on `release/1.1.0` and ensure the tree is clean.
2. Set `plugin_version` to the new plugin version and `runtime_version` to the
   exact runtime already published on PyPI. For the current candidate these are
   `1.1.0rc6` and `1.1.0rc5`, respectively.
3. If native payloads changed, build and publish all eight Wheels under a new
   runtime version before preparing the plugin. If they did not change, do not
   rebuild or re-upload them.
4. Confirm `CHECKSUMS.txt` describes the selected runtime version. When a new
   runtime was built, run `sha256sum -c CHECKSUMS.txt` in its artifact directory.
5. Confirm every new Wheel contains its runtime manifest, licenses, source URL,
   and intended platform tag.
6. Run the packaging tests, commit, then create and push an annotated tag on
   that commit:

```bash
git tag -a 1.1.0rc6 -m "PiNozCam 1.1.0rc6"
git push origin release/1.1.0
git push origin 1.1.0rc6
```

## 2. Create the GitHub pre-release

The source ZIP is generated from the tag automatically. A plugin-only release
attaches `CHECKSUMS.txt` for the runtime it consumes; it does not duplicate the
unchanged runtime Wheels from the earlier runtime release.

```bash
gh release create 1.1.0rc6 \
  CHECKSUMS.txt \
  --repo DrAlexLiu/OctoPrint-PiNozCam \
  --verify-tag \
  --prerelease \
  --title "v1.1.0rc6" \
  --notes "Release candidate for PiNozCam 1.1.0."
```

Download the release metadata into an empty directory and verify that the tag
and recorded runtime version are the intended ones:

```bash
gh release download 1.1.0rc6 \
  --repo DrAlexLiu/OctoPrint-PiNozCam \
  --pattern CHECKSUMS.txt
git show 1.1.0rc6:setup.py | grep -E 'plugin_version|runtime_version'
```

When the runtime changes, its eight Wheels are attached to that runtime's
GitHub pre-release before they are uploaded to either package index. Download
those assets independently and run `sha256sum -c CHECKSUMS.txt` before upload.

## 3. Publish to TestPyPI

TestPyPI and production PyPI have separate projects and Trusted Publisher
registrations. The TestPyPI publisher uses:

- owner: `DrAlexLiu`
- repository: `OctoPrint-PiNozCam`
- workflow: `publish-testpypi.yml`
- environment: `testpypi`

The GitHub `testpypi` environment should require manual approval. Register the
same publisher for each of the six project names above. A project that does not
exist yet uses a pending publisher; after its first successful upload it becomes
an ordinary project publisher.

Run one target at a time while bootstrapping the projects:

```bash
gh workflow run publish-testpypi.yml \
  --repo DrAlexLiu/OctoPrint-PiNozCam \
  --ref release/1.1.0 \
  -f release_tag=1.1.0rc5 \
  -f runtime_target=cpu
```

Repeat with `rknn3566`, `rknn3576`, `rknn3588`, `a733`, and `jetson_orin`.
Approve each deployment under **Actions → workflow run → Review deployments**.
Do not select `all` after any file for that version has already been uploaded;
PyPI never permits replacing an existing filename.

Test a published Wheel without altering an OctoPrint environment:

```bash
python3 -m venv /tmp/pinozcam-wheel-test
/tmp/pinozcam-wheel-test/bin/pip install \
  --index-url https://test.pypi.org/simple/ \
  --no-deps pinozcam-runtime==1.1.0rc5
```

## 4. Publish a release candidate to production PyPI

Production PyPI is a separate package index. Register a pending Trusted
Publisher for each of the six project names, using:

- owner: `DrAlexLiu`
- repository: `OctoPrint-PiNozCam`
- workflow: `publish-pypi.yml`
- environment: `pypi`

The GitHub `pypi` environment must require manual approval. Upload one project
at a time while the pending publishers create the six projects:

```bash
gh workflow run publish-pypi.yml \
  --repo DrAlexLiu/OctoPrint-PiNozCam \
  --ref release/1.1.0 \
  -f release_tag=1.1.0rc5 \
  -f runtime_target=cpu
```

Repeat with `rknn3566`, `rknn3576`, `rknn3588`, `a733`, and `jetson_orin`.
Review and approve each `pypi` deployment before upload. Production PyPI does
not permit deleting a release file and uploading different bytes under the
same filename, so verify the GitHub Release first and never reuse a version.

## 5. End-to-end install test

The PyPI test above validates the package index. The following validates the
actual OctoPrint install path, including `setup.py` hardware selection and the
exact PyPI runtime dependency:

```text
https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/refs/tags/1.1.0rc6.zip
```

For every target:

1. Confirm the printer is not printing or paused.
2. Record the PiNozCam settings independently of credentials.
3. Stop OctoPrint.
4. Uninstall `OctoPrint-PiNozCam` and every old/new `pinozcam-runtime*`
   distribution. This does not delete OctoPrint's `config.yaml`.
5. Confirm no PiNozCam distribution remains in `pip list`.
6. Install the tag ZIP with `--no-cache-dir --no-build-isolation`. Preserve the
   pip output as evidence that the exact `runtime_version` came from PyPI rather
   than a GitHub asset URL.
7. Restart OctoPrint and confirm the plugin version and selected runtime.
8. Call `/plugin/pinozcam/check` and run one authenticated Speed Test.
9. Require HTTP 200, `backendError=false`, and a successful inference.
10. Confirm the user-controlled settings are unchanged. `maskSignature` is a
    server-maintained camera geometry value and may change when a frame is
    first acquired; it is not a user setting.

The minimum matrix is ARM 32-bit CPU, ARM 64-bit CPU, x86-64 CPU, RK3566,
RK3576, RK3588, A733, and Jetson Orin. A 64-bit kernel with a 32-bit Python
userspace must be included because pip selects Wheels for the userspace ABI.

## 6. Promote to a stable release

Do not promote an RC artifact by renaming it. After the candidate passes:

1. Build all eight runtime Wheels as version `1.1.0`, regenerate
   `CHECKSUMS.txt`, and publish them through the production workflow.
2. Set `plugin_version` and `runtime_version` to `1.1.0`.
3. Repeat the GitHub Release, checksum, clean-install, and inference checks.
4. The production publish action must not set
   TestPyPI's `repository-url`.
5. Manually merge the verified release commit to `master`, create the immutable
   `1.1.0` tag on that commit, and publish the non-prerelease GitHub Release.

## 1.1.0rc3 validation record

On 2026-08-09, the tag ZIP was clean-installed and a real inference was run on
five Raspberry Pi/OctoPi systems, x86-64, RK3566, RK3576, RK3588, A733, and
Jetson Orin. CPU, AWNN, and Vulkan selection passed. The three Rockchip
installations incorrectly selected the generic CPU Wheel; installing the
matching RKNN Wheel manually made all three NPU backends pass.

The cause is release code, not the boards: `_detect_rockchip_chip()` returns a
value such as `rk3588`, while `setup.py` builds `aarch64-rknnrk3588` instead of
the valid target `aarch64-rknn3588`. Therefore `1.1.0rc3` must remain a
pre-release and must not be promoted to `1.1.0`. The correction and a regression
test belong in `1.1.0rc4`.
