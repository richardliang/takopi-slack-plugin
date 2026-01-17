# takopi-slack-plugin

Slack transport plugin for Takopi. Supports Socket Mode (recommended) or Web
API polling, and responds in a single channel or DM.

## Requirements

- Python 3.14+
- takopi >=0.20.0
- Slack bot token with `chat:write`
- For polling: a history scope for the channel type (e.g. `channels:history`)
- For Socket Mode: an app token (`xapp-`) with `connections:write`

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
channel_id = "C12345678"
# message_overflow = "trim"  # or "split"
# reply_in_thread = false
# require_mention = false
# poll_interval_s = 1.0
```

### Socket Mode (recommended)

Socket Mode avoids polling and rate limits. Enable it in your Slack app and
create an app-level token with `connections:write`, then configure:

```toml
[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
socket_mode = true
channel_id = "C12345678"
require_mention = true
```

Enable Slack events for `message.channels`, `message.groups`, `message.im`,
`message.mpim`, and/or `app_mention`, depending on your channel type.

## Context shortcuts

Slack treats `/` at the start of a message as a slash command and will not
deliver it to the bot. Use a mention first so the message is delivered:

```
@takopi /<project> @<branch> do the thing
```

If you send only directives (no prompt), the bot saves the context for the
channel/thread:

```
@takopi /<project> @<branch>
```

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
