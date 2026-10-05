#!/usr/bin/env python
"""一键演示脚本：mock 模式下跑通 collect -> judge -> report -> badcase -> regression。

用法::

    D:\\ProgramData\\anaconda3\\python.exe run_demo.py
    python run_demo.py --out out --quiet

产物默认落在 eval_service/out/。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli import run_demo  # noqa: E402


def main() -> int:
    """解析参数并执行演示流程。"""
    parser = argparse.ArgumentParser(
        prog="run_demo.py",
        description="Dify 问答质量评测服务演示（--mock 离线全流程）",
    )
    parser.add_argument("--out", default=None, help="产物目录（默认 eval_service/out/）")
    parser.add_argument("--db", default=None, help="演示数据库路径（默认 eval_service/data/demo.db）")
    parser.add_argument("--quiet", action="store_true", help="精简输出")
    args = parser.parse_args()
    return run_demo(out_dir=args.out, db_path=args.db, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
