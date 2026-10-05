# -*- coding: utf-8 -*-
"""Dify 接入自检工具（仅标准库）。

用法：
    python tools/check_env.py check       # 校验 .env 凭据与三个应用的连通性
    python tools/check_env.py apps        # 打印每个应用的信息与输入变量（核对契约字段名）
    python tools/check_env.py datasets    # 列出知识库及其 ID（用于替换 Chatflow 里的占位符）
    python tools/check_env.py checklist   # 打印导入与配置清单

凭据一律从 %USERPROFILE% 下的 .env 读取，脚本只显示掩码，绝不打印明文。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path.home() / ".env"
REQUIRED_VARS = ("DIFY_BASE_URL", "DIFY_APP_API_KEY", "DIFY_EVAL_API_KEY", "DIFY_KB_API_KEY")
TIMEOUT = 25

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"


def load_env(path: Path) -> dict:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，支持引号。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def mask(secret: str) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= 10:
        return secret[:2] + "*" * (len(secret) - 2)
    return f"{secret[:6]}...{secret[-4:]} (长度 {len(secret)})"


def normalize_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def http_get(url: str, api_key: str, timeout: int = TIMEOUT) -> tuple[int, dict | str]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "dify-satisfaction-eval/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, f"网络不可达: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return 0, f"请求异常: {exc}"


def explain_status(status: int) -> str:
    return {
        0: "无法连接，请检查 DIFY_BASE_URL 与网络/代理",
        401: "鉴权失败：API Key 错误、已删除或复制不完整",
        403: "无权限：密钥不属于该应用，或工作区权限受限",
        404: "接口不存在：BASE_URL 可能缺少或多了路径（应形如 https://cloud.dify.ai）",
        429: "触发限流，请稍后重试",
    }.get(status, "")


def probe_app(base: str, api_key: str, label: str, expect_mode: str | None = None) -> bool:
    if not api_key:
        print(f"{FAIL} {label}: 未配置 API Key")
        return False
    status, payload = http_get(f"{base}/v1/info", api_key)
    if status != 200:
        print(f"{FAIL} {label}: GET /v1/info 返回 {status} {explain_status(status)}")
        if isinstance(payload, str) and payload:
            print(f"        响应: {payload[:300]}")
        return False
    if not isinstance(payload, dict):
        print(f"{WARN} {label}: 响应格式异常 {str(payload)[:200]}")
        return False
    name = payload.get("name", "?")
    mode = payload.get("mode", "?")
    print(f"{OK} {label}: 应用「{name}」模式={mode} 密钥={mask(api_key)}")
    if expect_mode and mode != expect_mode:
        print(f"{WARN} {label}: 期望模式 {expect_mode}，实际 {mode}，请确认导入的是正确文件")
        return False
    return True


def probe_dataset(base: str, api_key: str) -> bool:
    if not api_key:
        print(f"{WARN} 知识库 API: 未配置 DIFY_KB_API_KEY（仅影响知识库自动构建功能）")
        return True
    status, payload = http_get(f"{base}/v1/datasets?page=1&limit=1", api_key)
    if status != 200:
        print(f"{FAIL} 知识库 API: GET /v1/datasets 返回 {status} {explain_status(status)}")
        if isinstance(payload, str) and payload:
            print(f"        响应: {payload[:300]}")
        return False
    total = payload.get("total", "?") if isinstance(payload, dict) else "?"
    print(f"{OK} 知识库 API: 连通，当前工作区知识库数量={total} 密钥={mask(api_key)}")
    return True


def cmd_check(env: dict) -> int:
    print("=" * 76)
    print("Dify 接入自检")
    print("=" * 76)
    print(f"{INFO} 凭据文件: {ENV_PATH} ({'存在' if ENV_PATH.exists() else '不存在'})")
    for var in REQUIRED_VARS:
        value = env.get(var, "")
        print(f"       {var:<20} {'已配置 ' + mask(value) if value else '(缺失)'}")

    base = normalize_base(env.get("DIFY_BASE_URL", ""))
    if not base:
        print(f"\n{FAIL} 缺少 DIFY_BASE_URL，无法继续。")
        print("       请在 PowerShell 中执行（输入隐藏）：")
        print('       $v = Read-Host "Dify 地址" -AsSecureString; '
              '$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($v); '
              '$s=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($p); '
              '[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p); '
              'Add-Content -Path "$HOME\\.env" -Value "DIFY_BASE_URL=$s"')
        return 1

    print(f"\n{INFO} 服务地址: {base}")
    results = [
        probe_app(base, env.get("DIFY_APP_API_KEY", ""), "问答应用 (Chatflow)", "advanced-chat"),
        probe_app(base, env.get("DIFY_EVAL_API_KEY", ""), "评价工作流", "workflow"),
        probe_dataset(base, env.get("DIFY_KB_API_KEY", "")),
    ]
    print()
    if all(results):
        print(f"{OK} 全部检查通过，可以执行：python eval_service/cli.py demo")
        return 0
    print(f"{FAIL} 存在未通过的检查项，请按上面的提示修正后重跑。")
    return 1


def cmd_apps(env: dict) -> int:
    base = normalize_base(env.get("DIFY_BASE_URL", ""))
    if not base:
        print(f"{FAIL} 缺少 DIFY_BASE_URL")
        return 1
    for label, key_var, expect in (
        ("问答应用 (Chatflow)", "DIFY_APP_API_KEY", "advanced-chat"),
        ("评价工作流", "DIFY_EVAL_API_KEY", "workflow"),
        ("语料标注工作流", "DIFY_EVAL_API_KEY", "workflow"),
    ):
        api_key = env.get(key_var, "")
        if not api_key:
            print(f"{FAIL} {label}: {key_var} 未配置")
            continue
        print("=" * 76)
        print(f"{label}  ({key_var})")
        print("=" * 76)
        status, info = http_get(f"{base}/v1/info", api_key)
        if status != 200:
            print(f"{FAIL} /v1/info -> {status} {explain_status(status)}")
            continue
        print(json.dumps(info, ensure_ascii=False, indent=2)[:1200])
        status, params = http_get(f"{base}/v1/parameters?user=probe", api_key)
        if status == 200 and isinstance(params, dict):
            form = params.get("user_input_form") or []
            if form:
                print("\n输入变量（核对是否与契约一致）：")
                for group in form:
                    for var_type, spec in group.items():
                        print(f"  - {spec.get('variable'):<18} type={var_type:<12} "
                              f"required={spec.get('required')} label={spec.get('label')}")
        else:
            print(f"{WARN} /v1/parameters -> {status}（workflow 应用可能不提供该接口，属正常）")
    return 0


def cmd_datasets(env: dict) -> int:
    base = normalize_base(env.get("DIFY_BASE_URL", ""))
    api_key = env.get("DIFY_KB_API_KEY", "")
    if not base or not api_key:
        print(f"{FAIL} 需要 DIFY_BASE_URL 与 DIFY_KB_API_KEY")
        return 1
    status, payload = http_get(f"{base}/v1/datasets?page=1&limit=100", api_key)
    if status != 200:
        print(f"{FAIL} 拉取知识库列表失败: {status} {explain_status(status)}")
        return 1
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        print(f"{WARN} 当前工作区还没有知识库，请先在 Dify 里创建并上传文档。")
        return 1
    print(f"共 {payload.get('total')} 个知识库：\n")
    print(f"{'知识库 ID':<38} {'名称':<28} {'文档数':<8} 索引方式")
    print("-" * 96)
    for row in rows:
        print(f"{row.get('id',''):<38} {str(row.get('name',''))[:26]:<28} "
              f"{row.get('document_count','?'):<8} {row.get('indexing_technique','')}")
    print("\n把上面某个 ID 填到 dsl/01-多模态智能问答-Chatflow.yml 里 knowledge-retrieval 节点的 dataset_ids，")
    print("或在 Dify 画布上点开「知识检索」节点重新选择知识库。")
    return 0


CHECKLIST = """
================================================================================
Dify 侧配置清单（按顺序做完即可跑通全流程）
================================================================================
[ ] 1. 工作区安装并授权 DeepSeek 插件（设置 → 模型供应商 → DeepSeek → 填 API Key）
       默认模型：deepseek-flash（provider = langgenius/deepseek/deepseek）
       评分节点可换成 deepseek-v4-pro 以提升评委质量
[ ] 2. 新建知识库 → 上传业务文档 → 选择索引方式（高质量）→ 等待索引完成
       记录知识库 ID（浏览器地址栏 /datasets/<uuid>/documents 中的 uuid）
[ ] 3. 工作室 → 创建应用 → 从 DSL 文件导入 → 选择 dsl/01-多模态智能问答-Chatflow.yml
[ ] 4. 打开导入后的 Chatflow，点开「知识检索」节点，把 dataset_ids 换成第 2 步的知识库
       （占位符是 00000000-0000-0000-0000-000000000000）
[ ] 5. 同法导入 dsl/02-满意度评价-Workflow.yml 与 dsl/03-语料标注-Workflow.yml
[ ] 6. 三个应用分别：访问 API → API 密钥 → 创建（形如 app-xxx，只显示一次，立即保存）
[ ] 7. 知识库：API → 创建密钥（形如 dataset-xxx）
[ ] 8. 把 4 个变量写入 %USERPROFILE%\\.env（务必用隐藏回显方式，不要贴到聊天里）：
       DIFY_BASE_URL / DIFY_APP_API_KEY / DIFY_EVAL_API_KEY / DIFY_KB_API_KEY
[ ] 9. 运行自检：python tools/check_env.py check
[ ] 10. 运行 demo：python eval_service/cli.py demo
[ ] 11. 在 Dify 里对三个应用分别点「运行一次」做冒烟测试：
        - 01：问一个知识库里有答案的问题（应正常作答并带引用）
        - 01：发送「#评价 4 建议补充XX说明」（应返回评价回执）
        - 02：填 question/answer 直接运行（应输出 csi 与 verdict）
        - 03：贴一段文档运行（应输出 jsonl 与 review_markdown）
[ ] 12. 发布应用 → 复制 WebApp 链接发给试用用户，开始采集真实满意度数据
================================================================================
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dify 接入自检工具")
    parser.add_argument("command", choices=["check", "apps", "datasets", "checklist"], help="要执行的检查")
    parser.add_argument("--env", default=str(ENV_PATH), help=".env 路径，默认 %%USERPROFILE%%\\.env")
    args = parser.parse_args(argv)

    env = load_env(Path(args.env))
    if args.command == "check":
        return cmd_check(env)
    if args.command == "apps":
        return cmd_apps(env)
    if args.command == "datasets":
        return cmd_datasets(env)
    print(CHECKLIST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
