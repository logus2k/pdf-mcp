/* Stateful streaming parser for <think>...</think>, ported from
 * cv/widget/cv-chat.js (which in turn ported it from noted/ChatService.js).
 *
 * Why stateful rather than a regex over the finished text: tokens arrive in
 * arbitrary chunks, so a tag can be split across two deliveries ("<thi" +
 * "nk>"). And once inside a reasoning block, tag-looking text is ordinary
 * reasoning content — only </think> leaves that mode.
 *
 * The <voice> handling from the CV original is dropped: that exists to feed
 * TTS, and no voice pipeline is wired here yet.
 */

export class ThinkingParser {
  constructor() {
    this._inThinking = false;
    this._buffer = "";
    this.thinkingBuffer = "";
  }

  /* Returns one of:
   *   {type:'thinking_start'}
   *   {type:'thinking_delta', thinking}
   *   {type:'thinking_end', thinking, answer}
   *   {type:'answer_delta', answer}
   */
  processToken(token) {
    this._buffer += token;

    if (!this._inThinking && this._buffer.indexOf("<think>") >= 0) {
      this._inThinking = true;
      const after = this._buffer.split("<think>").pop();
      this._buffer = "";
      // A model that derails into a second <think> block should append, not
      // replace — otherwise the long first block is thrown away.
      if (this.thinkingBuffer) this.thinkingBuffer += "\n\n---\n\n";
      if (after.indexOf("</think>") >= 0) {
        this._inThinking = false;
        const parts = after.split("</think>");
        this.thinkingBuffer += parts[0];
        return {
          type: "thinking_end",
          thinking: this.thinkingBuffer,
          answer: this._stripLeading(parts.slice(1).join("</think>")),
        };
      }
      this.thinkingBuffer += after;
      return { type: "thinking_start", thinking: this.thinkingBuffer };
    }

    if (this._inThinking && this._buffer.indexOf("</think>") >= 0) {
      this._inThinking = false;
      const parts = this._buffer.split("</think>");
      this.thinkingBuffer += parts[0];
      this._buffer = "";
      return {
        type: "thinking_end",
        thinking: this.thinkingBuffer,
        answer: this._stripLeading(parts.slice(1).join("</think>")),
      };
    }

    // Hold back a possible partial tag at the chunk boundary so it is not
    // emitted as visible text and then duplicated once completed.
    const held = this._heldBack(this._buffer);
    const emit = this._buffer.slice(0, this._buffer.length - held);
    this._buffer = this._buffer.slice(this._buffer.length - held);

    if (!emit) return { type: this._inThinking ? "thinking_delta" : "answer_delta",
                        thinking: this.thinkingBuffer, answer: "" };

    if (this._inThinking) {
      this.thinkingBuffer += emit;
      return { type: "thinking_delta", thinking: this.thinkingBuffer };
    }
    return { type: "answer_delta", answer: emit };
  }

  /* How many trailing characters look like the start of a tag we care about. */
  _heldBack(buf) {
    const candidates = this._inThinking ? ["</think>"] : ["<think>"];
    for (const tag of candidates) {
      for (let n = Math.min(tag.length - 1, buf.length); n > 0; n--) {
        if (buf.slice(buf.length - n) === tag.slice(0, n)) return n;
      }
    }
    return 0;
  }

  _stripLeading(text) {
    let i = 0;
    while (i < text.length && (text[i] === "\n" || text[i] === "\r" ||
                               text[i] === " " || text[i] === "\t")) i++;
    return text.slice(i);
  }

  /* Anything still held back at end of stream is real text after all. */
  flush() {
    const rest = this._buffer;
    this._buffer = "";
    if (!rest) return "";
    if (this._inThinking) { this.thinkingBuffer += rest; return ""; }
    return rest;
  }
}
