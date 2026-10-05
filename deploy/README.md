# Dify 内网私有化部署（不使用 Docker）

本目录是**完整的交付物**：从联网构建机打包，到内网 VM 一键安装。

## 交付物结构

```
deploy/
├─ 01-架构与部署方案.md      ← 先读这个：方案、组件清单、为什么这么做
├─ 02-VM部署手册.md          ← VM 上照着做即可（逐步命令）
├─ 03-配置填写清单.md        ← ★ 你唯一需要动手填的地方
├─ 04-验收与排障.md          ← 验收清单 + 故障速查表
├─ vm-probe.sh               ← VM 环境探测脚本（第一步先跑它）
├─ config/
│  └─ dify.env.template      ← ★ 参数填写表（复制成 dify.env 后填写）
├─ build/                    ← 在联网 Windows 构建机上运行
│  ├─ build-all.ps1          ← 一键生成完整离线包
│  ├─ oci-pull.ps1           ← 免 Docker 从镜像仓库取官方产物
│  ├─ fetch-plugins.ps1      ← 从插件市场下载 .difypkg
│  ├─ elf-inspect.mjs        ← ELF 兼容性检查（验证能否在目标机跑）
│  └─ wheel-check.mjs        ← Python 依赖 wheel 预检
└─ vm/                       ← 拷进内网 VM 运行
   ├─ install.sh             ← 一键安装（536 行，含全部步骤）
   ├─ verify.sh              ← 部署后自检（24 项检查）
   ├─ build-sandbox.sh       ← 沙箱本地编译（备用路径）
   ├─ web-start.sh           ← 前端启动器
   ├─ sandbox-start.sh       ← 沙箱启动器（自动适配 glibc）
   ├─ systemd/               ← 6 个 systemd 单元
   └─ nginx/                 ← 反向代理配置
```

## 三步走

**第一步（现在）**：在 VM 上跑探测脚本，确认系统合格
```bash
bash vm-probe.sh
```
> 关键判定：`ldd --version` 必须 **≥ 2.28**。CentOS 7 是 2.17，**不支持**。

**第二步**：在构建机生成离线包（**按目标 CPU 架构选一个**）

| 目标 CPU | 构建命令 | 产物目录 | 体积 |
| --- | --- | --- | --- |
| **飞腾 / 鲲鹏（ARM64）** ★ 当前目标 | `powershell -ExecutionPolicy Bypass -File build\build-arm64.ps1` | `D:\app\dify\deploy-bundle-arm64` | 1231 MB |
| 兆芯 / 海光（x86_64） | `powershell -ExecutionPolicy Bypass -File build\build-all.ps1` | `D:\app\dify\deploy-bundle` | 1325 MB |

> ARM64 构建脚本复用 x86_64 包中架构无关的部分（数据库源码、沙箱源码、插件、脚本），
> 只重建架构相关的组件：便携 Python、依赖树、前端运行时、工具链、插件守护进程、沙箱二进制。
> 增量重建约 10 分钟；完全从零约 40 分钟。

产物目录整个拷进 U 盘。包内 `ARCH.txt` 标明目标架构，安装脚本会自动比对，
**拿错架构的包会直接报错终止**，不会装到一半才失败。

**第三步**：内网 VM 上安装
```bash
cp -a /mnt/usb/deploy-bundle /opt/dify-bundle
cd /opt/dify-bundle
cp config/dify.env.template config/dify.env && vi config/dify.env   # 填 15 项
sudo bash vm/install.sh
sudo bash vm/verify.sh
```

## 本方案的核心结论（都有实测证据）

| 结论 | 证据 |
| --- | --- |
| **目标机必须 glibc ≥ 2.28** | Dify 依赖里的 `onnxruntime` 无 manylinux_2_17 wheel；`bcrypt` 的 .so 要求 GLIBC_2.28（实测 456 个 ELF 文件） |
| Web 前端可完全脱离 glibc | 官方镜像基于 Alpine，node 为 musl 构建，实测依赖仅 musl 加载器 + libstdc++ |
| plugin-daemon 可静态编译 | 纯 Go 无 CGO，实测交叉编译出零依赖静态二进制 |
| Python 运行时兼容性最好 | python-build-standalone 实测仅要求 GLIBC_2.17 |
| 沙箱需特殊处理 | 官方沙箱二进制要求 GLIBC_2.34；已备"随包 glibc"与"本地编译"两条路径 |
| DeepSeek 插件支持自定义网关 | 其 provider 定义中含 `endpoint_url` 字段，现有 DSL 无需改动 |
| Embedding 的 Base URL 不能带 /v1 | 该插件对非对话模型会自动追加 `/v1`，填错会导致 `/v1/v1` 404 |
