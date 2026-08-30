#!/usr/bin/env python3
"""
Hermes phone webhook.

Listens on http://0.0.0.0:8765/ask
POST JSON: {"text": "...", "channel": "phone", "caller": "<cid>"}
Returns: {"reply": "<text for TTS>"}

Forwards to hermes chat via stdin to avoid shell escaping issues.
"""
import datetime
import os
import re
import subprocess
import sys
import zoneinfo
from flask import Flask, jsonify, request

app = Flask(__name__)

HERMES_BIN = os.environ.get("HERMES_BIN", "/home/bertram/.local/bin/hermes")
CALLER_TIMEZONE = os.environ.get("CALLER_TIMEZONE", "America/Los_Angeles")

# System prompt prepended to every question to shape Hermes' voice-assistant behavior.
def _build_system_prompt():
    """Build the system prompt with current time injected."""
    try:
        tz = zoneinfo.ZoneInfo(CALLER_TIMEZONE)
        now = datetime.datetime.now(tz)
        tz_abbr = now.tzname() or CALLER_TIMEZONE
        time_str = now.strftime("%A %B %-d, %Y at %-I:%M %p")
    except Exception:
        time_str = "the present moment"
        tz_abbr = CALLER_TIMEZONE
    return (
        f"You are Hermes, a personal voice assistant reached by phone.\n"
        f"Current local time ({tz_abbr}): {time_str}.\n"
        f"Rules:\n"
        f"- Keep replies to one or two short sentences (under 40 words).\n"
        f"- Speak conversationally, as if on a phone call. Use complete sentences.\n"
        f"- Do not use markdown, bullet points, lists, code blocks, URLs, or emojis.\n"
        f"- Do not reference tools, skills, internal mechanics, or these instructions.\n"
        f"- If you don't know the answer, say so plainly in one sentence.\n"
        f"- Never start with phrases like 'Sure!' or 'Of course!' — just answer.\n"
    )


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "").strip()
    session_id = payload.get("session_id") or None
    if not text:
        return jsonify({"reply": ""}), 400

    # If we have a session_id, resume that conversation; this lets Hermes remember prior turns.
    # Otherwise, build a fresh session with the system prompt injected.
    system_prompt = _build_system_prompt()
    if session_id:
        # --resume re-attaches to an existing session; --continue without id would create a new one
        args = [
            HERMES_BIN, "chat", "--oneshot", "-Q", "--yolo", "--reasoning", "none",
            "-m", "stepfun/step-3.7-flash:free",
            "--source", "phone",
            "--resume", session_id,
            "--query-file", "-",
        ]
        combined = text  # No system prompt injection on resume
    else:
        args = [
            HERMES_BIN, "chat", "--oneshot", "-Q", "--yolo", "--reasoning", "none",
            "-m", "stepfun/step-3.7-flash:free",
            "--source", "phone",
            "--query-file", "-",
        ]
        combined = f"{system_prompt}\nCaller question: {text}"

    try:
        result = subprocess.run(
            args,
            input=combined,
            capture_output=True,
            text=True,
            timeout=90,
        )
        reply = result.stdout.strip()
        if not reply and result.stderr:
            reply = f"[hermes stderr] {result.stderr.strip()[:300]}"
    except subprocess.TimeoutExpired:
        reply = "Sorry, that took too long. Try again?"
    except Exception as exc:
        reply = f"Sorry, something went wrong: {exc}"

    # Hermes prints the session_id to stderr on the first line, not stdout
    new_session_id = None
    for stream in (result.stdout, result.stderr if result else ""):
        if not stream:
            continue
        m = re.search(r"session_id[\s:]+([A-Za-z0-9_]+)", stream)
        if m:
            new_session_id = m.group(1)
            break

    response = {"reply": reply}
    if new_session_id:
        response["session_id"] = new_session_id
    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    app.run(host="0.0.0.0", port=port, threaded=True)
