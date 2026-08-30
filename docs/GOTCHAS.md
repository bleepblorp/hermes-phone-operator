# Asterisk 22 + ARI + edge-tts + faster-whisper gotchas

These are the pitfalls we hit. Read this before debugging anything.

## Audio path

### TTS must be multi-format

Asterisk's `sound:` URI lookup tries extensions in this order:

1. `${sounds_dir}/${voice}.gsm`
2. `${sounds_dir}/${voice}.ulaw`
3. `${sounds_dir}/${voice}.WAV` (wav49)

If you only write one format (e.g. `output.wav` in PCM s16le), Asterisk will return:

```
file.c: File hermes-tts/foo.wav does not exist in any format
file.c: Unable to open hermes-tts/foo.wav (format (ulaw)): No such file or directory
res_stasis_playback.c: Playback failed for sound:hermes-tts/foo.wav
```

**Fix:** Always synthesize with all three:

```python
ffmpeg -i input.mp3 -ar 8000 -ac 1 -f wav -acodec pcm_mulaw output.ulaw
ffmpeg -i input.mp3 -ar 8000 -ac 1 -f gsm output.gsm
ffmpeg -i input.mp3 -ar 8000 -ac 1 -acodec gsm_ms -f wav output.WAV
```

### Recording path must exist

ARI `/channels/{cid}/record` creates files at `astspooldir/recording/{name}.wav` — typically `/var/spool/asterisk/recording/`. If the directory doesn't exist, Asterisk returns:

```
WARNING ari/resource_channels.c: Unrecognized recording error: No such file or directory
```

…and ARI returns HTTP 500. Always ensure:

```bash
sudo mkdir -p /var/spool/asterisk/recording
sudo chown asterisk:asterisk /var/spool/asterisk/recording
```

### MixMonitor dialplan redirect BREAKS audio back to the phone

If you redirect a channel to a context that runs `MixMonitor` then returns to Stasis, **RTP flow back to the phone stops**. The `PlaybackFinished` events fire, but the user hears silence. This took hours to debug.

Don't do this:

```ini
[hermes-record]
exten => 0,1,MixMonitor(file.wav,a)
 same => n,Wait(5)
 same => n,Stasis(hermes)
```

Do this instead: call ARI `/channels/{cid}/record` directly on the main channel from inside Stasis. Audio stays in the main pipeline.

## ARI / Stasis

### Bind address gets clobbered

If you edit `/etc/asterisk/ari_general_custom.conf` with:

```ini
[general]
enabled=yes
bindaddr=0.0.0.0
```

…this gets overwritten by FreePBX on every `dialplan reload` because FreePBX regenerates the base config.

**Fix:** Set `Asterisk Builtin mini-HTTP server bind` to `0.0.0.0` in the **FreePBX GUI** under **Settings → Advanced Settings**. That's persistent.

### ARI user must be added via the GUI

Direct file edits to `ari_additional.conf` won't stick (same regeneration problem). Add ARI users through **Settings → Asterisk REST Interface Users**.

### `Stasis(hermes)` not `Stasis(app="hermes")`

On Asterisk 22, the unquoted form works reliably. Quotes around the app name occasionally caused `App 'hermes' not subscribed` errors.

### `App 'X' not subscribed to channel` is mostly cosmetic

It shows up in the logs but doesn't block StasisStart. It can, however, block `/channels/{cid}/record` — see the MixMonitor note above.

## Hermes integration

### `session_id` is on STDERR, not STDOUT

```bash
$ hermes chat --oneshot -Q --yolo --query-file - <<< "hi"
session_id: 20260830_222814_dbca6e
Hi! What can I help you with?
```

The `session_id` line goes to STDERR. To use it for `--resume`, parse stderr:

```python
import re
m = re.search(r"session_id[:\s]+([A-Za-z0-9_]+)", result.stderr)
if m:
    session_id = m.group(1)
```

### Reasoning burns time and produces empty responses

`minimax/minimax-m3` with default reasoning often produces only reasoning and no final answer, then retries with empty responses. Use `--reasoning none` for fast voice-friendly replies:

```bash
hermes chat --oneshot -Q --yolo --reasoning none \
  -m minimax/minimax-m3 --source phone --query-file -
```

### Shell escaping with `--query-file -`

Don't use shell interpolation to pass user prompts. Use `--query-file -` and pipe via stdin — that handles apostrophes, quotes, and newlines safely.

## Voice

### `en-US-EmmaMultilingualNeural` is the recommended default

Warm, natural female voice that handles occasional non-English words well. To preview other voices:

```python
import asyncio, edge_tts
async def main():
    voices = await edge_tts.list_voices()
    en = [v for v in voices if v["Locale"].startswith("en-")]
    for v in en[:20]:
        print(v["ShortName"], v["Gender"])
asyncio.run(main())
```

## SIP / ATA

### Linksys PAP2T/SPA dial plans need explicit `0S0`

Default dial plans strip `0` as a leading digit. To dial 0, you must add `0S0`:

```
(0S0|2xxS0|*xx|911S0|[3469]11|9[2-9]xx[2-9]xxxxxxS0)
```

After updating, **reboot the ATA** — most firmwares only apply dial plan changes on reboot.

## systemd

### User systemd can't look up supplementary groups

`systemctl --user start foo` fails with `status=216/GROUP` if the unit has `Group=` or `User=` referencing users the user manager can't enumerate.

**Fix:** Drop `User=` and `Group=` from the user unit. The service runs as the logged-in user automatically.
