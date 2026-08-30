#!/bin/bash
# Sync Hermes call logs from FreePBX to local Obsidian vault.
# Runs every 5 minutes via cron.

REMOTE="hermes@192.168.1.36"
REMOTE_DIR="/var/log/hermes-calls/"
LOCAL_DIR="/home/bertram/obsidian-vault/Bertram's Notebook/Hermes Phone Calls"

mkdir -p "$LOCAL_DIR"

# Copy any new .md files from remote
scp -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
    "$REMOTE":"$REMOTE_DIR"*.md "$LOCAL_DIR"/ 2>/dev/null
