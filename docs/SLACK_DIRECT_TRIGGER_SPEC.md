# Slack Direct Trigger Spec

## Summary

`takopi-slack-plugin` already consumes Slack `events_api` envelopes over
Socket Mode and can start runs from:

- `app_mention` events in channels
- plain `message` events in DMs and MPIMs
- plain `message` events inside existing threads

The missing behavior is top-level channel messages that should become Takopi
prompts without requiring `@takopi` in the message body.

This spec adds that capability as an opt-in transport setting and keeps the
existing Python transport as the source of truth for prompt parsing, context
storage, command dispatch, and file handling. Bolt JS is treated as an optional
future ingress adapter, not the default implementation.

## Problem

Today, `_should_process_socket_message()` in
`src/takopi_slack_plugin/bridge.py` only accepts top-level channel messages
when the incoming event type is `app_mention`. In practice that means a user
must start a prompt with `@takopi ...` unless they are:

- sending a DM
- replying inside an existing Takopi thread

That behavior is safe for mixed-use channels, but it blocks a dedicated
Takopi channel workflow where every human message should be treated as the
prompt itself.

## Goals

- Let dedicated Slack channels treat a top-level message as the full Takopi
  prompt without a bot mention.
- Preserve the current default behavior for existing installations.
- Keep thread session reuse, inline command dispatch, uploads, allowlists,
  and reply routing unchanged.
- Leave a clean seam for a future Bolt JS ingress layer if we want Slack's
  higher-level listener API later.

## Non-goals

- Replacing the current Python transport with a Node service in v1.
- Changing slash command, shortcut, interactivity, or file-upload semantics.
- Making every allowed channel direct-triggered by default.
- Supporting partial free-form conversation parsing in mixed team channels.

## External Constraints

- Slack's Bolt JS docs define `app.message()` as the message listener API.
- Slack requires the matching `message.*` event subscriptions for the
  conversation types we want to hear from.
- Socket Mode remains compatible with this feature, whether ingress is the
  current websocket loop or a future Bolt receiver.

For direct channel triggers, the Slack app still needs the same history scopes
plus the relevant `message.channels`, `message.groups`, `message.im`, and
`message.mpim` subscriptions.

## Recommendation

Implement direct triggers in the existing Python transport first.

Reasoning:

- The plugin already has working Socket Mode ingestion, filtering, hot reload,
  and tests.
- Prompt execution, thread state, and command routing already live in Python.
- Pulling in Bolt JS now would add a second runtime and a second Slack ingress
  implementation before we have proven the product behavior.

If we still want Bolt later, v1 should extract a small normalization seam so a
Bolt process can hand the same normalized inbound message to the existing
Python handler.

## Proposed Configuration

Add a new Slack transport setting:

```toml
[transports.slack]
channel_message_mode = "mention_or_thread"
```

Allowed values:

- `mention_or_thread` (default)
  Keeps current behavior.
- `direct_or_thread`
  In allowed channels, any top-level human-authored message becomes the prompt.
  Thread replies still behave exactly as they do today.

Design notes:

- This is intentionally transport-wide for the first cut.
- The feature stays opt-in and defaults to current behavior.
- Docs should strongly recommend using `direct_or_thread` only in dedicated
  Takopi channels.

Future extension if needed:

- `channel_message_mode_by_channel = { "C123": "direct_or_thread" }`

That should be deferred until a real use case requires mixed modes in the same
installation.

## User-visible Behavior

When `channel_message_mode = "direct_or_thread"`:

- A human user posts `fix the failing preview deploy` in an allowed channel.
- The full message text becomes the Takopi prompt.
- The bot replies using the configured `reply_mode`.
- If `reply_mode = "thread"`, follow-up replies in the thread continue the
  same Takopi session exactly as they do today.
- If the user includes directives like `/project` or `@branch`, the existing
  directive parser continues to handle them.
- Inline plugin commands like `/preview start 3000` still route through the
  current command dispatch path.
- Mentioning `@takopi` still works and the mention is stripped before prompt
  execution, but it is no longer required.

Unchanged behavior:

- DMs and MPIMs still work without a mention.
- Existing thread replies still work.
- Bot-authored events, most Slack subtypes, and blank messages are still
  ignored.
- `allowed_user_ids` and `allowed_channel_ids` remain the first gating layer.

## Internal Design

### 1. Config model

Update `src/takopi_slack_plugin/config.py`:

- Add `channel_message_mode: Literal["mention_or_thread", "direct_or_thread"]`
  to `SlackTransportSettings`.
- Parse and validate the setting in `SlackTransportSettings.from_config()`.
- Reject unknown values with `ConfigError`.

### 2. Runtime state

Ensure the live Slack state object carries `channel_message_mode` so hot reload
can swap the behavior without restarting the process.

### 3. Message filtering

Change the channel gating logic in `src/takopi_slack_plugin/bridge.py`.

Current behavior:

```python
def _should_process_socket_message(event, message) -> bool:
    if event.get("type") == "app_mention":
        return True
    if event.get("channel_type") in {"im", "mpim"}:
        return True
    return message.thread_ts is not None
```

Proposed behavior:

```python
def _should_process_socket_message(cfg, event, message) -> bool:
    if event.get("type") == "app_mention":
        return True
    if event.get("channel_type") in {"im", "mpim"}:
        return True
    if message.thread_ts is not None:
        return True
    return (
        event.get("channel_type") in {"channel", "group"}
        and cfg.state.channel_message_mode == "direct_or_thread"
    )
```

Important details:

- Mention events stay accepted in every mode.
- Direct mode only changes top-level messages in public or private channels.
- Existing subtype and bot filtering in `_should_skip_message()` stays intact.
- `_strip_bot_mention()` should continue to run so a message that still starts
  with `@takopi` works in either mode.

### 4. Prompt execution path

Do not fork a new execution path.

After filtering, the message should continue through the current flow:

- `_safe_handle_slack_message()`
- `_handle_slack_message()`
- directive parsing
- thread-store lookup
- file upload handling
- inline command extraction
- `run_engine()`

That keeps the feature small and avoids regressions in session behavior.

### 5. Hot reload

The existing config reload path should treat `channel_message_mode` like the
other transport settings:

- update live state on valid reload
- keep old behavior on invalid reload
- reconnect only if token-level Slack auth settings change

Direct-trigger mode should not require a websocket reconnect if only the local
filtering behavior changes.

## Bolt JS Adapter Plan

This is a follow-on design, not part of v1 delivery.

If we want Bolt's listener API later, add a separate ingress adapter instead of
moving prompt logic into Node.

Suggested shape:

- New optional package or service, for example
  `services/takopi-slack-bolt-listener`.
- Use `@slack/bolt` with Socket Mode enabled.
- Register `app.message()` plus `app.event("app_mention")`.
- Normalize the inbound event into a small payload:

```json
{
  "event_type": "message",
  "channel_type": "channel",
  "channel_id": "C123",
  "user_id": "U123",
  "ts": "1710000000.000100",
  "thread_ts": null,
  "text": "fix the failing preview deploy",
  "files": []
}
```

- Hand that payload to the Python plugin over a local adapter boundary.

Recommended adapter boundary:

- local HTTP on `127.0.0.1`
- or stdio if we want a tightly coupled subprocess model

Not recommended:

- duplicating directive parsing or session logic in Node
- having separate command-routing rules in Bolt and Python

## Files To Change In V1

- `src/takopi_slack_plugin/config.py`
  Add config parsing and validation.
- `src/takopi_slack_plugin/bridge.py`
  Make message gating mode-aware and hot-reloadable.
- `tests/test_slack_config.py`
  Cover parsing and invalid values.
- `tests/test_slack_access_and_routing.py`
  Cover direct top-level channel handling.
- `tests/test_slack_hot_reload.py`
  Cover changing the mode via config reload without restart.
- `README.md`
  Document direct-trigger mode, recommended channel usage, and required Slack
  event subscriptions.

## Test Plan

- Add config tests for the new default and invalid values.
- Add routing tests proving:
  - top-level channel message is rejected in `mention_or_thread`
  - top-level channel message is accepted in `direct_or_thread`
  - thread replies and DMs remain accepted in both modes
- Add hot-reload coverage proving a config change flips behavior live.
- Add one end-to-end transport test proving a top-level channel message in
  direct mode reaches `_handle_slack_message()` with the raw text as prompt.

## Rollout

Phase 1:

- Ship `channel_message_mode`
- update docs
- keep default behavior unchanged

Phase 2, only if needed:

- extract a normalized ingress interface in Python
- prototype a Bolt JS sidecar against that interface

## Risks

- In a mixed-use channel, direct mode would treat normal conversation as
  Takopi prompts.
- `reply_mode = "channel"` is more likely to feel noisy when every top-level
  message triggers a run.
- Slack event subscriptions must stay aligned with the conversation types we
  expect to support.

## Open Questions

- Do we need per-channel mode overrides immediately, or is a transport-wide
  toggle enough for the first release?
- Do we want an additional prefix or regex guard for mixed-use channels, or is
  "dedicated Takopi channels only" sufficient as the product stance?
- If Bolt is eventually added, should the adapter boundary be local HTTP or a
  managed child process over stdio?
