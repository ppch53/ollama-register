"""OpenAI-compatible adapter for Puter `/drivers/call`.

Listens on 127.0.0.1:8001 (or HOST/PORT env) and exposes:
  GET  /v1/models                      - list models that Puter can route
  POST /v1/chat/completions            - OpenAI Chat Completions
                                         (stream + non-stream)

Per-request flow:
  1. Read `Authorization: Bearer <puter_token>` from the incoming request.
  2. Translate the OpenAI body to Puter's `/drivers/call` envelope:
       {"interface": "puter-chat-completion",
        "driver": "ai-chat",
        "method": "complete",
        "args": {"model": ..., "messages": ..., "stream": true|false,
                 "max_tokens": ..., "temperature": ..., ...}}
  3. POST to https://api.puter.com/drivers/call via the rayobyte proxy
     (PROXY env var, defaults to http://127.0.0.1:1081).
  4. Translate the Puter response back to OpenAI Chat Completion shape.

Streaming: Puter returns application/x-ndjson. Each chunk is one JSON
line; we re-emit as OpenAI SSE chunks (`data: {...}\n\n` + `data: [DONE]`).

Tooling/system: kept simple. Only "messages" and "model" are required.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx
from quart import Quart, Response, abort, jsonify, request

PUTER_API = os.environ.get("PUTER_API", "https://api.puter.com")
PROXY = os.environ.get("PROXY", "http://127.0.0.1:1081")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8001"))

# Lock list of models we expose (sample from earlier probe).
DEFAULT_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "claude-sonnet-4-5",
    "claude-3-5-sonnet-latest",
    "claude-3-7-sonnet",
    "gpt-5",
    "gpt-4o",
    "gpt-4o-mini",
    "deepseek-chat",
    "deepseek-reasoner",
    "kimi-k2.6",
    "minimax-m2.5",
    "minimax-m2.7",
    "glm-5",
    "glm-5.1",
    "glm-4.7",
    "qwen3.5",
    "qwen3-coder:480b",
    "qwen3-next:80b",
    "nemotron-3-super",
    "gemma4:31b",
    "gemma3:27b",
]

app = Quart(__name__)
app.config["RESPONSE_TIMEOUT"] = 600


def _client_kwargs() -> dict:
    kw: dict = {"timeout": httpx.Timeout(120.0, connect=30.0), "http2": False}
    if PROXY:
        kw["proxy"] = PROXY
    return kw


def _bearer() -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401, description="Missing Bearer token")
    return auth[7:]


def _translate_openai_to_puter(body: dict) -> dict:
    args: dict[str, Any] = {
        "messages": body.get("messages") or [],
    }
    if "model" in body and body["model"]:
        args["model"] = body["model"]
    for k in (
        "temperature",
        "max_tokens",
        "stream",
        "tools",
        "tool_choice",
        "top_p",
        "stop",
        "response_format",
        "reasoning_effort",
    ):
        if k in body and body[k] is not None:
            args[k] = body[k]
    return {
        "interface": "puter-chat-completion",
        "driver": "ai-chat",
        "method": "complete",
        "args": args,
    }


def _normalise_message(raw_msg: dict | None) -> dict:
    """Puter's response message has different shapes across providers."""
    if not raw_msg:
        return {"role": "assistant", "content": ""}
    # Anthropic-shape: {"role":"assistant","content":[{"type":"text","text":"..."}]}
    if isinstance(raw_msg.get("content"), list):
        text_parts = []
        for part in raw_msg["content"]:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        return {
            "role": raw_msg.get("role", "assistant"),
            "content": "".join(text_parts),
        }
    # OpenAI-shape passthrough
    return {
        "role": raw_msg.get("role", "assistant"),
        "content": raw_msg.get("content") or "",
    }


def _translate_puter_to_openai(puter_response: dict, *, model_used: str) -> dict:
    """Non-stream: convert {success: true, result: {...}} to OpenAI shape."""
    if not puter_response.get("success", True):
        # an error from puter
        err = puter_response.get("error") or puter_response.get("message") or "unknown puter error"
        raise RuntimeError(f"puter error: {err}")
    result = puter_response.get("result") or {}
    msg = _normalise_message(result.get("message"))
    usage = result.get("usage") or {}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_used,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "logprobs": result.get("logprobs"),
                "finish_reason": result.get("finish_reason") or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
        },
    }


@app.route("/v1/models", methods=["GET"])
async def models() -> Any:
    return jsonify(
        {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "puter"}
                for m in DEFAULT_MODELS
            ],
        }
    )


@app.route("/v1/chat/completions", methods=["POST"])
async def chat_completions() -> Any:
    token = _bearer()
    body = await request.get_json(silent=True) or {}
    model = body.get("model") or "gpt-4o-mini"
    stream = bool(body.get("stream"))

    puter_body = _translate_openai_to_puter(body)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://puter.com",
        "Referer": "https://puter.com/",
    }

    if not stream:
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            try:
                r = await client.post(
                    f"{PUTER_API}/drivers/call", json=puter_body, headers=headers
                )
            except httpx.HTTPError as exc:
                return jsonify({"error": {"message": f"upstream: {exc}", "type": "upstream_error"}}), 502
        if r.status_code >= 400:
            return Response(
                r.content,
                status=r.status_code,
                content_type=r.headers.get("Content-Type", "application/json"),
            )
        try:
            payload = r.json()
        except ValueError:
            return jsonify({"error": {"message": "non-json upstream", "type": "upstream_error"}}), 502
        try:
            translated = _translate_puter_to_openai(payload, model_used=model)
        except RuntimeError as exc:
            return jsonify({"error": {"message": str(exc), "type": "puter_error"}}), 502
        return jsonify(translated)

    # streaming path - puter returns application/x-ndjson, we re-emit as SSE
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    async def sse_stream():
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{PUTER_API}/drivers/call",
                    json=puter_body,
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        text = await resp.aread()
                        yield f"data: {json.dumps({'error': text.decode('utf-8','ignore')})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except ValueError:
                            continue
                        # detect insufficient_funds / errors
                        if chunk.get("error"):
                            yield (
                                "data: "
                                + json.dumps({"error": chunk["error"]})
                                + "\n\n"
                            )
                            continue
                        # normal chunk: text in chunk["text"] or chunk["content"]
                        delta_text = ""
                        if isinstance(chunk.get("text"), str):
                            delta_text = chunk["text"]
                        elif isinstance(chunk.get("content"), str):
                            delta_text = chunk["content"]
                        elif isinstance(chunk.get("delta"), str):
                            delta_text = chunk["delta"]
                        if delta_text:
                            payload = {
                                "id": request_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"role": "assistant", "content": delta_text},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(payload)}\n\n"
                        if chunk.get("type") == "usage" or chunk.get("metadata"):
                            # ignore for now, openai chunking doesn't carry usage
                            continue
            except httpx.HTTPError as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        # final tail
        tail = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(sse_stream(), mimetype="text/event-stream")


@app.route("/health", methods=["GET"])
async def health() -> Any:
    return jsonify({"ok": True, "proxy": PROXY, "puter_api": PUTER_API})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
