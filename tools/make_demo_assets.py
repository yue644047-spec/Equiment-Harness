"""生成演示用资产（图片 / 视频）与资产契约清单。

对应设计报告（docs/11）3.4 资产索引与指标 T3~T6：
图片与视频只做登记、维护、定位与检索呈现，不 OCR、不抽帧、不转写。

用法：
    python tools/make_demo_assets.py            # 生成到 assets/
    python tools/make_demo_assets.py --force    # 覆盖已有文件

产物：
    assets/images/*.png      演示图片（合成的示意图，非真实装备资料）
    assets/videos/*.mp4      演示视频占位文件（仅用于登记与检索演示）
    assets/thumbs/*.png      缩略图（用于界面预览）
    assets/assets.json       资产契约清单（权威来源）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval_service"))
sys.path.insert(0, str(ROOT / "kb_bridge"))

from asset_index import make_thumbnail  # noqa: E402
from chunk_index import ChunkIndex, load_ledger_current  # noqa: E402

#: 演示资产规格：keyword 用于在语料中定位所属条款（关联片段标识）
ASSET_SPECS: List[Dict[str, object]] = [
    {
        "asset_id": "AST-IMG-0001",
        "name": "液压系统原理图",
        "asset_type": "image",
        "doc_no": "HYS-GY-001",
        "keyword": "液压系统由液压泵站",
        "figure_no": "图3-1",
        "page": "12",
        "section": "3 工作原理",
        "caption": "图3-1 某型液压举升系统液压系统原理图",
        "tags": ["原理图", "液压系统", "HYS-1"],
        "palette": [(210, 226, 240), (31, 95, 139), (120, 160, 190)],
    },
    {
        "asset_id": "AST-IMG-0002",
        "name": "液压泵站分解图",
        "asset_type": "image",
        "doc_no": "HYS-WX-003",
        "keyword": "液压泵站",
        "figure_no": "图4-2",
        "page": "27",
        "section": "4 拆卸与装配",
        "caption": "图4-2 液压泵站分解图（含密封件位置）",
        "tags": ["分解图", "液压泵站", "维修"],
        "palette": [(226, 232, 224), (46, 125, 91), (140, 175, 150)],
    },
    {
        "asset_id": "AST-IMG-0003",
        "name": "操作面板示意图",
        "asset_type": "image",
        "doc_no": "HYS-CZ-002",
        "keyword": "操作面板",
        "figure_no": "图6-1",
        "page": "18",
        "section": "6 操作步骤",
        "caption": "图6-1 操作面板按钮与指示灯布置示意",
        "tags": ["操作面板", "示意", "操作规程"],
        "palette": [(240, 232, 214), (200, 127, 10), (215, 175, 110)],
    },
    {
        "asset_id": "AST-IMG-0004",
        "name": "滤芯堵塞部位示意",
        "asset_type": "image",
        "doc_no": "HYS-GZ-004",
        "keyword": "滤芯堵塞",
        "figure_no": "图2-3",
        "page": "9",
        "section": "2 典型故障",
        "caption": "图2-3 滤芯堵塞常见部位与检查顺序",
        "tags": ["故障案例", "滤芯", "堵塞"],
        "palette": [(240, 220, 220), (192, 57, 43), (200, 140, 140)],
    },
    {
        "asset_id": "AST-VID-0001",
        "name": "举升操作演示录像",
        "asset_type": "video",
        "doc_no": "HYS-CZ-002",
        "keyword": "作业前检查",
        "figure_no": "视频1",
        "page": "",
        "section": "2 作业前检查",
        "caption": "视频1 举升作业前检查与启动操作演示",
        "tags": ["操作演示", "视频", "作业前检查"],
        "palette": [],
    },
]

#: 演示视频的时长（秒），仅登记在清单里
VIDEO_SECONDS = 96


def make_mp4_placeholder(path: Path) -> Path:
    """生成结构可识别的 MP4 占位文件（含 ftyp 盒），用于资产登记演示。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41"
    free = struct.pack(">I", 8) + b"free"
    path.write_bytes(ftyp + free)
    return path


def draw_schematic(path: Path, width: int, height: int, palette: List[Tuple[int, int, int]], seed: int) -> Path:
    """绘制一张合成的示意图（色块 + 连线 + 网格），作为演示图片。"""
    bg, accent, mid = palette if len(palette) == 3 else ((235, 238, 242), (31, 95, 139), (150, 170, 190))
    px = bytearray()
    blocks = []
    # 依据 seed 生成若干互不重叠的方框（模拟设备部件）
    cols, rows = 3, 2
    for r in range(rows):
        for c in range(cols):
            if (r * cols + c + seed) % 4 == 3:
                continue
            x0 = int(width * (0.08 + c * 0.30))
            y0 = int(height * (0.22 + r * 0.36))
            w = int(width * 0.20)
            h = int(height * 0.22)
            blocks.append((x0, y0, w, h, (r * cols + c + seed) % 2 == 0))
    for y in range(height):
        px.append(0)
        for x in range(width):
            color = bg
            if x % 40 == 0 or y % 40 == 0:
                color = (max(bg[0] - 10, 0), max(bg[1] - 10, 0), max(bg[2] - 10, 0))
            if 6 <= x < width - 6 and 6 <= y < height - 6:
                if x < 10 or x >= width - 10 or y < 10 or y >= height - 10:
                    color = accent
            for (x0, y0, w, h, filled) in blocks:
                if x0 <= x < x0 + w and y0 <= y < y0 + h:
                    if filled:
                        color = mid if ((x - x0) // 8 + (y - y0) // 8) % 2 == 0 else accent
                    else:
                        edge = x < x0 + 2 or x >= x0 + w - 2 or y < y0 + 2 or y >= y0 + h - 2
                        color = accent if edge else bg
            px.extend(color)
    # 底部图例条
    for y in range(height - 22, height - 8):
        pass
    from asset_index import write_png

    return write_png(path, width, height, bytes(px))


def main(argv: Optional[List[str]] = None) -> int:
    """生成演示资产、缩略图与清单。"""
    parser = argparse.ArgumentParser(description="生成演示用资产与资产契约清单")
    parser.add_argument("--out", default=str(ROOT / "assets"), help="输出目录（默认 assets/）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    images_dir = out_dir / "images"
    videos_dir = out_dir / "videos"
    thumbs_dir = out_dir / "thumbs"

    # 语料索引：用于把资产挂载到真实条款上（关联片段标识）
    ledger = load_ledger_current(ROOT / "samples")
    index = ChunkIndex()
    for directory in (ROOT / "samples", ROOT / "demo_corpus"):
        index.chunks.extend(ChunkIndex.build(directory, ledger).chunks)
    index._build_stats()

    manifest: Dict[str, object] = {
        "note": "演示资产清单（资产契约的权威来源）。图片与视频只登记与检索，不做内容理解。",
        "assets": [],
    }

    for seed, spec in enumerate(ASSET_SPECS):
        asset_type = str(spec["asset_type"])
        doc_no = str(spec["doc_no"])
        # 定位关联片段（同资料内按关键词检索）
        anchors = index.search(str(spec["keyword"]), top_k=3, doc_nos=[doc_no])
        chunk = anchors[0].chunk if anchors else None
        stem = f"{spec['asset_id']}_{spec['name']}"
        if asset_type == "image":
            path = images_dir / f"{stem}.png"
            if args.force or not path.exists():
                draw_schematic(path, 420, 280, list(spec["palette"]), seed)
        else:
            path = videos_dir / f"{stem}.mp4"
            if args.force or not path.exists():
                make_mp4_placeholder(path)

        thumb_rel = ""
        thumb = make_thumbnail(path, thumbs_dir / f"{stem}.png")
        if thumb is not None:
            thumb_rel = str(thumb.relative_to(out_dir))

        raw = path.read_bytes()
        item = {
            "asset_id": spec["asset_id"],
            "name": spec["name"],
            "asset_type": asset_type,
            "path": str(path.relative_to(out_dir)),
            "size": len(raw),
            "fingerprint": hashlib.sha256(raw).hexdigest()[:16],
            "doc_no": doc_no,
            "doc_version": (chunk.version if chunk else ""),
            "section": spec["section"],
            "page": spec["page"],
            "figure_no": spec["figure_no"],
            "chunk_id": (chunk.chunk_id if chunk else ""),
            "clause_no": (chunk.clause_no if chunk else ""),
            "caption": spec["caption"],
            "tags": list(spec["tags"]),
            "thumb": thumb_rel,
            "security": "内部",
        }
        if asset_type == "video":
            item["duration_seconds"] = VIDEO_SECONDS
        manifest["assets"].append(item)
        print(f"  {spec['asset_id']:<14} {asset_type:<5} {path.name}  → 挂载 {item['chunk_id'] or '（未匹配到条款）'}")

    (out_dir / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已生成 {len(manifest['assets'])} 个演示资产与清单：{out_dir / 'assets.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
