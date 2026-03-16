# takopi-slack-plugin

slack transport plugin for takopi. socket mode only, supports thread or
top-level replies, and stores per-thread context + resume tokens.

For the planned direct-trigger and Bolt ingress update, see
[docs/SLACK_DIRECT_TRIGGER_SPEC.md](docs/SLACK_DIRECT_TRIGGER_SPEC.md).

## features

- socket mode only; listens in one or more configured channels or dms
- thread sessions (context + resume tokens) stored at
  `~/.takopi/slack_thread_sessions_state.json`
- slash commands + message shortcuts for overrides and plugin commands
- cancel button on progress messages
- archive button on responses (deletes worktree or resets to origin/main)
- configurable action buttons next to archive (mapped to takopi commands)
- optional user allowlist for who can invoke the bot
- configurable reply mode: thread replies or top-level channel messages
- optional stale worktree reminders prompting archive (default 24h)
- message overflow: split or trim long responses

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the transport routing and
session model. See [docs/TAKOPI_TOML.md](docs/TAKOPI_TOML.md) for a focused
configuration guide.

## requirements

- python 3.14+
- takopi >=0.20.0
- slack bot token with `chat:write`, `commands`, `app_mentions:read`, and
  the matching history scopes for your channel type (`channels:history`,
  `groups:history`, `im:history`, `mpim:history`)
- slack app token (`xapp-`) with `connections:write`

## install

install into the same python environment as takopi.

```sh
uv tool install -U takopi --with takopi-slack-plugin
```

or, with a virtualenv:

```sh
pip install takopi-slack-plugin
```

## setup

create a slack app and enable socket mode.

1. create an app-level token with `connections:write`
2. add the bot scopes listed above and install the app
3. enable events for `app_mention` plus the right `message.*` event for your
   channel type
4. enable interactivity & shortcuts, create a slash command (for example
   `/takopi`), and optionally add dedicated commands like `/takopi-preview`
   for common plugins plus message shortcuts with callback id
   `takopi:<plugin_id>`
5. invite the bot to the default channel plus any extra channels you configure

add to `~/.takopi/takopi.toml`:

```toml
transport = "slack"

[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C12345678"
allowed_user_ids = ["U12345678"]
allowed_channel_ids = ["C87654321"]
reply_mode = "thread"
message_overflow = "split"
stale_worktree_reminder = true
stale_worktree_hours = 24
stale_worktree_check_interval_s = 600
action_handlers = [
  { id = "preview", command = "preview", args = "start" },
]
action_blocks = """
[
  {
    "type": "actions",
    "elements": [
      {
        "type": "button",
        "text": { "type": "plain_text", "text": "Preview" },
        "action_id": "takopi-slack:action:preview",
        "style": "primary"
      },
      {
        "type": "button",
        "text": { "type": "plain_text", "text": "archive" },
        "action_id": "takopi-slack:archive"
      }
    ]
  }
]
"""
[transports.slack.plugin_channels]
cron = "C87654321"

[transports.slack.files]
enabled = false
auto_put = true
auto_put_mode = "upload"
uploads_dir = "incoming"
```

set `message_overflow = "trim"` if you prefer truncation instead of followups.

`allowed_user_ids` limits who can invoke the bot. Leave it empty to allow any
user in an allowed channel.

`allowed_channel_ids` adds extra channels that the bot will listen in. The
default `channel_id` is always included automatically. In channels, top-level
messages must mention the bot; plain `message` events are only processed for
thread replies. Use `allowed_channel_ids = ["*"]` or `["all"]` to allow every
channel the bot has joined.

`reply_mode = "thread"` keeps the current threaded response behavior.
`reply_mode = "channel"` posts bot replies as top-level messages instead.

`action_handlers` maps arbitrary Block Kit `action_id` values to Takopi
commands. Use `action_id` for full control, or `id` to generate
`takopi-slack:action:<id>`. There is no built-in limit.

`action_blocks` lets you provide raw Block Kit JSON (as a JSON string, or
`@/path/to/blocks.json`) to render alongside the message text instead of the
default actions. Use `action_id = "takopi-slack:archive"` for the archive
action, and `action_id = "takopi-slack:action:<id>"` to map to entries in
`action_handlers`.

Archive now requires confirmation: clicking `takopi-slack:archive` posts a
confirm/cancel prompt, and confirmation deletes the worktree (discarding local
changes).

`plugin_channels` routes command outputs from specific plugin commands to
alternate channels. Entries are keyed by command id, or by a command and first
argument pair for subcommand-style routing:

```toml
[transports.slack.plugin_channels]
cron = "C87654321"
"cron summary" = "C43210987"
```

If a command is not listed, output stays in the invoking channel.

You can also normalize plugin-style IDs here; `takopi-cron` and `cron` map to
the same command key.

if you use a plugin allowlist, enable this distribution:

```toml
[plugins]
enabled = ["takopi-slack-plugin"]
```

For step-by-step examples, migration notes, and common channel-routing setups,
see [docs/TAKOPI_TOML.md](docs/TAKOPI_TOML.md).

## usage

```sh
takopi --transport slack
```

if you already set `transport = "slack"`, `takopi` is enough.

directives are optional. use `/project` and `@branch` in the first line to
target a project or worktree; otherwise the run uses the default takopi context.

inline command mode: if the remaining prompt includes a `/command` that matches
a registered command plugin, the slack bridge dispatches it instead of running
the engine.

example (inline command):

```
@takopi /zkp2p-clients @feat/login /preview start <port> --dev "pnpm --filter @zkp2p/web dev -- --host 127.0.0.1 --port <port>"
```

example (worktree):

```
@takopi /zkp2p-clients @feat/web/monad-usdt0 add a retry to the api call
```

thread replies reuse stored context, so you can reply without repeating
directives.

slash command built-ins (via `/takopi <command>` or `/takopi-<command>`):

```
/takopi status
/takopi engine <engine|clear>
/takopi model <engine> <model|clear>
/takopi reasoning <engine> <level|clear>
/takopi session clear
```

message shortcuts pass the selected message text as arguments to the plugin
command identified by `takopi:<plugin_id>`.

progress messages include a cancel button; enable interactivity & shortcuts so
clicks are delivered in socket mode.

for opinionated gating, see `docs/AGENTS.example.md` and `docs/GATING_README.md`, and
customize `~/.codex/AGENTS.md`.

## license

mit
