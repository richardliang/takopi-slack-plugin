# takopi-slack-plugin

Slack transport plugin for Takopi. Uses Slack Socket Mode only, and responds in a
single channel or DM.

## Requirements

- Python 3.14+
- takopi >=0.20.0
- Slack bot token with `chat:write`
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
```

### Required Directives

Slack messages must include both a project and worktree directive in the
first line. Messages that do not match are ignored.

Example:

```
@takopi /zkp2p-clients @feat/web/monad-usdt0 add a retry to the API call
```

### Socket Mode (required)

Enable Socket Mode in your Slack app and create an app-level token with
`connections:write`, then configure:

```toml
[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C12345678"
require_mention = true
```

Enable Slack events for `message.channels`, `message.groups`, `message.im`,
`message.mpim`, and/or `app_mention`, depending on your channel type.

### Thread Sessions

To retain context and resume tokens per Slack thread, enable session mode:

```toml
[transports.slack]
session_mode = "thread"
reply_in_thread = true
```

Takopi stores per-thread sessions at `~/.takopi/slack_thread_sessions_state.json`.
The thread context updates when you set project/branch directives.

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
