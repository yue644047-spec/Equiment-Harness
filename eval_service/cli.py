#!/usr/bin/env python
"""Dify 问答质量评测服务 —— 统一命令行入口。

子命令：
    init-db     初始化 SQLite 数据库（建表）
    collect     从 Dify Service API 拉取会话 / 消息 / 反馈
    judge       调用满意度评价工作流做多维评分
    report      生成 Markdown / HTML / CSV（可选 xlsx）报表
    badcase     挖掘 badcase 并输出知识补全与提示词优化建议
    attribute   归因前置：判定问题出在资料还是系统，产出文档缺陷清单
    scorecard   生成文档质量记分卡（DQI）与缺陷整改单
    grounding   生成后可核验性校验（答案里的数值/编号/版本/条款是否被命中片段支撑）
    dashboard   启动文档质量治理看板（Windows 本地版，浏览器访问）
    regression  黄金集回归评测（升级前后对比）
    kb-build    本地文档批量建知识库 + 索引状态跟踪
    demo        一键跑通 mock 全流程

示例：
    python cli.py init-db
    python cli.py collect --mock --limit 50
    python cli.py judge --mock --concurrency 4
    python cli.py report --period week --format md,html,csv --out out/
    python cli.py badcase --min-severity P1
    python cli.py attribute --only-badcase
    python cli.py scorecard --format md,csv
    python cli.py dashboard --open            # 治理看板（本机浏览器打开）
    python cli.py regression --golden golden_set.jsonl --baseline out/baseline.json --save-baseline
    python cli.py kb-build --source ../samples --dataset-id ds-xxx --dry-run
    python cli.py demo
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 保证以脚本方式运行时也能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from attribution import DEFECT_ACTION, attribute_evaluation, build_defect  # noqa: E402
from badcase import mine_badcases  # noqa: E402
from collector import Collector  # noqa: E402
from config import BASE_DIR, Config, load_config  # noqa: E402
from corpus import load_corpus  # noqa: E402
from dify_client import DifyClient, DifyError  # noqa: E402
from dashboard import DEFAULT_PORT, run_dashboard  # noqa: E402
from doc_score import build_scorecard  # noqa: E402
from judge import Judge  # noqa: E402
from kb_build import DEFAULT_SUFFIXES, KbBuilder  # noqa: E402
from regression import RegressionRunner  # noqa: E402
from report import ReportBuilder  # noqa: E402
from store import Store  # noqa: E402

DEFAULT_GOLDEN = BASE_DIR / "golden_set.jsonl"
DEFAULT_BASELINE = "out/baseline.json"

#: 默认语料目录（治理度量用：条款清单与台账现行版本）
DEFAULT_SAMPLES = BASE_DIR.parent / "samples"


# ---------------------------------------------------------------- 工具


def parse_since(value: Optional[str]) -> Optional[int]:
    """把 --since YYYY-MM-DD 解析成 Unix 秒（本地时区零点）。

    :raises SystemExit: 格式非法
    """
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    print(f"[cli] --since 格式非法：{value}（期望 YYYY-MM-DD）")
    raise SystemExit(2)


#: 周期关键字的别名（兼容中文与单字母写法）
PERIOD_ALIASES = {
    "day": "day", "日": "day", "天": "day", "d": "day", "daily": "day", "today": "day", "1d": "day",
    "week": "week", "周": "week", "星期": "week", "w": "week", "weekly": "week", "7d": "week",
    "month": "month", "月": "month", "m": "month", "monthly": "month", "30d": "month",
    "all": "all", "全部": "all", "所有": "all", "none": "all", "": "all",
}


def normalize_period(value: Optional[str]) -> str:
    """把 --period 的输入规范化为 day/week/month/all。

    :param value: 用户输入，例如 day / 周 / week
    :return: day / week / month / all
    :raises SystemExit: 无法识别的取值
    """
    if not value:
        return "all"
    key = str(value).strip().lower()
    if key in PERIOD_ALIASES:
        return PERIOD_ALIASES[key]
    print(f"[cli] --period 取值非法：{value}（可用：day/week/month/all，或 日/周/月/全部）")
    raise SystemExit(2)


def resolve_path(value: str, base: Optional[Path] = None) -> Path:
    """把用户传入的相对路径解析为绝对路径（相对当前工作目录）。"""
    path = Path(value)
    if path.is_absolute():
        return path
    return (base or Path.cwd()) / path


def open_store(cfg: Config, auto_init: bool = True) -> Store:
    """打开数据库（必要时自动建表）。"""
    store = Store(cfg.db_path)
    if auto_init:
        store.init_schema()
    return store


def print_table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> None:
    """打印等宽文本表格（纯标准库实现）。"""
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


# ---------------------------------------------------------------- 子命令实现


def cmd_init_db(args: argparse.Namespace) -> int:
    """初始化数据库并打印表结构概览。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    store = open_store(cfg)
    stats = store.stats_overview()
    print(f"[cli] 数据库已初始化：{cfg.db_path}")
    print_table([[k, v] for k, v in stats.items()], ["表名", "行数"])
    store.close()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """采集会话 / 消息 / 反馈。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    cfg.print_summary()
    store = open_store(cfg)
    client = DifyClient.from_config(cfg, "app")
    collector = Collector(client, store, cfg)
    stats = collector.collect(limit=args.limit, since_ts=parse_since(args.since), user=args.user)
    print("[cli] 采集完成：")
    print_table(
        [[k, v] for k, v in stats.as_dict().items() if k != "errors"],
        ["指标", "数值"],
    )
    if stats.errors:
        print("[cli] 错误明细：")
        for err in stats.errors[:10]:
            print(f"  - {err}")
    store.close()
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    """调用评价工作流批量评分。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    store = open_store(cfg)
    client = DifyClient.from_config(cfg, "eval")
    judge = Judge(client, store, cfg, concurrency=args.concurrency)
    stats = judge.run(limit=args.limit, include_receipt_only=args.include_receipt_only)
    print("[cli] 评分完成：")
    print_table([[k, v] for k, v in stats.as_dict().items() if k != "errors"], ["指标", "数值"])
    if stats.errors:
        print("[cli] 失败明细：")
        for err in stats.errors[:10]:
            print(f"  - {err}")
    store.close()
    return 0 if stats.failed == 0 else 1


def cmd_report(args: argparse.Namespace) -> int:
    """生成报表。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    out_dir = resolve_path(args.out) if args.out else cfg.out_dir
    store = open_store(cfg)
    formats = [f.strip() for f in (args.format or "md,html,csv").split(",") if f.strip()]
    builder = ReportBuilder(store, out_dir)
    outputs = builder.generate(period=normalize_period(args.period), formats=formats)
    print("[cli] 报表生成完成：")
    print_table([[k.upper(), str(v)] for k, v in outputs.items()], ["格式", "路径"])
    store.close()
    return 0


def cmd_badcase(args: argparse.Namespace) -> int:
    """挖掘 badcase。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    out_dir = resolve_path(args.out) if args.out else cfg.out_dir
    store = open_store(cfg)
    result = mine_badcases(store, out_dir, min_severity=args.min_severity)
    items: List[Dict[str, Any]] = result["items"]
    if items:
        print("[cli] badcase 清单（前 20 条）：")
        print_table(
            [[i["severity"], i["message_id"], i.get("csi"), i.get("annotation_label"), str(i.get("question"))[:28]]
             for i in items[:20]],
            ["严重度", "消息ID", "CSI", "标签", "问题"],
        )
    gaps = result["gaps"][:5]
    if gaps:
        print("[cli] 知识补全清单（Top5）：")
        for kw, cnt, _rel in gaps:
            print(f"  - {kw}（命中 {cnt} 次）")
    store.close()
    return 0


def cmd_attribute(args: argparse.Namespace) -> int:
    """归因前置：判定低分回答的责任在资料还是系统，产出文档缺陷清单。

    纪律（docs/10 §4）：只有「检索到了、忠实引用了、却仍然答不好」才计入文档缺陷；
    系统侧问题（检索 / 分块 / 提示词 / 模型）单独统计，不得记到资料头上。
    """
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    samples = resolve_path(args.samples) if args.samples else DEFAULT_SAMPLES
    store = open_store(cfg)

    corpus = load_corpus(samples)
    print("[cli] 语料索引：")
    for line in corpus.summary_lines():
        print(line)

    rows = store.fetch_evaluations(only_badcase=bool(args.only_badcase))
    if not rows:
        print("[cli] 没有可归因的评价记录，请先执行 collect 与 judge。")
        store.close()
        return 0

    resources_map = store.fetch_resources_map()
    results = [
        attribute_evaluation(row, resources_map.get(str(row.get("message_id"))), corpus)
        for row in rows
    ]
    updated = store.update_attribution(results)

    defect_items = [
        build_defect(item, row, DEFECT_ACTION)
        for item, row in zip(results, rows)
        if item.get("blame") in ("doc", "corpus")
    ]
    n_defects = store.replace_defects(defect_items)
    n_doc_defects = sum(1 for i in defect_items if i.get("blame") == "doc")

    blame_counter: Dict[str, int] = {}
    for item in results:
        key = str(item.get("blame") or "none")
        blame_counter[key] = blame_counter.get(key, 0) + 1
    type_counter: Dict[str, int] = {}
    for item in defect_items:
        key = str(item.get("defect_label") or item.get("defect_type"))
        type_counter[key] = type_counter.get(key, 0) + 1
    doc_counter: Dict[str, int] = {}
    for item in defect_items:
        key = str(item.get("doc_no"))
        doc_counter[key] = doc_counter.get(key, 0) + 1

    print(
        f"\n[cli] 归因完成：评价 {len(rows)} 条，写回 {updated} 条；"
        f"文档缺陷 {n_doc_defects} 条 + 覆盖缺口 {n_defects - n_doc_defects} 条（按 资料+条款+类型 去重）"
    )
    print_table(
        [
            ["文档缺陷（可定位到某份资料）", blame_counter.get("doc", 0)],
            ["覆盖缺口（资料整体，无片段可定位）", blame_counter.get("corpus", 0)],
            ["系统侧问题（不计入资料）", blame_counter.get("system", 0)],
            ["无需归因（未见异常）", blame_counter.get("none", 0)],
        ],
        ["归因对象", "条数"],
    )
    if type_counter:
        print("[cli] 缺陷类型分布（Top10）：")
        print_table(
            [[k, v] for k, v in sorted(type_counter.items(), key=lambda kv: -kv[1])[:10]],
            ["缺陷类型", "条数"],
        )
    if doc_counter:
        print("[cli] 缺陷按资料分布：")
        print_table(
            [[k, v] for k, v in sorted(doc_counter.items(), key=lambda kv: -kv[1])],
            ["资料编号", "缺陷数"],
        )
        print("[cli] 提示：P0 / P1 类文档缺陷需人工确认后再派单（docs/10 §4.2）。")
    else:
        print("[cli] 本期未发现文档侧缺陷（或样本不足）。")
    store.close()
    return 0


def cmd_grounding(args: argparse.Namespace) -> int:
    """重算「生成后可核验性」：答案中的事实性 token 是否被命中片段支撑。

    确定性校验，不调用大模型；可对历史评价批量回填（v1 只度量、不参与判定）。
    """
    from grounding import check_grounding, summarize  # noqa: PLC0415

    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    store = open_store(cfg)
    sql = "SELECT message_id, answer, contexts, grounding_level FROM evaluations WHERE 1=1"
    params: List[Any] = []
    if args.only_unchecked:
        sql += " AND (grounding_level IS NULL OR grounding_level = '')"
    rows = [dict(r) for r in store.conn.execute(sql, params).fetchall()]

    levels: Dict[str, int] = {}
    ungrounded_values: Dict[str, int] = {}
    for row in rows:
        result = check_grounding(row.get("answer") or "", row.get("contexts") or "")
        store.set_grounding(row["message_id"], result["rate"], result["level"], result["ungrounded"])
        levels[result["level"]] = levels.get(result["level"], 0) + 1
        for item in result["ungrounded"]:
            key = f"{item['kind']}:{item['value']}"
            ungrounded_values[key] = ungrounded_values.get(key, 0) + 1

    print(f"[grounding] 已校验 {len(rows)} 条评价（确定性比对，未调用大模型）")
    print_table(
        [[level, str(count), summarize(level)] for level, count in sorted(levels.items())],
        ["结果", "条数", "说明"],
    )
    if ungrounded_values:
        top = sorted(ungrounded_values.items(), key=lambda kv: -kv[1])[:10]
        print("[grounding] 未支撑项 Top10（答案里出现、但命中片段里没有）：")
        print_table([[k, str(v)] for k, v in top], ["未支撑项", "次数"])
    store.close()
    return 0


def cmd_scorecard(args: argparse.Namespace) -> int:
    """生成文档质量记分卡与缺陷整改单。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    out_dir = resolve_path(args.out) if args.out else cfg.out_dir
    samples = resolve_path(args.samples) if args.samples else DEFAULT_SAMPLES
    formats = [x.strip() for x in (args.format or "md,csv").split(",") if x.strip()]

    store = open_store(cfg)
    corpus = load_corpus(samples)
    rows = store.fetch_evaluations()
    if not rows:
        print("[cli] 数据库里还没有评价记录，请先执行 collect / judge。")
        store.close()
        return 0
    if not any(str(r.get("blame") or "").strip() for r in rows):
        print("[cli] 提示：尚未执行归因前置，doc_no / blame 列为空，建议先跑 attribute 子命令。")

    defects = store.fetch_defects()
    result = build_scorecard(rows, defects, corpus, out_dir, formats=formats)
    cards = result["rows"]

    print(f"\n[cli] 文档质量记分卡：{len(cards)} 份资料（缺陷 {len([d for d in defects if d.get('blame') == 'doc'])} 条）")
    print_table(
        [
            [
                c["doc_no"],
                c["probes"],
                c["csi_mean"] if c["csi_mean"] is not None else "—",
                c["defect_total"],
                c["defect_density"] if c["defect_density"] is not None else "—",
                c["dqi"] if c["dqi"] is not None else "样本不足",
                c["top_defects"] or "—",
            ]
            for c in cards
        ],
        ["资料", "探针数", "CSI", "缺陷数", "缺陷密度/100", "DQI", "Top 缺陷"],
    )
    print("[cli] 产物：")
    for key, path in result["paths"].items():
        print(f"  - doc_scorecard.{key:<3} {path}")
    store.close()
    return 0


def cmd_regression(args: argparse.Namespace) -> int:
    """黄金集回归评测。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    out_dir = resolve_path(args.out) if args.out else cfg.out_dir
    golden = resolve_path(args.golden) if args.golden else DEFAULT_GOLDEN
    baseline = resolve_path(args.baseline) if args.baseline else resolve_path(DEFAULT_BASELINE)
    store = open_store(cfg)
    runner = RegressionRunner(cfg, store, out_dir, concurrency=args.concurrency, mock_variant=args.mock_variant)
    result = runner.run(golden, baseline, save_baseline=args.save_baseline)
    summary = result["comparison"]["summary"]
    print("[cli] 回归评测完成：")
    print_table(
        [
            ["黄金集条数", summary["total"]],
            ["当前平均 CSI", summary["mean_csi"]],
            ["baseline 平均 CSI", summary["baseline_mean_csi"]],
            ["平均变化", summary["mean_delta"]],
            ["改进/退步/不变", f"{summary['improved']}/{summary['regressed']}/{summary['unchanged']}"],
            ["显著性", summary["significance"]],
        ],
        ["指标", "数值"],
    )
    rows = result["comparison"]["rows"]
    print("[cli] 变化明细（Top5，退步优先）：")
    print_table(
        [[r["id"], r["status"], r["baseline_csi"], r["current_csi"],
          (f"{r['delta']:+.2f}" if r["delta"] is not None else "-"), str(r["question"])[:26]] for r in rows[:5]],
        ["ID", "结论", "baseline", "当前", "Δ", "问题"],
    )
    for key, path in result["paths"].items():
        print(f"[cli] 已生成 {key}：{path}")
    store.close()
    return 0


def cmd_kb_build(args: argparse.Namespace) -> int:
    """知识库导入。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    source = resolve_path(args.source)
    store = open_store(cfg)
    builder = KbBuilder(cfg, store)
    if args.dry_run:
        files = builder.dry_run(source, DEFAULT_SUFFIXES)
        print(f"[cli] DRY-RUN 完成：{len(files)} 个文件")
        store.close()
        return 0
    stats = builder.build(
        source=source,
        dataset_id=args.dataset_id or "",
        create_dataset_name=args.create_dataset,
        hit_test_query=args.hit_test,
        poll_timeout=args.poll_timeout,
    )
    print("[cli] 知识库导入完成：")
    print_table(
        [["提交文档", stats.get("documents", 0)], ["跳过", stats.get("skipped", 0)],
         ["失败", len(stats.get("errors", []))], ["dataset_id", stats.get("dataset_id", "")]],
        ["指标", "数值"],
    )
    store.close()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """启动文档质量治理看板（Windows 本地版，仅标准库）。"""
    cfg = load_config(env_file=args.env_file, mock=args.mock)
    cfg.db_path = resolve_path(args.db) if args.db else cfg.db_path
    out_dir = resolve_path(args.out) if args.out else cfg.out_dir
    samples = resolve_path(args.samples) if args.samples else DEFAULT_SAMPLES

    # 先确保库与目录就绪（表结构、输出目录），再交给看板进程
    store = open_store(cfg)
    store.close()
    cfg.ensure_dirs()
    return run_dashboard(
        cfg,
        db_path=cfg.db_path,
        samples_dir=samples,
        out_dir=out_dir,
        host=args.host,
        port=args.port,
        open_browser=bool(args.open),
    )


def cmd_demo(args: argparse.Namespace) -> int:
    """一键跑通 mock 全流程（collect -> judge -> report -> badcase -> regression）。"""
    return run_demo(out_dir=args.out, db_path=args.db, quiet=args.quiet)


# ---------------------------------------------------------------- demo 流程


def run_demo(out_dir: Optional[str] = None, db_path: Optional[str] = None, quiet: bool = False) -> int:
    """端到端演示：mock 模式下完成后不依赖任何凭据与网络。

    :param out_dir: 产物目录，默认 eval_service/out
    :param db_path: 演示数据库，默认 eval_service/data/demo.db
    :return: 进程退出码
    """
    cfg = load_config(env_file=None, mock=True)
    cfg.out_dir = resolve_path(out_dir) if out_dir else (BASE_DIR / "out")
    cfg.db_path = resolve_path(db_path) if db_path else (BASE_DIR / "data" / "demo.db")
    cfg.ensure_dirs()

    banner = "=" * 72
    print(banner)
    print("Dify 问答质量评测服务 —— MOCK 全流程演示")
    print(f"数据库：{cfg.db_path}")
    print(f"产物目录：{cfg.out_dir}")
    print(f"fixtures：{cfg.fixtures_dir}")
    print(banner)

    # 每次演示重建演示库，保证结果可复现
    if cfg.db_path.exists():
        cfg.db_path.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(str(cfg.db_path) + suffix)
            if side.exists():
                side.unlink()
    store = open_store(cfg)
    print("\n[1/6] 初始化数据库 …")
    print_table([[k, v] for k, v in store.stats_overview().items()], ["表名", "行数"])

    print("\n[2/6] 采集会话 / 消息 / 反馈 …")
    collector = Collector(DifyClient.from_config(cfg, "app"), store, cfg)
    cstats = collector.collect()

    print("\n[3/6] 调用满意度评价工作流评分 …")
    judge = Judge(DifyClient.from_config(cfg, "eval"), store, cfg, concurrency=cfg.concurrency)
    jstats = judge.run()

    print("\n[4/6] 生成评测报表 …")
    outputs = ReportBuilder(store, cfg.out_dir).generate(period="all", formats=("md", "html", "csv"))

    print("\n[5/6] 挖掘 badcase 并输出改进建议 …")
    bc = mine_badcases(store, cfg.out_dir, min_severity="P2")

    print("\n[6/6] 回归评测（baseline 版本 vs 当前版本）…")
    golden = DEFAULT_GOLDEN
    baseline_path = cfg.out_dir / "baseline.json"
    base_runner = RegressionRunner(cfg, store, cfg.out_dir, concurrency=cfg.concurrency, mock_variant="baseline")
    base_runner.run(golden, baseline_path, save_baseline=True)
    print("[demo] 已生成 baseline（模拟升级前版本）")
    cur_runner = RegressionRunner(cfg, store, cfg.out_dir, concurrency=cfg.concurrency, mock_variant="current")
    reg = cur_runner.run(golden, baseline_path, save_baseline=False)

    rows = store.fetch_evaluations()
    import metrics as _metrics  # 延迟导入，避免与局部变量名冲突

    agg = _metrics.aggregate(rows)
    print("\n" + banner)
    print("演示完成，关键结果：")
    print_table(
        [
            ["采集消息", cstats.messages],
            ["评价成功", jstats.succeeded],
            ["CSI 均值", agg["csi"]["mean"]],
            ["达标率(>=3.5)", f"{(agg['pass_rate_35'] or 0) * 100:.1f}%"],
            ["badcase 数", len(bc["items"])],
            ["回归改进/退步", f"{reg['comparison']['summary']['improved']}/{reg['comparison']['summary']['regressed']}"],
        ],
        ["指标", "数值"],
    )
    print("\n产物清单：")
    for key, path in outputs.items():
        print(f"  - report.{key:<5} {path}")
    print(f"  - badcase_report.md {bc['paths']['md']}")
    print(f"  - badcases.csv     {bc['paths']['csv']}")
    for key, path in reg["paths"].items():
        print(f"  - regression.{key:<8} {path}")
    print(banner)
    store.close()
    return 0


# ---------------------------------------------------------------- 参数解析


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（含全部子命令）。"""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Dify 问答质量评测服务：采集 -> 多维评分 -> 报表 -> badcase -> 回归。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="提示：所有子命令都支持 --mock，可在无凭据、无网络的情况下跑通流程。",
    )
    parser.add_argument("--version", action="version", version="dify-eval-service 1.0.0")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mock", action="store_true", help="离线模式：使用 tests/fixtures 模拟 Dify 响应（无需凭据/网络）")
    common.add_argument("--env-file", default=None, help="自定义 .env 路径（默认 C:\\Users\\Administrator\\.env，环境变量优先）")
    common.add_argument("--db", default=None, help="SQLite 数据库路径（默认 eval_service/data/eval.db）")

    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    p_init = sub.add_parser("init-db", parents=[common], help="初始化数据库表结构")
    p_init.set_defaults(func=cmd_init_db)

    p_collect = sub.add_parser("collect", parents=[common], help="拉取会话 / 消息 / 反馈并落库")
    p_collect.add_argument("--limit", type=int, default=None, help="最多采集多少条消息（取最近 N 条）")
    p_collect.add_argument("--since", default=None, help="只采集该日期之后的数据，格式 YYYY-MM-DD")
    p_collect.add_argument("--user", default=None,
                           help="会话归属的 user 标识（Dify 按 user 隔离会话；门户传入的是登录用户名，如 admin）")
    p_collect.set_defaults(func=cmd_collect)

    p_judge = sub.add_parser("judge", parents=[common], help="调用满意度评价工作流做多维评分")
    p_judge.add_argument("--limit", type=int, default=None, help="最多评分数（默认全部未评分消息）")
    p_judge.add_argument("--concurrency", type=int, default=4, help="并发数（默认 4）")
    p_judge.add_argument("--include-receipt-only", action="store_true",
                         help="把「评价回执」消息也当作问答样本评分（默认跳过这类非问答消息）")
    p_judge.set_defaults(func=cmd_judge)

    p_report = sub.add_parser("report", parents=[common], help="生成 report.md / report.html / report.csv")
    p_report.add_argument("--period", default="all", metavar="{day,week,month,all}",
                          help="统计周期：day/week/month/all，也接受 日/周/月/全部（默认 all）")
    p_report.add_argument("--format", default="md,html,csv", help="输出格式，逗号分隔：md,html,csv,xlsx")
    p_report.add_argument("--out", default=None, help="输出目录（默认 eval_service/out/）")
    p_report.set_defaults(func=cmd_report)

    p_bad = sub.add_parser("badcase", parents=[common], help="挖掘 badcase 并生成改进建议")
    p_bad.add_argument("--min-severity", default="P2", choices=["P0", "P1", "P2"],
                       help="只输出该严重度及以上（P0 最严重，默认 P2=全部）")
    p_bad.add_argument("--out", default=None, help="输出目录（默认 eval_service/out/）")
    p_bad.set_defaults(func=cmd_badcase)

    p_reg = sub.add_parser("regression", parents=[common], help="黄金集回归评测与升级前后对比")
    p_reg.add_argument("--golden", default=None, help="黄金集 JSONL 路径（默认 eval_service/golden_set.jsonl）")
    p_reg.add_argument("--baseline", default=None, help="baseline JSON 路径（默认 out/baseline.json）")
    p_reg.add_argument("--save-baseline", action="store_true", help="把本次结果保存为新的 baseline")
    p_reg.add_argument("--concurrency", type=int, default=4, help="并发数（默认 4）")
    p_reg.add_argument("--mock-variant", default="current", choices=["current", "baseline"],
                       help="mock 模式下取用的答案版本（演示改进/退步用）")
    p_reg.add_argument("--out", default=None, help="输出目录（默认 eval_service/out/）")
    p_reg.set_defaults(func=cmd_regression)

    p_kb = sub.add_parser("kb-build", parents=[common], help="本地文档批量建知识库")
    p_kb.add_argument("--source", required=True, help="本地目录或文件（支持 .md/.markdown/.txt）")
    p_kb.add_argument("--dataset-id", default=None, help="目标知识库 ID（不填需配合 --create-dataset）")
    p_kb.add_argument("--create-dataset", default=None, help="先创建知识库，参数为知识库名称")
    p_kb.add_argument("--dry-run", action="store_true", help="只打印将上传的内容与分段预览，不发起写请求")
    p_kb.add_argument("--hit-test", default=None, help="导入完成后用该 query 做一次命中测试")
    p_kb.add_argument("--poll-timeout", type=int, default=300, help="索引状态轮询超时（秒）")
    p_kb.set_defaults(func=cmd_kb_build)

    p_attr = sub.add_parser("attribute", parents=[common], help="归因前置：判定问题出在资料还是系统")
    p_attr.add_argument("--samples", default=None, help="语料目录（默认项目 samples/）")
    p_attr.add_argument("--only-badcase", action="store_true", help="只归因 badcase，减少正常回答的噪声")
    p_attr.set_defaults(func=cmd_attribute)

    p_ground = sub.add_parser("grounding", parents=[common],
                              help="生成后可核验性校验：答案里的数值/编号/版本/条款是否被命中片段支撑")
    p_ground.add_argument("--only-unchecked", action="store_true", help="只处理尚未校验过的评价")
    p_ground.set_defaults(func=cmd_grounding)

    p_score = sub.add_parser("scorecard", parents=[common], help="生成文档质量记分卡与缺陷整改单")
    p_score.add_argument("--samples", default=None, help="语料目录（默认项目 samples/）")
    p_score.add_argument("--out", default=None, help="输出目录（默认 eval_service/out/）")
    p_score.add_argument("--format", default="md,csv", help="输出格式，逗号分隔（默认 md,csv）")
    p_score.set_defaults(func=cmd_scorecard)

    p_dash = sub.add_parser("dashboard", parents=[common], help="启动文档质量治理看板（Windows 本地版）")
    p_dash.add_argument("--samples", default=None, help="语料目录（默认项目 samples/）")
    p_dash.add_argument("--out", default=None, help="产物目录（默认 eval_service/out/）")
    p_dash.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机；填 0.0.0.0 可局域网访问）")
    p_dash.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    p_dash.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p_dash.set_defaults(func=cmd_dashboard)

    p_demo = sub.add_parser("demo", parents=[common], help="一键跑通 mock 全流程")
    p_demo.add_argument("--out", default=None, help="产物目录（默认 eval_service/out/）")
    p_demo.add_argument("--quiet", action="store_true", help="精简输出")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表，默认取 sys.argv[1:]
    :return: 进程退出码
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except DifyError as exc:
        print(f"[cli] Dify 调用失败：{exc}")
        return 1
    except KeyboardInterrupt:
        print("[cli] 已被用户中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
