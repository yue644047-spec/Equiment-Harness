#!/bin/sh
# Dify 内网无 Docker 部署 - 目标机环境探测脚本
# 用法：把本文件拷到目标 VM，执行 sh vm-probe.sh > probe.txt 2>&1
# 或直接把下面整段粘贴到 VM 终端（无网也能跑，纯只读检查）
echo "=========== 1. OS ==========="
cat /etc/os-release 2>/dev/null || cat /etc/redhat-release 2>/dev/null
echo "kernel : $(uname -srm)"
echo "arch   : $(uname -m)"
echo "glibc  : $(ldd --version 2>/dev/null | head -1)"
echo "systemd: $(ps -p 1 -o comm= 2>/dev/null)"
echo "selinux: $(getenforce 2>/dev/null || echo n/a)"
echo
echo "=========== 2. 硬件 ==========="
echo "cpu cores : $(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo)"
free -h 2>/dev/null | head -2
echo "--- disk ---"
df -h / /opt /data /home 2>/dev/null | sort -u
echo
echo "=========== 3. 权限与包管理 ==========="
id
for c in dnf yum rpm apt yumdownloader createrepo; do printf "%-14s %s\n" "$c" "$(command -v $c || echo MISSING)"; done
echo
echo "=========== 4. 编译与运行时工具 ==========="
for c in gcc gcc-c++ make pkg-config go python3 python3.11 python3.12 node nodejs npm redis-server psql nginx tar unzip curl openssl git jq; do
  printf "%-14s %s\n" "$c" "$(command -v $c 2>/dev/null || echo MISSING)"
done
echo "python3 -V : $(python3 -V 2>&1)"
echo "libseccomp : $(ls /usr/lib64/libseccomp.so* /usr/lib/libseccomp.so* 2>/dev/null | tr '\n' ' ' || echo MISSING)"
echo
echo "=========== 5. 端口占用 ==========="
ss -lntp 2>/dev/null | head -25 || netstat -lntp 2>/dev/null | head -25
echo
echo "=========== 6. 网络 ==========="
ip -4 addr show 2>/dev/null | grep -E "inet " | sed 's/^ *//'
echo "默认网关: $(ip route 2>/dev/null | grep default)"
echo "DNS: $(cat /etc/resolv.conf 2>/dev/null | grep -v '^#' | head -3 | tr '\n' ' ')"
echo "防火墙: $(systemctl is-active firewalld 2>/dev/null)"
echo
echo "=========== 7. 已有中间件痕迹 ==========="
ls -d /usr/pgsql* /var/lib/pgsql* /var/lib/mysql /var/lib/redis /opt/* 2>/dev/null | head -20
echo
echo "=========== 8. 时间与时区 ==========="
date; timedatectl 2>/dev/null | head -4
echo "=========== DONE ==========="
