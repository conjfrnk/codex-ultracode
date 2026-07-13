import textwrap
from typing import Dict, List

from .agent_team_transcript import validate_agent_team_transcript
from .errors import ValidationError
from .redaction import redact_terminal_text
from .run_control import TEAM_CONSOLE_SNAPSHOT_SCHEMA


TEAM_CONSOLE_LAYOUTS = {"auto", "columns", "stack"}
TEAM_CONSOLE_MIN_WIDTH = 40
TEAM_CONSOLE_MAX_WIDTH = 300
TEAM_CONSOLE_MIN_PANEL_WIDTH = 38
TEAM_CONSOLE_PANEL_GAP = 2
TEAM_CONSOLE_MAX_HISTORY = 5
TEAM_CONSOLE_MAX_DISPLAY_EVENTS = 50
TEAM_CONSOLE_MIN_MESSAGE_CHARS = 100
TEAM_CONSOLE_MAX_MESSAGE_CHARS = 4000
TEAM_CONSOLE_DEFAULT_MESSAGE_CHARS = 1200
TEAM_CONSOLE_REFRESH_MIN_MS = 100
TEAM_CONSOLE_REFRESH_MAX_MS = 5000
TEAM_CONSOLE_TIMEOUT_MAX_SECONDS = 24 * 60 * 60

_SNAPSHOT_FIELDS = {
    "schema",
    "step_id",
    "generation",
    "team_status",
    "round",
    "max_rounds",
    "max_events",
    "max_bytes",
    "transcript_count",
    "members",
    "transcripts",
}
_MEMBER_FIELDS = {
    "id",
    "role",
    "lead",
    "status",
    "turns",
    "current_task_id",
}


def validate_team_console_snapshot(snapshot: Dict) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise ValidationError("team console snapshot has invalid fields")
    if snapshot.get("schema") != TEAM_CONSOLE_SNAPSHOT_SCHEMA:
        raise ValidationError("team console snapshot has an unsupported schema")
    for field in ("step_id", "team_status"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise ValidationError("team console snapshot %s is invalid" % field)
    _bounded_int(snapshot.get("generation"), 0, 10**9, "generation")
    _bounded_int(snapshot.get("round"), 0, 16, "round")
    _bounded_int(snapshot.get("max_rounds"), 1, 16, "max_rounds")
    if snapshot["round"] > snapshot["max_rounds"]:
        raise ValidationError("team console snapshot round exceeds max_rounds")
    _bounded_int(snapshot.get("max_events"), 8, 1024, "max_events")
    _bounded_int(snapshot.get("max_bytes"), 4 * 1024, 2 * 1024 * 1024, "max_bytes")
    members = snapshot.get("members")
    if not isinstance(members, list) or not members:
        raise ValidationError("team console snapshot members must be non-empty")
    member_ids = []
    for member in members:
        if not isinstance(member, dict) or set(member) != _MEMBER_FIELDS:
            raise ValidationError("team console snapshot member has invalid fields")
        for field in ("id", "role", "status"):
            if not isinstance(member.get(field), str) or not member[field]:
                raise ValidationError("team console snapshot member %s is invalid" % field)
        if not isinstance(member.get("lead"), bool):
            raise ValidationError("team console snapshot member lead is invalid")
        _bounded_int(member.get("turns"), 0, 10**9, "member turns")
        current_task_id = member.get("current_task_id")
        if current_task_id is not None and (
            not isinstance(current_task_id, str) or not current_task_id
        ):
            raise ValidationError("team console snapshot current_task_id is invalid")
        member_ids.append(member["id"])
    if len(set(member_ids)) != len(member_ids):
        raise ValidationError("team console snapshot members contain duplicates")
    transcripts = snapshot.get("transcripts")
    if not isinstance(transcripts, list):
        raise ValidationError("team console snapshot transcripts must be a list")
    _bounded_int(
        snapshot.get("transcript_count"),
        0,
        16 * 16 * 100,
        "transcript_count",
    )
    if snapshot["transcript_count"] != len(transcripts):
        raise ValidationError("team console snapshot transcript count changed")
    transcript_ids = []
    for transcript in transcripts:
        validate_agent_team_transcript(transcript)
        if transcript["step_id"] != snapshot["step_id"]:
            raise ValidationError("team console snapshot transcript step binding changed")
        if transcript["generation"] != snapshot["generation"]:
            raise ValidationError("team console snapshot transcript generation changed")
        if transcript["member_id"] not in member_ids:
            raise ValidationError("team console snapshot transcript member binding changed")
        if transcript["max_events"] != snapshot["max_events"]:
            raise ValidationError("team console snapshot transcript event bound changed")
        if transcript["max_bytes"] != snapshot["max_bytes"]:
            raise ValidationError("team console snapshot transcript byte bound changed")
        transcript_ids.append(transcript["transcript_id"])
    if len(set(transcript_ids)) != len(transcript_ids):
        raise ValidationError("team console snapshot transcripts contain duplicates")


def render_team_console(
    snapshot: Dict,
    *,
    width: int = 120,
    layout: str = "auto",
    history: int = 1,
    max_events: int = 8,
    max_message_chars: int = TEAM_CONSOLE_DEFAULT_MESSAGE_CHARS,
) -> str:
    validate_team_console_snapshot(snapshot)
    _validate_render_options(
        width=width,
        layout=layout,
        history=history,
        max_events=max_events,
        max_message_chars=max_message_chars,
    )
    members = snapshot["members"]
    panel_columns = _panel_columns(layout, width, len(members))
    gap_width = TEAM_CONSOLE_PANEL_GAP * (panel_columns - 1)
    panel_width = (width - gap_width) // panel_columns
    header = (
        "TEAM %s | %s | generation %d | round %d/%d | %d member(s) | %d transcript(s)"
        % (
            redact_terminal_text(snapshot["step_id"]),
            snapshot["team_status"],
            snapshot["generation"],
            snapshot["round"],
            snapshot["max_rounds"],
            len(members),
            snapshot["transcript_count"],
        )
    )
    panels = [
        _member_panel(
            member,
            snapshot["transcripts"],
            panel_width,
            history,
            max_events,
            max_message_chars,
        )
        for member in members
    ]
    output = _wrap_text(header, width)
    output.append("=" * min(width, max(len(line) for line in output)))
    for offset in range(0, len(panels), panel_columns):
        row = panels[offset : offset + panel_columns]
        height = max(len(panel) for panel in row)
        for panel in row:
            panel.extend([" " * panel_width] * (height - len(panel)))
        for line_index in range(height):
            output.append(
                (" " * TEAM_CONSOLE_PANEL_GAP).join(
                    panel[line_index] for panel in row
                ).rstrip()
            )
    return "\n".join(output).rstrip() + "\n"


def team_console_is_terminal(snapshot: Dict) -> bool:
    validate_team_console_snapshot(snapshot)
    return snapshot["team_status"] != "running" and not any(
        transcript["status"] == "active" for transcript in snapshot["transcripts"]
    )


def _member_panel(
    member: Dict,
    transcripts: List[Dict],
    panel_width: int,
    history: int,
    max_events: int,
    max_message_chars: int,
) -> List[str]:
    member_transcripts = [
        transcript
        for transcript in transcripts
        if transcript["member_id"] == member["id"]
    ][-history:]
    label = "%s%s [%s]" % (
        redact_terminal_text(member["id"]),
        " *" if member["lead"] else "",
        member["status"],
    )
    logical = [label, "role: %s" % redact_terminal_text(member["role"])]
    if member["current_task_id"] is not None:
        logical.append(
            "current: %s" % redact_terminal_text(member["current_task_id"])
        )
    if not member_transcripts:
        logical.extend(["", "No retained turn yet."])
    for index, transcript in enumerate(member_transcripts):
        if index or logical:
            logical.append("")
        logical.append(
            "r%d a%d | %s | %s"
            % (
                transcript["round"],
                transcript["attempt"],
                redact_terminal_text(transcript["task_id"]),
                transcript["status"],
            )
        )
        events = transcript["events"][-max_events:]
        if not events:
            logical.append("(waiting for retained activity)")
        for event in events:
            logical.extend(_event_lines(event, max_message_chars))
        if transcript["events_dropped"]:
            logical.append(
                "... %d event(s) dropped" % transcript["events_dropped"]
            )
    return _box(logical, panel_width)


def _event_lines(event: Dict, max_message_chars: int) -> List[str]:
    if event["type"] == "assistant-message":
        text = redact_terminal_text(event["text"])
        truncated = len(text) > max_message_chars
        text = text[:max_message_chars]
        lines = text.splitlines() or [""]
        rendered = ["assistant: %s" % lines[0]]
        rendered.extend("  %s" % line for line in lines[1:])
        if truncated or event["truncated"]:
            rendered.append("  ... message truncated")
        return rendered
    if event["type"] == "activity":
        return [
            "%s %s"
            % (
                event["item_type"].replace("_", " "),
                event["status"],
            )
        ]
    return [event["type"].replace("-", " ")]


def _box(logical_lines: List[str], panel_width: int) -> List[str]:
    inner_width = panel_width - 2
    lines = []
    for line in logical_lines:
        lines.extend(_wrap_text(line, inner_width) if line else [""])
    border = "+%s+" % ("-" * inner_width)
    return [border] + ["|%s|" % line.ljust(inner_width) for line in lines] + [border]


def _panel_columns(layout: str, width: int, member_count: int) -> int:
    if layout == "stack" or member_count == 1:
        return 1
    maximum = max(
        1,
        (width + TEAM_CONSOLE_PANEL_GAP)
        // (TEAM_CONSOLE_MIN_PANEL_WIDTH + TEAM_CONSOLE_PANEL_GAP),
    )
    if layout == "columns" and maximum < 2:
        raise ValidationError(
            "team-console columns layout needs width %d or greater"
            % (2 * TEAM_CONSOLE_MIN_PANEL_WIDTH + TEAM_CONSOLE_PANEL_GAP)
        )
    return min(member_count, maximum)


def _validate_render_options(
    *,
    width: int,
    layout: str,
    history: int,
    max_events: int,
    max_message_chars: int,
) -> None:
    _bounded_int(width, TEAM_CONSOLE_MIN_WIDTH, TEAM_CONSOLE_MAX_WIDTH, "width")
    if layout not in TEAM_CONSOLE_LAYOUTS:
        raise ValidationError("team-console layout is invalid")
    _bounded_int(history, 1, TEAM_CONSOLE_MAX_HISTORY, "history")
    _bounded_int(max_events, 1, TEAM_CONSOLE_MAX_DISPLAY_EVENTS, "max_events")
    _bounded_int(
        max_message_chars,
        TEAM_CONSOLE_MIN_MESSAGE_CHARS,
        TEAM_CONSOLE_MAX_MESSAGE_CHARS,
        "max_message_chars",
    )


def _wrap_text(value: str, width: int) -> List[str]:
    wrapped = textwrap.wrap(
        str(value),
        width=width,
        replace_whitespace=False,
        drop_whitespace=True,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped or [""]


def _bounded_int(value, minimum: int, maximum: int, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValidationError(
            "team-console %s must be an integer from %d to %d"
            % (label, minimum, maximum)
        )
