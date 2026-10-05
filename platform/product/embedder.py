"""模型网关（产品内置）：嵌入与对话，全部走 OpenAI 兼容的 url + api key。

配置来源（后者覆盖前者）：
  1. platform/config/equipment.env 的 MODEL_* 段；
  2. 产品库 providers 表（门户「模型提供商」页写入，可随时改 url/key/模型名）。

因此"换模型厂商"= 改一行配置或点一下界面，两个引擎内核（RAGFlow 源码 / Dify graphon）共用同一份。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from .store import Store, content_hash

ROLES = {"chat": "MODEL_CHAT", "embedding": "MODEL_EMBEDDING", "rerank": "MODEL_RERANK"}


class ModelError(RuntimeError):
    """模型调用失败（错误信息可直接展示给用户）。"""


class ModelGateway:
    """按角色解析模型配置，并提供嵌入 / 对话 / 重排调用。"""

    def __init__(self, env: Dict[str, str], store: Optional[Store] = None):
        self.env = env
        self.store = store
        self._client = httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))

    # ------------------------------------------------------------ 配置解析
    def resolve(self, role: str) -> Dict[str, Any]:
        """角色 → {provider, name, base_url, api_key, provider_id}；产品库绑定优先。

        取值顺序（后者覆盖前者）：配置文件 MODEL_* 默认值 → 绑定的提供商记录
        （厂商 / 模型名 / url / key）→ 角色绑定的 extra（max_tokens / dim）。
        """
        prefix = ROLES.get(role)
        if prefix is None:
            raise ModelError(f"未知模型角色：{role}")
        config: Dict[str, Any] = {
            "provider": self.env.get(f"{prefix}_PROVIDER", ""),
            "name": self.env.get(f"{prefix}_NAME", ""),
            "base_url": self.env.get(f"{prefix}_BASE_URL", ""),
            "api_key": self.env.get(f"{prefix}_API_KEY", ""),
            "max_tokens": self.env.get(f"{prefix}_MAX_TOKENS", ""),
            "dim": self.env.get(f"{prefix}_DIM", ""),
            "provider_id": "",
        }
        if self.store is None:
            return config
        binding = self.store.get_role_binding(role) or {}
        provider_id = binding.get("provider_id") or ""
        config["provider_id"] = provider_id
        record = self.store.get_provider(provider_id)
        if record and record.get("enabled"):
            config.update({
                "provider": record.get("provider") or config["provider"],
                "name": record.get("name") or config["name"],
                "base_url": record.get("base_url") or config["base_url"],
                "api_key": record.get("api_key") or config["api_key"],
            })
            config.update({k: v for k, v in (record.get("extra") or {}).items() if v not in (None, "")})
        config.update({k: v for k, v in (binding.get("extra") or {}).items() if v not in (None, "")})
        return config

    def public_config(self) -> Dict[str, Any]:
        """对外暴露的模型配置（key 打码）。"""
        out: Dict[str, Any] = {}
        for role in ROLES:
            item = self.resolve(role)
            key = item.get("api_key") or ""
            item["api_key"] = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ("****" if key else "")
            item["configured"] = bool(item.get("base_url") and item.get("name"))
            out[role] = item
        return out

    def _endpoint(self, role: str, path: str) -> str:
        config = self.resolve(role)
        base = (config.get("base_url") or "").rstrip("/")
        if not base:
            raise ModelError(f"{role} 模型未配置 base_url（在「模型提供商」里填 url + api key）")
        if base.endswith("/v1") or base.endswith("/api"):
            return base + path
        return base + "/v1" + path

    def _headers(self, role: str) -> Dict[str, str]:
        config = self.resolve(role)
        headers = {"Content-Type": "application/json"}
        if config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        return headers

    # ------------------------------------------------------------ 嵌入
    def embed(self, texts: List[str], batch_size: int = 16, use_cache: bool = True) -> List[List[float]]:
        """批量嵌入（带产品库缓存；同一段文本只算一次）。"""
        if not texts:
            return []
        config = self.resolve("embedding")
        model = config.get("name") or "embedding"
        vectors: List[Optional[List[float]]] = [None] * len(texts)
        pending: List[int] = []
        hashes = [content_hash(model, text) for text in texts]
        if use_cache and self.store is not None:
            cached = self.store.cached_vectors(hashes, model)
            for index, digest in enumerate(hashes):
                if digest in cached:
                    vectors[index] = cached[digest]
                else:
                    pending.append(index)
        else:
            pending = list(range(len(texts)))

        max_chars = int(self.env.get("MODEL_EMBEDDING_MAX_CHARS", "6000") or 6000)
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            payload = {"model": model, "input": [texts[i][:max_chars] for i in batch]}
            data = self._post("embedding", "/embeddings", payload)
            items = data.get("data") or []
            if len(items) != len(batch):
                raise ModelError(f"嵌入返回数量不符：请求 {len(batch)} 条，返回 {len(items)} 条")
            fresh: List[Dict[str, Any]] = []
            for slot, item in zip(batch, items):
                vector = [float(x) for x in item.get("embedding") or []]
                vectors[slot] = vector
                fresh.append({"hash": hashes[slot], "vector": vector})
            if self.store is not None and fresh:
                self.store.cache_vectors(fresh, model)
        if any(v is None for v in vectors):
            raise ModelError("嵌入结果不完整")
        return [v or [] for v in vectors]

    def embed_one(self, text: str) -> List[float]:
        vectors = self.embed([text])
        return vectors[0] if vectors else []

    # ------------------------------------------------------------ 对话
    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.2,
             max_tokens: Optional[int] = None) -> str:
        config = self.resolve("chat")
        payload: Dict[str, Any] = {
            "model": config.get("name") or "chat",
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        limit = max_tokens or int(config.get("max_tokens") or 2048)
        payload["max_tokens"] = limit
        data = self._post("chat", "/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"对话返回为空：{json.dumps(data, ensure_ascii=False)[:200]}")
        return str((choices[0].get("message") or {}).get("content") or "")

    def chat_stream(self, messages: List[Dict[str, str]], temperature: float = 0.2,
                    max_tokens: Optional[int] = None):
        """流式对话：逐段 yield 文本增量。"""
        config = self.resolve("chat")
        payload = {"model": config.get("name") or "chat", "messages": messages,
                   "temperature": temperature, "stream": True,
                   "max_tokens": max_tokens or int(config.get("max_tokens") or 2048)}
        url = self._endpoint("chat", "/chat/completions")
        with self._client.stream("POST", url, headers=self._headers("chat"), json=payload) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", "ignore")[:300]
                raise ModelError(f"对话失败（HTTP {response.status_code}）：{body}")
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line in ("", "[DONE]"):
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    yield delta

    # ------------------------------------------------------------ 重排
    def rerank_configured(self) -> bool:
        """重排角色是否配置齐（base_url + 模型名）——与门户「系统设置」凭据判据同一口径。"""
        config = self.resolve("rerank")
        return bool(config.get("base_url") and config.get("name"))

    def rerank(self, query: str, documents: List[str],
               top_n: Optional[int] = None) -> Dict[str, Any]:
        """调用重排模型（OpenAI / Jina 兼容的 `POST /v1/rerank`）。

        返回 `{"model": …, "results": [{"index": i, "relevance_score": s, "document": …}, …]}`，
        `results` 由服务端按 relevance_score 降序给出，调用方直接用顺序即可。
        """
        if not documents:
            return {"model": "", "results": []}
        config = self.resolve("rerank")
        if not (config.get("base_url") and config.get("name")):
            raise ModelError("重排模型未配置：请在「模型接入配置」补一条接入配置并绑定 rerank 角色，"
                             "或填 equipment.env 的 MODEL_RERANK_*")
        payload: Dict[str, Any] = {"model": config.get("name"), "query": query,
                                   "documents": documents, "top_n": top_n or len(documents)}
        data = self._post("rerank", "/rerank", payload)
        results = data.get("results")
        if not isinstance(results, list):
            raise ModelError(f"重排返回格式不符（缺 results）：{json.dumps(data, ensure_ascii=False)[:200]}")
        return {"model": data.get("model") or config.get("name"), "results": results}

    # ------------------------------------------------------------ 连通性测试
    def test(self, role: str) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            if role == "embedding":
                vector = self.embed_one("连通性测试")
                return {"ok": True, "detail": f"嵌入维度 {len(vector)}",
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "config": self.public_config()["embedding"]}
            if role == "chat":
                answer = self.chat([{"role": "user", "content": "只回复两个字：就绪"}], max_tokens=16)
                return {"ok": True, "detail": f"模型回复：{answer.strip()[:40]}",
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "config": self.public_config()["chat"]}
            if role == "rerank":
                reranked = self.rerank("液压系统的额定工作压力是多少？",
                                       ["额定工作压力 16 MPa。", "滤芯每 250 工作小时更换一次。"])
                results = reranked.get("results") or []
                top = results[0] if results else {}
                return {"ok": True,
                        "detail": f"重排可用：{len(results)} 条打分，最高 {top.get('relevance_score')}"
                                  f"（模型 {reranked.get('model')}）",
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "config": self.public_config()["rerank"]}
            return {"ok": False, "detail": f"{role} 暂不支持连通性测试"}
        except (ModelError, httpx.HTTPError) as exc:
            return {"ok": False, "detail": str(exc)[:300],
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "config": self.public_config().get(role, {})}

    def _post(self, role: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self._endpoint(role, path)
        try:
            response = self._client.post(url, headers=self._headers(role), json=payload)
        except httpx.HTTPError as exc:
            raise ModelError(f"{role} 模型不可达（{url}）：{exc}") from exc
        if response.status_code >= 400:
            raise ModelError(f"{role} 模型调用失败（HTTP {response.status_code}）：{response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ModelError(f"{role} 模型返回非 JSON：{response.text[:200]}") from exc
