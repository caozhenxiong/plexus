# plexus

Watch local coding-agent session JSONL files, plus the Codex TUI log for compatibility, then forward:

- `task_complete` events
- `exec_command` approval requests
- `request_user_input` questions from Plan mode
- timeout reminders for unanswered Plan questions and unhandled Plan decisions

to Moshi or Bark on iPhone.

## Files

- `watch_sessions.py`: long-running watcher for `~/.codex/sessions`

## Local run

```bash
python3 /home/linus/workspace/tools/plexus/watch_sessions.py \
  --config /home/linus/.config/plexus/config.toml
```

## Dry run

```bash
python3 /home/linus/workspace/tools/plexus/watch_sessions.py \
  --config /home/linus/.config/plexus/config.toml \
  --dry-run --verbose
```

## Deploy

Use the bundled deploy script to keep macOS and Ubuntu on the same revision:

```bash
./scripts/deploy.sh --commit "Describe the Plexus change"
```

What it does:

- runs a local Python syntax check
- commits and pushes the current branch if `--commit` is provided
- migrates the local macOS launch agent from `codex-notify` to `plexus`
- rewrites local and Ubuntu `notification_icon` to the current Bark asset URL
- migrates local and Ubuntu config/state paths from `codex-notify` to `plexus`
- restarts the local launch agent
- SSHes into Ubuntu, runs `git pull --ff-only`, and restarts `plexus.service`

Useful options:

- `--skip-local`: only update Ubuntu
- `--skip-remote`: only restart the local macOS launch agent
- `--skip-push`: use the already-pushed revision
- `--dry-run`: print the actions without executing them

If your Ubuntu SSH still uses password auth, export `PLEXUS_REMOTE_PASSWORD` before running the script so it can do the remote hop non-interactively.

## Notes

- Existing session files are primed without replaying old `task_complete` events.
- Existing session files are primed without replaying old approval events.
- New or appended `task_complete` events are deduplicated by `turn_id`.
- Plan-mode `task_complete` events whose final answer contains `<proposed_plan>` are sent as `等待决策`, using the plan title as the body.
- Approval requests are detected from session JSONL `function_call` items where `name=exec_command` and `sandbox_permissions=require_escalated`.
- New approval events are deduplicated by `exec_command.call_id`.
- New Plan-mode questions are deduplicated by `request_user_input.call_id`.
- Plan questions are cleared when the session records a matching `function_call_output`.
- Plan decisions are cleared when the same session starts a later turn.
- Timeout knobs:
  - `question_timeout_seconds`: first reminder delay for unanswered Plan questions
  - `decision_timeout_seconds`: first reminder delay for `等待决策`
  - `reminder_interval_seconds`: repeat interval after the first timeout reminder
- The Bark config accepts either:
  - `bark_url` copied from the Bark app test URL
  - `bark_server` + `bark_key`
- `moshi_token` enables Moshi's native webhook notifications.
- `notification_provider` controls which provider is preferred:
  - `auto`: prefer Moshi for ordinary notifications, but switch to Bark when a MuxDeck deeplink is available
  - `bark`: always prefer Bark when it is configured
  - `moshi`: always prefer Moshi when it is configured
- If `moshi_token` is set and `notification_provider = "auto"`, Plexus normally prefers Moshi over Bark.
- If `muxdeck_host_id` is set and Bark is configured, notifications with a known `cwd` switch to Bark automatically so the tap action can open `MuxDeck` directly.
- `notification_icon` can be any image URL supported by Bark.
- The deploy script defaults it to the pinned public asset:

```text
https://raw.githubusercontent.com/caozhenxiong/muxdeck-assets/baf0a535eb67c5f56a764971c49984fd155efda0/assets/muxdeck-bark-icon.png
```
- `notification_url` is optional. Bark will open it when you tap the notification.
- `notification_url` supports two placeholders when `muxdeck_host_id` is configured:
  - `{muxdeck_url}`: raw `muxdeck://...` deeplink
  - `{muxdeck_url_encoded}`: URL-encoded `muxdeck://...` deeplink
- For opening another iPhone app reliably, the stable pattern is:
  - create a Shortcut named `Open Moshi`
  - add the `Open App` action and choose `Moshi`
  - set `notification_url = "shortcuts://run-shortcut?name=Open%20Moshi"`

## Open MuxDeck Directly

To open the matching MuxDeck tmux or Codex session when you tap an iPhone notification, add this to the watcher config:

```toml
notification_provider = "bark"
muxdeck_host_id = "ubuntu"
```

Notes:

- `muxdeck_host_id` can be the MuxDeck host ID itself, the host display name slug, or the host address.
- MuxDeck host IDs default to a slug of the host display name, or the address if the display name is empty.
- Example slugs:
  - `Ubuntu Rig` -> `ubuntu-rig`
  - `MacBook` -> `macbook`
- The watcher will build a deeplink like:

```text
muxdeck://host/ubuntu/connect?cwd=/Users/linus/workspace/tools&transport=codex
```

- On open, MuxDeck resolves the host, finds the newest live session whose `cwd` matches, and opens it directly.
- If the app does not have a fresh snapshot yet, MuxDeck refreshes the host once and retries.
- This direct-open path currently requires Bark, because the Moshi webhook used here does not carry a tap-through deeplink.
- If Bark on your iPhone does not jump out of Bark reliably, use a Shortcut bridge instead:

```toml
notification_provider = "bark"
muxdeck_host_id = "macbook"
notification_url = "shortcuts://run-shortcut?name=Open%20MuxDeck&input=text&text={muxdeck_url_encoded}"
```

- In the `Open MuxDeck` shortcut:
  - accept text input
  - add `Open URLs`
  - use the shortcut input as the URL
