"""
Text-to-speech relay for tts_server.

Why a relay rather than a direct browser connection: tts_server's Socket.IO
allows a fixed list of origins and this app's is not on it. Connecting from the
backend keeps that service untouched — no shared config to edit, no port to
change here — and the browser talks only to its own origin.

(stt_server needs no relay: it sets cors_allowed_origins="*", so the page
connects to it directly and streams microphone PCM without a hop.)

Protocol, read from tts_server.py:
  -> register_audio_client {main_client_id, format:"base64", voice, speed}
  -> tts_text_chunk        {chunk, final, target_client_id}
  <- tts_audio_chunk       {audio_data: <base64 wav>, sample_rate: 24000, ...}
  <- tts_response_complete
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import socketio

TTS_URL = os.environ.get("EXPLORER_TTS_URL", "http://127.0.0.1:7700")
TTS_VOICE = os.environ.get("EXPLORER_TTS_VOICE", "af_heart")
TTS_SPEED = float(os.environ.get("EXPLORER_TTS_SPEED", "1.0"))
# Kokoro synthesises sentence by sentence; a long answer legitimately takes a
# while, but an unbounded wait would hang the request if the service stalls.
TTS_TIMEOUT = float(os.environ.get("EXPLORER_TTS_TIMEOUT", "180"))
# tts_server splits on sentences; anything past this is almost certainly a
# mis-click on a huge answer rather than something a person wants read aloud.
MAX_CHARS = int(os.environ.get("EXPLORER_TTS_MAX_CHARS", "4000"))


async def synthesize(text: str, voice: str | None = None,
                     speed: float | None = None) -> dict[str, Any]:
    """Speak `text`, returning the audio chunks tts_server produced.

    One short-lived connection per request. Kokoro holds per-session state
    keyed on the socket id, so a shared long-lived client would interleave
    concurrent requests' audio.
    """
    text = (text or "").strip()
    if not text:
        return {"chunks": [], "error": "empty text"}
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]

    client = socketio.AsyncClient(logger=False, engineio_logger=False)
    client_id = f"pdf-mcp-explorer-{uuid.uuid4().hex[:8]}"
    chunks: list[dict[str, Any]] = []
    finished = asyncio.Event()
    failure: dict[str, Any] = {}

    @client.on("tts_audio_chunk")
    async def _chunk(data):  # noqa: ANN001
        audio = (data or {}).get("audio_data")
        if audio:
            chunks.append({"audio": audio,
                           "sample_rate": data.get("sample_rate", 24000),
                           "text": data.get("sentence_text", "")})

    @client.on("tts_response_complete")
    async def _done(data):  # noqa: ANN001
        finished.set()

    @client.on("tts_error")
    async def _error(data):  # noqa: ANN001
        failure["message"] = str((data or {}).get("error", "tts_error"))
        finished.set()

    try:
        await client.connect(TTS_URL, transports=["websocket", "polling"],
                             wait_timeout=15)
        await client.emit("register_audio_client", {
            "main_client_id": client_id,
            "connection_type": "browser",
            "mode": "tts",
            "format": "base64",
            "voice": voice or TTS_VOICE,
            "speed": speed if speed is not None else TTS_SPEED,
            "enabled": True,
        })
        # The server maps audio back by target_client_id; its own sid is the
        # fallback, so send the whole text as one final chunk.
        await client.emit("tts_text_chunk", {
            "chunk": text, "final": True, "target_client_id": client_id,
        })
        try:
            await asyncio.wait_for(finished.wait(), timeout=TTS_TIMEOUT)
        except asyncio.TimeoutError:
            failure.setdefault("message",
                               f"tts_server did not finish within {TTS_TIMEOUT:.0f}s")
    except Exception as exc:
        failure["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return {"chunks": chunks, "truncated": truncated,
            "error": failure.get("message") if not chunks else None}


async def health() -> dict[str, Any]:
    """Whether voice output is usable, without synthesising anything."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as http:
            r = await http.get(f"{TTS_URL}/health")
            r.raise_for_status()
            data = r.json()
        return {"available": data.get("status") == "healthy",
                "url": TTS_URL, "voice": TTS_VOICE,
                "version": data.get("version")}
    except Exception as exc:
        return {"available": False, "url": TTS_URL,
                "detail": f"{type(exc).__name__}: {exc}"}
