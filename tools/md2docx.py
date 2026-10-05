# -*- coding: utf-8 -*-
"""将 docs/11-技术方案设计报告.md 渲染为 DOCX（复用《技术方案设计报告模板》的样式与自动编号）。
用法：python tools/md2docx.py
说明：Markdown 中的 <!--TOC--> 标记处会插入可更新的目录域；渲染完成后建议用 Word 打开并按 F9 更新域。
"""
"""由 Markdown 生成技术方案设计报告 DOCX，严格复用模板样式。"""
import re, sys, io
import docx
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

TEMPLATE = r'D:\app\dify\deploy-bundle-arm64\技术方案设计报告模板.docx'
MD       = sys.argv[1] if len(sys.argv) > 1 else r'D:\app\dify\docs\11-技术方案设计报告.md'
OUT      = sys.argv[2] if len(sys.argv) > 2 else r'D:\app\dify\docs\11-技术方案设计报告.docx'

S_H1, S_H2, S_H3 = 'Heading 1', '标题2', '标题3'
S_BODY, S_CAP = '正文样式', 'Caption'

def set_run(run, name='宋体', size=14, bold=False, mono=False):
    if mono:
        name = 'Consolas'
    run.font.size = Pt(size)
    run.font.bold = bold
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts'); rPr.append(rf)
    rf.set(qn('w:ascii'), name); rf.set(qn('w:hAnsi'), name)
    rf.set(qn('w:eastAsia'), '宋体' if mono else name)

def shade(cell, color='D9E2F3'):
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement('w:shd'); sh.set(qn('w:val'), 'clear'); sh.set(qn('w:fill'), color)
    tcPr.append(sh)

def clear_body(doc):
    body = doc.element.body
    for child in list(body.iterchildren()):
        if child.tag == qn('w:sectPr'):
            continue
        body.remove(child)

BT = chr(96)
TOKEN = re.compile(r'\*\*(.+?)\*\*|' + BT + r'([^' + BT + r']+)' + BT)

def add_rich(par, text, name='宋体', size=14, mono=False):
    pos = 0
    for m in TOKEN.finditer(text):
        if m.start() > pos:
            set_run(par.add_run(text[pos:m.start()]), name, size, False, mono)
        if m.group(1) is not None:
            set_run(par.add_run(m.group(1)), name, size, True, False)
        else:
            set_run(par.add_run(m.group(2)), 'Consolas', size - 2, False, True)
        pos = m.end()
    if pos < len(text):
        set_run(par.add_run(text[pos:]), name, size, False, mono)

def strip_num(s):
    return re.sub(r'^\d+(\.\d+)*[\.\s、]+', '', s).strip()

def is_sep(line):
    return bool(re.match(r'^\|[\s:|-]+\|$', line.strip()))

def split_row(line):
    s = line.strip()
    if s.startswith('|'): s = s[1:]
    if s.endswith('|'):   s = s[:-1]
    return [c.strip() for c in s.split('|')]

def parse_table(lines, i):
    header = split_row(lines[i]); rows = []; j = i + 2
    while j < len(lines) and lines[j].strip().startswith('|'):
        rows.append(split_row(lines[j])); j += 1
    return header, rows, j

def set_col_widths(table, widths):
    table.autofit = False
    for row in table.rows:
        for idx, w in enumerate(widths):
            if idx < len(row.cells):
                row.cells[idx].width = w

def main():
    with io.open(MD, encoding='utf-8') as f:
        lines = f.read().split('\n')
    doc = docx.Document(TEMPLATE)
    clear_body(doc)
    i = 0; first_h1 = True; ntab = 0
    while i < len(lines):
        line = lines[i].rstrip(); s = line.strip()
        if not s:
            i += 1; continue
        if s.startswith(BT * 3):
            i += 1; buf = []
            while i < len(lines) and not lines[i].strip().startswith(BT * 3):
                buf.append(lines[i].rstrip()); i += 1
            i += 1
            for b in buf:
                p = doc.add_paragraph(style=S_BODY)
                p.paragraph_format.first_line_indent = Cm(0)
                p.paragraph_format.left_indent = Cm(0.5)
                p.paragraph_format.line_spacing = Pt(11)
                p.paragraph_format.space_after = Pt(0)
                set_run(p.add_run(b if b else ' '), 'Consolas', 9, False, True)
            continue
        if s.startswith('|') and i + 1 < len(lines) and is_sep(lines[i + 1]):
            # 防止 Word 把相邻的两张表合并成一张：表前若已是表行，先插入空段落
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0 and lines[j].strip().startswith('|'):
                doc.add_paragraph(style=S_BODY)
            header, rows, nxt = parse_table(lines, i)
            t = doc.add_table(rows=1, cols=len(header))
            t.style = 'Table Grid'
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            for k, h in enumerate(header):
                cell = t.rows[0].cells[k]; cell.text = ''
                par = cell.paragraphs[0]; par.alignment = WD_ALIGN_PARAGRAPH.CENTER
                add_rich(par, h, '黑体', 10.5); shade(cell)
            for r in rows:
                cells = t.add_row().cells
                for k in range(len(header)):
                    val = r[k] if k < len(r) else ''
                    add_rich(cells[k].paragraphs[0], val, '宋体', 10.5)
            ncol = len(header)
            # 按各列内容长度按比例分配列宽，避免窄列挤压换行
            weights = []
            for k in range(ncol):
                lens = [len(header[k])]
                for r in rows:
                    if k < len(r):
                        lens.append(len(r[k]))
                lens.sort()
                w = lens[int(len(lens) * 0.8)] if lens else 4
                weights.append(min(max(w, 4), 34))
            tot = float(sum(weights)) or 1.0
            widths = [max(2.0, 15.5 * w / tot) for w in weights]
            scale = 15.5 / sum(widths)
            widths = [Cm(w * scale) for w in widths]
            set_col_widths(t, widths)
            i = nxt; continue
        if s.startswith('#### '):
            p = doc.add_paragraph(style=S_H3); add_rich(p, strip_num(s[5:]), '黑体', 14); i += 1; continue
        if s.startswith('### '):
            p = doc.add_paragraph(style=S_H2); add_rich(p, strip_num(s[4:]), '黑体', 14); i += 1; continue
        if s.startswith('## '):
            p = doc.add_paragraph(style=S_H1); add_rich(p, strip_num(s[3:]), '黑体', 14); i += 1; continue
        if s.startswith('# '):
            p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if first_h1:
                p.paragraph_format.space_before = Pt(140); p.paragraph_format.space_after = Pt(30)
                set_run(p.add_run(s[2:].strip()), '黑体', 24, True); first_h1 = False
            else:
                set_run(p.add_run(s[2:].strip()), '黑体', 14, True)
            i += 1; continue
        if s == '<!--TOC-->':
            # 目录另起一页
            pb = doc.add_paragraph()
            pb.paragraph_format.first_line_indent = Cm(0)
            _r = pb.add_run()
            _br = OxmlElement('w:br'); _br.set(qn('w:type'), 'page'); _r._element.append(_br)
            # 目录标题
            ph = doc.add_paragraph()
            ph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            ph.paragraph_format.space_before = Pt(12)
            ph.paragraph_format.space_after = Pt(12)
            set_run(ph.add_run('目　录'), '黑体', 16, True)
            # 目录域，Word 打开后按 F9 或打开时自动更新
            pt = doc.add_paragraph()
            pt.paragraph_format.first_line_indent = Cm(0)
            r1 = pt.add_run()
            fc = OxmlElement('w:fldChar'); fc.set(qn('w:fldCharType'), 'begin'); r1._element.append(fc)
            r2 = pt.add_run()
            it = OxmlElement('w:instrText'); it.set(qn('xml:space'), 'preserve')
            it.text = 'TOC \\o "1-3" \\h \\z \\u'
            r2._element.append(it)
            r3 = pt.add_run()
            fc2 = OxmlElement('w:fldChar'); fc2.set(qn('w:fldCharType'), 'separate'); r3._element.append(fc2)
            r4 = pt.add_run('（正文目录，请在 Word 中按 Ctrl+A 后按 F9 更新）')
            set_run(r4, '宋体', 10.5)
            r5 = pt.add_run()
            fc3 = OxmlElement('w:fldChar'); fc3.set(qn('w:fldCharType'), 'end'); r5._element.append(fc3)
            i += 1; continue
        if re.match(r'^-{3,}$', s):
            i += 1; continue
        if re.match(r'^(表|图)\s*\d+', s):
            p = doc.add_paragraph(style=S_CAP); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Cm(0)
            add_rich(p, s, '黑体', 10.5); i += 1; continue
        if s.startswith('> '):
            p = doc.add_paragraph(style=S_BODY)
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.left_indent = Cm(0.85)
            p.paragraph_format.line_spacing = Pt(20)
            add_rich(p, s[2:].strip(), '楷体', 12); i += 1; continue
        m = re.match(r'^([-*])\s+(.*)$', s)
        if m:
            p = doc.add_paragraph(style=S_BODY)
            p.paragraph_format.first_line_indent = Cm(-0.42)
            p.paragraph_format.left_indent = Cm(0.85)
            add_rich(p, '● ' + m.group(2)); i += 1; continue
        m = re.match(r'^(\d+)[\.、]\s+(.*)$', s)
        if m:
            p = doc.add_paragraph(style=S_BODY)
            p.paragraph_format.first_line_indent = Cm(-0.42)
            p.paragraph_format.left_indent = Cm(0.85)
            add_rich(p, m.group(1) + '）' + m.group(2)); i += 1; continue
        p = doc.add_paragraph(style=S_BODY)
        add_rich(p, s)
        i += 1
    doc.save(OUT)
    print('SAVED', OUT)

main()