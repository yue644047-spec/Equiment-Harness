"""资产索引：图片与视频的资产契约登记与元数据检索。

对应设计报告（docs/11）3.3 资产契约 / 3.4 资产索引 / 指标 T3~T6：
1. 图片与视频**只登记、只检索、不理解**——不 OCR、不抽帧、不转写、不做画面理解；
2. 可检索维度：所属资料、所属章节、页码、图注标题、文件名、人工标签；
3. 资产以引用附件形式挂载在所属文本片段上，随片段返回（关联片段标识）；
4. 内容指纹（sha256）作为幂等键与换版同步依据；缩略图用于界面预览。

离线可跑：全部为本地文件与标准库实现。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: 支持的资产类型（对应指标 T3 / T5）
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}
VIDEO_SUFFIXES = {".mp4", ".avi"}
THUMB_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass
class Asset:
    """资产契约（字段与设计报告表 17 一致）。"""

    asset_id: str                 # 资产编号（全局唯一）
    name: str                     # 资产名称
    asset_type: str               # image / video
    suffix: str                   # 文件格式
    size: int                     # 文件大小（字节）
    fingerprint: str              # 内容指纹（sha256 前 16 位）
    doc_no: str = ""              # 所属资料编号
    doc_version: str = ""         # 所属资料版本号
    section: str = ""             # 所属章节
    page: str = ""                # 页码
    figure_no: str = ""           # 图序号 / 表序号
    chunk_id: str = ""            # 关联片段标识
    caption: str = ""             # 图注标题
    tags: List[str] = field(default_factory=list)   # 人工标签
    thumb: str = ""               # 缩略图路径
    security: str = "内部"        # 密级
    path: str = ""                # 原始文件路径

    def to_dict(self) -> Dict[str, Any]:
        """转为字典（供 JSON 输出与看板渲染）。"""
        data = dict(self.__dict__)
        data["tags"] = list(self.tags)
        return data

    @property
    def searchable_text(self) -> str:
        """可检索文本：图注、名称、标签、章节、图序号、文件名、资料编号。"""
        return " ".join([
            self.name, self.caption, " ".join(self.tags), self.section,
            self.figure_no, Path(self.path).name if self.path else "", self.doc_no,
        ])

    def match(self, keyword: str) -> bool:
        """关键词是否命中该资产（大小写不敏感的子串匹配）。"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return True
        return kw in self.searchable_text.lower()


class AssetIndex:
    """资产索引：登记 + 检索 + 挂载。"""

    def __init__(self, assets: Optional[Sequence[Asset]] = None):
        self.assets: List[Asset] = list(assets or [])

    # ------------------------------------------------ 构建
    @classmethod
    def load(cls, assets_dir: Path | str, manifest_name: str = "assets.json") -> "AssetIndex":
        """从资产目录 + 清单文件构建索引。

        清单（manifest）是资产契约的权威来源；目录中未登记的文件会被列出为「待登记」，
        与设计报告「对未标注资产做醒目标记并提示补录」的要求一致。
        """
        root = Path(assets_dir)
        index = cls()
        manifest_path = root / manifest_name
        if not manifest_path.exists():
            return index
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in data.get("assets", []):
            path = root / item.get("path", "")
            fingerprint = item.get("fingerprint") or ""
            size = int(item.get("size") or 0)
            if path.exists():
                raw = path.read_bytes()
                size = len(raw)
                if not fingerprint:
                    fingerprint = hashlib.sha256(raw).hexdigest()[:16]
                if not item.get("thumb"):
                    thumb = root / "thumbs" / (Path(item.get("path", "")).stem + ".png")
                    if thumb.exists():
                        item["thumb"] = str(thumb.relative_to(root))
            index.assets.append(
                Asset(
                    asset_id=item.get("asset_id", ""),
                    name=item.get("name", ""),
                    asset_type=item.get("asset_type", "image"),
                    suffix=path.suffix.lower(),
                    size=size,
                    fingerprint=fingerprint,
                    doc_no=item.get("doc_no", ""),
                    doc_version=item.get("doc_version", ""),
                    section=item.get("section", ""),
                    page=str(item.get("page", "")),
                    figure_no=item.get("figure_no", ""),
                    chunk_id=item.get("chunk_id", ""),
                    caption=item.get("caption", ""),
                    tags=list(item.get("tags") or []),
                    thumb=item.get("thumb", ""),
                    security=item.get("security", "内部"),
                    path=str(path) if path.exists() else item.get("path", ""),
                )
            )
        return index

    def unregistered(self, assets_dir: Path | str) -> List[str]:
        """列出目录中尚未登记的资产文件（用于补录提示）。"""
        root = Path(assets_dir)
        known = {Path(a.path).resolve() for a in self.assets if a.path}
        missing: List[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in (IMAGE_SUFFIXES | VIDEO_SUFFIXES):
                continue
            if "thumbs" in path.parts:
                continue
            if path.resolve() not in known:
                missing.append(str(path.relative_to(root)))
        return missing

    # ------------------------------------------------ 检索
    def search(
        self,
        keyword: str = "",
        asset_type: str = "",
        doc_no: str = "",
        section: str = "",
        page: str = "",
        tags: Optional[Sequence[str]] = None,
    ) -> List[Asset]:
        """按元数据检索资产（对应指标 T4 / T6 的可检索维度）。"""
        results: List[Asset] = []
        for asset in self.assets:
            if asset_type and asset.asset_type != asset_type:
                continue
            if doc_no and asset.doc_no != doc_no:
                continue
            if section and section not in asset.section:
                continue
            if page and str(asset.page) != str(page):
                continue
            if tags and not set(tags).issubset({t for t in asset.tags}):
                continue
            if not asset.match(keyword):
                continue
            results.append(asset)
        results.sort(key=lambda a: (a.doc_no, a.figure_no, a.asset_id))
        return results

    def attach_map(self) -> Dict[str, List[Asset]]:
        """chunk_id -> 资产列表（检索时随片段返回引用附件）。"""
        mapping: Dict[str, List[Asset]] = {}
        for asset in self.assets:
            if asset.chunk_id:
                mapping.setdefault(asset.chunk_id, []).append(asset)
        return mapping

    def by_chunk(self, chunk_id: str) -> List[Asset]:
        """取某个片段挂载的资产。"""
        return [a for a in self.assets if a.chunk_id == chunk_id]

    # ------------------------------------------------ 摘要
    def stats(self) -> Dict[str, Any]:
        """资产概览。"""
        images = sum(1 for a in self.assets if a.asset_type == "image")
        videos = sum(1 for a in self.assets if a.asset_type == "video")
        tagged = sum(1 for a in self.assets if a.tags)
        captioned = sum(1 for a in self.assets if a.caption)
        return {
            "total": len(self.assets),
            "images": images,
            "videos": videos,
            "with_tags": tagged,
            "with_caption": captioned,
            "coverage": round(captioned / len(self.assets), 3) if self.assets else None,
        }


def make_thumbnail(src: Path, dst: Path, max_width: int = 240) -> Optional[Path]:
    """生成缩略图（仅 PNG/JPG 源图；PDF 与视频不生成缩略图）。

    纯标准库实现：解码用最简 PNG 读取器，缩放用最近邻，编码用最简 PNG 写入器。
    非 PNG 源图（JPG）在演示环境中直接复制原图作为预览。
    """
    if src.suffix.lower() not in THUMB_SUFFIXES:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() != ".png":
        dst.write_bytes(src.read_bytes())
        return dst
    pixels, width, height = read_png(src)
    if not pixels:
        return None
    scale = max(1, width // max_width)
    new_w, new_h = width // scale, height // scale
    out = bytearray()
    for y in range(new_h):
        out.append(0)
        for x in range(new_w):
            r, g, b = pixels[(y * scale) * width + (x * scale)]
            out.extend((r, g, b))
    write_png(dst, new_w, new_h, bytes(out))
    return dst


# ---------------------------------------------------------------- 最简 PNG 读写


def read_png(path: Path):
    """读取 8 位 RGB/RGBA PNG，返回 (像素列表, 宽, 高)。"""
    import struct
    import zlib

    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return [], 0, 0
    pos = 8
    width = height = color_type = 0
    idat = b""
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        data = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, _depth, color_type = struct.unpack(">IIBB", data[:10])
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
    channels = {2: 3, 6: 4, 0: 1}.get(color_type)
    if not channels:
        return [], 0, 0
    stream = zlib.decompress(idat)
    stride = width * channels
    pixels = []
    prev = bytearray(stride)
    idx = 0
    for _y in range(height):
        ftype = stream[idx]
        idx += 1
        line = bytearray(stream[idx : idx + stride])
        idx += stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        prev = line
        for x in range(width):
            if channels >= 3:
                pixels.append((line[x * channels], line[x * channels + 1], line[x * channels + 2]))
            else:
                v = line[x]
                pixels.append((v, v, v))
    return pixels, width, height


def write_png(path: Path, width: int, height: int, raw_rgb: bytes) -> Path:
    """写入 8 位 RGB PNG（raw_rgb 需含每行首字节的 filter=0）。"""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    body = zlib.compress(bytes(raw_rgb), 9)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", body) + chunk(b"IEND", b""))
    return path
