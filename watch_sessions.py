#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - macOS system Python < 3.11
    import tomli as tomllib


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "plexus" / "config.toml"
DEFAULT_WATCH_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_LOG_FILE = Path.home() / ".codex" / "log" / "codex-tui.log"
DEFAULT_STATE_FILE = Path.home() / ".local" / "state" / "plexus" / "state.json"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_BODY_MAX_LEN = 160
DEFAULT_NOTIFICATION_GROUP = "plexus"
DEFAULT_QUESTION_TIMEOUT_SECONDS = 120.0
DEFAULT_DECISION_TIMEOUT_SECONDS = 300.0
DEFAULT_REMINDER_INTERVAL_SECONDS = 600.0
STATE_LIMIT = 5000
PENDING_KIND_PLAN_QUESTION = "plan_question"
PENDING_KIND_PLAN_DECISION = "plan_decision"


@dataclass
class CompletionEvent:
    turn_id: str
    project_name: str
    notification_title: str
    completed_at: str
    last_agent_message: str
    cwd: str
    source_file: Path


@dataclass
class ApprovalEvent:
    approval_id: str
    project_name: str
    justification: str
    command: str
    cwd: str
    source_file: Path


@dataclass
class QuestionEvent:
    question_id: str
    project_name: str
    prompt: str
    cwd: str
    source_file: Path


@dataclass
class PendingState:
    pending_id: str
    kind: str
    project_name: str
    summary: str
    cwd: str
    source_file: str
    turn_id: str
    created_at: float
    next_remind_at: float
    reminder_count: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "PendingState | None":
        if not isinstance(raw, dict):
            return None

        pending_id = raw.get("pending_id")
        kind = raw.get("kind")
        if not isinstance(pending_id, str) or not pending_id:
            return None
        if not isinstance(kind, str) or not kind:
            return None

        try:
            created_at = float(raw.get("created_at", 0) or 0)
            next_remind_at = float(raw.get("next_remind_at", created_at) or created_at)
            reminder_count = int(raw.get("reminder_count", 0) or 0)
        except (TypeError, ValueError):
            return None

        return cls(
            pending_id=pending_id,
            kind=kind,
            project_name=str(raw.get("project_name", "") or ""),
            summary=str(raw.get("summary", "") or ""),
            cwd=str(raw.get("cwd", "") or ""),
            source_file=str(raw.get("source_file", "") or ""),
            turn_id=str(raw.get("turn_id", "") or ""),
            created_at=created_at,
            next_remind_at=next_remind_at,
            reminder_count=reminder_count,
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            "pending_id": self.pending_id,
            "kind": self.kind,
            "project_name": self.project_name,
            "summary": self.summary,
            "cwd": self.cwd,
            "source_file": self.source_file,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "next_remind_at": self.next_remind_at,
            "reminder_count": self.reminder_count,
        }


@dataclass
class Config:
    moshi_token: str = ""
    bark_url: str = ""
    bark_server: str = ""
    bark_key: str = ""
    notification_provider: str = "auto"
    watch_root: Path = DEFAULT_WATCH_ROOT
    log_file: Path = DEFAULT_LOG_FILE
    state_file: Path = DEFAULT_STATE_FILE
    poll_interval: float = DEFAULT_POLL_INTERVAL
    body_max_len: int = DEFAULT_BODY_MAX_LEN
    notification_group: str = DEFAULT_NOTIFICATION_GROUP
    question_timeout_seconds: float = DEFAULT_QUESTION_TIMEOUT_SECONDS
    decision_timeout_seconds: float = DEFAULT_DECISION_TIMEOUT_SECONDS
    reminder_interval_seconds: float = DEFAULT_REMINDER_INTERVAL_SECONDS
    notification_icon: str = ""
    notification_url: str = ""
    muxdeck_host_id: str = ""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.completion_turn_ids: OrderedDict[str, float] = OrderedDict()
        self.approval_ids: OrderedDict[str, float] = OrderedDict()
        self.question_ids: OrderedDict[str, float] = OrderedDict()
        self.pending_events: OrderedDict[str, PendingState] = OrderedDict()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("failed to load state file %s: %s", self.path, exc)
            return

        completion_entries = raw.get("seen_completion_turn_ids", raw.get("seen_turn_ids", []))
        if isinstance(completion_entries, list):
            for entry in completion_entries:
                if not isinstance(entry, dict):
                    continue
                turn_id = entry.get("turn_id")
                seen_at = entry.get("seen_at", 0)
                if isinstance(turn_id, str):
                    self.completion_turn_ids[turn_id] = float(seen_at)
        else:
            logging.warning("ignoring malformed completion state in %s", self.path)

        approval_entries = raw.get("seen_approval_ids", [])
        if isinstance(approval_entries, list):
            for entry in approval_entries:
                if not isinstance(entry, dict):
                    continue
                approval_id = entry.get("approval_id")
                seen_at = entry.get("seen_at", 0)
                if isinstance(approval_id, str):
                    self.approval_ids[approval_id] = float(seen_at)
        else:
            logging.warning("ignoring malformed approval state in %s", self.path)

        question_entries = raw.get("seen_question_ids", [])
        if isinstance(question_entries, list):
            for entry in question_entries:
                if not isinstance(entry, dict):
                    continue
                question_id = entry.get("question_id")
                seen_at = entry.get("seen_at", 0)
                if isinstance(question_id, str):
                    self.question_ids[question_id] = float(seen_at)
        else:
            logging.warning("ignoring malformed question state in %s", self.path)

        pending_entries = raw.get("pending_events", [])
        if isinstance(pending_entries, list):
            for entry in pending_entries:
                pending = PendingState.from_raw(entry)
                if pending is None:
                    continue
                self.pending_events[pending.pending_id] = pending
        else:
            logging.warning("ignoring malformed pending state in %s", self.path)
        self._prune()

    def contains_completion(self, turn_id: str) -> bool:
        return turn_id in self.completion_turn_ids

    def add_completion(self, turn_id: str) -> None:
        self.completion_turn_ids[turn_id] = time.time()
        self.completion_turn_ids.move_to_end(turn_id)
        self._prune()
        self.save()

    def contains_approval(self, approval_id: str) -> bool:
        return approval_id in self.approval_ids

    def add_approval(self, approval_id: str) -> None:
        self.approval_ids[approval_id] = time.time()
        self.approval_ids.move_to_end(approval_id)
        self._prune()
        self.save()

    def contains_question(self, question_id: str) -> bool:
        return question_id in self.question_ids

    def add_question(self, question_id: str) -> None:
        self.question_ids[question_id] = time.time()
        self.question_ids.move_to_end(question_id)
        self._prune()
        self.save()

    def upsert_pending(self, pending: PendingState) -> None:
        existing = self.pending_events.get(pending.pending_id)
        if existing is not None:
            pending.created_at = existing.created_at or pending.created_at
            pending.next_remind_at = existing.next_remind_at or pending.next_remind_at
            pending.reminder_count = existing.reminder_count
        self.pending_events[pending.pending_id] = pending
        self.pending_events.move_to_end(pending.pending_id)
        self._prune()
        self.save()

    def resolve_pending(self, pending_id: str) -> bool:
        if pending_id not in self.pending_events:
            return False
        self.pending_events.pop(pending_id, None)
        self.save()
        return True

    def resolve_plan_decisions(self, source_file: Path, turn_id: str, resolved_at: float) -> int:
        source = str(source_file)
        resolved_ids = [
            pending_id
            for pending_id, pending in self.pending_events.items()
            if pending.kind == PENDING_KIND_PLAN_DECISION
            and pending.source_file == source
            and pending.turn_id != turn_id
            and pending.created_at <= resolved_at
        ]
        if not resolved_ids:
            return 0
        for pending_id in resolved_ids:
            self.pending_events.pop(pending_id, None)
        self.save()
        return len(resolved_ids)

    def due_pending_events(self, now: float) -> list[PendingState]:
        return [
            pending
            for pending in self.pending_events.values()
            if pending.next_remind_at <= now
        ]

    def schedule_next_reminder(self, pending_id: str, now: float, interval_seconds: float) -> None:
        pending = self.pending_events.get(pending_id)
        if pending is None:
            return
        pending.reminder_count += 1
        pending.next_remind_at = now + max(interval_seconds, 1.0)
        self.pending_events[pending_id] = pending
        self.pending_events.move_to_end(pending_id)
        self.save()

    def _prune(self) -> None:
        while len(self.completion_turn_ids) > STATE_LIMIT:
            self.completion_turn_ids.popitem(last=False)
        while len(self.approval_ids) > STATE_LIMIT:
            self.approval_ids.popitem(last=False)
        while len(self.question_ids) > STATE_LIMIT:
            self.question_ids.popitem(last=False)
        while len(self.pending_events) > STATE_LIMIT:
            self.pending_events.popitem(last=False)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seen_completion_turn_ids": [
                {"turn_id": turn_id, "seen_at": seen_at}
                for turn_id, seen_at in self.completion_turn_ids.items()
            ],
            "seen_approval_ids": [
                {"approval_id": approval_id, "seen_at": seen_at}
                for approval_id, seen_at in self.approval_ids.items()
            ],
            "seen_question_ids": [
                {"question_id": question_id, "seen_at": seen_at}
                for question_id, seen_at in self.question_ids.items()
            ],
            "pending_events": [
                pending.to_raw()
                for pending in self.pending_events.values()
            ],
        }
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)


def _parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class PushNotifier:
    def __init__(
        self,
        moshi_token: str,
        bark_url: str,
        bark_server: str,
        bark_key: str,
        notification_provider: str,
        notification_group: str,
        body_max_len: int,
        notification_icon: str,
        notification_url: str,
        muxdeck_host_id: str,
        dry_run: bool,
    ) -> None:
        self.moshi_token = moshi_token.strip()
        self.notification_provider = notification_provider.strip().lower() or "auto"
        self.notification_group = notification_group
        self.body_max_len = body_max_len
        self.notification_icon = notification_icon.strip()
        self.notification_url = notification_url.strip()
        self.muxdeck_host_id = muxdeck_host_id.strip()
        self.dry_run = dry_run
        self.base_url, self.key = self._resolve_endpoint(
            bark_url=bark_url,
            bark_server=bark_server,
            bark_key=bark_key,
        )
        self._warned_unconfigured = False
        self._warned_moshi_deeplink = False

    @staticmethod
    def _resolve_endpoint(
        bark_url: str,
        bark_server: str,
        bark_key: str,
    ) -> tuple[str, str]:
        if bark_server and bark_key:
            return bark_server.rstrip("/"), bark_key.strip("/")

        if not bark_url:
            return "", ""

        parsed = urllib.parse.urlparse(bark_url)
        if not parsed.scheme or not parsed.netloc:
            logging.warning("invalid bark_url, expected a full URL: %s", bark_url)
            return "", ""

        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            logging.warning(
                "invalid bark_url, expected the Bark key in the first path segment: %s",
                bark_url,
            )
            return "", ""

        return f"{parsed.scheme}://{parsed.netloc}", segments[0]

    def configured(self) -> bool:
        return bool(self.moshi_token or (self.base_url and self.key))

    def send_completion(self, event: CompletionEvent) -> None:
        body = self._format_completion_body(event)
        self._send(
            title=event.notification_title,
            subtitle=event.project_name,
            body=body,
            identifier=event.turn_id,
            event_type="completion",
            cwd=event.cwd,
        )

    def send_approval(self, event: ApprovalEvent) -> None:
        body = event.justification.strip()
        if not body:
            body = f"等待确认: {event.command.strip()}" if event.command.strip() else "有命令正在等待确认"
        self._send(
            title="Codex 等待确认",
            subtitle=event.project_name,
            body=self._compact_message(body, fallback="有命令正在等待确认"),
            identifier=event.approval_id,
            event_type="approval",
            cwd=event.cwd,
        )

    def send_question(self, event: QuestionEvent) -> None:
        self._send(
            title="Codex 等待回答",
            subtitle=event.project_name,
            body=self._compact_message(event.prompt, fallback="Plan 模式有问题等待回答"),
            identifier=event.question_id,
            event_type="question",
            cwd=event.cwd,
        )

    def send_timeout(self, pending: PendingState) -> None:
        title, fallback = self._timeout_metadata(pending.kind)
        summary = pending.summary.strip()
        if summary:
            body = f"{self._format_elapsed(time.time() - pending.created_at)}未处理: {summary}"
        else:
            body = fallback
        self._send(
            title=title,
            subtitle=pending.project_name,
            body=self._compact_message(body, fallback=fallback),
            identifier=pending.pending_id,
            event_type="timeout",
            cwd=pending.cwd,
        )

    def _send(
        self,
        title: str,
        subtitle: str,
        body: str,
        identifier: str,
        event_type: str,
        cwd: str,
    ) -> None:
        if not self.configured():
            if not self._warned_unconfigured:
                logging.warning("no notification provider is configured; Codex notifications are disabled")
                self._warned_unconfigured = True
            return

        if self.notification_provider == "bark" and self.base_url and self.key:
            if self._send_bark(
                title=title,
                subtitle=subtitle,
                body=body,
                identifier=identifier,
                event_type=event_type,
                cwd=cwd,
            ):
                return
            if self.moshi_token:
                self._send_moshi(
                    title=title,
                    subtitle=subtitle,
                    body=body,
                    identifier=identifier,
                    event_type=event_type,
                )
            return

        if self.notification_provider == "moshi" and self.moshi_token:
            self._send_moshi(
                title=title,
                subtitle=subtitle,
                body=body,
                identifier=identifier,
                event_type=event_type,
            )
            return

        if self._should_use_bark_for_muxdeck_deeplink(cwd):
            if self._send_bark(
                title=title,
                subtitle=subtitle,
                body=body,
                identifier=identifier,
                event_type=event_type,
                cwd=cwd,
            ):
                return
            if self.moshi_token:
                self._send_moshi(
                    title=title,
                    subtitle=subtitle,
                    body=body,
                    identifier=identifier,
                    event_type=event_type,
                )
                return

        if self.moshi_token:
            self._send_moshi(
                title=title,
                subtitle=subtitle,
                body=body,
                identifier=identifier,
                event_type=event_type,
            )
            return

        self._send_bark(
            title=title,
            subtitle=subtitle,
            body=body,
            identifier=identifier,
            event_type=event_type,
            cwd=cwd,
        )

    def _should_use_bark_for_muxdeck_deeplink(self, cwd: str) -> bool:
        return bool(self.base_url and self.key and self._muxdeck_notification_url(cwd))

    def _send_bark(
        self,
        title: str,
        subtitle: str,
        body: str,
        identifier: str,
        event_type: str,
        cwd: str,
    ) -> bool:
        if not (self.base_url and self.key):
            logging.error("Bark provider selected but bark_url/bark_key is incomplete")
            return False

        path = "/".join(
            urllib.parse.quote(part, safe="")
            for part in (self.key, title, subtitle, body)
        )
        query_params = {"group": self.notification_group}
        if self.notification_icon:
            query_params["icon"] = self.notification_icon
        notification_url = self._resolved_notification_url(cwd)
        if notification_url:
            query_params["url"] = notification_url
        query = urllib.parse.urlencode(query_params)
        url = f"{self.base_url}/{path}?{query}"

        if self.dry_run:
            logging.info("dry-run Bark notification: %s", url)
            return True

        request = urllib.request.Request(
            url=url,
            headers={"User-Agent": "plexus/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except urllib.error.URLError as exc:
            logging.error("failed to send Bark notification: %s", exc)
            return False

        logging.info(
            "sent Bark %s notification for %s (%s)",
            event_type,
            identifier,
            subtitle,
        )
        return True

    def _resolved_notification_url(self, cwd: str) -> str:
        muxdeck_url = self._muxdeck_notification_url(cwd)
        template = self.notification_url.strip()
        if not template:
            return muxdeck_url

        if "{muxdeck_url}" not in template and "{muxdeck_url_encoded}" not in template:
            return template

        if not muxdeck_url:
            return ""

        return (
            template
            .replace("{muxdeck_url_encoded}", urllib.parse.quote(muxdeck_url, safe=""))
            .replace("{muxdeck_url}", muxdeck_url)
        )

    def _muxdeck_notification_url(self, cwd: str) -> str:
        if not self.muxdeck_host_id:
            return ""

        encoded_host = urllib.parse.quote(self.muxdeck_host_id, safe="")
        normalized_cwd = cwd.strip()
        if not normalized_cwd:
            return f"muxdeck://host/{encoded_host}"

        query = urllib.parse.urlencode(
            {
                "cwd": normalized_cwd,
                "transport": "codex",
            }
        )
        return f"muxdeck://host/{encoded_host}/connect?{query}"

    def _send_moshi(
        self,
        title: str,
        subtitle: str,
        body: str,
        identifier: str,
        event_type: str,
    ) -> None:
        if not self.moshi_token:
            logging.error("Moshi provider selected but moshi_token is empty")
            return

        if self.muxdeck_host_id and not self._warned_moshi_deeplink:
            logging.warning(
                "muxdeck_host_id is configured, but Moshi notifications do not carry tap-through deeplinks in this notifier; Bark is required for direct-open"
            )
            self._warned_moshi_deeplink = True

        message_title = title if not subtitle else f"{title} · {subtitle}"
        payload = {
            "token": self.moshi_token,
            "title": message_title,
            "message": body,
        }

        if self.dry_run:
            logging.info(
                "dry-run Moshi notification: title=%r message=%r",
                message_title,
                body,
            )
            return

        request = urllib.request.Request(
            url="https://api.getmoshi.app/api/webhook",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "plexus/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except urllib.error.URLError as exc:
            logging.error("failed to send Moshi notification: %s", exc)
            return

        logging.info(
            "sent Moshi %s notification for %s (%s)",
            event_type,
            identifier,
            subtitle,
        )

    def _compact_message(self, message: str, fallback: str) -> str:
        first_line = ""
        for line in message.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break
        if not first_line:
            return fallback
        if len(first_line) <= self.body_max_len:
            return first_line
        return first_line[: self.body_max_len - 1].rstrip() + "…"

    def _format_completion_body(self, event: CompletionEvent) -> str:
        result_summary = self._single_line(event.last_agent_message)
        if not result_summary:
            return "任务已完成"
        return self._truncate(result_summary)

    @staticmethod
    def _timeout_metadata(kind: str) -> tuple[str, str]:
        if kind == PENDING_KIND_PLAN_QUESTION:
            return "Codex 等待回答超时", "Plan 模式有问题长时间未回答"
        if kind == PENDING_KIND_PLAN_DECISION:
            return "Codex 等待决策超时", "Plan 模式方案长时间未决策"
        return "Codex 超时未处理", "有待处理事项长时间未处理"

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        seconds = max(int(seconds), 0)
        if seconds < 90:
            return "约 1 分钟"
        if seconds < 3600:
            return f"约 {round(seconds / 60)} 分钟"
        hours = round(seconds / 3600)
        return f"约 {hours} 小时"

    @staticmethod
    def _single_line(message: str) -> str:
        for line in message.splitlines():
            stripped = line.strip()
            if stripped:
                if stripped in {"<proposed_plan>", "</proposed_plan>"}:
                    continue
                if stripped.startswith("```"):
                    continue
                if stripped.lower() in {"none", "null"}:
                    continue
                if stripped.startswith("#"):
                    stripped = stripped.lstrip("#").strip()
                if stripped:
                    return stripped
        return ""

    def _truncate(self, text: str) -> str:
        if len(text) <= self.body_max_len:
            return text
        return text[: self.body_max_len - 1].rstrip() + "…"


class SessionFileTracker:
    def __init__(
        self,
        path: Path,
        state_store: StateStore,
        notifier: PushNotifier,
        process_existing_events: bool,
        question_timeout_seconds: float,
        decision_timeout_seconds: float,
    ) -> None:
        self.path = path
        self.state_store = state_store
        self.notifier = notifier
        self.process_existing_events = process_existing_events
        self.question_timeout_seconds = question_timeout_seconds
        self.decision_timeout_seconds = decision_timeout_seconds
        self.offset = 0
        self.project_name = ""
        self.cwd = ""
        self.current_turn_id = ""
        self.current_turn_mode = ""
        self.latest_agent_message_by_turn: dict[str, str] = {}
        self.latest_final_answer_by_turn: dict[str, str] = {}
        self.turn_mode_by_turn: dict[str, str] = {}
        self._prime()

    def _prime(self) -> None:
        if not self.path.exists():
            return

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    self._handle_line(raw_line, emit=self.process_existing_events)
                self.offset = handle.tell()
        except OSError as exc:
            logging.error("failed to prime session file %s: %s", self.path, exc)

    def poll(self) -> None:
        if not self.path.exists():
            return

        try:
            size = self.path.stat().st_size
        except OSError as exc:
            logging.error("failed to stat session file %s: %s", self.path, exc)
            return

        if size < self.offset:
            logging.info("session file truncated, restarting tail: %s", self.path)
            self.offset = 0
            self.project_name = ""
            self.cwd = ""
            self.current_turn_id = ""
            self.current_turn_mode = ""
            self.latest_agent_message_by_turn.clear()
            self.latest_final_answer_by_turn.clear()
            self.turn_mode_by_turn.clear()

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                handle.seek(self.offset)
                while True:
                    line_start = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith("\n"):
                        handle.seek(line_start)
                        break
                    self._handle_line(raw_line, emit=True)
                self.offset = handle.tell()
        except OSError as exc:
            logging.error("failed to read session file %s: %s", self.path, exc)

    def _handle_line(self, raw_line: str, emit: bool) -> None:
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            logging.debug("skipping malformed jsonl line in %s", self.path)
            return

        item_type = item.get("type")
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        event_time = self._extract_event_time(item, payload)

        if item_type == "session_meta":
            self._capture_project_name(payload.get("cwd"))
            return

        if item_type == "turn_context":
            turn_id = self._coerce_text(payload.get("turn_id"))
            if turn_id:
                self._resolve_pending_decisions(turn_id, event_time)
            self.current_turn_id = turn_id
            self.current_turn_mode = self._extract_collaboration_mode(payload)
            if self.current_turn_id and self.current_turn_mode:
                self.turn_mode_by_turn[self.current_turn_id] = self.current_turn_mode
            self._capture_project_name(payload.get("cwd"))
            return

        if item_type == "response_item":
            self._capture_response_item(payload, emit=emit, event_time=event_time)
            return

        if item_type != "event_msg":
            return

        event_type = payload.get("type")
        if event_type == "task_started":
            turn_id = self._coerce_text(payload.get("turn_id"))
            if turn_id:
                self._resolve_pending_decisions(turn_id, event_time)
                self.current_turn_id = turn_id
                self.current_turn_mode = self._coerce_text(payload.get("collaboration_mode_kind"))
                if self.current_turn_mode:
                    self.turn_mode_by_turn[turn_id] = self.current_turn_mode
            return

        if event_type == "agent_message":
            self._capture_agent_message(payload)
            return

        if event_type != "task_complete":
            return

        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str):
            return

        line_completed_at = self._extract_event_time(item, payload)
        if not emit:
            self._register_pending_decision(turn_id, payload, line_completed_at)
            self._clear_turn_state(turn_id)
            return

        if self.state_store.contains_completion(turn_id):
            self._register_pending_decision(turn_id, payload, line_completed_at)
            self._clear_turn_state(turn_id)
            return

        project_name = self.project_name or "unknown-project"
        completed_at = payload.get("completed_at", "")
        last_agent_message = self._coerce_text(payload.get("last_agent_message"))
        if not last_agent_message:
            last_agent_message = (
                self.latest_final_answer_by_turn.get(turn_id, "")
                or self.latest_agent_message_by_turn.get(turn_id, "")
            )
        notification_title = self._completion_notification_title(turn_id, last_agent_message)
        event = CompletionEvent(
            turn_id=turn_id,
            project_name=project_name,
            notification_title=notification_title,
            completed_at=str(completed_at),
            last_agent_message=last_agent_message,
            cwd=self.cwd,
            source_file=self.path,
        )
        self.notifier.send_completion(event)
        self.state_store.add_completion(turn_id)
        self._register_pending_decision(turn_id, payload, line_completed_at, message=last_agent_message)
        self._clear_turn_state(turn_id)

    def _capture_project_name(self, cwd: Any) -> None:
        if not isinstance(cwd, str) or not cwd:
            return
        self.cwd = cwd
        name = Path(cwd).name.strip()
        if name:
            self.project_name = name

    def _capture_response_item(self, payload: dict[str, Any], emit: bool, event_time: float) -> None:
        payload_type = payload.get("type")
        if payload_type == "message" and payload.get("role") == "assistant":
            turn_id = self.current_turn_id
            if not turn_id:
                return

            message = self._extract_message_from_content(payload.get("content"))
            if not message:
                return

            self.latest_agent_message_by_turn[turn_id] = message
            if payload.get("phase") == "final_answer":
                self.latest_final_answer_by_turn[turn_id] = message
            return

        if payload_type == "function_call_output":
            question_id = self._coerce_text(payload.get("call_id"))
            if question_id:
                self.state_store.resolve_pending(self._question_pending_id(question_id))
            return

        if payload_type == "function_call" and payload.get("name") == "exec_command":
            self._capture_exec_command_approval(payload, emit=emit)
            return

        if payload_type != "function_call" or payload.get("name") != "request_user_input":
            return

        question_id = self._coerce_text(payload.get("call_id"))
        if not question_id:
            turn_id = self.current_turn_id or "unknown-turn"
            question_id = f"{turn_id}:request_user_input"
        project_name = self.project_name or "unknown-project"
        prompt = self._extract_request_user_input_prompt(payload.get("arguments"))
        self.state_store.upsert_pending(
            PendingState(
                pending_id=self._question_pending_id(question_id),
                kind=PENDING_KIND_PLAN_QUESTION,
                project_name=project_name,
                summary=prompt,
                cwd=self.cwd,
                source_file=str(self.path),
                turn_id=self.current_turn_id,
                created_at=event_time,
                next_remind_at=event_time + self.question_timeout_seconds,
            )
        )
        if not emit or self.state_store.contains_question(question_id):
            return
        event = QuestionEvent(
            question_id=question_id,
            project_name=project_name,
            prompt=prompt,
            cwd=self.cwd,
            source_file=self.path,
        )
        self.notifier.send_question(event)
        self.state_store.add_question(question_id)

    def _capture_exec_command_approval(self, payload: dict[str, Any], emit: bool) -> None:
        raw_arguments = payload.get("arguments")
        arguments = raw_arguments
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                logging.debug("skipping malformed exec_command arguments in %s", self.path)
                return

        if not isinstance(arguments, dict):
            return

        if self._coerce_text(arguments.get("sandbox_permissions")).strip() != "require_escalated":
            return

        approval_id = self._coerce_text(payload.get("call_id")).strip()
        if not approval_id:
            turn_id = self.current_turn_id or "unknown-turn"
            approval_id = f"{turn_id}:exec_command"

        if not emit or self.state_store.contains_approval(approval_id):
            return

        project_name = self.project_name or "unknown-project"
        justification = self._coerce_text(arguments.get("justification")).strip()
        command = self._coerce_text(arguments.get("cmd")).strip()
        event = ApprovalEvent(
            approval_id=approval_id,
            project_name=project_name,
            justification=justification,
            command=command,
            cwd=self.cwd,
            source_file=self.path,
        )
        self.notifier.send_approval(event)
        self.state_store.add_approval(approval_id)

    def _capture_agent_message(self, payload: dict[str, Any]) -> None:
        turn_id = self.current_turn_id
        if not turn_id:
            return

        message = self._coerce_text(payload.get("message"))
        if not message:
            return

        self.latest_agent_message_by_turn[turn_id] = message
        if payload.get("phase") == "final_answer":
            self.latest_final_answer_by_turn[turn_id] = message

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _extract_message_from_content(self, content: Any) -> str:
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _extract_collaboration_mode(self, payload: dict[str, Any]) -> str:
        collaboration_mode = payload.get("collaboration_mode")
        if not isinstance(collaboration_mode, dict):
            return ""
        return self._coerce_text(collaboration_mode.get("mode")).strip().lower()

    def _extract_request_user_input_prompt(self, arguments: Any) -> str:
        raw = arguments
        if isinstance(arguments, str):
            try:
                raw = json.loads(arguments)
            except json.JSONDecodeError:
                raw = None

        if not isinstance(raw, dict):
            return "Plan 模式有问题等待回答"

        questions = raw.get("questions")
        if not isinstance(questions, list) or not questions:
            return "Plan 模式有问题等待回答"

        first = questions[0] if isinstance(questions[0], dict) else {}
        header = self._coerce_text(first.get("header")).strip()
        prompt = self._coerce_text(first.get("question")).strip()
        if header and prompt:
            prompt = f"{header}: {prompt}"
        elif not prompt:
            prompt = header

        if len(questions) > 1:
            suffix = f" 等 {len(questions)} 题"
            prompt = f"{prompt}{suffix}" if prompt else f"{len(questions)} 个问题等待回答"

        return prompt or "Plan 模式有问题等待回答"

    def _completion_notification_title(self, turn_id: str, message: str) -> str:
        mode = self.turn_mode_by_turn.get(turn_id, "").strip().lower()
        if mode == "plan" and "<proposed_plan>" in message:
            return "Codex 等待决策"
        return "Codex 完成"

    def _register_pending_decision(
        self,
        turn_id: str,
        payload: dict[str, Any],
        event_time: float,
        message: str = "",
    ) -> None:
        resolved_message = message
        if not resolved_message:
            resolved_message = self._coerce_text(payload.get("last_agent_message"))
        if not resolved_message:
            resolved_message = (
                self.latest_final_answer_by_turn.get(turn_id, "")
                or self.latest_agent_message_by_turn.get(turn_id, "")
            )

        if self._completion_notification_title(turn_id, resolved_message) != "Codex 等待决策":
            return

        project_name = self.project_name or "unknown-project"
        summary = self.notifier._format_completion_body(
            CompletionEvent(
                turn_id=turn_id,
                project_name=project_name,
                notification_title="Codex 等待决策",
                completed_at="",
                last_agent_message=resolved_message,
                cwd=self.cwd,
                source_file=self.path,
            )
        )
        self.state_store.upsert_pending(
            PendingState(
                pending_id=self._decision_pending_id(turn_id),
                kind=PENDING_KIND_PLAN_DECISION,
                project_name=project_name,
                summary=summary,
                cwd=self.cwd,
                source_file=str(self.path),
                turn_id=turn_id,
                created_at=event_time,
                next_remind_at=event_time + self.decision_timeout_seconds,
            )
        )

    def _resolve_pending_decisions(self, turn_id: str, event_time: float) -> None:
        if not turn_id:
            return
        self.state_store.resolve_plan_decisions(self.path, turn_id=turn_id, resolved_at=event_time)

    def _extract_event_time(self, item: dict[str, Any], payload: dict[str, Any]) -> float:
        line_timestamp = _parse_timestamp(item.get("timestamp"))
        if line_timestamp is not None:
            return line_timestamp
        payload_timestamp = _parse_timestamp(payload.get("completed_at"))
        if payload_timestamp is not None:
            return payload_timestamp
        return time.time()

    @staticmethod
    def _question_pending_id(question_id: str) -> str:
        return f"question:{question_id}"

    @staticmethod
    def _decision_pending_id(turn_id: str) -> str:
        return f"decision:{turn_id}"

    def _clear_turn_state(self, turn_id: str) -> None:
        self.latest_agent_message_by_turn.pop(turn_id, None)
        self.latest_final_answer_by_turn.pop(turn_id, None)
        self.turn_mode_by_turn.pop(turn_id, None)
        if self.current_turn_id == turn_id:
            self.current_turn_id = ""
            self.current_turn_mode = ""


class LogFileTracker:
    def __init__(
        self,
        path: Path,
        process_existing_events: bool,
    ) -> None:
        self.path = path
        self.process_existing_events = process_existing_events
        self.offset = 0
        self._prime()

    def _prime(self) -> None:
        if not self.path.exists():
            return

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    self._handle_line(raw_line, emit=self.process_existing_events)
                self.offset = handle.tell()
        except OSError as exc:
            logging.error("failed to prime log file %s: %s", self.path, exc)

    def poll(self) -> None:
        if not self.path.exists():
            return

        try:
            size = self.path.stat().st_size
        except OSError as exc:
            logging.error("failed to stat log file %s: %s", self.path, exc)
            return

        if size < self.offset:
            logging.info("log file truncated, restarting tail: %s", self.path)
            self.offset = 0

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                handle.seek(self.offset)
                while True:
                    line_start = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith("\n"):
                        handle.seek(line_start)
                        break
                    self._handle_line(raw_line, emit=True)
                self.offset = handle.tell()
        except OSError as exc:
            logging.error("failed to read log file %s: %s", self.path, exc)

    def _handle_line(self, raw_line: str, emit: bool) -> None:
        # Approval requests are emitted from session JSONL `function_call` items.
        # The TUI log's `op.dispatch.exec_approval` lines only appear once the user
        # has already approved, so using them would notify too late.
        return


class CodexNotifyWatcher:
    def __init__(
        self,
        config_path: Path,
        config: Config,
        dry_run: bool,
    ) -> None:
        self.config_path = config_path
        self.config_mtime_ns: int | None = None
        self.dry_run = dry_run
        self.config = config
        self.state_store = StateStore(config.state_file)
        self.notifier = PushNotifier(
            moshi_token=config.moshi_token,
            bark_url=config.bark_url,
            bark_server=config.bark_server,
            bark_key=config.bark_key,
            notification_provider=config.notification_provider,
            notification_group=config.notification_group,
            body_max_len=config.body_max_len,
            notification_icon=config.notification_icon,
            notification_url=config.notification_url,
            muxdeck_host_id=config.muxdeck_host_id,
            dry_run=dry_run,
        )
        self.trackers: dict[Path, SessionFileTracker] = {}
        self.log_tracker = LogFileTracker(
            path=config.log_file,
            process_existing_events=False,
        )
        self.running = True
        self._update_config_mtime()
        self._bootstrap_existing_files()

    def _update_config_mtime(self) -> None:
        try:
            self.config_mtime_ns = self.config_path.stat().st_mtime_ns
        except FileNotFoundError:
            self.config_mtime_ns = None

    def _bootstrap_existing_files(self) -> None:
        for path in self._list_session_files():
            self.trackers[path] = SessionFileTracker(
                path=path,
                state_store=self.state_store,
                notifier=self.notifier,
                process_existing_events=False,
                question_timeout_seconds=self.config.question_timeout_seconds,
                decision_timeout_seconds=self.config.decision_timeout_seconds,
            )

    def _list_session_files(self) -> list[Path]:
        if not self.config.watch_root.exists():
            return []
        return sorted(self.config.watch_root.rglob("*.jsonl"))

    def reload_config_if_needed(self) -> None:
        try:
            current_mtime = self.config_path.stat().st_mtime_ns
        except FileNotFoundError:
            current_mtime = None

        if current_mtime == self.config_mtime_ns:
            return

        self.config = load_config(self.config_path)
        self.state_store = StateStore(self.config.state_file)
        self.notifier = PushNotifier(
            moshi_token=self.config.moshi_token,
            bark_url=self.config.bark_url,
            bark_server=self.config.bark_server,
            bark_key=self.config.bark_key,
            notification_provider=self.config.notification_provider,
            notification_group=self.config.notification_group,
            body_max_len=self.config.body_max_len,
            notification_icon=self.config.notification_icon,
            notification_url=self.config.notification_url,
            muxdeck_host_id=self.config.muxdeck_host_id,
            dry_run=self.dry_run,
        )

        for path in list(self.trackers):
            self.trackers[path].state_store = self.state_store
            self.trackers[path].notifier = self.notifier
            self.trackers[path].question_timeout_seconds = self.config.question_timeout_seconds
            self.trackers[path].decision_timeout_seconds = self.config.decision_timeout_seconds
        if self.log_tracker.path != self.config.log_file:
            self.log_tracker = LogFileTracker(
                path=self.config.log_file,
                process_existing_events=False,
            )

        self.config_mtime_ns = current_mtime
        logging.info("reloaded config from %s", self.config_path)

    def poll(self) -> None:
        self.reload_config_if_needed()

        current_files = set(self._list_session_files())
        for path in sorted(current_files - self.trackers.keys()):
            logging.info("tracking new session file %s", path)
            self.trackers[path] = SessionFileTracker(
                path=path,
                state_store=self.state_store,
                notifier=self.notifier,
                process_existing_events=True,
                question_timeout_seconds=self.config.question_timeout_seconds,
                decision_timeout_seconds=self.config.decision_timeout_seconds,
            )

        for path in list(self.trackers):
            if path not in current_files:
                logging.info("session file disappeared, removing tracker: %s", path)
                self.trackers.pop(path, None)
                continue
            self.trackers[path].poll()
        self.log_tracker.poll()
        self._poll_pending_timeouts()

    def _poll_pending_timeouts(self) -> None:
        now = time.time()
        due_events = self.state_store.due_pending_events(now)
        for pending in due_events:
            self.notifier.send_timeout(pending)
            self.state_store.schedule_next_reminder(
                pending.pending_id,
                now=now,
                interval_seconds=self.config.reminder_interval_seconds,
            )

    def run(self, run_for: float | None) -> None:
        deadline = time.monotonic() + run_for if run_for is not None else None
        logging.info(
            "watching Codex sessions under %s and log file %s",
            self.config.watch_root,
            self.config.log_file,
        )
        while self.running:
            self.poll()
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(self.config.poll_interval)


def load_config(path: Path) -> Config:
    config = Config()
    if not path.exists():
        return config

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logging.error("failed to load config %s: %s", path, exc)
        return config

    if bark_url := raw.get("bark_url"):
        config.bark_url = str(bark_url).strip()
    if moshi_token := raw.get("moshi_token"):
        config.moshi_token = str(moshi_token).strip()
    if bark_server := raw.get("bark_server"):
        config.bark_server = str(bark_server).strip()
    if bark_key := raw.get("bark_key"):
        config.bark_key = str(bark_key).strip()
    if notification_provider := raw.get("notification_provider"):
        provider = str(notification_provider).strip().lower()
        if provider in {"auto", "bark", "moshi"}:
            config.notification_provider = provider
    if watch_root := raw.get("watch_root"):
        config.watch_root = Path(os.path.expanduser(str(watch_root)))
    if log_file := raw.get("log_file"):
        config.log_file = Path(os.path.expanduser(str(log_file)))
    if state_file := raw.get("state_file"):
        config.state_file = Path(os.path.expanduser(str(state_file)))
    if poll_interval := raw.get("poll_interval"):
        config.poll_interval = float(poll_interval)
    if body_max_len := raw.get("body_max_len"):
        config.body_max_len = int(body_max_len)
    if notification_group := raw.get("notification_group"):
        config.notification_group = str(notification_group)
    if question_timeout_seconds := raw.get("question_timeout_seconds"):
        config.question_timeout_seconds = float(question_timeout_seconds)
    if decision_timeout_seconds := raw.get("decision_timeout_seconds"):
        config.decision_timeout_seconds = float(decision_timeout_seconds)
    if reminder_interval_seconds := raw.get("reminder_interval_seconds"):
        config.reminder_interval_seconds = float(reminder_interval_seconds)
    if notification_icon := raw.get("notification_icon"):
        config.notification_icon = str(notification_icon).strip()
    if notification_url := raw.get("notification_url"):
        config.notification_url = str(notification_url).strip()
    if muxdeck_host_id := raw.get("muxdeck_host_id"):
        config.muxdeck_host_id = str(muxdeck_host_id).strip()
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch Codex session files and send mobile notifications for completion, approvals, and Plan-mode interactions.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--watch-root",
        type=Path,
        help="Override the session root to watch",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Override the Codex TUI log file path retained for compatibility",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Override the path used to persist seen turn IDs",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        help="Override the poll interval in seconds",
    )
    parser.add_argument(
        "--run-for",
        type=float,
        help="Exit after the given number of seconds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not send Bark requests; only log what would be sent",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)
    if args.watch_root is not None:
        config.watch_root = args.watch_root
    if args.log_file is not None:
        config.log_file = args.log_file
    if args.state_file is not None:
        config.state_file = args.state_file
    if args.poll_interval is not None:
        config.poll_interval = args.poll_interval

    watcher = CodexNotifyWatcher(
        config_path=args.config,
        config=config,
        dry_run=args.dry_run,
    )

    def _stop(*_: Any) -> None:
        logging.info("received shutdown signal")
        watcher.running = False

    try:
        import signal

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except Exception:
        pass

    watcher.run(run_for=args.run_for)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
