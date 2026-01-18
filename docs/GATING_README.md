# Opinionated Gating (Optional)

This plugin does not enforce `/project` or `@branch` directives. If you want
stricter behavior, add rules to your global Codex instructions and let the
agent enforce them.

## How to enable
1. Copy `docs/AGENTS.example.md` into `~/.codex/AGENTS.md`, or merge the sections you
   want into your existing file.
2. Restart the agent so the new policy is picked up.

## Example messages
- Read-only:
  - `@takopi /zkp2p-clients summarize the retry logic in api client`
- Write:
  - `@takopi /zkp2p-clients @feat/web/monad-usdt0 add a retry to the api call`

## Notes
- The opinionated policy is about instructions, not enforcement in code.
- If you remove the policy, the agent will accept messages without directives.
