#!/bin/bash
# 代码沙箱启动包装。
# 背景：官方沙箱二进制要求 glibc >= 2.34。
#   - 目标机 glibc 达标  -> 直接运行
#   - 目标机 glibc 较低  -> 用随包携带的、与官方二进制同源的 glibc 加载器运行
# 支持 x86_64 与 aarch64（飞腾/鲲鹏）两种架构，加载器按实际存在的文件自动选择。
set -e
SD="${SANDBOX_DIR:-/opt/dify/sandbox}"

GLV="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0.0)"
MAJ="${GLV%%.*}"; MIN="${GLV##*.}"

if [ "${MAJ:-0}" -gt 2 ] || { [ "${MAJ:-0}" -eq 2 ] && [ "${MIN:-0}" -ge 34 ]; }; then
  exec "${SD}/main"
fi

# 按架构挑选随包加载器（x86_64 -> ld-linux-x86-64.so.2；aarch64 -> ld-linux-aarch64.so.1）
LOADER=""
for cand in "${SD}/glibc/ld-linux-x86-64.so.2" "${SD}/glibc/ld-linux-aarch64.so.1"; do
  [ -x "${cand}" ] && LOADER="${cand}" && break
done

if [ -n "${LOADER}" ]; then
  echo "[sandbox] 本机 glibc ${GLV} < 2.34，改用随包 glibc 运行（$(basename "${LOADER}")）"
  exec "${LOADER}" --library-path "${SD}/glibc" "${SD}/main"
fi

echo "[sandbox][FATAL] 本机 glibc ${GLV} 过低且离线包缺少随包 glibc（架构 $(uname -m)），请改用本地编译路径" >&2
exit 1
