"""Dify Service API 客户端：统一封装、超时、重试与错误处理，并内置 mock 注入点。

覆盖的端点（Dify 1.17.x，路径前缀 {BASE}/v1，鉴权头 Authorization: Bearer <key>）::

    GET  /v1/conversations?user=&limit=&last_id=
    GET  /v1/messages?conversation_id=&limit=&first_id=
    POST /v1/messages/{message_id}/feedbacks
    GET  /v1/app/feedbacks?page=&limit=
    GET  /v1/conversations/{conversation_id}/variables
    POST /v1/chat-messages
    POST /v1/workflows/run
    GET  /v1/workflows/run/{workflow_run_id}
    POST /v1/datasets
    POST /v1/datasets/{dataset_id}/document/create-by-text
    POST /v1/datasets/{dataset_id}/document/create-by-file
    GET  /v1/datasets/{dataset_id}/documents
    GET  /v1/datasets/{dataset_id}/documents/{batch}/indexing-status
    POST /v1/datasets/{dataset_id}/retrieve

依赖：仅标准库（urllib.request / json / mimetypes / uuid / time / random）。
"""

from __future__ import annotations

import json
import mimetypes
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from config import Config, mask_secret

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.8

#: 需要重试的 HTTP 状态码
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class DifyError(Exception):
    """Dify 调用异常基类。"""


class DifyAPIError(DifyError):
    """服务端返回了非 2xx 响应。"""

    def __init__(self, status: int, message: str, payload: Any = None, path: str = ""):
        """
        :param status: HTTP 状态码
        :param message: 错误信息
        :param payload: 原始响应体
        :param path: 请求路径
        """
        super().__init__(f"HTTP {status} {path} -> {message}")
        self.status = status
        self.message = message
        self.payload = payload
        self.path = path


class DifyNetworkError(DifyError):
    """网络层错误（超时、DNS、连接重置等），重试耗尽后抛出。"""


# ---------------------------------------------------------------- mock 后端


class MockBackend:
    """读取 tests/fixtures/*.json 模拟 Dify 的 HTTP 响应。

    为了让 --period day/week/month 过滤在离线演示时依然有意义，会把 fixtures 中
    所有 created_at/updated_at 平移到「距离当前时间 1 小时」为最新，保持相对间隔。
    """

    def __init__(self, fixtures_dir: Path, shift_time: bool = True):
        """
        :param fixtures_dir: fixtures 目录
        :param shift_time: 是否把时间戳平移到当前时间附近
        """
        self.fixtures_dir = Path(fixtures_dir)
        self.shift_time = shift_time
        self._cache: Dict[str, Any] = {}
        self._created_docs: Dict[str, Dict[str, Any]] = {}
        self._datasets: Dict[str, Dict[str, Any]] = {}
        if not self.fixtures_dir.exists():
            raise DifyError(f"mock 模式需要 fixtures 目录：{self.fixtures_dir}")

    # ------------------------------------------------ 基础工具
    def _load(self, name: str, default: Any = None) -> Any:
        """读取并缓存一个 fixture 文件。"""
        if name in self._cache:
            return self._cache[name]
        path = self.fixtures_dir / name
        if not path.exists():
            if default is not None:
                self._cache[name] = default
                return default
            raise DifyError(f"缺少 fixture 文件：{path}")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._cache[name] = data
        return data

    def _shift(self, data: Any) -> Any:
        """递归平移时间戳字段。"""
        if not self.shift_time:
            return data
        shift = self._compute_shift()
        if shift == 0:
            return data
        return _shift_timestamps(data, shift)

    def _compute_shift(self) -> int:
        """计算所有 fixtures 里最大时间戳与目标时间（now-3600）之间的差值。"""
        if getattr(self, "_shift_value", None) is not None:
            return self._shift_value
        stamps: List[int] = []
        for name in ("conversations.json", "messages.json", "app_feedbacks.json"):
            try:
                data = self._load(name, {})
            except DifyError:
                continue
            _collect_timestamps(data, stamps)
        target = int(time.time()) - 3600
        self._shift_value = (target - max(stamps)) if stamps else 0
        return self._shift_value

    # ------------------------------------------------ 分发
    def handle(self, method: str, path: str, params: Dict[str, Any], body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """把一个 HTTP 请求映射为 fixtures 中的响应。

        :param method: GET/POST
        :param path: /v1/... 路径
        :param params: 查询参数
        :param body: JSON 请求体
        :return: 与真实 API 同构的响应字典
        """
        method = method.upper()
        params = params or {}
        body = body or {}
        route = path

        if method == "GET" and route == "/v1/conversations":
            return self._conversations(params)
        if method == "GET" and route == "/v1/messages":
            return self._messages(params)
        m = re.fullmatch(r"/v1/messages/([^/]+)/feedbacks", route)
        if method == "POST" and m:
            return {"result": "success", "mock": True, "message_id": m.group(1)}
        if method == "GET" and route == "/v1/app/feedbacks":
            return self._app_feedbacks(params)
        m = re.fullmatch(r"/v1/conversations/([^/]+)/variables", route)
        if method == "GET" and m:
            variables = self._load("conversation_variables.json", {})
            return variables.get(m.group(1), {"limit": 100, "has_more": False, "data": []})
        if method == "POST" and route == "/v1/chat-messages":
            return self._chat_messages(body)
        if method == "POST" and route == "/v1/workflows/run":
            sample = self._load("workflow_run.json", None)
            if sample is None:
                raise DifyError("mock 模式下缺少 workflow_run.json")
            out = json.loads(json.dumps(sample))
            out.setdefault("data", {})["inputs"] = body.get("inputs", {})
            return out
        m = re.fullmatch(r"/v1/workflows/run/([^/]+)", route)
        if method == "GET" and m:
            sample = self._load("workflow_run.json", {})
            out = json.loads(json.dumps(sample))
            out.setdefault("data", {})["id"] = m.group(1)
            return out
        if method == "POST" and route == "/v1/datasets":
            return self._create_dataset(body)
        m = re.fullmatch(r"/v1/datasets/([^/]+)/document/create-by-text", route)
        if method == "POST" and m:
            return self._create_document(m.group(1), body)
        m = re.fullmatch(r"/v1/datasets/([^/]+)/document/create-by-file", route)
        if method == "POST" and m:
            return self._create_document(m.group(1), body)
        m = re.fullmatch(r"/v1/datasets/([^/]+)/documents", route)
        if method == "GET" and m:
            return self._documents(m.group(1), params)
        m = re.fullmatch(r"/v1/datasets/([^/]+)/documents/([^/]+)/indexing-status", route)
        if method == "GET" and m:
            return self._indexing_status(m.group(1), m.group(2))
        m = re.fullmatch(r"/v1/datasets/([^/]+)/retrieve", route)
        if method == "POST" and m:
            return self._retrieve(m.group(1), body)
        raise DifyAPIError(404, f"mock 未实现的端点：{method} {route}", None, route)

    # ------------------------------------------------ 具体端点
    def _conversations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        data = self._shift(self._load("conversations.json", {}))
        items = list(data.get("data", []))
        limit = int(params.get("limit") or 100)
        last_id = params.get("last_id")
        if last_id:
            ids = [c.get("id") for c in items]
            if last_id in ids:
                items = items[ids.index(last_id) + 1:]
        page = items[:limit]
        return {"limit": limit, "has_more": len(items) > len(page), "data": page}

    def _messages(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mapping = self._shift(self._load("messages.json", {}))
        conv_id = params.get("conversation_id") or ""
        payload = mapping.get(conv_id)
        if payload is None:
            return {"limit": 0, "has_more": False, "data": []}
        items = list(payload.get("data", []))
        limit = int(params.get("limit") or 100)
        first_id = params.get("first_id")
        if first_id:
            ids = [m.get("id") for m in items]
            if first_id in ids:
                items = items[: ids.index(first_id)]
        page = items[:limit]
        return {"limit": limit, "has_more": len(items) > len(page), "data": page}

    def _app_feedbacks(self, params: Dict[str, Any]) -> Dict[str, Any]:
        data = self._shift(self._load("app_feedbacks.json", {}))
        items = list(data.get("data", []))
        limit = int(params.get("limit") or 100)
        page = int(params.get("page") or 1)
        start = max(page - 1, 0) * limit
        chunk = items[start:start + limit]
        return {"limit": limit, "has_more": start + limit < len(items), "data": chunk}

    def _chat_messages(self, body: Dict[str, Any]) -> Dict[str, Any]:
        mapping = self._load("chat_messages.json", {})
        query = (body.get("query") or "").strip()
        item = mapping.get(query)
        # 回归演示：同一条问题可提供 current / baseline 两套答案，由 inputs.mock_variant 选择
        if isinstance(item, dict) and ("current" in item or "baseline" in item):
            variant = str(((body.get("inputs") or {}).get("mock_variant")) or "current").strip().lower()
            item = item.get(variant) or item.get("current") or item.get("baseline")
        if item is None:
            item = {
                "answer": f"（mock）知识库中未收录该问题的标准答案：{query}",
                "retriever_resources": [],
                "conversation_id": "mock-conv-regression",
                "message_id": f"mock-msg-{uuid.uuid4().hex[:8]}",
            }
        out = json.loads(json.dumps(item))
        out.setdefault("id", out.get("message_id") or f"mock-msg-{uuid.uuid4().hex[:8]}")
        out.setdefault("conversation_id", body.get("conversation_id") or "mock-conv-regression")
        out.setdefault("created_at", int(time.time()))
        out["answer"] = out.get("answer", "")
        out["mode"] = "chat"
        out["metadata"] = {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        return out

    def _create_dataset(self, body: Dict[str, Any]) -> Dict[str, Any]:
        ds_id = f"mock-dataset-{uuid.uuid4().hex[:8]}"
        item = {
            "id": ds_id,
            "name": body.get("name") or "mock-dataset",
            "description": body.get("description") or "",
            "indexing_technique": body.get("indexing_technique") or "high_quality",
            "permission": body.get("permission") or "only_me",
            "document_count": 0,
            "word_count": 0,
            "created_at": int(time.time()),
        }
        self._datasets[ds_id] = item
        return item

    def _create_document(self, dataset_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        doc_id = f"mock-doc-{uuid.uuid4().hex[:8]}"
        name = body.get("name") or "untitled"
        content = body.get("text") or body.get("content") or ""
        rec = {
            "id": doc_id,
            "dataset_id": dataset_id,
            "name": name,
            "char_count": len(content),
            "segment_count": max(1, len(content) // 400),
            "status": "completed",
            "batch": f"mock-batch-{doc_id[-4:]}",
        }
        self._created_docs[doc_id] = rec
        return {
            "document": {
                "id": doc_id,
                "position": len(self._created_docs),
                "data_source_type": "upload_file",
                "data_source_info": {"upload_file": {"name": name}},
                "dataset_process_rule_id": "mock-rule",
                "name": name,
                "created_from": "api",
                "created_by": "mock-user",
                "created_at": int(time.time()),
                "tokens": len(content),
                "indexing_status": "indexing",
                "enabled": True,
                "archived": False,
                "word_count": len(content),
            },
            "batch": rec["batch"],
        }

    def _documents(self, dataset_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        items = [
            {
                "id": r["id"],
                "name": r["name"],
                "indexing_status": "completed",
                "word_count": r["char_count"],
                "segment_count": r["segment_count"],
                "created_at": int(time.time()),
            }
            for r in self._created_docs.values()
            if r["dataset_id"] == dataset_id
        ]
        return {"data": items, "has_more": False, "limit": len(items) or 20, "total": len(items)}

    def _indexing_status(self, dataset_id: str, batch: str) -> Dict[str, Any]:
        items = [
            {
                "id": r["id"],
                "indexing_status": "completed",
                "processing_started_at": time.time() - 3,
                "processing_completed_at": time.time(),
                "tokens": r["char_count"],
                "segment_count": r["segment_count"],
                "error": None,
            }
            for r in self._created_docs.values()
            if r["dataset_id"] == dataset_id and r["batch"] == batch
        ]
        if not items:
            items = [{"id": "mock-doc-unknown", "indexing_status": "completed", "error": None}]
        return {"data": items}

    def _retrieve(self, dataset_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        mapping = self._load("retrieve.json", {})
        query = (body.get("query") or "").strip()
        hit = mapping.get(query)
        if hit is None:
            # 退化策略：用字符重合度挑一个最接近的 fixture 命中
            best_score, best = 0.0, None
            qset = set(query)
            for k, v in mapping.items():
                if not qset:
                    continue
                overlap = len(qset & set(k)) / len(qset)
                if overlap > best_score:
                    best_score, best = overlap, v
            hit = best if best and best_score >= 0.34 else {"records": []}
        return json.loads(json.dumps(hit))


def _collect_timestamps(data: Any, out: List[int]) -> None:
    """递归收集 created_at / updated_at 的整数时间戳。"""
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("created_at", "updated_at", "processing_started_at", "processing_completed_at") and isinstance(v, (int, float)):
                out.append(int(v))
            else:
                _collect_timestamps(v, out)
    elif isinstance(data, list):
        for item in data:
            _collect_timestamps(item, out)


def _shift_timestamps(data: Any, shift: int) -> Any:
    """递归平移时间戳字段（返回新对象，不改动缓存）。"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in ("created_at", "updated_at", "processing_started_at", "processing_completed_at") and isinstance(v, (int, float)):
                result[k] = int(v) + shift
            else:
                result[k] = _shift_timestamps(v, shift)
        return result
    if isinstance(data, list):
        return [_shift_timestamps(item, shift) for item in data]
    return data


# ---------------------------------------------------------------- 主客户端


class DifyClient:
    """Dify Service API 客户端。

    典型用法::

        client = DifyClient(cfg.api_base, cfg.app_api_key, timeout=cfg.timeout)
        convs = client.list_conversations(user="demo", limit=20)

    mock 模式::

        client = DifyClient("https://mock.local", "app-mock", mock=MockBackend(fixtures_dir))
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        mock: Optional[MockBackend] = None,
        user: str = "eval-service",
        verbose: bool = True,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        """
        :param base_url: Dify BASE，例如 https://cloud.dify.ai
        :param api_key: 该角色的 Service API Key
        :param timeout: 单次请求超时（秒）
        :param max_retries: 可重试错误的最大重试次数
        :param backoff_base: 指数退避基数（秒）
        :param mock: 注入的 MockBackend；非 None 时走离线分支
        :param user: 默认 user 标识
        :param verbose: 是否打印请求日志（不含密钥）
        :param sleep_fn: 休眠函数，便于测试替换
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.mock = mock
        self.user = user
        self.verbose = verbose
        self._sleep = sleep_fn
        self.call_count = 0

    # ------------------------------------------------ 构造
    @classmethod
    def from_config(cls, cfg: Config, role: str = "app", mock_backend: Optional[MockBackend] = None) -> "DifyClient":
        """按角色从 Config 构造客户端。

        :param cfg: 配置对象
        :param role: app / eval / kb
        :param mock_backend: mock 后端；cfg.mock 为真但未提供时会按 fixtures 目录自动创建
        """
        if mock_backend is None and cfg.mock:
            mock_backend = MockBackend(cfg.fixtures_dir)
        key = ""
        if mock_backend is None:
            key = cfg.require(role)
        return cls(
            cfg.api_base,
            key,
            timeout=cfg.timeout,
            max_retries=cfg.max_retries,
            backoff_base=cfg.backoff_base,
            mock=mock_backend,
            verbose=not cfg.mock,
        )

    @property
    def is_mock(self) -> bool:
        """是否处于 mock 模式。"""
        return self.mock is not None

    # ------------------------------------------------ 底层请求
    def _log(self, message: str) -> None:
        """打印请求日志（不回显密钥）。"""
        if self.verbose:
            print(f"[dify] {message}")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        raw_body: Optional[bytes] = None,
        content_type: str = "application/json",
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发送请求，带超时、指数退避重试与统一错误处理。

        :param method: GET / POST / PATCH / DELETE
        :param path: 以 /v1 开头的路径
        :param params: 查询参数（None 值会被剔除）
        :param json_body: JSON 请求体
        :param raw_body: 原始字节体（multipart 上传用）
        :param content_type: Content-Type
        :param api_key: 覆盖默认 Key（知识库接口用 dataset- Key）
        :return: 解析后的响应字典
        :raises DifyAPIError: 4xx（不可重试）或重试耗尽
        :raises DifyNetworkError: 网络错误重试耗尽
        """
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.mock is not None:
            self.call_count += 1
            self._log(f"MOCK {method} {path} params={clean_params}")
            return self.mock.handle(method, path, clean_params, json_body)

        url = f"{self.base_url}{path}"
        if clean_params:
            url = f"{url}?{urllib.parse.urlencode(clean_params)}"

        key = api_key if api_key is not None else self.api_key
        if not key:
            raise DifyAPIError(401, "缺少 API Key（请检查 .env 或命令行参数）", None, path)

        data: Optional[bytes] = None
        if raw_body is not None:
            data = raw_body
        elif json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=data, method=method.upper())
            req.add_header("Authorization", f"Bearer {key}")
            req.add_header("Content-Type", content_type)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "dify-eval-service/1.0")
            self.call_count += 1
            try:
                self._log(f"{method} {path} (attempt {attempt + 1}/{self.max_retries + 1})")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read().decode("utf-8", errors="replace")
                    return json.loads(payload) if payload.strip() else {}
            except urllib.error.HTTPError as exc:
                body_text = ""
                try:
                    body_text = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001 - 读取失败不影响主流程
                    body_text = ""
                detail = _extract_error_message(body_text) or exc.reason
                last_error = DifyAPIError(exc.code, str(detail), body_text, path)
                if exc.code not in RETRYABLE_STATUS:
                    raise last_error
                self._log(f"  -> HTTP {exc.code}，将重试：{detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = DifyNetworkError(f"{method} {path} 网络错误：{exc}")
                self._log(f"  -> 网络错误，将重试：{exc}")
            except json.JSONDecodeError as exc:
                raise DifyAPIError(200, f"响应不是合法 JSON：{exc}", None, path) from exc

            if attempt < self.max_retries:
                delay = self.backoff_base * (2 ** attempt) + random.uniform(0, 0.25)
                self._sleep(delay)
        assert last_error is not None
        raise last_error

    # ------------------------------------------------ 会话与消息
    def list_conversations(self, user: Optional[str] = None, limit: int = 100, last_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /v1/conversations —— 会话列表。"""
        return self.request(
            "GET", "/v1/conversations",
            params={"user": user or self.user, "limit": limit, "last_id": last_id},
        )

    def iter_conversations(self, user: Optional[str] = None, page_size: int = 100, max_pages: int = 50) -> List[Dict[str, Any]]:
        """分页拉取全部会话。"""
        all_items: List[Dict[str, Any]] = []
        last_id: Optional[str] = None
        for _ in range(max_pages):
            resp = self.list_conversations(user=user, limit=page_size, last_id=last_id)
            items = resp.get("data") or []
            all_items.extend(items)
            if not resp.get("has_more") or not items:
                break
            last_id = items[-1].get("id")
        return all_items

    def list_messages(
        self,
        conversation_id: str,
        limit: int = 100,
        first_id: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/messages —— 会话内消息列表。

        Dify 的服务 API 按 user 隔离会话：**必须带上该会话的归属 user**，
        否则返回 404 Conversation Not Exists（历史缺陷：这里没带 user，
        导致真实模式采集恒为 0 条消息）。
        """
        return self.request(
            "GET", "/v1/messages",
            params={"conversation_id": conversation_id, "limit": limit,
                    "first_id": first_id, "user": user or self.user},
        )

    def iter_messages(
        self,
        conversation_id: str,
        page_size: int = 100,
        max_pages: int = 50,
        user: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """分页拉取某会话的全部消息（按时间倒序返回，最终按时间升序整理）。"""
        collected: List[Dict[str, Any]] = []
        first_id: Optional[str] = None
        for _ in range(max_pages):
            resp = self.list_messages(conversation_id, limit=page_size, first_id=first_id, user=user)
            items = resp.get("data") or []
            collected.extend(items)
            if not resp.get("has_more") or not items:
                break
            first_id = items[0].get("id")
        collected.sort(key=lambda m: m.get("created_at") or 0)
        return collected

    def set_message_feedback(
        self,
        message_id: str,
        rating: Optional[str],
        content: str = "",
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/messages/{message_id}/feedbacks —— 设置/取消反馈。"""
        return self.request(
            "POST", f"/v1/messages/{message_id}/feedbacks",
            json_body={"rating": rating, "content": content, "user": user or self.user},
        )

    def list_app_feedbacks(self, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        """GET /v1/app/feedbacks —— 应用反馈列表。"""
        return self.request("GET", "/v1/app/feedbacks", params={"page": page, "limit": limit})

    def iter_app_feedbacks(self, page_size: int = 100, max_pages: int = 50) -> List[Dict[str, Any]]:
        """分页拉取全部应用反馈。"""
        all_items: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            resp = self.list_app_feedbacks(page=page, limit=page_size)
            items = resp.get("data") or []
            all_items.extend(items)
            if not resp.get("has_more") or not items:
                break
        return all_items

    def get_conversation_variables(self, conversation_id: str) -> Dict[str, Any]:
        """GET /v1/conversations/{id}/variables —— 会话变量。"""
        return self.request("GET", f"/v1/conversations/{conversation_id}/variables")

    # ------------------------------------------------ 问答与工作流
    def chat_messages(
        self,
        query: str,
        *,
        inputs: Optional[Dict[str, Any]] = None,
        conversation_id: str = "",
        user: Optional[str] = None,
        response_mode: str = "blocking",
    ) -> Dict[str, Any]:
        """POST /v1/chat-messages —— 调用问答应用（阻塞模式）。"""
        return self.request(
            "POST", "/v1/chat-messages",
            json_body={
                "query": query,
                "inputs": inputs or {},
                "response_mode": response_mode,
                "conversation_id": conversation_id,
                "user": user or self.user,
            },
        )

    def run_workflow(
        self,
        inputs: Dict[str, Any],
        *,
        user: Optional[str] = None,
        response_mode: str = "blocking",
    ) -> Dict[str, Any]:
        """POST /v1/workflows/run —— 调用工作流（阻塞模式）。"""
        return self.request(
            "POST", "/v1/workflows/run",
            json_body={"inputs": inputs, "response_mode": response_mode, "user": user or self.user},
        )

    def get_workflow_run(self, workflow_run_id: str) -> Dict[str, Any]:
        """GET /v1/workflows/run/{id} —— 查询工作流执行结果。"""
        return self.request("GET", f"/v1/workflows/run/{workflow_run_id}")

    # ------------------------------------------------ 知识库
    def create_dataset(
        self,
        name: str,
        *,
        description: str = "",
        indexing_technique: str = "high_quality",
        permission: str = "only_me",
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/datasets —— 创建空知识库。"""
        return self.request(
            "POST", "/v1/datasets",
            json_body={
                "name": name,
                "description": description,
                "indexing_technique": indexing_technique,
                "permission": permission,
            },
            api_key=api_key,
        )

    def create_document_by_text(
        self,
        dataset_id: str,
        name: str,
        text: str,
        *,
        indexing_technique: str = "high_quality",
        process_rule: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/datasets/{id}/document/create-by-text —— 通过文本创建文档。"""
        payload = {
            "name": name,
            "text": text,
            "indexing_technique": indexing_technique,
            "process_rule": process_rule or {"mode": "automatic"},
        }
        return self.request(
            "POST", f"/v1/datasets/{dataset_id}/document/create-by-text",
            json_body=payload, api_key=api_key,
        )

    def create_document_by_file(
        self,
        dataset_id: str,
        file_path: Path,
        *,
        indexing_technique: str = "high_quality",
        process_rule: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/datasets/{id}/document/create-by-file —— multipart 上传本地文件。"""
        path = Path(file_path)
        if not path.exists():
            raise DifyError(f"待上传文件不存在：{path}")
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data_field = {
            "indexing_technique": indexing_technique,
            "process_rule": process_rule or {"mode": "automatic"},
        }
        body, content_type = _encode_multipart(
            fields={"data": json.dumps(data_field, ensure_ascii=False)},
            files={"file": (path.name, content, mime)},
        )
        return self.request(
            "POST", f"/v1/datasets/{dataset_id}/document/create-by-file",
            raw_body=body, content_type=content_type, api_key=api_key,
        )

    def list_documents(self, dataset_id: str, page: int = 1, limit: int = 100, keyword: Optional[str] = None, api_key: Optional[str] = None) -> Dict[str, Any]:
        """GET /v1/datasets/{id}/documents —— 文档列表。"""
        return self.request(
            "GET", f"/v1/datasets/{dataset_id}/documents",
            params={"page": page, "limit": limit, "keyword": keyword}, api_key=api_key,
        )

    def get_indexing_status(self, dataset_id: str, batch: str, api_key: Optional[str] = None) -> Dict[str, Any]:
        """GET /v1/datasets/{id}/documents/{batch}/indexing-status —— 分段状态。"""
        return self.request(
            "GET", f"/v1/datasets/{dataset_id}/documents/{batch}/indexing-status", api_key=api_key
        )

    def wait_indexing(
        self,
        dataset_id: str,
        batch: str,
        *,
        timeout: int = 300,
        interval: float = 2.0,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """轮询索引状态直到全部完成或超时。

        :return: 最后一次的状态响应，附加 \"timed_out\" 字段
        """
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_indexing_status(dataset_id, batch, api_key=api_key)
            items = last.get("data") or []
            if items and all((it.get("indexing_status") in ("completed", "error")) for it in items):
                return last
            if self.mock is not None:
                return last
            self._sleep(interval)
        last["timed_out"] = True
        return last

    def retrieve(
        self,
        dataset_id: str,
        query: str,
        *,
        top_k: int = 4,
        score_threshold: float = 0.0,
        retrieval_model: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/datasets/{id}/retrieve —— 命中测试。"""
        model = retrieval_model or {
            "search_method": "hybrid_search",
            "reranking_enable": False,
            "top_k": top_k,
            "score_threshold_enabled": score_threshold > 0,
            "score_threshold": score_threshold,
        }
        return self.request(
            "POST", f"/v1/datasets/{dataset_id}/retrieve",
            json_body={"query": query, "retrieval_model": model}, api_key=api_key,
        )


# ---------------------------------------------------------------- 工具函数


def _extract_error_message(body_text: str) -> str:
    """从错误响应体里提取 message/code，提取不到则截断原文。"""
    if not body_text:
        return ""
    try:
        payload = json.loads(body_text)
    except ValueError:
        return body_text[:300]
    if isinstance(payload, dict):
        for key in ("message", "error", "code"):
            if payload.get(key):
                return str(payload[key])
    return str(payload)[:300]


def _encode_multipart(
    fields: Dict[str, str],
    files: Dict[str, Tuple[str, bytes, str]],
) -> Tuple[bytes, str]:
    """手工构造 multipart/form-data 请求体（标准库无现成实现）。

    :param fields: 普通表单字段
    :param files: 文件字段 -> (文件名, 内容, MIME)
    :return: (body, content_type)
    """
    boundary = f"----difyeval{uuid.uuid4().hex}"
    lines: List[bytes] = []
    for key, value in fields.items():
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"))
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))
    for key, (filename, content, mime) in files.items():
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(
            f'Content-Disposition: form-data; name="{key}"; filename="{filename}"'.encode("utf-8")
        )
        lines.append(f"Content-Type: {mime}".encode("utf-8"))
        lines.append(b"")
        lines.append(content)
    lines.append(f"--{boundary}--".encode("utf-8"))
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def smoke_test(cfg: Config) -> None:
    """连通性自检：打印配置掩码并发起一次最小请求。"""
    cfg.print_summary()
    client = DifyClient.from_config(cfg, "app")
    data = client.request("GET", "/v1/conversations", params={"user": "eval-service", "limit": 1})
    print(f"[dify] 连通性正常，返回 {len(data.get('data') or [])} 条会话")


__all__ = [
    "DifyClient", "MockBackend", "DifyError", "DifyAPIError", "DifyNetworkError", "smoke_test",
]
