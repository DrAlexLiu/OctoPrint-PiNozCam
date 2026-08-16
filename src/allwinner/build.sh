#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
AWNN_SDK_DIR=${AWNN_SDK_DIR:?set AWNN_SDK_DIR to the vendor libawnn_viplite directory}
VIP_INCLUDE_DIR=${VIP_INCLUDE_DIR:-/usr/local/include/viplite}
VIP_LIB_DIR=${VIP_LIB_DIR:-/usr/local/lib}
VIP_RUNTIME_LIB_DIR=${VIP_RUNTIME_LIB_DIR:-$VIP_LIB_DIR}
# VIPLite renamed its runtime between major versions: v2.0 is
# libNBGlinker/libVIPhal (A733), v1.13 is libVIPlite/libVIPuser (T527). The
# awnn_* API above them is identical, so only the link names differ and one
# script serves both stacks.
VIP_LIBS=${VIP_LIBS:-NBGlinker VIPhal}
CC=${CC:-gcc}
CXX=${CXX:-g++}
OUT=${OUT:-$HERE/nozcam_allwinner_daemon}
OBJ_DIR=${OBJ_DIR:-$HERE/.build}

for source in awnn_lib.c awnn_lib.h awnn_internal.h \
              awnn_quantize.c awnn_quantize.h; do
  if [ ! -f "$AWNN_SDK_DIR/$source" ]; then
    echo "missing vendor source: $AWNN_SDK_DIR/$source" >&2
    exit 1
  fi
done
if [ ! -f "$VIP_INCLUDE_DIR/vip_lite.h" ]; then
  echo "vip_lite.h not found under $VIP_INCLUDE_DIR" >&2
  exit 1
fi
for lib in $VIP_LIBS; do
  if [ ! -e "$VIP_LIB_DIR/lib$lib.so" ]; then
    echo "lib$lib.so not found under $VIP_LIB_DIR" >&2
    exit 1
  fi
done

COMMON_INCLUDES=(-I"$HERE/include" -I"$HERE/../common" \
                 -isystem "$AWNN_SDK_DIR" -isystem "$VIP_INCLUDE_DIR")

mkdir -p "$OBJ_DIR"
"$CXX" -O2 -std=c++14 -Wall -Wextra -Werror \
  -Wno-unused-function -Wno-format-truncation "${COMMON_INCLUDES[@]}" \
  -c "$HERE/nozcam_allwinner_daemon.cpp" -o "$OBJ_DIR/daemon.o"
"$CXX" -O2 -std=c++14 -Wall -Wextra -Werror -Wno-format-truncation \
  -I"$HERE/../common" -c "$HERE/../common/nozcam_postprocess.cpp" \
  -o "$OBJ_DIR/postprocess.o"
# The unmodified vendor internal header defines tentative timing arrays in
# each includer. Compile awnn_lib.c as C with -fcommon so the daemon's C++
# definition resolves them without modifying or redistributing that header.
"$CC" -O2 -std=c11 -fcommon -Wall -Wno-unused-but-set-variable \
  "${COMMON_INCLUDES[@]}" \
  -c "$AWNN_SDK_DIR/awnn_lib.c" -o "$OBJ_DIR/awnn_lib.o"
# awnn_quantize.h lacks extern "C" guards; the daemon supplies the guard.
"$CC" -O2 -std=c11 -Wall "${COMMON_INCLUDES[@]}" \
  -c "$AWNN_SDK_DIR/awnn_quantize.c" -o "$OBJ_DIR/awnn_quantize.o"

LINK_LIBS=()
for lib in $VIP_LIBS; do
  LINK_LIBS+=("-l$lib")
done

"$CXX" "$OBJ_DIR/daemon.o" "$OBJ_DIR/postprocess.o" \
  "$OBJ_DIR/awnn_lib.o" "$OBJ_DIR/awnn_quantize.o" \
  -L"$VIP_LIB_DIR" -Wl,-rpath,"$VIP_RUNTIME_LIB_DIR" \
  "${LINK_LIBS[@]}" -lpthread -lm -Wl,-z,noexecstack -o "$OUT"

echo "built: $OUT"
file "$OUT"
