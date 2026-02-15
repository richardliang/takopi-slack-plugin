# Alert Message Routing Spec (Generic)

## 1. Purpose

Define a simple, plugin-agnostic way to route flagged messages to different Slack channels based on message type and optional plugin/source rules.

## 2. Design Goals

- Work for any plugin/source.
- Work for any message type (error, warning, transaction, moderation, etc.).
- Keep routing rules simple and deterministic.
- Keep payload format stable so plugins can evolve independently.

## 3. Canonical Event Format

Every plugin emits a normalized event before routing:

```json
{
  "message_id": "uuid-or-stable-id",
  "occurred_at": "2026-02-15T15:30:00Z",
  "plugin": "plugin-name",
  "message_type": "error",
  "flags": ["urgent", "security"],
  "title": "Short summary",
  "body": "Human-readable details",
  "metadata": {
    "key": "value"
  }
}
```

Required fields:

- `message_id`
- `occurred_at`
- `plugin`
- `message_type`
- `flags` (empty array allowed)
- `title`
- `body`

`metadata` is optional and free-form.

## 4. Routing Configuration

```json
{
  "mode": "first_match",
  "default_channel": "#general-alerts",
  "routes": [
    {
      "id": "security-urgent",
      "when": {
        "message_type": "error",
        "all_flags": ["security", "urgent"]
      },
      "channel": "#security-alerts"
    },
    {
      "id": "payments-warning",
      "when": {
        "plugin": "payments-plugin",
        "message_type": "warning",
        "any_flags": ["fraud", "chargeback"]
      },
      "channel": "#payments-alerts"
    },
    {
      "id": "ops-fallback",
      "when": {
        "message_type": "error"
      },
      "channel": "#ops-alerts"
    }
  ]
}
```

Field rules:

- `mode`: `first_match` or `all_matches`
- `default_channel`: optional fallback channel when no route matches
- `routes`: ordered list
- `routes[].id`: unique rule id
- `routes[].channel`: Slack channel id or name
- `routes[].when.plugin`: optional; exact match or `"*"`
- `routes[].when.message_type`: optional; exact match or `"*"`
- `routes[].when.all_flags`: optional; all listed flags must exist
- `routes[].when.any_flags`: optional; at least one listed flag must exist

## 5. Matching Semantics

1. Validate event schema.
2. Evaluate routes in order.
3. A route matches when all provided conditions are true.
4. If `mode = first_match`, send to the first matched route only.
5. If `mode = all_matches`, send once per unique matched channel.
6. If no route matches and `default_channel` exists, send to `default_channel`.
7. If no route matches and no default exists, do not send.

## 6. Delivery Semantics

- Use `message_id + channel + route_id` as an idempotency key.
- Deduplicate repeats for a short TTL (recommended: 5 minutes).
- Retry failed Slack sends up to 3 times with exponential backoff.
- Log delivery outcome per attempt (`sent`, `retrying`, `failed`).

## 7. Slack Message Template (Minimum)

```text
[ALERT] {message_type} [{flags_csv}]
Plugin: {plugin}
Title: {title}
Body: {body}
Message ID: {message_id}
```

All additional context should be derived from `metadata`.

## 8. Plugin Contract

Any plugin can participate if it emits the canonical event format.

- Plugin-specific fields must go under `metadata`.
- Plugins should not contain channel routing logic.
- Routing decisions are centralized in the routing configuration.

## 9. Reference Flow

1. Plugin emits event.
2. Router validates event.
3. Router matches routes.
4. Router sends alert message to channel(s).
5. Router records delivery and errors.

## 10. Future Extensions (Optional)

- Per-route templates.
- Per-route severity thresholds.
- Time-window suppression (rate limiting).
- User/group mentions per route.
