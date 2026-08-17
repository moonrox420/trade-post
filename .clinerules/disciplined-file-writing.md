# Disciplined File Writing

> Scope: Kilroy orchestrated specialist agents.

## Objective

Prevent unnecessary disk writes. Only persist code for meaningful, intentional changes.

## When to Write

Use `write_to_file` or `replace_in_file` **only** when at least one applies:
- **Refactor** — restructuring existing code
- **Bug fix** — correcting incorrect behavior
- **Feature implementation** — adding a new capability tied to user goal
- **Configuration change** — updating settings at user direction
- **File creation request** — user explicitly asks to create or scaffold
- **Verified result** — experiment produced a confirmed outcome to retain

If none apply, use in-chat output only.

## When NOT to Write

Avoid writing when content is:
- A throwaway test snippet or scratch script for validation only
- An exploratory variant or alternative approach example
- A one-off command or query result better displayed inline
- A draft or WIP not confirmed or reviewed
- A code sample whose purpose is explanation or illustration

## Mandatory Decision Rule

Before every `write_to_file` or `replace_in_file` call, state:
"Persisting to disk because: [refactor | bug fix | feature | config change | explicit request | verified result]."

If the agent cannot fill in that bracket, the file must not be written.

## Edge Cases

- User says "save this" or "write it": explicit request applies, write it.
- User says "try X": tentative exploration stays in chat. If it succeeds and becomes chosen, write it.
- Multiple iterations: only the final chosen version touches disk.
- Tests or experiments for validation: keep in chat unless user asks to keep them.

## Signaling

When a file is written, include the qualifying reason in chat.
When content is inline, make clear it is for review only and not saved to disk.
