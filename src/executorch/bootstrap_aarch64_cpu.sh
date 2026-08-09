#!/usr/bin/env bash
# Build the AArch64 PiNozCam CPU daemon on a native ARM64 Linux host.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <work-root> <artifact-root>" >&2
  exit 2
fi
if [ "$(uname -m)" != "aarch64" ]; then
  echo "this native build requires an aarch64 host" >&2
  exit 2
fi

WORK_ROOT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
ARTIFACT_ROOT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ET_ROOT="$WORK_ROOT/executorch"
ET_BUILD_DIR="$ET_ROOT/cmake-out-aarch64"
VENV="$WORK_ROOT/codegen-venv"
PATCH="$SCRIPT_DIR/patches/upsample_nearest2d_2x_fastpath.patch"

ET_TAG=v1.3.1
ET_COMMIT=e2f18eb23c45bd22ca332b0b8b49a81de304b472
TORCH_VERSION=2.12.1
JOBS=${JOBS:-$(getconf _NPROCESSORS_ONLN)}

mkdir -p "$WORK_ROOT" "$ARTIFACT_ROOT/bin"
if [ ! -d "$ET_ROOT/.git" ]; then
  git clone --branch "$ET_TAG" --depth 1 --recurse-submodules \
    --shallow-submodules https://github.com/pytorch/executorch.git "$ET_ROOT"
fi

actual_commit=$(git -C "$ET_ROOT" rev-parse HEAD)
if [ "$actual_commit" != "$ET_COMMIT" ]; then
  echo "ExecuTorch commit mismatch: $actual_commit (expected $ET_COMMIT)" >&2
  exit 1
fi
if git -C "$ET_ROOT" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
  echo "PiNozCam ExecuTorch patch is already applied"
elif git -C "$ET_ROOT" apply --check "$PATCH"; then
  git -C "$ET_ROOT" apply "$PATCH"
else
  echo "PiNozCam ExecuTorch patch does not apply to $ET_COMMIT" >&2
  exit 1
fi
git -C "$ET_ROOT" diff --check

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  --index-url https://download.pytorch.org/whl/cpu \
  "torch==$TORCH_VERSION"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  "PyYAML==6.0.3" "ruamel.yaml==0.19.1" "flatbuffers==25.12.19"

cmake -S "$ET_ROOT" -B "$ET_BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE="$VENV/bin/python" \
  -DEXECUTORCH_BUILD_XNNPACK=ON \
  -DEXECUTORCH_BUILD_EXECUTOR_RUNNER=ON \
  -DEXECUTORCH_BUILD_EXTENSION_DATA_LOADER=ON \
  -DEXECUTORCH_BUILD_EXTENSION_RUNNER_UTIL=ON \
  -DCMAKE_EXE_LINKER_FLAGS=-static
cmake --build "$ET_BUILD_DIR" --parallel "$JOBS" --target executor_runner

ET_ROOT="$ET_ROOT" ET_BUILD_DIR="$ET_BUILD_DIR" \
  "$SCRIPT_DIR/build.sh" native

daemon="$ET_BUILD_DIR/nozcam_daemon"
if [ ! -x "$daemon" ]; then
  echo "daemon was not produced: $daemon" >&2
  exit 1
fi
if ! file "$daemon" | grep -q 'ARM aarch64'; then
  echo "daemon is not an AArch64 ELF" >&2
  file "$daemon" >&2
  exit 1
fi
if ! file "$daemon" | grep -q 'statically linked'; then
  echo "daemon is not statically linked" >&2
  file "$daemon" >&2
  exit 1
fi
if nm -D "$daemon" 2>/dev/null | grep -q GLIBC; then
  echo "daemon unexpectedly contains dynamic GLIBC references" >&2
  exit 1
fi

install -m 0755 "$daemon" \
  "$ARTIFACT_ROOT/bin/nozcam_daemon.aarch64.static"
sha256sum "$ARTIFACT_ROOT/bin/nozcam_daemon.aarch64.static"
