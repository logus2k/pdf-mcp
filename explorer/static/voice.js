/* Voice I/O.
 *
 * TTS goes through this app's backend (/api/speak): tts_server's Socket.IO
 * allows a fixed origin list that this app is not on, so relaying server-side
 * avoids editing a shared service.
 *
 * STT connects straight to stt_server, which sets cors_allowed_origins="*".
 * Microphone audio is captured with the AudioWorklet + resampler vendored from
 * cv/widget (originally noted): mono Float32 off the render thread, resampled
 * to the 16 kHz PCM16 that stt_server expects.
 */

import { AudioResampler } from "./audioResampler.js";

const el = (id) => document.getElementById(id);

/* ---------------- speaking ---------------- */

const speech = { audio: null, queue: [], playing: false, button: null };

export function stopSpeaking() {
  speech.queue = [];
  speech.playing = false;
  if (speech.audio) {
    speech.audio.pause();
    speech.audio.src = "";
    speech.audio = null;
  }
  if (speech.button) {
    speech.button.classList.remove("speaking");
    speech.button.textContent = "🔊";
    speech.button = null;
  }
}

function playNext() {
  const next = speech.queue.shift();
  if (!next) { stopSpeaking(); return; }
  const audio = new Audio(`data:audio/wav;base64,${next.audio}`);
  speech.audio = audio;
  audio.onended = () => { if (speech.playing) playNext(); };
  audio.onerror = () => stopSpeaking();
  audio.play().catch(() => stopSpeaking());
}

export async function speak(text, button) {
  // Clicking the active button stops playback, matching the citation toggle.
  if (speech.playing && speech.button === button) { stopSpeaking(); return; }
  stopSpeaking();

  speech.button = button;
  if (button) { button.classList.add("speaking"); button.textContent = "⏹"; }
  try {
    const res = await fetch("api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `speak failed (${res.status})`);
    if (!data.chunks || !data.chunks.length) throw new Error("no audio returned");
    speech.queue = data.chunks;
    speech.playing = true;
    playNext();
  } catch (err) {
    stopSpeaking();
    const note = el("composer-note");
    if (note) { note.className = "composer-note"; note.textContent = String(err.message || err); }
  }
}

/* ---------------- listening ---------------- */

const mic = {
  socket: null, ctx: null, stream: null, node: null,
  resampler: null, active: false, onText: null,
};

// stt_server converts int16 PCM at this rate; anything else transcribes as noise.
const STT_RATE = 16000;

export function isListening() { return mic.active; }

export async function startListening(stt, onText, onStatus) {
  // `stt` is {url, path}: an empty url means "this origin", which is how the
  // page reaches stt_server through the reverse proxy.
  if (mic.active) return;
  if (typeof io === "undefined") throw new Error("socket.io client not loaded");
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("no microphone API in this browser");

  mic.onText = onText;
  mic.stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });

  mic.ctx = new (window.AudioContext || window.webkitAudioContext)();
  await mic.ctx.audioWorklet.addModule("static/recorder-worklet.js");
  mic.resampler = new AudioResampler(mic.ctx.sampleRate, STT_RATE);

  const target = stt.url || window.location.origin;
  mic.socket = io(target, {
    path: stt.path || "/socket.io",
    transports: ["websocket", "polling"],
    reconnection: false,
  });
  mic.socket.on("connect", () => onStatus?.("listening"));
  mic.socket.on("connect_error", (e) => { onStatus?.(`stt unreachable: ${e.message}`); stopListening(); });
  mic.socket.on("transcription", (payload) => {
    const text = (payload && (payload.text || payload.transcript)) || "";
    if (text.trim()) mic.onText?.(text.trim(), payload);
  });
  mic.socket.on("error", (e) => onStatus?.(`stt error: ${JSON.stringify(e).slice(0, 80)}`));

  const source = mic.ctx.createMediaStreamSource(mic.stream);
  mic.node = new AudioWorkletNode(mic.ctx, "recorder-worklet");
  mic.node.port.onmessage = (ev) => {
    if (!mic.active || !mic.socket?.connected) return;
    const pcm16 = mic.resampler.process(ev.data);   // Float32 -> Int16 @16k
    if (pcm16 && pcm16.length) {
      mic.socket.emit("audio_data", { audio: pcm16.buffer, sampleRate: STT_RATE });
    }
  };
  source.connect(mic.node);
  // Keep the graph pulling without routing the mic to the speakers.
  const sink = mic.ctx.createGain();
  sink.gain.value = 0;
  mic.node.connect(sink).connect(mic.ctx.destination);

  mic.active = true;
}

export function stopListening() {
  mic.active = false;
  try { mic.node?.disconnect(); } catch (e) {}
  try { mic.stream?.getTracks().forEach((t) => t.stop()); } catch (e) {}
  try { mic.ctx?.close(); } catch (e) {}
  try { mic.socket?.disconnect(); } catch (e) {}
  mic.node = mic.stream = mic.ctx = mic.socket = null;
}
