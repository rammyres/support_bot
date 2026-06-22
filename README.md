# Telegram Customer ↔ Representative Relay Bot

A Telegram bot that connects a customer to a specific representative using a
one-time code, then relays text, photos, videos, voice notes, audio, and
documents between them in real time. Representatives must be approved by an
admin before they can use the bot, and all media exchanged in a conversation
is saved to local disk with a database record for later reference.

## How it works

1. An **admin** approves a Telegram user as a representative.
2. The **representative** registers and picks a display name.
3. The representative generates a one-time **code** and shares it with a
   customer through any channel (phone, email, website, etc.) outside of
   Telegram.
4. The **customer** opens a private chat with the bot and sends the code.
5. The bot links them into a live session. From that point, anything either
   side sends is relayed to the other side automatically, labeled with the
   representative's display name.
6. Either side can end the conversation at any time.

## Telegram limitation you should know about

Telegram's native **"Forwarded from"** tag is tied to a real account/channel
identity — the Bot API has no way to set it to an arbitrary custom name (this
is intentional, to prevent impersonation). This bot works around that by
using `copy_message` (no forward tag at all) and prepending a small label
line with the representative's chosen name before each relayed message:

```
👤 João
Hi, how can I help you today?
```

## Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram numeric user ID (and any other admins') — get it from
  [@userinfobot](https://t.me/userinfobot)

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Set these environment variables before running:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Token from @BotFather |
| `ADMIN_IDS` | Yes (for approvals) | Comma-separated Telegram user IDs allowed to approve representatives, e.g. `111111111,222222222` |
| `SUPPORT_BOT_DB` | No | Path to the SQLite file (default: `support_bot.db`) |
| `SUPPORT_BOT_MEDIA_DIR` | No | Folder where received media is stored (default: `media_storage`) |

Example:

```bash
export BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRstuVWXyz"
export ADMIN_IDS="111111111,222222222"
```

## Running

```bash
python3 support_bot.py
```

The bot uses long polling — keep the process running (e.g. under `systemd`,
`pm2`, `tmux`, or a Docker container) for it to stay online.

## Commands

### Everyone
- `/start` — shown when no code is provided; basic instructions.
- `/start <CODE>` — customer enters a code to connect to the assigned
  representative.
- `/end` — ends the current active conversation (works for either side).

### Representatives
- `/register` — begin registration; you'll be asked to pick a display name.
- `/name <new name>` — change your display name at any time.
- `/newcode` — generate a one-time code to give to a customer (requires
  admin approval first).

### Admins only
- `/approve <telegram_id>` — approve a pending representative.
- `/revoke <telegram_id>` — revoke a representative's access.
- `/listreps` — list all registered representatives and their status.

## Data storage

All state lives in a local SQLite database (`support_bot.db` by default):

- **representatives** — Telegram ID, display name, approval status.
- **codes** — one-time codes and which representative they're tied to.
- **sessions** — active/past customer ↔ representative pairings.
- **media_log** — a record of every photo/video/document/audio/voice file
  relayed, including who sent it, the local file path, and timestamp.

Media files themselves are downloaded into `media_storage/<customer_id>/`,
named with a timestamp, sender role, and type
(e.g. `20260621_143000_123456_customer_photo.jpg`).

## Current design limits

- A representative handles **one customer at a time**. Ask if you need a
  multi-session/queue model for concurrent customers per representative.
- Codes are single-use and tied to one representative.
- There's no built-in transcript export — the `media_log` table and SQLite
  file are there if you want to build reporting on top later.

## Privacy / compliance note

This bot stores customer files indefinitely on local disk. Depending on your
jurisdiction (e.g. Brazil's LGPD, or GDPR in the EU) you are likely required
to inform customers that their messages and files are being recorded, define
a retention/deletion policy, and restrict access to the storage folder and
database. This script does not implement consent prompts or automatic
deletion — add those if your legal requirements call for them.
