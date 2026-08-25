/* AudioResampler — mono Float32 (e.g. 48 kHz) → Int16 PCM at a target rate.
 *
 * Ported from cv/widget/cv-chat.js, where it is inline; that copy is itself
 * vendored from noted/frontend/js/AudioResampler.js. Converted to a module and
 * to class syntax here, algorithm unchanged: linear interpolation with a carry
 * buffer so samples straddling a chunk boundary are not dropped.
 *
 * stt_server reads int16 at 16 kHz; feeding it the AudioContext's native rate
 * (usually 48 kHz) transcribes as noise.
 */
export class AudioResampler {
  constructor(inRate, outRate) {
    this._ratio = inRate / outRate;
    this._carry = new Float32Array(0);
  }

  /** Float32 chunk in, Int16Array out (or null while under one output sample). */
  pushFloat32(chunk) {
    const input = new Float32Array(this._carry.length + chunk.length);
    input.set(this._carry, 0);
    input.set(chunk, this._carry.length);

    const outLen = Math.floor(input.length / this._ratio);
    if (outLen === 0) { this._carry = input; return null; }

    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const idx = i * this._ratio;
      const i0 = Math.floor(idx);
      const i1 = Math.min(i0 + 1, input.length - 1);
      const frac = idx - i0;
      let s = input[i0] * (1 - frac) + input[i1] * frac;
      s = Math.max(-1, Math.min(1, s));
      out[i] = (s < 0 ? s * 0x8000 : s * 0x7fff) | 0;
    }
    this._carry = input.subarray(Math.floor(outLen * this._ratio));
    return out;
  }

  /** Alias used by voice.js. */
  process(chunk) { return this.pushFloat32(chunk); }
}
