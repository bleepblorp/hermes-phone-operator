#!/usr/bin/env python3
"""Hermes voice bridge with multi-turn conversation."""
import asyncio
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

import websockets

APP_NAME = "hermes"
ARI_URL = "http://127.0.0.1:8088/ari"
ARI_USER = "hermes"
ARI_PASS = "81205cdb86a02902edf6f1a6811799df"

HERMES_URL = os.environ.get("HERMES_URL", "http://192.168.1.39:8765/ask")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-EmmaMultilingualNeural")
RECORD_DIR = "/var/spool/asterisk/recording"
TMP_DIR = "/var/lib/asterisk/sounds/hermes-tts"

GREETING_TEXT = "You've reached the Operator. How may I assist you?"
GREETING_FILE_BASE = "operator-greeting"
GOODBYE_TEXT = "Goodbye."
REPROMPT_TEXT = "Are you still there?"

MAX_TURNS = 20

OBSIDIAN_CALL_LOG_DIR = os.environ.get(
    "OBSIDIAN_CALL_LOG_DIR",
    "/var/log/hermes-calls",
)
RECORD_MAX_SEC = 5
RECORD_SILENCE_SEC = 1
CALLBACK_MIN_MIN = 1
CALLBACK_MAX_MIN = 60

EXIT_PHRASES = [
    "goodbye",
    "good bye",
    "that's all",
    "thats all",
    "i'm okay for now",
    "im okay for now",
    "i'm ok for now",
    "i'm done",
    "that's it",
    "nothing else",
    "no thanks",
    "no thank you",
    "that's enough",
    "hang up",
    "end the call",
    "good night",
]


def ari_call(method, path, payload=None):
    req = urllib.request.Request(
        f"{ARI_URL}{path}",
        headers={
            "Authorization": f"Basic {base64.b64encode(f'{ARI_USER}:{ARI_PASS}'.encode()).decode()}",
            "Content-Type": "application/json",
        },
        method=method,
        data=json.dumps(payload).encode() if payload else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        print(f"[ari] {method} {path} -> {e.code}: {body}", flush=True)
        raise


def http_post_json(url, payload, timeout=30):
    req = urllib.request.Request(
        url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "null")


def whisper_transcribe(wav_path):
    from faster_whisper import WhisperModel
    if not hasattr(whisper_transcribe, "_model"):
        whisper_transcribe._model = WhisperModel(
            WHISPER_MODEL, device="cpu", compute_type="int8"
        )
    model = whisper_transcribe._model
    segments, _ = model.transcribe(wav_path, beam_size=5, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def tts_to_file(text, out_path):
    """Synthesize text into all formats Asterisk's sound: lookup needs."""
    import edge_tts
    mp3_path = out_path + ".mp3"
    communicate = edge_tts.Communicate(text, voice=TTS_VOICE)
    await communicate.save(mp3_path)
    base = out_path.rsplit(".", 1)[0]
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", mp3_path, "-ar", "8000", "-ac", "1",
            "-f", "wav", "-acodec", "pcm_mulaw",
            out_path,
        ],
        check=True,
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", mp3_path, "-ar", "8000", "-ac", "1",
                "-f", "gsm", f"{base}.gsm",
            ],
            check=True,
        )
    except Exception as exc:
        print(f"[tts] gsm conversion failed: {exc}", flush=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", mp3_path, "-ar", "8000", "-ac", "1",
                "-acodec", "gsm_ms", "-f", "wav",
                f"{base}.WAV",
            ],
            check=True,
        )
    except Exception as exc:
        print(f"[tts] WAV49 conversion failed: {exc}", flush=True)
    os.remove(mp3_path)


async def tts_and_play(cid, text, base_name):
    """Synthesize text to multi-format files in TMP_DIR, then play to caller."""
    if not text:
        return
    tts_path = f"{TMP_DIR}/{base_name}.ulaw"
    await tts_to_file(text, tts_path)
    sound_uri = f"sound:hermes-tts/{base_name}"
    try:
        ari_call("POST", f"/channels/{cid}/play", {"media": sound_uri})
    except Exception as exc:
        print(f"[tts-play] error: {exc}", flush=True)


def is_exit_phrase(text):
    """Return True if the transcript matches any known exit phrase."""
    if not text:
        return False
    t = text.lower().strip().rstrip(".!?,")
    if t in EXIT_PHRASES:
        return True
    # Common short variants
    if len(t) <= 30 and any(p in t for p in ["goodbye", "bye bye", "hang up", "that's all", "thats all", "that's it", "thats it", "nothing else", "no thanks", "no thank you"]):
        return True
    return False


# Per-call state: cid -> {"silent_count": int, "turn_count": int, "session_id": str|None}
CALL_STATE = {}


def new_call_state():
    return {
        "silent_count": 0,
        "turn_count": 0,
        "session_id": None,
        "transcript": [],  # list of {"role": "user"|"assistant", "text": str, "ts": iso}
        "started_at": None,
        "caller_name": None,
    }


def start_recording(cid, suffix=""):
    """Start a fresh ARI recording on the main channel."""
    safe = cid.replace(".", "_")
    rec_name = f"rec-{safe}{suffix}"
    ari_call(
        "POST",
        f"/channels/{cid}/record?name={rec_name}&format=wav"
        f"&maxDurationSeconds={RECORD_MAX_SEC}"
        f"&maxSilenceSeconds={RECORD_SILENCE_SEC}"
        f"&beep=true&terminateOn=%23&ifExists=overwrite",
    )
    return f"{RECORD_DIR}/{rec_name}.wav", rec_name


async def handle_turn(cid, rec_path, turn_index):
    """Process one user turn: silence → reprompt/goodbye; text → exit/Hermes/play/re-record."""
    state = CALL_STATE.get(cid) or new_call_state()
    CALL_STATE[cid] = state
    turn_index_str = str(turn_index)

    transcript = await asyncio.to_thread(whisper_transcribe, rec_path)
    print(f"[turn {turn_index} {cid}] transcript: {transcript!r}", flush=True)

    # Record user turn in call transcript
    from datetime import datetime
    state.setdefault("transcript", []).append({
        "role": "user",
        "text": transcript or "(silence)",
        "ts": datetime.now().isoformat(timespec="seconds"),
    })

    if not transcript:
        # Empty / silence
        state["silent_count"] += 1
        if state["silent_count"] >= 2:
            print(f"[turn {turn_index} {cid}] silent twice, hanging up", flush=True)
            await tts_and_play(cid, GOODBYE_TEXT, f"goodbye-{turn_index_str}")
            await asyncio.sleep(2)
            try:
                ari_call("DELETE", f"/channels/{cid}", {})
            except Exception:
                pass
            await write_obsidian_call_log(cid)
            CALL_STATE.pop(cid, None)
            return False  # do not re-record
        # Re-prompt once
        await tts_and_play(cid, REPROMPT_TEXT, f"reprompt-{turn_index_str}")
        await asyncio.sleep(2)
        new_rec_path, _ = start_recording(cid, suffix=f"-{int(turn_index_str) + 1}")
        return True  # re-record

    # Reset silent count and check exit phrase
    state["silent_count"] = 0
    if is_exit_phrase(transcript):
        print(f"[turn {turn_index} {cid}] exit phrase detected", flush=True)
        callback_req = parse_callback_request(transcript)
        if callback_req:
            minutes = callback_req
            ext = state.get('extension', 'PJSIP/200')
            reminder = f'Reminder: you asked me to call you back {minutes} minutes ago.'
            reply_text = "Okay, I'll call you back in " + str(minutes) + " minutes."
            state.setdefault('transcript', []).append({
                'role': 'assistant',
                'text': reply_text,
                'ts': datetime.now().isoformat(timespec='seconds'),
            })
            await tts_and_play(cid, reply_text, f'callback-confirm-{turn_index}')
            await asyncio.sleep(1)
            asyncio.create_task(schedule_callback(ext, minutes * 60, reminder))
            ari_call('DELETE', f'/channels/{cid}', {})
            await write_obsidian_call_log(cid)
            CALL_STATE.pop(cid, None)
            return False
        await tts_and_play(cid, GOODBYE_TEXT, f"goodbye-{turn_index_str}")
        await asyncio.sleep(2)
        try:
            ari_call("DELETE", f"/channels/{cid}", {})
        except Exception:
            pass
        await write_obsidian_call_log(cid)
        CALL_STATE.pop(cid, None)
        return False

    state["turn_count"] += 1
    if state["turn_count"] > MAX_TURNS:
        print(f"[turn {turn_index} {cid}] max turns reached, hanging up", flush=True)
        await tts_and_play(cid, GOODBYE_TEXT, f"goodbye-{turn_index_str}")
        await asyncio.sleep(2)
        try:
            ari_call("DELETE", f"/channels/{cid}", {})
        except Exception:
            pass
        await write_obsidian_call_log(cid)
        CALL_STATE.pop(cid, None)
        return False

    # Thinking tone
    try:
        ari_call("POST", f"/channels/{cid}/play", {"media": "sound:one-moment-please"})
    except Exception as exc:
        print(f"[turn {turn_index} {cid}] thinking tone error: {exc}", flush=True)

    # Ask Hermes (with session resume if we have one)
    payload = {
        "text": transcript,
        "channel": "phone",
        "caller": cid,
    }
    if state["session_id"]:
        payload["session_id"] = state["session_id"]
    try:
        _, reply = http_post_json(HERMES_URL, payload, timeout=180)
        reply_text = reply.get("reply", "") if isinstance(reply, dict) else str(reply)
        if "session_id" in reply and reply["session_id"]:
            state["session_id"] = reply["session_id"]
    except Exception as exc:
        print(f"[turn {turn_index} {cid}] hermes error: {exc}", flush=True)
        reply_text = "Sorry, I had trouble reaching my brain right now."
    print(f"[turn {turn_index} {cid}] hermes: {reply_text!r}", flush=True)

    # Record assistant turn in transcript
    state.setdefault("transcript", []).append({
        "role": "assistant",
        "text": reply_text,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })

    # Synthesize and play reply
    safe_cid = cid.replace(".", "_")
    await tts_and_play(cid, reply_text, f"reply-{safe_cid}-{turn_index_str}")
    await asyncio.sleep(max(3, len(reply_text) * 0.06))

    # Continue: start next recording
    new_rec_path, _ = start_recording(cid, suffix=f"-{int(turn_index_str) + 1}")
    return True



async def schedule_callback(extension: str, delay_sec: int, reminder_text: str):
    """Schedule an outbound call via ARI channel originate after a delay."""
    await asyncio.sleep(delay_sec)
    try:
        payload = {
            "endpoint": extension,
            "app": APP_NAME,
            "appArgs": f"callback|{reminder_text}",
            "callerId": "Hermes <0>",
            "timeout": 30,
        }
        status, _ = ari_call("POST", "/channels", payload)
        print(f"[callback] originated {extension} after {delay_sec}s (status={status})", flush=True)
    except Exception as exc:
        print(f"[callback] failed: {exc}", flush=True)


def parse_callback_request(text: str):
    """Return minutes or None if not a callback request."""
    m = re.search(r"call me back in (\d+)\s*(?:minute|min|m)", text, re.IGNORECASE)
    if not m:
        return None
    minutes = int(m.group(1))
    if minutes < CALLBACK_MIN_MIN or minutes > CALLBACK_MAX_MIN:
        return None
    return minutes


async def handle_callback_channel(cid, reminder_text: str):
    """Answer a callback-originated channel, play reminder, hang up."""
    try:
        ari_call("POST", f"/channels/{cid}/answer", {})
        base = f"callback-{cid.replace('.', '_')}"
        await tts_and_play(cid, reminder_text, base)
        await asyncio.sleep(2)
        ari_call("DELETE", f"/channels/{cid}", {})
        print(f"[callback] reminder played for {cid}", flush=True)
    except Exception as exc:
        print(f"[callback] error: {exc}", flush=True)


async def write_obsidian_call_log(cid):
    """Save the call transcript as a markdown file in Obsidian."""
    state = CALL_STATE.get(cid)
    if not state:
        return
    transcript = state.get("transcript") or []
    if not transcript:
        return
    try:
        os.makedirs(OBSIDIAN_CALL_LOG_DIR, exist_ok=True)
        from datetime import datetime
        started = state.get("started_at") or datetime.now().isoformat(timespec="seconds")
        # Filename: 2026-08-30_224515_call.md (timestamp of start)
        try:
            stamp = datetime.fromisoformat(started).strftime("%Y-%m-%d_%H%M%S")
        except Exception:
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(OBSIDIAN_CALL_LOG_DIR, f"{stamp}_hermes_call.md")
        # Find caller name from channel cid via ARI
        caller_line = f"- **Caller channel:** `{cid}`"
        try:
            status, info = ari_call("GET", f"/channels/{cid}")
            name = info.get("caller", {}).get("name", "") or ""
            num = info.get("caller", {}).get("number", "") or ""
            if name or num:
                caller_line = f"- **Caller:** {name} ({num})"
        except Exception:
            pass
        lines = [
            "---",
            f"date: {started}",
            f"tags: [hermes, phone, voice-bridge]",
            "---",
            "",
            f"# Hermes call {stamp}",
            "",
            caller_line,
            f"- **Started:** {started}",
            f"- **Turns:** {len([t for t in transcript if t['role'] == 'user'])}",
            "",
            "## Transcript",
            "",
        ]
        for t in transcript:
            role = t["role"].capitalize()
            lines.append(f"**[{t['ts']}] {role}:** {t['text']}")
            lines.append("")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        print(f"[obsidian] wrote {path}", flush=True)
    except Exception as exc:
        print(f"[obsidian] write failed: {exc}", flush=True)


async def stasis(websocket):
    print("[stasis] connected", flush=True)
    if not os.path.exists(TMP_DIR):
        os.makedirs(TMP_DIR, exist_ok=True)
    try:
        os.chmod(TMP_DIR, 0o777)
    except PermissionError:
        pass

    # Synthesize static prompts at startup
    for text, base in [
        (GREETING_TEXT, GREETING_FILE_BASE),
        (GOODBYE_TEXT, "goodbye-static"),
        (REPROMPT_TEXT, "reprompt-static"),
    ]:
        paths = [
            f"{TMP_DIR}/{base}.ulaw",
            f"{TMP_DIR}/{base}.gsm",
            f"{TMP_DIR}/{base}.WAV",
        ]
        if not all(os.path.exists(p) for p in paths):
            try:
                await tts_to_file(text, f"{TMP_DIR}/{base}.ulaw")
                print(f"[startup] synthesized {base}", flush=True)
            except Exception as exc:
                print(f"[startup] {base} failed: {exc}", flush=True)

    try:
        while True:
            msg = await websocket.recv()
            event = json.loads(msg)
            etype = event.get("type", "?")
            cid = event.get("channel", {}).get("id", "")
            chan_name = event.get("channel", {}).get("name", "")
            print(f"[event] {etype} cid={cid} name={chan_name}", flush=True)

            if etype == "StasisStart":
                if chan_name.startswith("Snoop/"):
                    continue
                app_args = event.get("appArgs", "") or ""
                if app_args.startswith("callback|"):
                    reminder_text = app_args.split("callback|", 1)[1]
                    asyncio.create_task(handle_callback_channel(cid, reminder_text))
                    continue
                await handle_call(cid)

            elif etype == "RecordingFinished":
                rec = event.get("recording", {})
                rec_name = rec.get("name", "")
                rec_path = f"{RECORD_DIR}/{rec_name}.wav"
                chan_cid = (
                    event.get("channel", {}).get("id")
                    or rec.get("channel_id")
                    or rec.get("target_channel")
                )
                if not chan_cid:
                    try:
                        _, chans = ari_call("GET", "/channels")
                        chan_cid = next(
                            (c["id"] for c in chans if c.get("name", "").startswith("PJSIP/")),
                            None,
                        )
                    except Exception:
                        pass
                if chan_cid and os.path.exists(rec_path):
                    # Extract turn index from rec_name (e.g., rec-1788..._1-0)
                    m = re.search(r"-(\d+)$", rec_name.rsplit(".", 1)[0])
                    turn_index = int(m.group(1)) if m else 0
                    asyncio.create_task(handle_turn(chan_cid, rec_path, turn_index))

            elif etype == "StasisEnd":
                print(f"[stasis] end cid={cid}", flush=True)
                # Backstop: write log if call ended without explicit hangup
                try:
                    await write_obsidian_call_log(cid)
                except Exception:
                    pass
                CALL_STATE.pop(cid, None)

    except Exception as exc:
        print(f"[stasis] error: {exc}", flush=True)


async def handle_call(cid):
    """First entry: answer, play greeting, start first recording."""
    try:
        state = new_call_state()
        from datetime import datetime
        state["started_at"] = datetime.now().isoformat(timespec="seconds")
        CALL_STATE[cid] = state
        ari_call("POST", f"/channels/{cid}/answer", {})
        print(f"[call {cid}] answered", flush=True)
        ari_call("POST", f"/channels/{cid}/play", {"media": f"sound:hermes-tts/{GREETING_FILE_BASE}"})
        await asyncio.sleep(2)
        await asyncio.sleep(0.5)
        start_recording(cid, suffix="-0")
        print(f"[call {cid}] recording started (turn 0)", flush=True)
    except Exception as exc:
        print(f"[call {cid}] error: {exc}", flush=True)


async def main():
    while True:
        try:
            ari_call("GET", "/asterisk/info")
            print("[ari] reachable", flush=True)
            ws_url = (
                f"ws://127.0.0.1:8088/ari/events?app={APP_NAME}"
                f"&api_key={ARI_USER}:{ARI_PASS}"
            )
            async with websockets.connect(ws_url) as ws:
                await stasis(ws)
        except Exception as exc:
            print(f"[main] error: {exc}, retrying", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
