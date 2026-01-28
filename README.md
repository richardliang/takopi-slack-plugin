# takopi-slack-plugin

slack transport plugin for takopi. socket mode only, replies in threads, and
stores per-thread context + resume tokens.

## features

- socket mode only; listens in a single channel or dm
- thread sessions (context + resume tokens) stored at
  `~/.takopi/slack_thread_sessions_state.json`
- slash commands + message shortcuts for overrides and plugin commands
- cancel button on progress messages
- archive button on responses (deletes worktree or resets to origin/main)
- optional stale worktree reminders prompting archive (default 24h)
- message overflow: split or trim long responses

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
5. invite the bot to the target channel

add to `~/.takopi/takopi.toml`:

```toml
transport = "slack"

[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C12345678"
message_overflow = "split"
stale_worktree_reminder = true
stale_worktree_hours = 24
stale_worktree_check_interval_s = 600

[transports.slack.files]
enabled = false
auto_put = true
auto_put_mode = "upload"
uploads_dir = "incoming"
```

set `message_overflow = "trim"` if you prefer truncation instead of followups.

if you use a plugin allowlist, enable this distribution:

```toml
[plugins]
enabled = ["takopi-slack-plugin"]
```

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
@takopi /zkp2p-clients @feat/login /preview start 3000 --dev "pnpm --filter @zkp2p/web dev -- --host 127.0.0.1 --port {port}"
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
