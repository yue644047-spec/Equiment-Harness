"""生成演示用业务数据库（装备台账 / 履历 / 检修记录）。

对应设计报告（docs/11）3.7 数据库交互查询模块：
业务库由甲方提供，系统以**只读账号**访问，仅执行白名单查询模板。

用法：
    python tools/make_demo_business_db.py [--out demo_data/business.db] [--force]
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
DROP TABLE IF EXISTS equipment;
DROP TABLE IF EXISTS service_history;
DROP TABLE IF EXISTS maintenance;

CREATE TABLE equipment (
    equipment_no    TEXT PRIMARY KEY,
    model           TEXT NOT NULL,
    name            TEXT NOT NULL,
    unit            TEXT,
    commission_date TEXT,
    status          TEXT,
    runtime_hours   REAL
);

CREATE TABLE service_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_no TEXT NOT NULL,
    event_date   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    description  TEXT,
    operator     TEXT,
    doc_ref      TEXT
);

CREATE TABLE maintenance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_no  TEXT NOT NULL,
    maint_date    TEXT NOT NULL,
    item          TEXT,
    result        TEXT,
    next_due_date TEXT,
    maintainer    TEXT
);

CREATE INDEX idx_hist_eq ON service_history(equipment_no, event_date);
CREATE INDEX idx_maint_eq ON maintenance(equipment_no, maint_date);
"""

EQUIPMENT = [
    ("EQ-1001", "HYS-1", "某型液压举升系统", "某部一营", "2023-04-12", "在用", 1420.5),
    ("EQ-1002", "HYS-1", "某型液压举升系统", "某部一营", "2023-06-30", "在用", 1176.0),
    ("EQ-1003", "HYS-1", "某型液压举升系统", "某部二营", "2024-03-08", "在用", 642.5),
    ("EQ-1004", "HYS-1", "某型液压举升系统", "某部二营", "2024-09-19", "待修", 388.0),
    ("EQ-2001", "HYS-2", "某型液压举升系统（改进型）", "某部修理所", "2025-01-15", "在用", 210.0),
]

HISTORY = [
    ("EQ-1001", "2023-04-12", "列装", "完成列装验收与首次操作培训", "张建国", "HYS-CZ-002"),
    ("EQ-1001", "2024-05-20", "换件", "更换液压油滤芯与回油滤网", "李强", "HYS-WX-003"),
    ("EQ-1001", "2025-07-02", "检修", "年度检修，检测溢流阀开启压力 17.5 MPa，正常", "王海涛", "HYS-WX-003"),
    ("EQ-1002", "2024-06-11", "故障", "举升无力，排查为滤芯堵塞，更换滤芯后恢复", "李强", "HYS-GZ-004"),
    ("EQ-1002", "2025-06-18", "检修", "半年检，油液污染度检测合格", "王海涛", "HYS-WX-003"),
    ("EQ-1003", "2025-03-25", "故障", "平台缓慢下沉，排查为电磁换向阀内漏", "赵鹏", "HYS-GZ-004"),
    ("EQ-1003", "2025-09-10", "换件", "更换电磁换向阀与密封件", "赵鹏", "HYS-WX-003"),
    ("EQ-1004", "2025-11-02", "故障", "举升到位后无法保压，待修", "赵鹏", "HYS-GZ-004"),
    ("EQ-2001", "2025-01-15", "列装", "完成列装验收", "张建国", "HYS-CZ-002"),
]

MAINTENANCE = [
    ("EQ-1001", "2025-07-02", "年度检修", "合格", "2026-07-01", "王海涛"),
    ("EQ-1001", "2026-01-15", "半年检", "合格", "2026-07-15", "王海涛"),
    ("EQ-1002", "2025-06-18", "半年检", "合格", "2025-12-18", "王海涛"),
    ("EQ-1002", "2025-12-20", "半年检", "合格", "2026-06-20", "李强"),
    ("EQ-1003", "2025-09-10", "换件后复检", "合格", "2026-03-10", "赵鹏"),
    ("EQ-1004", "2025-10-05", "故障排查", "不合格（待更换阀件）", "2026-01-05", "赵鹏"),
    ("EQ-2001", "2025-07-20", "首次保养", "合格", "2026-01-20", "李强"),
]


def main(argv: Optional[List[str]] = None) -> int:
    """生成演示业务库。"""
    parser = argparse.ArgumentParser(description="生成演示用业务数据库")
    parser.add_argument("--out", default=str(ROOT / "demo_data" / "business.db"), help="输出路径")
    parser.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"[skip] 已存在：{out}（如需重建请加 --force）")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(str(out))
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO equipment VALUES (?,?,?,?,?,?,?)", EQUIPMENT)
    conn.executemany(
        "INSERT INTO service_history (equipment_no, event_date, event_type, description, operator, doc_ref)"
        " VALUES (?,?,?,?,?,?)",
        HISTORY,
    )
    conn.executemany(
        "INSERT INTO maintenance (equipment_no, maint_date, item, result, next_due_date, maintainer)"
        " VALUES (?,?,?,?,?,?)",
        MAINTENANCE,
    )
    conn.commit()
    # 只读账号（演示）：业务系统读账号在真实环境中由甲方提供
    conn.execute("CREATE TABLE IF NOT EXISTS _readme (note TEXT)")
    conn.execute(
        "INSERT INTO _readme VALUES ('演示用业务库：装备台账 / 履历 / 检修记录。系统只读访问，仅执行白名单查询模板。')"
    )
    conn.commit()
    conn.close()

    print(f"已生成演示业务库：{out}")
    print(f"  装备 {len(EQUIPMENT)} 台，履历 {len(HISTORY)} 条，检修记录 {len(MAINTENANCE)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
