# Telegram and Discord setup

[← Back to README](../README.md)

Both integrations are optional. A Connection Test uses the values currently
in the fields, so credentials can be checked before saving.

With either one connected, your phone gets failure alerts with the analysed
picture, and these buttons work from the chat:

- 🔍 **Check** — see the current camera view and printer status.
- 🔇 **Mute / Unmute** — control alerts for the current print.
- ⏸️ **Pause / Resume** — step in without opening OctoPrint.
- ⏹️ **Stop** — cancel the print after a confirmation.

## ✈️ Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the
   **Bot Token**.
2. Get your **Chat ID** (for a private chat, a group, or a supergroup). Chat
   IDs must contain at least five digits, with an optional leading minus sign
   for a group.
3. Enter both under **Settings → PiNozCam → Monitoring & Alerts**, press
   **Connection Test**, then save.

Use **one bot token per printer**. Several printers may post into the same
chat, but Telegram permits only one active update consumer per bot token.
Sharing a token makes button delivery unpredictable and produces a Telegram
`409 Conflict`.

## 💬 Discord

1. Create a bot at
   [discord.com/developers](https://discord.com/developers/applications).
2. Invite it with permission to view the channel, send messages, and attach
   files.
3. Enter its **Bot Token** and the channel's 17–20 digit **Channel ID**, press
   **Connection Test**, then save. Channel IDs are entered as text so a very
   large ID cannot be rounded by the browser.

No incoming port, public domain, or TLS certificate is needed — the bot only
makes outbound connections. If the button connection drops temporarily, alerts
are still delivered; buttons recover on reconnect.

## Monitoring several printers

- **Telegram:** one bot per printer; they may all post into the same group.
- **Discord:** printers may share one bot and one channel. Buttons carry a
  hidden per-installation identity, so only the printer that created a message
  answers its buttons. Typed commands such as `!check` have no target — in a
  shared channel, use buttons instead.

Give every OctoPrint instance a distinct **Settings → Appearance → Name** so
its messages are easy to recognise.

## Privacy notes

| Optional configuration | Data sent outside your network |
|---|---|
| Telegram credentials enabled | analysed camera image and print status to Telegram |
| Discord credentials enabled | analysed camera image and print status to Discord; commands read from the configured channel |

Anyone allowed to operate the configured chat/channel controls may request a
view or operate the printer, so use a private destination and protect bot
tokens as passwords. Tokens and camera credentials are redacted from logs;
draft credentials require OctoPrint administrator permission.
