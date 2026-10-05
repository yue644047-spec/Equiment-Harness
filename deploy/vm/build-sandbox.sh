#!/bin/bash
# =============================================================================
#  编译 Dify 代码沙箱（备选路径）
#  为什么要在目标机本地编译：官方沙箱二进制需要 glibc >= 2.34，
#  老系统上跑不了；本地编译可得到与本机 glibc 完全匹配的二进制。
#  用法：bash build-sandbox.sh <WORK目录> <沙箱安装目录> <沙箱用Python的bin目录>
# =============================================================================
set -euo pipefail

WORK="$1"
OUT="$2"
PYBIN="$3"

log() { echo "    [sandbox-build] $*"; }
die() { echo "    [sandbox-build][FAIL] $*" >&2; exit 1; }

GOBIN="${WORK}/toolchain/go/bin/go"
[ -x "${GOBIN}" ] || die "未找到 Go 工具链：${GOBIN}"

# 选择 C 编译器：优先系统 gcc，否则用离线包自带的 zig
CC_BIN="${CC:-}"
ZIGBIN="$(find "${WORK}/toolchain" -maxdepth 3 -type f -name zig 2>/dev/null | head -1 || true)"
if [ -z "${CC_BIN}" ]; then
  if command -v gcc >/dev/null 2>&1; then
    CC_BIN="gcc"
  elif [ -n "${ZIGBIN}" ]; then
    chmod +x "${ZIGBIN}" 2>/dev/null || true
    CC_BIN="${ZIGBIN} cc"
  else
    die "既没有 gcc 也没有 zig，无法编译"
  fi
fi
log "C 编译器: ${CC_BIN}"

# ---------- 1. libseccomp（静态库）----------
if [ ! -f "${WORK}/seccomp/lib/libseccomp.a" ]; then
  log "编译 libseccomp"
  cd "${WORK}"
  [ -d libseccomp-src ] || { mkdir -p libseccomp-src && tar -xzf libseccomp-*.tar.gz -C libseccomp-src --strip-components=1; }
  cd libseccomp-src
  make distclean >/dev/null 2>&1 || true
  CC="${CC_BIN}" ./configure --prefix="${WORK}/seccomp" --disable-shared --enable-static \
    >"${WORK}/libseccomp-configure.log" 2>&1 || die "libseccomp configure 失败，见 ${WORK}/libseccomp-configure.log"
  make -j"$(nproc)" >"${WORK}/libseccomp-make.log" 2>&1 || {
    log "make 失败，尝试用 gperf 重新生成源码表"
    die "libseccomp 编译失败，见 ${WORK}/libseccomp-make.log"
  }
  make install >/dev/null 2>&1 || die "libseccomp 安装失败"
fi
log "libseccomp 就绪: ${WORK}/seccomp/lib/libseccomp.a"

# ---------- 2. 沙箱本体 ----------
export CGO_ENABLED=1
export GOFLAGS=-mod=vendor
export CC="${CC_BIN}"
export CGO_CFLAGS="-I${WORK}/seccomp/include"
export CGO_LDFLAGS="-L${WORK}/seccomp/lib -lseccomp"
export GOPATH="${WORK}/gopath"
export GOCACHE="${WORK}/gocache"
export GOOS=linux GOARCH=amd64

cd "${WORK}/dify-sandbox" || die "找不到沙箱源码目录"

log "编译 python 运行库（c-shared）"
"${GOBIN}" build -o internal/core/runner/python/python.so -buildmode=c-shared -ldflags="-s -w" cmd/lib/python/main.go \
  || die "python.so 编译失败"

log "编译 nodejs 运行库（c-shared）"
"${GOBIN}" build -o internal/core/runner/nodejs/nodejs.so -buildmode=c-shared -ldflags="-s -w" cmd/lib/nodejs/main.go \
  || die "nodejs.so 编译失败"

log "编译主程序（会把上面两个 .so 内嵌进去）"
"${GOBIN}" build -o "${OUT}/main" -ldflags="-s -w" cmd/server/main.go || die "main 编译失败"

chmod +x "${OUT}/main"
log "完成: ${OUT}/main ($(du -h "${OUT}/main" | cut -f1))"
