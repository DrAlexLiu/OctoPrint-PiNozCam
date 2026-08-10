#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
WORKSPACE=${GITHUB_WORKSPACE:-$(cd "$HERE/../.." && pwd)}
ARTIFACT_ROOT=${ARTIFACT_ROOT:-"$WORKSPACE/runtime-artifacts"}

AWNN_SDK_DIR=${AWNN_SDK_DIR:?set AWNN_SDK_DIR to the AWNN/VIPLite vendor SDK directory}
VIP_INCLUDE_DIR=${VIP_INCLUDE_DIR:-/usr/local/include/viplite}
VIP_LIB_DIR=${VIP_LIB_DIR:-/usr/local/lib}

for tool in file gcc g++ sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "required build tool is unavailable: $tool" >&2
    exit 1
  fi
done

for source in awnn_lib.c awnn_lib.h awnn_internal.h awnn_quantize.c awnn_quantize.h; do
  if [ ! -f "$AWNN_SDK_DIR/$source" ]; then
    echo "missing vendor source: $AWNN_SDK_DIR/$source" >&2
    exit 1
  fi
done

if [ ! -f "$VIP_INCLUDE_DIR/vip_lite.h" ]; then
  echo "vip_lite.h not found under $VIP_INCLUDE_DIR" >&2
  exit 1
fi

if [ ! -e "$VIP_LIB_DIR/libNBGlinker.so" ] || [ ! -e "$VIP_LIB_DIR/libVIPhal.so" ]; then
  echo "libNBGlinker.so or libVIPhal.so not found under $VIP_LIB_DIR" >&2
  exit 1
fi

check_digest() {
  expected=$1
  path=$2
  echo "$expected  $path" | sha256sum --check --strict
}

# Pin the exact bytes fetched from the public SDK commit so an upstream change
# cannot silently alter a release artifact.
check_digest cd5887d5ed7a46235c7ad79366ad83ec0aa2368bc487848c57e1726b220b7e30 \
  "$AWNN_SDK_DIR/awnn_lib.c"
check_digest 5f59b1ee572ca445b9b8d2f9abbb96ce50b352c653cd59841c2c4feb78b02624 \
  "$AWNN_SDK_DIR/awnn_lib.h"
check_digest 9c1a653d70e46bc63a7b53f56a8d5b03e2a2b03e5f635e4484cc983f48363380 \
  "$AWNN_SDK_DIR/awnn_internal.h"
check_digest aa23ce3dd31785fc798ffeeca8f9aa473d327c9f7dca8fd062cfeede018c7f6f \
  "$AWNN_SDK_DIR/awnn_quantize.c"
check_digest 853d82b643d0392759cc3ab515b49509b5e036d1694233ac0ca4156233e72bc7 \
  "$AWNN_SDK_DIR/awnn_quantize.h"
check_digest 33fee8f985210cb34f14b7e2b0614096e6972718f87d8107497521d561d9df41 \
  "$VIP_INCLUDE_DIR/vip_lite.h"
check_digest 82f049b0ed0065dd4d443e37eeb1edfcbef24457c9c24e36170d64d5b748ca66 \
  "$VIP_LIB_DIR/libNBGlinker.so"
check_digest 3ed5357b26bd6c4fb68fbdc0b21d637227dda711d1fe67b9e28accdc48bb11f2 \
  "$VIP_LIB_DIR/libVIPhal.so"

mkdir -p "$ARTIFACT_ROOT/bin"

AWNN_SDK_DIR="$AWNN_SDK_DIR" \
VIP_INCLUDE_DIR="$VIP_INCLUDE_DIR" \
VIP_LIB_DIR="$VIP_LIB_DIR" \
OUT="$ARTIFACT_ROOT/bin/nozcam_daemon.awnn.aarch64" \
OBJ_DIR="$ARTIFACT_ROOT/.build" \
"$HERE/build.sh"

file "$ARTIFACT_ROOT/bin/nozcam_daemon.awnn.aarch64"
echo "runtime artifacts prepared in $ARTIFACT_ROOT"
