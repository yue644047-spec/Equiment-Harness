# -*- coding: utf-8 -*-
"""把一批真实交付文档（docx/pdf/xlsx/txt/md）导入 RAGFlow 数据集并触发解析。

与 E:\llm-stack\setup\ragflow_ingest.py 的分工：那个脚本面向 HYS 教学语料（带 8 字段文档头），
本脚本面向**真实交付物**（如 D:\app\审价\...\交付成果），自动从 docx 正文里抓「文档编号 / 版本」
写入 meta_fields，供检索侧的版本硬过滤与引用契约使用。

用法：
    python tools/ragflow_ingest_docs.py --dataset "SwSuite交付文档" --source "D:\app\审价\1、385万（4号文）\交付成果"
    python tools/ragflow_ingest_docs.py --dataset X --source Y --dry-run
    python tools/ragflow_ingest_docs.py --dataset X --source Y --apply --graph
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENV_FILE = Path(r"E:\llm-stack\.env")
BASE = "http://127.0.0.1:9380"
SUFFIXES = {".docx", ".pdf", ".xlsx", ".txt", ".md"}


def load_env() -> dict:
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def api(key: str, method: str, path: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "message": exc.read().decode()[:300]}


def upload_file(key: str, dataset_id: str, path: Path) -> str:
    """multipart 上传（标准库手写，避免依赖 requests/httpx）。"""
    boundary = "----dsh" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode("utf-8")
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/v1/datasets/{dataset_id}/documents", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=600).read().decode())
    except urllib.error.HTTPError as exc:
        print("   上传失败：", exc.code, exc.read().decode()[:200])
        return ""
    items = data.get("data") or []
    return (items[0].get("id") if items else "") or ""


def docx_text(path: Path) -> str:
    """从 docx 里抽纯文本（仅用于抓元数据，不参与入库）。"""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        return ""
    return re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))


def docx_meta(path: Path) -> dict:
    """抓「文档编号 / 版本」，供 meta_fields（版本硬过滤与引用契约）。"""
    text = docx_text(path)
    meta = {}
    for label, key in (("文档编号", "doc_no"), ("版　　本", "version"), ("版本", "version"),
                       ("文档名称", "title"), ("密　级", "security"), ("密级", "security")):
        if key in meta:
            continue
        match = re.search(label + r"[^\S\n]*[:：]?[^\S\n]*([A-Za-z0-9\u4e00-\u9fa5－\-\.]+)", text[:4000])
        if match and match.group(1).strip():
            meta[key] = match.group(1).strip()
    return meta


def ensure_dataset(key: str, name: str, chunk_token_num: int = 512) -> str:
    listing = api(key, "GET", "/api/v1/datasets?page=1&page_size=100")
    for item in listing.get("data") or []:
        if item.get("name") == name:
            print("数据集已存在：", item["id"])
            return item["id"]
    created = api(key, "POST", "/api/v1/datasets", {
        "name": name, "chunk_method": "naive", "embedding_model": "bge-m3@Ollama",
        "parser_config": {"chunk_token_num": chunk_token_num, "delimiter": "\n## ", "html4excel": False},
    })
    dataset_id = ((created.get("data") or {}) or {}).get("id", "")
    print("创建数据集：", created.get("code"), dataset_id)
    return dataset_id


def main() -> int:
    parser = argparse.ArgumentParser(description="导入真实交付文档到 RAGFlow 并解析")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--graph", action="store_true", help="导入后触发知识图谱构建")
    parser.add_argument("--poll-timeout", type=int, default=3600)
    parser.add_argument("--chunk-token-num", type=int, default=512, help="新建数据集时的分段长度")
    parser.add_argument("--retry-failed", action="store_true",
                        help="有文档解析失败时，自动用一半的分段长度重试一次（入库门禁）")
    args = parser.parse_args()

    env = load_env()
    key = env.get("RAGFLOW_API_KEY", "")
    if not key:
        print("[FATAL] 缺少 RAGFLOW_API_KEY")
        return 2

    files = sorted(p for p in Path(args.source).iterdir()
                   if p.is_file() and p.suffix.lower() in SUFFIXES)
    if not files:
        print("[FATAL] 目录下没有可导入的文件")
        return 2
    print("待导入 %d 个文件：" % len(files))
    for f in files:
        print("   %-42s %8.1f KB" % (f.name, f.stat().st_size / 1024))
    if not args.apply:
        print("[dry-run] 未上传；加 --apply 生效")
        return 0

    dataset_id = ensure_dataset(key, args.dataset, args.chunk_token_num)
    if not dataset_id:
        return 2
    started = time.time()
    doc_ids: list[tuple[str, Path]] = []
    for f in files:
        doc_id = upload_file(key, dataset_id, f)
        if doc_id:
            doc_ids.append((doc_id, f))
            meta = docx_meta(f) if f.suffix.lower() == ".docx" else {}
            if meta:
                resp = api(key, "PUT", f"/api/v1/datasets/{dataset_id}/documents/{doc_id}",
                           {"meta_fields": meta})
                print("   元数据 %s -> %s (%s)" % (meta, resp.get("code"), f.name))
    print("上传完成：%d/%d 个" % (len(doc_ids), len(files)))

    ids = [d for d, _ in doc_ids]
    triggered = api(key, "POST", f"/api/v1/datasets/{dataset_id}/chunks", {"document_ids": ids})
    print("触发解析：", triggered.get("code"), str(triggered.get("message"))[:80])

    retried = False
    deadline = time.time() + args.poll_timeout
    while time.time() < deadline:
        time.sleep(20)
        listing = api(key, "GET", f"/api/v1/datasets/{dataset_id}/documents?page=1&page_size=100")
        items = (listing.get("data") or {}).get("docs") or []
        pending = [d for d in items if str(d.get("run")) not in ("DONE", "3")]
        chunks = sum(int(d.get("chunk_count") or 0) for d in items)
        tokens = sum(int(d.get("token_count") or 0) for d in items)
        print("  解析中… 完成 %d/%d，片段 %d，token %d" % (len(items) - len(pending), len(items), chunks, tokens))
        if items and not pending:
            failed = [d for d in items if str(d.get("run")) == "FAIL"]
            if failed and args.retry_failed and not retried:
                # 入库门禁：整篇失败大多是"某一段（常为大表格）超出嵌入模型上下文"，
                # 自动把分段长度减半后重试一次，避免静默丢资料（对应考核项 Q8）。
                retried = True
                print("\n[门禁] %d 个文档解析失败，按更小分段重试：%s"
                      % (len(failed), [str(d.get("name"))[:20] for d in failed]))
                for d in failed:
                    print("   失败原因：", str(d.get("progress_msg") or "").strip().splitlines()[-1][:160])
                smaller = max(64, int(args.chunk_token_num) // 2)
                api(key, "PUT", f"/api/v1/datasets/{dataset_id}",
                    {"parser_config": {"chunk_token_num": smaller}})
                api(key, "POST", f"/api/v1/datasets/{dataset_id}/chunks",
                    {"document_ids": [d.get("id") for d in failed]})
                print("   已用 chunk_token_num=%d 重新触发解析" % smaller)
                continue
            print("\n解析完成（耗时 %.0f 秒）：" % (time.time() - started))
            ok = 0
            for d in items:
                status = "✅" if str(d.get("run")) == "DONE" and int(d.get("chunk_count") or 0) > 0 else "❌"
                ok += 1 if status == "✅" else 0
                print("  %s %-38s 片段 %-5s token %-7s %s" % (
                    status, str(d.get("name"))[:36], d.get("chunk_count"), d.get("token_count"),
                    (d.get("progress_msg") or "").strip().splitlines()[-1][:40] if d.get("progress_msg") else ""))
            print("解析成功率：%d/%d" % (ok, len(items)))
            print("dataset_id =", dataset_id)
            if args.graph:
                g = api(key, "POST", f"/api/v1/datasets/{dataset_id}/run_graphrag")
                print("触发知识图谱：", g.get("code"), str(g.get("message"))[:80])
            return 0 if ok == len(items) else 1
    print("[WARN] 等待超时，请稍后复查解析状态")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
