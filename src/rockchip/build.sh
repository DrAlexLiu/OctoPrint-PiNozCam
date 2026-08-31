#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CXX=${CXX:-g++}
OUT=${OUT:-$HERE/nozcam_rockchip_daemon}
RKNN_INCLUDE_DIR=${RKNN_INCLUDE_DIR:-/usr/include}

if [ ! -f "$RKNN_INCLUDE_DIR/rknn_api.h" ]; then
  echo "rknn_api.h not found under $RKNN_INCLUDE_DIR" >&2
  echo "Set RKNN_INCLUDE_DIR to the official RKNN SDK include directory." >&2
  exit 1
fi

if [ -z "${RKNN_LIB_DIR:-}" ]; then
  for candidate in /usr/lib/aarch64-linux-gnu /usr/lib /usr/local/lib; do
    if [ -e "$candidate/librknnrt.so" ]; then
      RKNN_LIB_DIR=$candidate
      break
    fi
  done
fi
if [ -z "${RKNN_LIB_DIR:-}" ] || [ ! -e "$RKNN_LIB_DIR/librknnrt.so" ]; then
  echo "librknnrt.so not found; set RKNN_LIB_DIR." >&2
  exit 1
fi

"$CXX" -O2 -std=c++14 -Wall -Wextra -Werror \
  -I"$HERE/../common" -I"$RKNN_INCLUDE_DIR" \
  "$HERE/nozcam_rockchip_daemon.cpp" "$HERE/../common/nozcam_postprocess.cpp" \
  -L"$RKNN_LIB_DIR" -lrknnrt -Wl,-z,noexecstack -o "$OUT"

echo "built: $OUT"
file "$OUT"
