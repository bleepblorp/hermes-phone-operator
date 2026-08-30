# Installation guide

## Prerequisites

- FreePBX 17+ with ARI module (default in FreePBX 16/17) at `http://<freepbx>:8088`
- A working PJSIP endpoint (you need at least one registered phone to dial from)
- This VM (any modern Linux with Python 3.11+ and outbound HTTPS)
- Hermes CLI installed at `/home/bertram/.local/bin/hermes` on this VM, and a working model (e.g. `minimax/minimax-m3`)
- ssh key auth between this VM and FreePBX as user `hermes` with `NOPASSWD sudo`

## 1. FreePBX ARI setup (via GUI)

1. **Settings → Asterisk REST Interface Users**
   - Add user `hermes` with a strong password (we use `81205cdb86a02902edf6f1a6811799df`)
   - Set `read_only = no`
   - Apply Config

2. **Settings → Advanced Settings → Asterisk Builtin mini-HTTP server**
   - Set `bindaddr = 0.0.0.0`
   - Submit
   - This is the critical one — `ari_general_custom.conf` overrides get clobbered on every reload, so you have to set it here.

3. **Verify from this VM:**
   ```bash
   curl -s -u hermes:<password> http://<freepbx>:8088/ari/asterisk/info
   ```
   Should return a JSON blob with `system.system.version`.

## 2. SSH user on FreePBX

```bash
# On FreePBX (or via Proxmox console)
sudo adduser hermes
sudo mkdir -p /home/hermes/.ssh
# Paste this VM's ed25519 public key
sudo bash -c "cat >> /home/hermes/.ssh/authorized_keys"
sudo chown -R hermes:hermes /home/hermes/.ssh
echo "hermes ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/90-hermes
sudo chmod 440 /etc/sudoers.d/90-hermes
```

Verify:
```bash
ssh hermes@<freepbx> 'sudo asterisk -rx "core show channels concise"'
```

## 3. FreePBX directories

```bash
# Recordings (ARI writes here)
sudo mkdir -p /var/spool/asterisk/recording
sudo chown asterisk:asterisk /var/spool/asterisk/recording

# TTS output (hermes writes here)
sudo mkdir -p /var/lib/asterisk/sounds/hermes-tts
sudo chown hermes:hermes /var/lib/asterisk/sounds/hermes-tts

# Call logs (Obsidian sync pulls from here)
sudo mkdir -p /var/log/hermes-calls
sudo chown hermes:hermes /var/log/hermes-calls
```

## 4. Python venv on FreePBX

```bash
sudo apt-get install -y python3-pip python3-venv ffmpeg
sudo -u hermes python3 -m venv /opt/hermes-ari/venv
sudo -u hermes /opt/hermes-ari/venv/bin/pip install \
    faster-whisper edge-tts flask websockets requests
```

## 5. Bridge code

```bash
sudo mkdir -p /opt/hermes-ari
sudo cp bridge/bridge.py /opt/hermes-ari/
sudo chown -R hermes:hermes /opt/hermes-ari
```

## 6. FreePBX dialplan

Edit `/etc/asterisk/extensions_custom.conf`. Append:

```ini
[hermes-record]
exten => 0,1,NoOp(Hermes recording)
 same => n,Set(RECORDING_FILE=/opt/hermes-ari/recordings/${UNIQUEID}.wav)
 same => n,MixMonitor(${RECORDING_FILE},a)
 same => n,Playback(beep)
 same => n,Wait(6)
 same => n,StopMixMonitor()
 same => n,Stasis(hermes,playback)
 same => n,Hangup()
```

**NOTE:** This context exists for legacy reasons but the bridge no longer uses it. The dialplan only needs the `hermes-context`:

```ini
[hermes-context]
exten => 0,1,NoOp(Entering Hermes ARI bridge)
 same => n,Stasis(hermes)
 same => n,Hangup()
```

Then route extension 0 to this context. Easiest: add to `[from-internal-custom]`:

```ini
[from-internal-custom]
exten => 0,1,NoOp(Send 0 to Hermes ARI)
 same => n,Goto(hermes-context,0,1)
```

Reload:

```bash
sudo asterisk -rx "dialplan reload"
```

## 7. systemd on FreePBX

```bash
sudo cp deploy/hermes-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-bridge.service
sudo systemctl status hermes-bridge.service --no-pager
```

Should show `Active: active (running)`.

## 8. Webhook on this VM

```bash
# Set up venv
python3 -m venv webhook/venv
webhook/venv/bin/pip install -r webhook/requirements.txt  # if you add one; otherwise just `pip install flask`

# Install systemd --user unit
mkdir -p ~/.config/systemd/user
cp deploy/hermes-phone-server.service ~/.config/systemd/user/
loginctl enable-linger bertram
systemctl --user daemon-reload
systemctl --user enable --now hermes-phone-server.service
systemctl --user status hermes-phone-server.service
```

## 9. Linksys ATA dial plan

For each ATA line, set the dial plan to:

```
(0S0|2xxS0|*xx|911S0|[3469]11|9[2-9]xx[2-9]xxxxxxS0)
```

The `0S0` part is what makes the ATA actually send `0` to FreePBX. Save and reboot the ATA.

## 10. Obsidian sync (optional)

```bash
mkdir -p ~/bin
cp deploy/sync-hermes-calls.sh ~/bin/
chmod +x ~/bin/sync-hermes-calls.sh

# Add crontab
crontab -l > /tmp/cron.bak
echo "*/5 * * * * ~/bin/sync-hermes-calls.sh >> /var/log/hermes-sync.log 2>&1" >> /tmp/cron.bak
crontab /tmp/cron.bak
```

Make sure the destination Obsidian folder exists:

```bash
mkdir -p ~/obsidian-vault/Bertram's\ Notebook/Hermes\ Phone\ Calls/
```

## Verification

From any registered phone, dial **0**. You should hear:

1. Synthesized greeting: "You've reached the Operator. How may I assist you?"
2. Beep
3. Speak your question
4. "One moment please" thinking tone
5. Hermes reply (Emma's voice)
6. Beep again for next turn

Say "goodbye" to hang up. The call will appear as a markdown file in your Obsidian vault within 5 minutes.
