# takopi-slack-plugin

Slack transport plugin for Takopi. Uses Slack socket connections only, and
responds in a single channel or DM.

## Requirements

- Python 3.14+
- takopi >=0.20.0
- Slack bot token with `chat:write`, `commands`, `app_mentions:read`, and
  the matching history scopes for your channel type (`channels:history`,
  `groups:history`, `im:history`, `mpim:history`)
- Slack app token (`xapp-`) with `connections:write`

## Install

Install into the same Python environment as Takopi.

Using uv tool installs:

```sh
uv tool install -U takopi --with takopi-slack-plugin
```

Using a virtualenv:

```sh
pip install takopi-slack-plugin
```

## Configure

Add to your `~/.takopi/takopi.toml`:

```toml
transport = "slack"

[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C12345678"
message_overflow = "split"
```

Set `message_overflow = "trim"` if you prefer truncation instead of followup
messages.

### Required Directives

New root messages must include both a project and worktree directive in the
first line. Messages that do not match are ignored.

Thread replies reuse the stored context, so you can reply without repeating
directives.

Example:

```
@takopi /zkp2p-clients @feat/web/monad-usdt0 add a retry to the API call
```

### Slash Command + Shortcuts

Configure a single slash command (for example `/takopi`) and optionally a
message shortcut.

Enable Slack "Slash Commands" and "Interactivity & Shortcuts" for the app so
Socket Mode can deliver command payloads and button/shortcut actions.

Slash command usage:

```
/takopi <plugin_id> [args...]
/takopi status
/takopi engine <engine|clear>
/takopi model <engine> <model|clear>
/takopi reasoning <engine> <level|clear>
/takopi session clear
```

Message shortcut:

- Create a message shortcut and set its Callback ID to `takopi:<plugin_id>`.
- The selected message text is passed as the command arguments.

### Cancel Button

Progress messages include a cancel button. Enable Slack "Interactivity &
Shortcuts" so button clicks are delivered in Socket Mode.

### Socket Connections (required)

Enable Slack socket connections in your app and create an app-level token with
`connections:write`, then configure:

```toml
[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C12345678"
```

Enable Slack events for `message.channels`, `message.groups`, `message.im`,
`message.mpim`, and/or `app_mention`, depending on your channel type.

### Thread Sessions

Takopi always replies in threads and stores resume tokens per thread at
`~/.takopi/slack_thread_sessions_state.json`.

Thread state also stores per-thread overrides for default engine/model/
reasoning. Use the `/takopi` slash command to manage them.
Replies inside a thread will automatically use that stored context.

If you use a plugin allowlist, enable this distribution:

```toml
[plugins]
enabled = ["takopi-slack-plugin"]
```

## Start

Run Takopi with the Slack transport:

```sh
takopi --transport slack
```

If you already set `transport = "slack"` in the config, `takopi` is enough.

Optional interactive setup:

```sh
takopi --onboard --transport slack
```
