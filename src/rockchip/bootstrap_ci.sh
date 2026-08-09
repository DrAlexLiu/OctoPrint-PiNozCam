#!/usr/bin/env bash
# Cross-build the shared AArch64 RKNN daemon from a pinned public SDK.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <work-root> <artifact-root>" >&2
  exit 2
fi

WORK_ROOT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
ARTIFACT_ROOT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
SDK_ROOT="$WORK_ROOT/rknn-toolkit2"

RKNN_TAG=v2.3.0
RKNN_COMMIT=a8dd54d41e92c95b4f95780ed0534362b2c98b92
RKNN_REPOSITORY=https://github.com/airockchip/rknn-toolkit2.git
CXX=${CXX:-aarch64-linux-gnu-g++}
READELF=${READELF:-aarch64-linux-gnu-readelf}

for tool in git "$CXX" "$READELF" file install sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "required build tool is unavailable: $tool" >&2
    exit 1
  fi
done

mkdir -p "$WORK_ROOT" "$ARTIFACT_ROOT/bin"

# A sparse partial clone avoids downloading the toolkit's large example models.
if [ ! -d "$SDK_ROOT/.git" ]; then
  git clone --branch "$RKNN_TAG" --depth 1 --filter=blob:none --no-checkout \
    "$RKNN_REPOSITORY" "$SDK_ROOT"
  git -C "$SDK_ROOT" sparse-checkout init --cone
  git -C "$SDK_ROOT" sparse-checkout set \
    rknpu2/runtime/Linux/librknn_api
  git -C "$SDK_ROOT" checkout --detach "$RKNN_COMMIT"
fi

actual_commit=$(git -C "$SDK_ROOT" rev-parse HEAD)
if [ "$actual_commit" != "$RKNN_COMMIT" ]; then
  echo "RKNN toolkit commit mismatch: $actual_commit" >&2
  echo "expected: $RKNN_COMMIT" >&2
  exit 1
fi

include_dir="$SDK_ROOT/rknpu2/runtime/Linux/librknn_api/include"
library_dir="$SDK_ROOT/rknpu2/runtime/Linux/librknn_api/aarch64"
vendor_library="$library_dir/librknnrt.so"
daemon="$WORK_ROOT/nozcam_daemon.rknn.aarch64"

if [ ! -f "$include_dir/rknn_api.h" ] || [ ! -f "$vendor_library" ]; then
  echo "the pinned SDK checkout does not contain the RKNN Linux API" >&2
  exit 1
fi
if ! file "$vendor_library" | grep -q 'ELF 64-bit.*ARM aarch64'; then
  echo "the pinned RKNN runtime is not an AArch64 ELF" >&2
  file "$vendor_library" >&2
  exit 1
fi

# Compile exactly once. Explicit PIE/hardening flags avoid depending on the
# cross-compiler's distro defaults; all compiler warnings are build failures.
"$CXX" -O2 -std=c++14 -Wall -Wextra -Wpedantic -Werror -fPIE \
  -I"$REPO_ROOT/src/common" -I"$include_dir" \
  "$SCRIPT_DIR/nozcam_rockchip_daemon.cpp" \
  "$REPO_ROOT/src/common/nozcam_postprocess.cpp" \
  -L"$library_dir" -Wl,-z,noexecstack,-z,relro,-z,now -pie \
  -lrknnrt -o "$daemon"

if ! file "$daemon" | grep -q 'ELF 64-bit.*ARM aarch64'; then
  echo "cross-build did not produce an AArch64 ELF" >&2
  file "$daemon" >&2
  exit 1
fi
if ! "$READELF" -h "$daemon" | grep -q 'Class:.*ELF64'; then
  echo "RKNN daemon is not an ELF64 executable" >&2
  exit 1
fi
if ! "$READELF" -h "$daemon" | grep -q 'Machine:.*AArch64'; then
  echo "RKNN daemon has the wrong ELF machine" >&2
  exit 1
fi
if ! "$READELF" -h "$daemon" | grep -q 'Type:.*DYN'; then
  echo "RKNN daemon is not an ELF ET_DYN executable" >&2
  exit 1
fi
if ! "$READELF" -d "$daemon" | grep -q 'FLAGS_1.*PIE'; then
  echo "RKNN daemon is not marked as a position-independent executable" >&2
  exit 1
fi

stack_line=$("$READELF" -W -l "$daemon" | grep 'GNU_STACK' || true)
if [ -z "$stack_line" ] || printf '%s\n' "$stack_line" | grep -q 'RWE'; then
  echo "RKNN daemon has a missing or executable GNU_STACK header" >&2
  printf '%s\n' "$stack_line" >&2
  exit 1
fi

if "$READELF" -d "$daemon" | grep -Eq '\((RPATH|RUNPATH)\)'; then
  echo "RKNN daemon unexpectedly contains RPATH/RUNPATH" >&2
  exit 1
fi

expected_dependencies=$(printf '%s\n' \
  ld-linux-aarch64.so.1 \
  libc.so.6 \
  libgcc_s.so.1 \
  libm.so.6 \
  librknnrt.so \
  libstdc++.so.6 | sort)
actual_dependencies=$("$READELF" -d "$daemon" \
  | sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p' | sort)
if [ "$actual_dependencies" != "$expected_dependencies" ]; then
  echo "unexpected RKNN daemon dependency set" >&2
  echo "actual:" >&2
  printf '%s\n' "$actual_dependencies" >&2
  echo "expected:" >&2
  printf '%s\n' "$expected_dependencies" >&2
  exit 1
fi

interpreter=$($READELF -W -l "$daemon" \
  | sed -n 's/.*Requesting program interpreter: \([^]]*\).*/\1/p')
if [ "$interpreter" != /lib/ld-linux-aarch64.so.1 ]; then
  echo "unexpected RKNN daemon interpreter: ${interpreter:-missing}" >&2
  exit 1
fi

glibc_versions=$("$READELF" -W --version-info "$daemon" \
  | grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/^GLIBC_//' | sort -Vu)
if [ -z "$glibc_versions" ]; then
  echo "could not determine the daemon's GLIBC symbol floor" >&2
  exit 1
fi
max_glibc=$(printf '%s\n' "$glibc_versions" | tail -n 1)
version_ceiling=$(printf '%s\n' 2.29 "$max_glibc" | sort -V | tail -n 1)
if [ "$version_ceiling" != 2.29 ]; then
  echo "RKNN daemon requires GLIBC_$max_glibc; maximum allowed is 2.29" >&2
  exit 1
fi

install -m 0755 "$daemon" \
  "$ARTIFACT_ROOT/bin/nozcam_daemon.rknn.aarch64"

echo "RKNN SDK source: $RKNN_REPOSITORY@$RKNN_COMMIT"
echo "RKNN daemon GLIBC versions: $(printf '%s' "$glibc_versions" | tr '\n' ' ')"
"$READELF" -d "$daemon" | sed -n 's/.*Shared library: \[\([^]]*\)\].*/NEEDED \1/p'
sha256sum "$ARTIFACT_ROOT/bin/nozcam_daemon.rknn.aarch64"
