#!/usr/bin/env python3
"""
Hermes phone webhook.

Listens on http://0.0.0.0:8765/ask
POST JSON: {"text": "...", "channel": "phone", "caller": "<cid>"}
Returns: {"reply": "<text for TTS>"}

Forwards to hermes chat via stdin to avoid shell escaping issues.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
import zoneinfo
from flask import Flask, jsonify, request

app = Flask(__name__)

HERMES_BIN = os.environ.get("HERMES_BIN", "/home/bertram/.local/bin/hermes")
CALLER_TIMEZONE = os.environ.get("CALLER_TIMEZONE", "America/Los_Angeles")
TEMPEST_TOKEN = os.environ.get("TEMPEST_TOKEN", "")
TEMPEST_STATION_ID = os.environ.get("TEMPEST_STATION_ID", "84180")
TEMPEST_API_BASE = os.environ.get("TEMPEST_API_BASE", "https://swd.weatherflow.com/swd/rest")
LMS_URL = os.environ.get("LMS_URL", "http://192.168.1.38:9000/jsonrpc.js")
LMS_DEFAULT_PLAYER = os.environ.get("LMS_DEFAULT_PLAYER", "00:04:20:29:45:77")
WEATHER_KEYWORDS = re.compile(
    r"\b(weather|temperature|temp|forecast|rain|wind|humidity|barometer|pressure|snow|storm|sunny|cloudy|degrees)\b",
    re.IGNORECASE,
)
MUSIC_KEYWORDS = re.compile(
    r"\b(play|pause|stop|resume|skip|next|previous|volume|louder|quieter|music|song|artist|album|playlist)\b",
    re.IGNORECASE,
)


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


def _is_weather_question(text: str) -> bool:
    return bool(WEATHER_KEYWORDS.search(text))


def _fetch_tempest_conditions() -> str:
    if not TEMPEST_TOKEN:
        return ""
    url = f"{TEMPEST_API_BASE}/observations/stn/{TEMPEST_STATION_ID}?token={TEMPEST_TOKEN}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return f"Tempest weather data unavailable: {exc}"

    try:
        obs = payload.get("obs", [[]])
        if not obs or not obs[0]:
            return "Tempest station returned no current observations."
        current = obs[0]
        keys = payload.get("ob_fields", [])
        if not keys:
            return "Tempest observation metadata is missing."

        def _v(name):
            try:
                return current[keys.index(name)]
            except ValueError:
                return None

        temp_c = _v("air_temp")
        humidity = _v("rh")
        wind_speed = _v("wind_avg")
        wind_gust = _v("wind_gust")
        precip = _v("precip_accumulation")
        press = _v("station_pressure")
        solar = _v("solar_radiation")

        def c_to_f(c):
            if c is None:
                return None
            return round((c * 9/5) + 32, 1)

        parts = []
        temp_f = c_to_f(temp_c)
        if temp_f is not None:
            parts.append(f"{temp_f} degrees Fahrenheit")
        if humidity is not None:
            parts.append(f"humidity {humidity:.0f} percent")
        if wind_speed is not None:
            parts.append(f"wind {wind_speed:.1f} meters per second")
        if wind_gust is not None:
            parts.append(f"gusts to {wind_gust:.1f}")
        if precip is not None:
            parts.append(f"precipitation last hour {precip:.1f} millimeters")
        if press is not None:
            parts.append(f"pressure {press:.1f} millibars")
        if solar is not None:
            parts.append(f"solar radiation {solar:.0f} watts per square meter")

        if not parts:
            return "Tempest data is available but I could not parse current conditions."
        return "Current conditions from the local Tempest station: " + ", ".join(parts) + "."
    except Exception as exc:
        return f"I had trouble reading the Tempest observation: {exc}"


def _lms_jsonrpc(player_id, *params):
    payload = json.dumps({
        "id": 1,
        "method": "slim.request",
        "params": [player_id, list(params)],
    }).encode()
    req = urllib.request.Request(
        LMS_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_music_request(text: str) -> bool:
    return bool(MUSIC_KEYWORDS.search(text))


def _handle_music_request(text: str) -> str:
    player = LMS_DEFAULT_PLAYER
    lower = text.lower()

    def lms(*params):
        try:
            _lms_jsonrpc(player, *params)
        except Exception as exc:
            return f"Sorry, I couldn't reach the music system: {exc}"

    # Playback controls
    if re.search(r"\b(pause|hold)\b", lower):
        lms("pause")
        return "Paused Radio."
    if re.search(r"\b(resume|unpause|continue)\b", lower):
        lms("play")
        return "Resuming Radio."
    if re.search(r"\b(stop)\b", lower):
        lms("stop")
        return "Stopped Radio."
    if re.search(r"\b(next|skip)\b", lower):
        lms("playlist", "index", "+1")
        return "Skipping to next track on Radio."
    if re.search(r"\b(previous|back)\b", lower):
        lms("playlist", "index", "-1")
        return "Going back to previous track on Radio."
    if re.search(r"\b(louder|turn it up|volume up)\b", lower):
        lms("mixer", "volume", "+10%")
        return "Turned Radio up."
    if re.search(r"\b(quieter|turn it down|volume down|shhh)\b", lower):
        lms("mixer", "volume", "-10%")
        return "Turned Radio down."

    # Search queries: playlist / artist / album / song
    query = None
    cmd = None
    m = re.search(r"play\s+(?:the\s+)?(?:playlist|playlists)?\s*(?:named\s+)?(.+)", lower)
    if m:
        query = m.group(1).strip().strip(".")
        cmd = "playlist"
    m = re.search(r"play\s+(?:artist|band|group)\s+(.+)", lower)
    if m:
        query = m.group(1).strip().strip(".")
        cmd = "artist"
    m = re.search(r"play\s+(?:album|record|cd)\s+(.+)", lower)
    if m:
        query = m.group(1).strip().strip(".")
        cmd = "album"
    m = re.search(r"play\s+(?:song|track|tune)\s+(.+)", lower)
    if m:
        query = m.group(1).strip().strip(".")
        cmd = "title"

    if query and cmd:
        lms("playlist", "loadtracks", cmd, query, "1")
        lms("play")
        label = cmd if cmd != "title" else "song"
        return f"Playing {label} {query} on Radio."

    # Fallback generic play
    lms("play")
    return "Playing Radio."


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "").strip()
    session_id = payload.get("session_id") or None
    if not text:
        return jsonify({"reply": ""}), 400

    system_prompt = _build_system_prompt()
    weather_context = ""
    if _is_weather_question(text):
        weather_context = "\n\nLive local weather data:\n" + _fetch_tempest_conditions() + "\n"

    music_reply = ""
    if _is_music_request(text):
        music_reply = _handle_music_request(text)

    if session_id:
        args = [
            HERMES_BIN, "chat", "--oneshot", "-Q", "--yolo", "--reasoning", "none",
            "-m", "stepfun/step-3.7-flash:free",
            "--source", "phone",
            "--resume", session_id,
            "--query-file", "-",
        ]
        combined = f"{text}{weather_context}"
    else:
        args = [
            HERMES_BIN, "chat", "--oneshot", "-Q", "--yolo", "--reasoning", "none",
            "-m", "stepfun/step-3.7-flash:free",
            "--source", "phone",
            "--query-file", "-",
        ]
        combined = f"{system_prompt}{weather_context}\nCaller question: {text}"

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

    new_session_id = None
    if not music_reply:
        for stream in (result.stdout, result.stderr if result else ""):
            if not stream:
                continue
            m = re.search(r"session_id[\s:]+([A-Za-z0-9_]+)", stream)
            if m:
                new_session_id = m.group(1)
                break

    response = {"reply": music_reply or reply}
    if not music_reply and new_session_id:
        response["session_id"] = new_session_id
    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    app.run(host="0.0.0.0", port=port, threaded=True)
