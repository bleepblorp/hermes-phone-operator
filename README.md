# Hermes Phone Operator

Dial **0** from any phone on your FreePBX system and you're connected to a Hermes voice assistant that answers, listens, transcribes, asks Hermes for a reply, speaks it back, and keeps the conversation going until you say "goodbye".

```
caller → SIP/ATA → FreePBX (Stasis) → ARI bridge (faster-whisper + edge-tts)
                                                   ↓
                                          webhook → Hermes chat --oneshot
                                                   ↓
                                          reply (multi-format audio)
                                                   ↓
                                          caller hears it
```

## What you get

- **Multi-turn voice calls** — Hermes remembers the conversation via `--resume <session_id>`
- **Local STT** — `faster-whisper` (small.en) running on FreePBX, no cloud
- **Local TTS** — `edge-tts` (Microsoft Edge's free TTS, no API key) with voice `en-US-EmmaMultilingualNeural`
- **A real webhook** on this VM that calls Hermes with a voice-assistant system prompt and current-time injection
- **Per-call logging** — every call is written as a markdown file in your Obsidian vault
- **Auto-restart** — both services managed by systemd (or `--user` systemd on this VM)
- **Clean exit phrases** — "goodbye", "that's all", "I'm okay for now", etc. all trigger a hangup
- **Silence handling** — one reprompt, then goodbye if still quiet
- **20-turn cap** as a safety

## Repository layout

```
hermes-phone-operator/
├── bridge/
│   └── bridge.py               # ARI WebSocket client (runs on FreePBX)
├── webhook/
│   └── webhook.py              # Flask app that calls Hermes (runs on this VM)
├── deploy/
│   ├── hermes-bridge.service   # systemd unit for the bridge (FreePBX)
│   ├── hermes-phone-server.service  # systemd --user unit for the webhook (this VM)
│   ├── extensions_custom.conf.snippet  # FreePBX dialplan fragment
│   ├── sync-hermes-calls.sh     # cron job that pulls logs into Obsidian
│   └── install.md               # deployment instructions
├── docs/
│   └── GOTCHAS.md               # the Asterisk 22 + edge-tts + ARI traps we hit
├── .gitignore
├── LICENSE
└── README.md
```

## Quick start

See [`deploy/install.md`](deploy/install.md) for the full walkthrough. TL;DR:

**FreePBX host:**
1. Install ARI user `hermes` via the GUI with read/write access
2. Set Asterisk Builtin mini-HTTP server bind to `0.0.0.0`
3. Drop `deploy/extensions_custom.conf.snippet` into `/etc/asterisk/extensions_custom.conf`
4. Copy `bridge/bridge.py` to `/opt/hermes-ari/`, set up a venv with `faster-whisper`, `edge-tts`, `websockets`, `flask`
5. Symlink `deploy/hermes-bridge.service` into `/etc/systemd/system/`, `systemctl enable --now`

**This VM (webhook host):**
1. Copy `webhook/webhook.py` and `webhook/requirements.txt`
2. Symlink `deploy/hermes-phone-server.service` into `~/.config/systemd/user/`, `systemctl --user enable --now`
3. (Optional) Symlink `deploy/sync-hermes-calls.sh` into `~/bin/`, add the cron line from `deploy/install.md`

## Voice customization

Change `TTS_VOICE` in `bridge/bridge.py` to any of 40+ English Edge-TTS voices. To preview a voice:

```bash
curl -s -X POST http://192.168.1.39:8765/ask \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello there"}'
```

Then change the line in `bridge/bridge.py`:

```python
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-EmmaMultilingualNeural")
```

Restart `hermes-bridge.service` to pick it up.

## Known issues / wishlist

- **Per-extension routing** — different greetings or behavior per caller (e.g., extension 200 vs front door call box)
- **Barge-in** — let the caller interrupt Hermes mid-response
- **Production hardening** — `--yolo` is unsafe; replace with restricted skills
- **First-call latency** — `faster-whisper` cold-starts on the first call after a fresh boot (~5–10s)

## License

MIT (or whatever you prefer — adjust as needed).
