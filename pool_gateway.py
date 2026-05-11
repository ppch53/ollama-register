"""
pool_gateway.py — OpenAI-compatible unified gateway with built-in pool scheduling,
dual-group routing (Puter / Ollama), master-key auth and per-key quota tracking.

Runs on 0.0.0.0:8002 (or PORT env) and exposes:
  GET  /v1/models
  POST /v1/chat/completions   (stream + non-stream)
  GET  /health
  GET  /admin/status          (no auth — bind to localhost only)

Env:
  MASTER_KEY            required
  PUTER_ADAPTER_URL     default http://127.0.0.1:8001
  OLLAMA_BASE_URL       default https://ollama.com
  PUTER_ACCOUNTS_FILE   default /opt/ollama-register/puter_accounts.json
  OLLAMA_ACCOUNTS_FILE  default /opt/ollama-register/accounts.json
  STATE_FILE            default /opt/ollama-register/pool_state.json
  PORT                  default 8002
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncGenerator

import httpx
from quart import Quart, Response, abort, jsonify, request

MASTER_KEY = os.environ.get("MASTER_KEY", "")
PUTER_ADAPTER_URL = os.environ.get("PUTER_ADAPTER_URL", "http://127.0.0.1:8001")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
PUTER_HEALTH_URL = os.environ.get("PUTER_HEALTH_URL", "https://api.puter.com/whoami")
OLLAMA_TAGS_URL = os.environ.get("OLLAMA_TAGS_URL", "https://ollama.com/api/tags")
PUTER_ACCOUNTS_FILE = os.environ.get("PUTER_ACCOUNTS_FILE", "/opt/ollama-register/puter_accounts.json")
OLLAMA_ACCOUNTS_FILE = os.environ.get("OLLAMA_ACCOUNTS_FILE", "/opt/ollama-register/accounts.json")
STATE_FILE = os.environ.get("STATE_FILE", "/opt/ollama-register/pool_state.json")
PORT = int(os.environ.get("PORT", "8002"))
ENABLED_BACKENDS = {
    backend.strip()
    for backend in os.environ.get("ENABLED_BACKENDS", "puter,ollama").split(",")
    if backend.strip()
}

# Model → backend mapping
PUTER_MODELS = {
    "gemini-3.1-pro-preview", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash",
    "claude-sonnet-4-5", "claude-3-5-sonnet-latest", "claude-3-7-sonnet",
    "gpt-5", "gpt-4o", "gpt-4o-mini",
    "deepseek-chat", "deepseek-reasoner",
    "kimi-k2.6", "minimax-m2.5", "minimax-m2.7",
    "glm-5", "glm-5.1", "glm-4.7",
    "qwen3.5", "qwen3-coder:480b", "qwen3-next:80b",
    "nemotron-3-super", "gemma4:31b", "gemma3:27b",
}

OLLAMA_MODELS = {
    "gpt-oss:120b", "gpt-oss:20b",
    "qwen3-coder:480b", "qwen3-next:80b",
    "gemma3:27b", "gemma3:4b",
    "glm-4.7:cloud", "minimax-m2.5:cloud", "nemotron-3-super:cloud",
}

ALL_MODELS = sorted(PUTER_MODELS | OLLAMA_MODELS)


def _route_model(model: str) -> str:
    if model in OLLAMA_MODELS:
        return "ollama"
    if model in PUTER_MODELS:
        return "puter"
    # heuristic fallback
    if ":cloud" in model or model.startswith("gpt-oss:"):
        return "ollama"
    return "puter"


def _backend_enabled(name: str) -> bool:
    return name in ENABLED_BACKENDS


@dataclass
class KeyRecord:
    key: str
    healthy: bool = True
    requests: int = 0
    tokens: int = 0
    errors: int = 0
    last_used: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "KeyRecord":
        return cls(**d)


@dataclass
class Pool:
    name: str
    keys: list[KeyRecord] = field(default_factory=list)
    _idx: int = field(default=0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def to_dict(self) -> dict:
        return {"keys": [k.to_dict() for k in self.keys]}

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "Pool":
        pool = cls(name=name)
        pool.keys = [KeyRecord.from_dict(x) for x in d.get("keys", [])]
        return pool

    def healthy_keys(self) -> list[KeyRecord]:
        return [k for k in self.keys if k.healthy]

    def pick(self) -> KeyRecord | None:
        hk = self.healthy_keys()
        if not hk:
            return None
        # weighted random: prefer less-used keys
        weights = [1.0 / (1 + k.requests) for k in hk]
        total = sum(weights)
        r = random.random() * total
        upto = 0.0
        for k, w in zip(hk, weights):
            upto += w
            if r <= upto:
                return k
        return hk[-1]

    def mark(self, key: str, *, healthy: bool | None = None, error: str = "", tokens: int = 0) -> None:
        for k in self.keys:
            if k.key == key:
                if healthy is not None:
                    k.healthy = healthy
                if error:
                    k.errors += 1
                    k.last_error = error
                if tokens:
                    k.tokens += tokens
                k.requests += 1
                k.last_used = time.time()
                break

    def set_health(self, key: str, *, healthy: bool, error: str = "") -> None:
        for k in self.keys:
            if k.key == key:
                k.healthy = healthy
                if error:
                    k.last_error = error
                break


class Gateway:
    def __init__(self) -> None:
        self.app = Quart(__name__)
        self.pools: dict[str, Pool] = {}
        self.state_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.route("/v1/models", methods=["GET"])(self.models_route)
        self.app.route("/v1/chat/completions", methods=["POST"])(self.chat_route)
        self.app.route("/health", methods=["GET"])(self.health_route)

        self.app.before_serving(self._on_start)
        self.app.after_serving(self._on_stop)

    async def _on_start(self) -> None:
        self._load_state()
        t1 = asyncio.create_task(self._save_loop())
        t2 = asyncio.create_task(self._health_probe_loop())
        t3 = asyncio.create_task(self._probe_all_pools())
        self._bg_tasks.update({t1, t2, t3})

    async def _on_stop(self) -> None:
        for t in self._bg_tasks:
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        await self._persist()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        puter_tokens: list[str] = []
        ollama_keys: list[str] = []

        try:
            with open(PUTER_ACCOUNTS_FILE, encoding="utf-8") as f:
                puter_tokens = [a["token"] for a in json.load(f)]
        except Exception as exc:
            print(f"[pool] warn loading puter accounts: {exc}")

        try:
            with open(OLLAMA_ACCOUNTS_FILE, encoding="utf-8") as f:
                accounts = json.load(f)
                ollama_keys = [
                    a["api_key"]
                    for a in accounts
                    if (a.get("status") or "verified") != "unverified"
                ]
        except Exception as exc:
            print(f"[pool] warn loading ollama accounts: {exc}")

        # merge with persisted state (keeps counters / health flags)
        persisted: dict = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    persisted = json.load(f)
            except Exception as exc:
                print(f"[pool] warn loading state: {exc}")

        self.pools["puter"] = self._merge_pool("puter", puter_tokens, persisted.get("puter", {}))
        self.pools["ollama"] = self._merge_pool("ollama", ollama_keys, persisted.get("ollama", {}))
        print(f"[pool] loaded puter={len(self.pools['puter'].keys)} ollama={len(self.pools['ollama'].keys)}")

    @staticmethod
    def _merge_pool(name: str, fresh_keys: list[str], persisted: dict) -> Pool:
        pool = Pool.from_dict(name, persisted)
        existing = {k.key for k in pool.keys}
        for fk in fresh_keys:
            if fk not in existing:
                pool.keys.append(KeyRecord(key=fk, healthy=True))
        # drop keys no longer in source files (unless manually added)
        pool.keys = [k for k in pool.keys if k.key in set(fresh_keys)]
        return pool

    async def _persist(self) -> None:
        async with self.state_lock:
            payload = {name: pool.to_dict() for name, pool in self.pools.items()}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self._persist()

    async def _health_probe_loop(self) -> None:
        """Every 10 min probe all configured backends with authenticated upstream requests."""
        while True:
            await asyncio.sleep(600)
            await self._probe_all_pools()

    async def _probe_all_pools(self) -> None:
        for name, pool in self.pools.items():
            if not _backend_enabled(name):
                continue
            for rec in pool.keys:
                await self._probe_key(name, pool, rec)

    async def _probe_key(self, name: str, pool: Pool, rec: KeyRecord) -> None:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if name == "puter":
                    response = await client.get(
                        PUTER_HEALTH_URL,
                        headers={"Authorization": f"Bearer {rec.key}"},
                    )
                    text = response.text[:500]
                    if response.status_code == 200:
                        try:
                            payload = response.json()
                        except Exception:
                            payload = {}
                        if payload.get("username") or payload.get("user"):
                            pool.set_health(rec.key, healthy=True, error="")
                            return
                    if response.status_code in (401, 403) or "suspended" in text.lower():
                        pool.set_health(rec.key, healthy=False, error=text)
                        return
                    pool.mark(rec.key, error=text)
                    return

                response = await client.get(
                    OLLAMA_TAGS_URL,
                    headers={"Authorization": f"Bearer {rec.key}"},
                )
                if response.status_code == 200:
                    pool.set_health(rec.key, healthy=True, error="")
                elif response.status_code in (401, 403):
                    pool.set_health(rec.key, healthy=False, error=response.text[:500])
                else:
                    pool.mark(rec.key, error=response.text[:500])
        except Exception as exc:
            pool.mark(rec.key, error=str(exc))

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    async def models_route(self) -> Any:
        active_models: list[str] = []
        if _backend_enabled("puter") and self.pools.get("puter") and self.pools["puter"].keys:
            active_models.extend(sorted(PUTER_MODELS))
        if _backend_enabled("ollama") and self.pools.get("ollama") and self.pools["ollama"].keys:
            active_models.extend(sorted(OLLAMA_MODELS))
        return jsonify({
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "pool"}
                for m in active_models
            ],
        })

    async def health_route(self) -> Any:
        health = {}
        for name, pool in self.pools.items():
            hk = pool.healthy_keys()
            health[name] = {"total": len(pool.keys), "healthy": len(hk)}
        return jsonify(health)

    async def chat_route(self) -> Any:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != MASTER_KEY:
            abort(401, description="Invalid master key")

        body = await request.get_json(silent=True) or {}
        model = body.get("model", "")
        stream = bool(body.get("stream"))
        backend = _route_model(model)
        if not _backend_enabled(backend):
            return jsonify({"error": {"message": f"Backend {backend} is disabled", "type": "backend_disabled"}}), 503
        pool = self.pools.get(backend)
        if pool is None:
            abort(400, description=f"Unknown backend for model {model}")

        # Try up to 3 healthy keys for non-stream; 1 for stream
        max_attempts = 3 if not stream else 1
        last_err: dict | None = None

        for _ in range(max_attempts):
            rec = pool.pick()
            if rec is None:
                break

            upstream_base = PUTER_ADAPTER_URL if backend == "puter" else OLLAMA_BASE_URL
            upstream_url = f"{upstream_base}/v1/chat/completions"
            upstream_headers = {
                "Authorization": f"Bearer {rec.key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            if stream:
                return await self._proxy_stream(upstream_url, upstream_headers, body, pool, rec)

            resp = await self._proxy_once(upstream_url, upstream_headers, body, pool, rec)
            if isinstance(resp, Response):
                return resp
            if isinstance(resp, dict) and resp.get("ok"):
                # Build Quart response from bytes
                r = resp["response"]
                return Response(
                    r.content,
                    status=r.status_code,
                    content_type=r.headers.get("content-type", "application/json"),
                )
            last_err = resp

        err_msg = last_err["error"] if last_err else "No healthy upstream keys available"
        return jsonify({"error": {"message": err_msg, "type": "pool_exhausted"}}), 503

    # ------------------------------------------------------------------
    # Proxy helpers
    # ------------------------------------------------------------------
    async def _proxy_once(
        self,
        url: str,
        headers: dict,
        body: dict,
        pool: Pool,
        rec: KeyRecord,
    ) -> dict:
        """Returns {"ok": True, "response": r} on success, {"error": msg} on failure."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
                r = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            pool.mark(rec.key, error=str(exc))
            return {"error": f"upstream network error: {exc}"}

        # Decide if this is a terminal upstream error
        if r.status_code >= 400:
            text = r.text[:500]
            # Auth / quota / ban → mark dead
            if r.status_code in (401, 403):
                pool.mark(rec.key, healthy=False, error=text)
            elif r.status_code == 402 or "insufficient" in text.lower() or "quota" in text.lower():
                pool.mark(rec.key, healthy=False, error=text)
            elif r.status_code == 429:
                pool.mark(rec.key, error=text)  # keep alive, just rate-limited
            else:
                pool.mark(rec.key, error=text)
            return {"error": f"upstream {r.status_code}: {text}"}

        # Try to extract usage for bookkeeping
        tokens_used = 0
        try:
            data = r.json()
            usage = data.get("usage") or {}
            tokens_used = (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
        except Exception:
            pass
        pool.mark(rec.key, tokens=tokens_used)
        return {"ok": True, "response": r}

    async def _proxy_stream(
        self,
        url: str,
        headers: dict,
        body: dict,
        pool: Pool,
        rec: KeyRecord,
    ) -> Response:
        async def _gen() -> AsyncGenerator[bytes, None]:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(300.0, connect=30.0)
                ) as client:
                    async with client.stream("POST", url, headers=headers, json=body) as resp:
                        if resp.status_code >= 400:
                            text = await resp.aread()
                            pool.mark(rec.key, error=text.decode("utf-8", "ignore")[:500])
                            yield text or b'{"error":"upstream error"}'
                            return
                        pool.mark(rec.key)
                        async for chunk in resp.aiter_raw():
                            if chunk:
                                yield chunk
            except httpx.HTTPError as exc:
                pool.mark(rec.key, error=str(exc))
                yield json.dumps({"error": str(exc)}).encode()

        return Response(_gen(), content_type="text/event-stream")


gateway = Gateway()
app = gateway.app

if __name__ == "__main__":
    if not MASTER_KEY:
        raise SystemExit("MASTER_KEY env var is required")
    app.run(host="0.0.0.0", port=PORT)
