"""配置加载模块：极简 .env 解析器 + 默认值 + 凭据校验。

设计要点：
1. 只依赖 Python 标准库，不引入 python-dotenv；
2. 环境变量优先于 .env 文件中的同名项；
3. 绝不打印密钥明文，只输出掩码（例如 app-abcd****wxyz）；
4. 缺失凭据时给出可操作的中文提示，而不是抛裸异常。

凭据键（共四个）：
    DIFY_BASE_URL        Dify 部署地址，例如 https://cloud.dify.ai
    DIFY_APP_API_KEY     问答应用（Chatflow）的 Service API Key，形如 app-xxx
    DIFY_EVAL_API_KEY    评价工作流应用的 Service API Key，形如 app-xxx
    DIFY_KB_API_KEY      知识库 API Key，形如 dataset-xxx（可选）
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量定义

#: 本包根目录（eval_service/）
BASE_DIR: Path = Path(__file__).resolve().parent

#: Windows 下约定的 .env 默认路径
DEFAULT_ENV_FILE: Path = Path(r"C:\Users\Administrator\.env")

ENV_BASE_URL = "DIFY_BASE_URL"
ENV_APP_API_KEY = "DIFY_APP_API_KEY"
ENV_EVAL_API_KEY = "DIFY_EVAL_API_KEY"
ENV_KB_API_KEY = "DIFY_KB_API_KEY"

#: 允许通过环境变量覆盖的可选项
ENV_TIMEOUT = "DIFY_TIMEOUT"
ENV_MAX_RETRIES = "DIFY_MAX_RETRIES"
ENV_CONCURRENCY = "DIFY_CONCURRENCY"
ENV_DB_PATH = "EVAL_DB_PATH"
ENV_OUT_DIR = "EVAL_OUT_DIR"
ENV_ENV_FILE = "DIFY_ENV_FILE"  # 指向自定义 .env 路径

#: KEY=VALUE 行；支持开头的 export
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


# ---------------------------------------------------------------- .env 解析


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析极简格式的 .env 文件。

    支持的语法：
      * 空行与以 # 开头的整行注释被忽略；
      * KEY=VALUE，等号两侧空白会被裁剪；
      * 允许 export KEY=VALUE；
      * 值可以用单引号或双引号包裹，包裹时内部内容原样保留；
      * 未加引号的值中，" #" 之后的内容视为行内注释。

    :param path: .env 文件路径
    :return: 键值字典；文件不存在时返回空字典
    """
    result: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return result
    except OSError as exc:  # 权限等问题不应中断主流程
        print(f"[config] 读取 .env 失败：{path} -> {exc}")
        return result

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            hash_pos = value.find(" #")
            if hash_pos >= 0:
                value = value[:hash_pos].rstrip()
        result[key] = value
    return result


def mask_secret(value: str, keep_head: int = 6, keep_tail: int = 4) -> str:
    """对密钥做掩码，避免在日志/报表中泄露。

    :param value: 原始密钥
    :return: 掩码字符串；空值时返回 "(未配置)"
    """
    if not value:
        return "(未配置)"
    if len(value) <= keep_head + keep_tail:
        return value[:2] + "*" * max(len(value) - 2, 0)
    return f"{value[:keep_head]}****{value[-keep_tail:]}"


# ---------------------------------------------------------------- 配置对象


@dataclass
class Config:
    """运行时配置聚合对象。"""

    base_url: str = "https://cloud.dify.ai"
    app_api_key: str = ""
    eval_api_key: str = ""
    kb_api_key: str = ""

    timeout: int = 60
    max_retries: int = 3
    backoff_base: float = 0.8
    concurrency: int = 4

    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "eval.db")
    out_dir: Path = field(default_factory=lambda: BASE_DIR / "out")
    fixtures_dir: Path = field(default_factory=lambda: BASE_DIR / "tests" / "fixtures")

    mock: bool = False
    env_file: Optional[Path] = None
    env_loaded: Dict[str, str] = field(default_factory=dict)

    # -------------------------------------------------- 便捷属性
    @property
    def has_app_key(self) -> bool:
        """是否已配置问答应用 Key。"""
        return bool(self.app_api_key)

    @property
    def has_eval_key(self) -> bool:
        """是否已配置评价工作流 Key。"""
        return bool(self.eval_api_key)

    @property
    def has_kb_key(self) -> bool:
        """是否已配置知识库 Key。"""
        return bool(self.kb_api_key)

    @property
    def api_base(self) -> str:
        """去掉尾部斜杠的 BASE，便于拼接 /v1/... 路径。"""
        return self.base_url.rstrip("/")

    # -------------------------------------------------- 输出
    def summary_lines(self) -> List[str]:
        """返回可直接打印的配置摘要（密钥掩码）。"""
        env_from = str(self.env_file) if self.env_file else "(未加载)"
        return [
            f"  运行模式   : {'mock（离线，无网络）' if self.mock else 'live（真实 Dify API）'}",
            f"  BASE_URL   : {self.api_base}",
            f"  APP_KEY    : {mask_secret(self.app_api_key)}",
            f"  EVAL_KEY   : {mask_secret(self.eval_api_key)}",
            f"  KB_KEY     : {mask_secret(self.kb_api_key)}",
            f"  .env 文件  : {env_from}",
            f"  数据库     : {self.db_path}",
            f"  输出目录   : {self.out_dir}",
            f"  超时/重试  : {self.timeout}s / {self.max_retries} 次",
            f"  默认并发   : {self.concurrency}",
        ]

    def print_summary(self) -> None:
        """打印配置摘要。"""
        print("[config] 当前配置：")
        for line in self.summary_lines():
            print(line)

    # -------------------------------------------------- 校验
    def require(self, role: str) -> str:
        """校验并返回指定角色的 Key。

        :param role: app（问答应用）/ eval（评价工作流）/ kb（知识库）
        :return: 对应的 API Key
        :raises SystemExit: 缺失时打印可操作提示并退出
        """
        mapping = {
            "app": (self.app_api_key, ENV_APP_API_KEY, "问答应用（Chatflow）的 Service API Key"),
            "eval": (self.eval_api_key, ENV_EVAL_API_KEY, "评价工作流应用的 Service API Key"),
            "kb": (self.kb_api_key, ENV_KB_API_KEY, "知识库 API Key（dataset-xxx）"),
        }
        if role not in mapping:
            raise ValueError(f"未知的凭据角色：{role}")
        value, env_name, desc = mapping[role]
        if value:
            return value
        _print_missing_credential_hint(env_name, desc, self.env_file)
        raise SystemExit(2)

    def ensure_dirs(self) -> None:
        """确保数据库目录与输出目录存在。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)


def _print_missing_credential_hint(env_name: str, desc: str, env_file: Optional[Path]) -> None:
    """打印缺失凭据时的可操作提示。"""
    target = env_file or DEFAULT_ENV_FILE
    print("")
    print("=" * 68)
    print(f"[config] 缺少凭据：{env_name}（{desc}）")
    print("请任选一种方式补齐后重试：")
    print(f"  1) 在 {target} 中追加一行：")
    print(f"       {env_name}=<你的真实值>")
    print(f"  2) 或在当前 PowerShell 会话中临时设置：")
    print(f'       $env:{env_name}="<你的真实值>"')
    print("  3) 若只想离线体验完整流程，请加 --mock 参数（无需任何凭据）。")
    print("注意：切勿把真实密钥写入 .env.example 或提交到代码仓库。")
    print("=" * 68)


# ---------------------------------------------------------------- 加载入口


def _env_int(name: str, default: int) -> int:
    """从环境变量读取整数，非法值回退默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"[config] 环境变量 {name}={raw!r} 不是整数，已回退默认值 {default}")
        return default


def load_config(
    env_file: Optional[Path] = None,
    mock: bool = False,
    base_dir: Optional[Path] = None,
) -> Config:
    """加载配置：环境变量优先，其次 .env 文件，最后默认值。

    :param env_file: 指定的 .env 路径；None 时按 DIFY_ENV_FILE -> 默认路径 顺序查找
    :param mock: 是否启用离线 mock 模式
    :param base_dir: 覆盖包根目录（测试用）
    :return: Config 实例
    """
    root = base_dir or BASE_DIR

    # 1) 决定 .env 路径
    candidate: Optional[Path] = None
    if env_file is not None:
        candidate = Path(env_file)
    elif os.environ.get(ENV_ENV_FILE):
        candidate = Path(os.environ[ENV_ENV_FILE])
    elif DEFAULT_ENV_FILE.exists():
        candidate = DEFAULT_ENV_FILE

    file_values: Dict[str, str] = {}
    if candidate is not None:
        file_values = parse_env_file(candidate)
        if not candidate.exists():
            print(f"[config] 提示：指定的 .env 文件不存在：{candidate}")
        candidate = candidate if candidate.exists() else candidate

    def pick(key: str, default: str = "") -> str:
        """环境变量优先于 .env 文件。"""
        env_val = os.environ.get(key)
        if env_val is not None and env_val.strip() != "":
            return env_val.strip()
        return file_values.get(key, default).strip()

    cfg = Config(
        base_url=pick(ENV_BASE_URL, "https://cloud.dify.ai") or "https://cloud.dify.ai",
        app_api_key=pick(ENV_APP_API_KEY),
        eval_api_key=pick(ENV_EVAL_API_KEY),
        kb_api_key=pick(ENV_KB_API_KEY),
        timeout=_env_int(ENV_TIMEOUT, 60),
        max_retries=_env_int(ENV_MAX_RETRIES, 3),
        concurrency=_env_int(ENV_CONCURRENCY, 4),
        db_path=Path(pick(ENV_DB_PATH, str(root / "data" / "eval.db"))),
        out_dir=Path(pick(ENV_OUT_DIR, str(root / "out"))),
        fixtures_dir=root / "tests" / "fixtures",
        mock=mock,
        env_file=candidate,
        env_loaded=file_values,
    )
    return cfg
