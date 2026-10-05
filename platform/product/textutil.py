"""文本整形工具：把解析器产出的 HTML 表格变成人与模型都读得懂的文本。

RAGFlow 的解析器对表格切片会给出 <table>…</table> 原文（保留结构便于前端渲染），
但直接塞进提示词既费 token 又容易让模型读错。这里提供两级处理：

  * html_table_to_text —— 把表格还原成 "| 单元格 | 单元格 |" 的竖线文本；
  * normalize_for_prompt —— 提示词专用：转表格 + 去标签 + 压空白。
前端仍拿到原始 HTML，可正常渲染成表格（两边口径不冲突）。
"""

from __future__ import annotations

import html
import re

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_CAPTION_RE = re.compile(r"<caption[^>]*>(.*?)</caption>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"[ \t\u00a0]+")


def _clean(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment or "")
    text = html.unescape(text)
    return _BLANK_RE.sub(" ", text).strip()


def html_table_to_text(content: str) -> str:
    """把一个或多个 HTML 表格转成竖线文本；无表格时原样返回。"""

    def convert(match: re.Match) -> str:
        body = match.group(1)
        caption = ""
        found = _CAPTION_RE.search(body)
        if found:
            caption = _clean(found.group(1))
        lines = []
        if caption:
            lines.append("表格：" + caption)
        for row in _ROW_RE.findall(body):
            cells = [_clean(cell) for cell in _CELL_RE.findall(row)]
            cells = [cell for cell in cells if cell != ""]
            if cells:
                lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    return _TABLE_RE.sub(convert, content or "")


def normalize_for_prompt(content: str, limit: int = 1500) -> str:
    """提示词片段整形：表格转文本、去标签、收敛空行，并做长度上限保护。"""
    text = html_table_to_text(content or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _BLANK_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if limit and len(text) > limit:
        text = text[:limit] + " …"
    return text.strip()


def looks_like_table(content: str) -> bool:
    return bool(content) and "<table" in content.lower()
