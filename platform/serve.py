"""一键启动：装备数据治理（完整产品）。

  python platform/serve.py            # 默认 http://127.0.0.1:8095
  python platform/serve.py --port 9000

启动后同一地址既是前端门户，也是后端 API；两个引擎内核（RAGFlow 源码 / Dify graphon）
由产品服务自动作为子进程拉起，无需手工准备任何上游服务。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "product"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def load_env() -> dict:
    values = {}
    env_file = Path(os.environ.get("EQUIPMENT_ENV", HERE / "config" / "equipment.env"))
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="装备数据治理 · 产品服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    env = load_env()
    host = args.host or env.get("PRODUCT_HOST", "127.0.0.1")
    port = args.port or int(env.get("PRODUCT_PORT", "8095"))

    import uvicorn

    print("=" * 68)
    print("  装备数据治理 · 产品服务")
    print("=" * 68)
    print(f"  地址        : http://{host}:{port}")
    print(f"  统一配置    : {HERE / 'config' / 'equipment.env'}")
    print(f"  数据目录    : {env.get('EQUIPMENT_DATA', HERE / 'data')}")
    print(f"  知识内核    : RAGFlow 源码  {env.get('KNOWLEDGE_SOURCE', '')}")
    print(f"  编排内核    : Dify graphon {env.get('ORCHESTRATION_PYTHON', '')}")
    print(f"  前端产物    : {env.get('PORTAL_DIST', '')}")
    print("=" * 68)

    uvicorn.run("product.server:app", host=host, port=port, log_level="info", app_dir=str(HERE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
