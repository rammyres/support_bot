
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
   representative's display name (or the customer's name, on the rep's side).
6. A representative can have  **several customers connected at the same
   time** . See "Multiple simultaneous conversations" below for how replies
   get routed to the right customer.
7. Either side can end a conversation at any time.

## Multiple simultaneous conversations

A representative isn't limited to one customer at a time.

* **Only one active conversation?** Plain messages go straight through, no
  extra step needed.
* **More than one active conversation?** Use Telegram's native **reply**
  feature: long-press (or right-click) the customer's message you want to
  answer and hit "Reply", then type your response. The bot reads which
  message you replied to and routes your answer to that exact customer,
  even if others are also messaging at the same time.
* `/chats` — lists everyone you're currently talking to, with a 👉 marker
  showing your current default.
* `/switch <id>` — sets a default customer for plain (non-reply) messages,
  using one of the IDs shown by `/chats`. This stays in effect until you
  reply to someone else or switch again.
* If you send a plain message while several conversations are open and no
  default is set, the bot will ask you to reply to a specific message or
  pick one with `/switch`, rather than guessing.

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

* Python 3.10+
* A Telegram bot token from [@BotFather](https://t.me/BotFather)
* Your Telegram numeric user ID (and any other admins') — get it from
  [@userinfobot](https://t.me/userinfobot)

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Set these environment variables before running:

| Variable                  | Required            | Description                                                                                       |
| ------------------------- | ------------------- | ------------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`             | Yes                 | Token from @BotFather                                                                             |
| `ADMIN_IDS`             | Yes (for approvals) | Comma-separated Telegram user IDs allowed to approve representatives, e.g.`111111111,222222222` |
| `SUPPORT_BOT_DB`        | No                  | Path to the SQLite file (default:`support_bot.db`)                                              |
| `SUPPORT_BOT_MEDIA_DIR` | No                  | Folder where received media is stored (default:`media_storage`)                                 |

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

## Bottom command menu

The bot sets Telegram's native command menu (the list that appears when you
tap the "/" or menu icon next to the message box), tailored per role:

* **Everyone / customers** — `/start`, `/register`, `/end`
* **Approved representatives** — adds `/name`, `/newcode`, `/chats`, `/switch`
* **Admins** — adds `/approve`, `/revoke`, `/listreps`

The menu updates automatically the moment someone registers, gets approved,
or gets revoked - no restart needed. On startup the bot also re-applies the
correct menu to every admin and already-approved representative, in case
their command list changed while the bot was offline.

## Commands

### Everyone

* `/start` — shown when no code is provided; basic instructions.
* `/start <CODE>` — customer enters a code to connect to the assigned
  representative.
* `/end` — ends the current active conversation (works for either side).

### Representatives

* `/register` — begin registration; you'll be asked to pick a display name.
* `/name <new name>` — change your display name at any time.
* `/newcode` — generate a one-time code to give to a customer (requires
  admin approval first).
* `/chats` — list your active conversations.
* `/switch <customer_id>` — set which customer plain messages go to by
  default, when you have more than one active conversation.

### Admins only

* `/approve <telegram_id>` — approve a pending representative.
* `/revoke <telegram_id>` — revoke a representative's access.
* `/listreps` — list all registered representatives and their status.

## Data storage

All state lives in a local SQLite database (`support_bot.db` by default):

* **representatives** — Telegram ID, display name, approval status, and
  which customer is currently their default reply target (used when they
  have multiple active conversations and send a plain, non-reply message).
* **codes** — one-time codes and which representative they're tied to.
* **sessions** — active/past customer ↔ representative pairings, including
  the customer's display name.
* **media_log** — a record of every photo/video/document/audio/voice file
  relayed, including who sent it, the local file path, and timestamp.
* **thread_map** — internal bookkeeping that links a message sent into a
  representative's chat back to the customer it came from, so replies route
  correctly. Entries accumulate over time; if the table grows large, old
  rows can be purged periodically since only entries needed to resolve very
  recent replies matter in practice.

Media files themselves are downloaded into `media_storage/<customer_id>/`,
named with a timestamp, sender role, and type
(e.g. `20260621_143000_123456_customer_photo.jpg`).

## Current design limits

* Codes are single-use and tied to one representative.
* There's no built-in transcript export — the `media_log` table and SQLite
  file are there if you want to build reporting on top later.
* There's no hard cap on how many simultaneous conversations a
  representative can take on — it scales with however many customers they
  can actually keep up with.

## Privacy / compliance note

This bot stores customer files indefinitely on local disk. Depending on your
jurisdiction (e.g. Brazil's LGPD, or GDPR in the EU) you are likely required
to inform customers that their messages and files are being recorded, define
a retention/deletion policy, and restrict access to the storage folder and
database. This script does not implement consent prompts or automatic
deletion — add those if your legal requirements call for them.
