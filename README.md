# takopi-slack-plugin

Slack transport plugin for Takopi. Polls the Slack Web API and responds in a
single channel or DM.

## Requirements

- Python 3.14+
- takopi >=0.20.0
- Slack bot token with `chat:write` and a history scope for the channel type
  (e.g. `channels:history` for public channels)

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
