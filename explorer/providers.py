"""
LLM providers behind one normalized streaming interface.

Two backends, deliberately different wire formats hidden behind the same
event stream so the agent loop never branches on provider:

    local   OpenAI-compatible  ->  http://127.0.0.1:8500/v1  (llama.cpp)
    claude  Anthropic Messages ->  https://api.anthropic.com/v1/messages

Both yield events of these shapes:

    {"type": "text",       "text": "..."}          incremental answer text
    {"type": "tool_call",  "id", "name", "input"}  a complete tool call
    {"type": "done",       "stop_reason": "..."}   turn finished
    {"type": "error",      "message": "..."}       fatal for this turn

Streaming matters here: the frontend's thinking parser consumes <think>
tokens as they arrive, so a batched response would defeat it.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

LOCAL_BASE = os.environ.get("EXPLORER_LLM_BASE", "http://127.0.0.1:8500/v1")
# Deliberately NOT a hardcoded model name. The local router swaps models in and
# out, so any pinned id eventually 404s ("model 'x' not found"). Set this only
# to force one; otherwise the active models are discovered at request time.
LOCAL_MODEL_OVERRIDE = os.environ.get("EXPLORER_LLM_MODEL", "").strip()

CLAUDE_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
CLAUDE_MODEL = os.environ.get("EXPLORER_CLAUDE_MODEL", "claude-sonnet-5")
CLAUDE_VERSION = "2023-06-01"

# Output cap per LLM call. 2000 was cutting real answers mid-sentence
# (finish_reason "length" at exactly 2000 completion tokens) with nothing shown
# to the reader. The local slot is 64K (--ctx-size 131072 / --parallel 2), and
# input — system prompt, 13 tool schemas, history, tool results — typically runs
# ~10K, so 32K of output still leaves headroom. llama.cpp clamps to whatever
# context actually remains, so an unusually long input degrades rather than
# erroring.
MAX_TOKENS = int(os.environ.get("EXPLORER_MAX_TOKENS", "32768"))
REQUEST_TIMEOUT = httpx.Timeout(900.0, connect=15.0)


class ProviderError(RuntimeError):
    pass


async def discover_local_models() -> list[dict[str, Any]]:
    """Ask the local router what it is actually serving right now.

    Embedding models are excluded by their own launch flags rather than by
    guessing from the name: a server started with --embeddings cannot answer a
    chat completion, so offering it would only produce a confusing failure.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
            response = await http.get(f"{LOCAL_BASE}/models")
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return []

    models = []
    for entry in payload.get("data", []):
        model_id = entry.get("id")
        if not model_id:
            continue
        status = entry.get("status") or {}
        args = status.get("args") or []
        if "--embeddings" in args or "--reranking" in args:
            continue
        context = None
        if "--ctx-size" in args:
            try:
                context = int(args[args.index("--ctx-size") + 1])
            except (ValueError, IndexError):
                context = None
        parallel = 1
        if "--parallel" in args:
            try:
                parallel = max(1, int(args[args.index("--parallel") + 1]))
            except (ValueError, IndexError):
                parallel = 1
        models.append({
            "id": model_id,
            "state": status.get("value"),
            "context": context,
            # A router slot gets ctx-size/parallel, which is the real per-request budget.
            "slot_context": (context // parallel) if context else None,
        })
    return models


async def available_providers() -> dict[str, Any]:
    """What the UI should offer, and whether each is actually usable.

    Local entries are whatever the router reports now, one per chat model, so
    the dropdown follows the machine instead of a constant in this file.
    """
    providers: dict[str, Any] = {}

    discovered = await discover_local_models()
    if LOCAL_MODEL_OVERRIDE:
        discovered = [m for m in discovered if m["id"] == LOCAL_MODEL_OVERRIDE] or [
            {"id": LOCAL_MODEL_OVERRIDE, "state": "pinned", "context": None, "slot_context": None}
        ]

    for model in discovered:
        budget = model.get("slot_context")
        detail = f"{LOCAL_BASE} · {model['id']}"
        if budget:
            detail += f" · {budget // 1024}K context per slot"
        providers[f"local:{model['id']}"] = {
            "label": f"Local · {model['id']}",
            "ready": True,
            "detail": detail,
        }

    if not providers:
        providers["local"] = {
            "label": "Local · unavailable",
            "ready": False,
            "detail": f"No chat model is being served at {LOCAL_BASE}",
        }

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    providers["claude"] = {
        "label": f"Claude · {CLAUDE_MODEL}",
        "ready": has_key,
        "detail": "ANTHROPIC_API_KEY is set" if has_key else "ANTHROPIC_API_KEY not set",
    }
    return providers


# --------------------------------------------------------------------------
# tool schema conversion
# --------------------------------------------------------------------------

def tools_for_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        schema = dict(t.get("inputSchema") or {"type": "object", "properties": {}})
        schema.pop("$schema", None)
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return out


def tools_for_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        schema = dict(t.get("inputSchema") or {"type": "object", "properties": {}})
        schema.pop("$schema", None)
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": schema,
        })
    return out


# --------------------------------------------------------------------------
# conversation conversion
#
# The agent keeps one neutral history:
#   {"role": "user"|"assistant", "content": str}
#   {"role": "assistant", "tool_calls": [{"id","name","input"}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# --------------------------------------------------------------------------

def convo_for_openai(history: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] == "tool":
            msgs.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            })
        elif m.get("tool_calls"):
            msgs.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("input") or {}),
                        },
                    }
                    for c in m["tool_calls"]
                ],
            })
        else:
            msgs.append({"role": m["role"], "content": m.get("content", "")})
    return msgs


def convo_for_anthropic(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic carries tool results as user-turn tool_result blocks, and
    consecutive same-role turns must be merged."""
    msgs: list[dict[str, Any]] = []

    def push(role: str, block: Any) -> None:
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"].append(block)
        else:
            msgs.append({"role": role, "content": [block]})

    for m in history:
        if m["role"] == "tool":
            push("user", {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            })
        elif m.get("tool_calls"):
            if m.get("content"):
                push("assistant", {"type": "text", "text": m["content"]})
            for c in m["tool_calls"]:
                push("assistant", {
                    "type": "tool_use",
                    "id": c["id"],
                    "name": c["name"],
                    "input": c.get("input") or {},
                })
        else:
            push(m["role"], {"type": "text", "text": m.get("content", "")})
    return msgs


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

async def _sse_lines(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for raw in response.aiter_lines():
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


async def stream_local(
    history: list[dict[str, Any]], tools: list[dict[str, Any]], system: str,
    model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    resolved = model or LOCAL_MODEL_OVERRIDE
    if not resolved:
        # No model named by the caller and none pinned: take whatever the
        # router is serving rather than guessing a name that may not exist.
        discovered = await discover_local_models()
        if not discovered:
            yield {"type": "error", "message":
                   f"No chat model is being served at {LOCAL_BASE}."}
            return
        resolved = discovered[0]["id"]

    body = {
        "model": resolved,
        "messages": convo_for_openai(history, system),
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
        "stream": True,
    }
    if tools:
        body["tools"] = tools_for_openai(tools)
        body["tool_choice"] = "auto"

    # tool_calls arrive as indexed deltas that must be reassembled
    pending: dict[int, dict[str, Any]] = {}
    stop_reason = "stop"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            async with client.stream(
                "POST", f"{LOCAL_BASE}/chat/completions", json=body
            ) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode(errors="replace")[:400]
                    yield {"type": "error", "message": f"local LLM {response.status_code}: {detail}"}
                    return
                async for chunk in _sse_lines(response):
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        stop_reason = choice["finish_reason"]
                    text = delta.get("content")
                    if text:
                        yield {"type": "text", "text": text}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        except httpx.HTTPError as exc:
            yield {"type": "error", "message": f"local LLM unreachable: {exc}"}
            return

    for idx in sorted(pending):
        slot = pending[idx]
        if not slot["name"]:
            continue
        try:
            parsed = json.loads(slot["args"]) if slot["args"].strip() else {}
        except json.JSONDecodeError:
            parsed = {"__raw": slot["args"]}
        yield {
            "type": "tool_call",
            "id": slot["id"] or f"call_{idx}",
            "name": slot["name"],
            "input": parsed,
        }
    yield {"type": "done", "stop_reason": stop_reason}


async def stream_claude(
    history: list[dict[str, Any]], tools: list[dict[str, Any]], system: str
) -> AsyncIterator[dict[str, Any]]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        yield {
            "type": "error",
            "message": (
                "ANTHROPIC_API_KEY is not set in this environment, so the Claude "
                "provider cannot be used. Export a key and restart the explorer, "
                "or switch the provider back to Local."
            ),
        }
        return

    body: dict[str, Any] = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": convo_for_anthropic(history),
        "stream": True,
    }
    if tools:
        body["tools"] = tools_for_anthropic(tools)

    blocks: dict[int, dict[str, Any]] = {}
    stop_reason = "end_turn"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            async with client.stream(
                "POST",
                f"{CLAUDE_BASE}/v1/messages",
                json=body,
                headers={
                    "x-api-key": key,
                    "anthropic-version": CLAUDE_VERSION,
                    "content-type": "application/json",
                },
            ) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode(errors="replace")[:400]
                    yield {"type": "error", "message": f"Claude {response.status_code}: {detail}"}
                    return
                async for event in _sse_lines(response):
                    kind = event.get("type")
                    if kind == "content_block_start":
                        blocks[event["index"]] = dict(event.get("content_block") or {})
                        blocks[event["index"]]["_json"] = ""
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            yield {"type": "text", "text": delta.get("text", "")}
                        elif delta.get("type") == "input_json_delta":
                            slot = blocks.setdefault(event["index"], {"_json": ""})
                            slot["_json"] = slot.get("_json", "") + delta.get("partial_json", "")
                    elif kind == "content_block_stop":
                        slot = blocks.get(event["index"]) or {}
                        if slot.get("type") == "tool_use":
                            raw = slot.get("_json", "")
                            try:
                                parsed = json.loads(raw) if raw.strip() else (slot.get("input") or {})
                            except json.JSONDecodeError:
                                parsed = {"__raw": raw}
                            yield {
                                "type": "tool_call",
                                "id": slot.get("id", ""),
                                "name": slot.get("name", ""),
                                "input": parsed,
                            }
                    elif kind == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    elif kind == "error":
                        yield {"type": "error", "message": json.dumps(event.get("error"))}
                        return
        except httpx.HTTPError as exc:
            yield {"type": "error", "message": f"Claude unreachable: {exc}"}
            return

    yield {"type": "done", "stop_reason": stop_reason}


def stream(provider: str, history, tools, system):
    """`provider` is "claude" or "local[:<model id>]".

    The model id travels in the provider string so the UI can offer every
    served model without this module knowing any of their names.
    """
    name, _, model = (provider or "local").partition(":")
    if name == "claude":
        return stream_claude(history, tools, system)
    return stream_local(history, tools, system, model=model or None)
