import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse
from urllib.parse import unquote

from .agent_team import (
    AGENT_TEAM_STATE_SCHEMA,
    AGENT_TEAM_STATE_SCHEMA_V4,
    AGENT_TEAM_STATE_SCHEMA_V5,
    MAX_AGENT_TEAM_MESSAGE_CHARS,
    MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
)
from .auto_orchestrator import collect_auto_orchestrations
from .clock import utc_now
from .dashboard import (
    DEFAULT_MAX_AUTO,
    DEFAULT_MAX_GOALS,
    DEFAULT_MAX_REPORTS,
    DEFAULT_MAX_RUNS,
    DEFAULT_MAX_TEXT_BYTES,
    STEP_HANDLE_PREFIX,
    collect_goal_detail,
    collect_goals,
    collect_run_detail,
    collect_reports,
    collect_routines,
    collect_runs,
    routine_handle,
    run_handle,
    step_handle,
)
from .background_supervisor import latest_background_routine_supervisor
from .errors import ValidationError
from .redaction import redact_text
from .routines import load_routine_manifest
from .routine_supervisor import execute_routine_manifest, routine_launch_approval, set_routine_paused
from .routine_service import status_routine_service
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES
from .run_control import (
    export_run_workflow_text,
    queue_team_task,
    read_team_transcript_view,
    reply_team_question,
    review_team_plan,
    pause_run,
    recover_run,
    reset_step,
    retry_packet,
    restart_run,
    resume_run,
    retry_step,
    skip_step,
    stop_run,
    terminate_run,
)
from .security import (
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    require_no_path_escape,
    validate_approval_tokens,
)


DEFAULT_LIVE_HOST = "127.0.0.1"
DEFAULT_LIVE_PORT = 8765
MAX_REASON_CHARS = 512
MAX_CONTROL_REQUEST_BYTES = 64 * 1024
LIVE_CONTROL_METADATA_MAX_BYTES = 1024 * 1024
PRIVATE_TRANSCRIPT_COOKIE_SECONDS = 15 * 60
PRIVATE_TRANSCRIPT_CLOCK_SKEW_SECONDS = 5
PRIVATE_TRANSCRIPT_REFRESH_SECONDS = 2
PRIVATE_TRANSCRIPT_PATH_PREFIX = "/private/team-transcript/"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LIVE_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
_CONTROL_LOCKS: Dict[str, threading.Lock] = {}
_CONTROL_LOCKS_GUARD = threading.Lock()


def create_live_server(
    runs_dir: Path,
    benchmarks_dir: Path,
    host: str = DEFAULT_LIVE_HOST,
    port: int = DEFAULT_LIVE_PORT,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_reports: int = DEFAULT_MAX_REPORTS,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    title: str = "Codex Conductor Live",
    control_token: Optional[str] = None,
    allow_remote: bool = False,
    goals_dir: Optional[Path] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    routines_dir: Optional[Path] = None,
    generated_dir: Optional[Path] = None,
    auto_dir: Optional[Path] = None,
    max_auto: int = DEFAULT_MAX_AUTO,
) -> Tuple[ThreadingHTTPServer, str]:
    _validate_bind(host, port, allow_remote)
    token = control_token or secrets.token_urlsafe(24)
    handler = _handler_factory(
        runs_dir=runs_dir,
        benchmarks_dir=benchmarks_dir,
        goals_dir=goals_dir or (Path(runs_dir).parent / "goals"),
        routines_dir=routines_dir or (Path(runs_dir).parent / "routines"),
        generated_dir=generated_dir or (Path(runs_dir).parent / "generated"),
        auto_dir=auto_dir,
        max_runs=max_runs,
        max_reports=max_reports,
        max_goals=max_goals,
        max_auto=max_auto,
        max_text_bytes=max_text_bytes,
        title=title,
        control_token=token,
    )
    return ThreadingHTTPServer((host, port), handler), token


def serve_live_dashboard(
    runs_dir: Path,
    benchmarks_dir: Path,
    host: str = DEFAULT_LIVE_HOST,
    port: int = DEFAULT_LIVE_PORT,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_reports: int = DEFAULT_MAX_REPORTS,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    title: str = "Codex Conductor Live",
    control_token: Optional[str] = None,
    allow_remote: bool = False,
    goals_dir: Optional[Path] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    routines_dir: Optional[Path] = None,
    generated_dir: Optional[Path] = None,
    auto_dir: Optional[Path] = None,
    max_auto: int = DEFAULT_MAX_AUTO,
) -> None:
    server, token = create_live_server(
        runs_dir=runs_dir,
        benchmarks_dir=benchmarks_dir,
        goals_dir=goals_dir,
        routines_dir=routines_dir,
        generated_dir=generated_dir,
        auto_dir=auto_dir,
        host=host,
        port=port,
        max_runs=max_runs,
        max_reports=max_reports,
        max_goals=max_goals,
        max_auto=max_auto,
        max_text_bytes=max_text_bytes,
        title=title,
        control_token=control_token,
        allow_remote=allow_remote,
    )
    print("Live dashboard: http://%s:%d/" % (host, server.server_port))
    print("Control token: %s" % token)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_live_payload(
    runs_dir: Path,
    benchmarks_dir: Path,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_reports: int = DEFAULT_MAX_REPORTS,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    goals_dir: Optional[Path] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    routines_dir: Optional[Path] = None,
    generated_dir: Optional[Path] = None,
    auto_dir: Optional[Path] = None,
    max_auto: int = DEFAULT_MAX_AUTO,
) -> Dict:
    resolved_goals_dir = Path(runs_dir).parent / "goals" if goals_dir is None else Path(goals_dir)
    resolved_routines_dir = Path(runs_dir).parent / "routines" if routines_dir is None else Path(routines_dir)
    resolved_generated_dir = Path(runs_dir).parent / "generated" if generated_dir is None else Path(generated_dir)
    resolved_auto_dir = resolved_generated_dir / "auto" if auto_dir is None else Path(auto_dir)
    runs = collect_runs(runs_dir, max_runs=max_runs, max_text_bytes=max_text_bytes)
    reports = collect_reports(benchmarks_dir, max_reports=max_reports)
    goals = collect_goals(resolved_goals_dir, max_goals=max_goals)
    auto_runs = collect_auto_orchestrations(
        resolved_auto_dir,
        generated_dir=resolved_generated_dir,
        goals_dir=resolved_goals_dir,
        runs_dir=runs_dir,
        max_records=max_auto,
    )
    return {
        "schema": "conductor.live_state.v1",
        "generated_at_utc": utc_now().isoformat(timespec="seconds") + "Z",
        "runs_dir": redact_text(str(runs_dir)),
        "benchmarks_dir": redact_text(str(benchmarks_dir)),
        "goals_dir": redact_text(str(resolved_goals_dir)),
        "auto_dir": redact_text(str(resolved_auto_dir)),
        "routines_dir": redact_text(str(resolved_routines_dir)),
        "routine_supervisor": _public_value(latest_background_routine_supervisor(resolved_routines_dir)),
        "routine_service": _public_value(status_routine_service(resolved_routines_dir)),
        "routines": [_public_routine(item) for item in collect_routines(resolved_routines_dir)],
        "runs": [_public_run(run) for run in runs],
        "auto_orchestrations": [_public_value(item) for item in auto_runs],
        "goals": [_public_value(goal) for goal in goals],
        "reports": [_public_value(report) for report in reports],
    }


def render_live_dashboard(payload: Dict, title: str = "Codex Conductor Live", message: str = "") -> str:
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    auto_runs = payload.get("auto_orchestrations") if isinstance(payload.get("auto_orchestrations"), list) else []
    goals = payload.get("goals") if isinstance(payload.get("goals"), list) else []
    reports = payload.get("reports") if isinstance(payload.get("reports"), list) else []
    routine_supervisor = payload.get("routine_supervisor") if isinstance(payload.get("routine_supervisor"), dict) else {}
    routine_service = payload.get("routine_service") if isinstance(payload.get("routine_service"), dict) else {}
    routines = payload.get("routines") if isinstance(payload.get("routines"), list) else []
    generated_at = _string(payload.get("generated_at_utc"))
    message_html = (
        '<section class="notice">%s</section>' % escape(message)
        if message
        else ""
    )
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; --border: #b8c0cc; --muted: #5f6875; --ok: #0f7b3e; --warn: #a05a00; --bad: #a32222; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.4; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
    main {{ padding: 18px 24px 32px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin: 26px 0 10px; font-size: 17px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .muted {{ color: var(--muted); }}
    .status-completed, .status-active {{ color: var(--ok); font-weight: 650; }}
    .status-planned, .status-running, .status-pending, .status-needs_resume, .status-paused, .status-pause_requested, .status-stop_requested, .status-restart_requested, .status-max_iterations_exhausted, .status-goal_not_satisfied {{ color: var(--warn); font-weight: 650; }}
    .status-failed, .status-blocked, .status-invalid, .status-workflow_failed, .status-needs-attention {{ color: var(--bad); font-weight: 650; }}
    .notice {{ padding: 10px 12px; border: 1px solid var(--border); margin-bottom: 14px; background: rgba(128,128,128,.08); }}
    .controls form {{ display: grid; gap: 6px; grid-template-columns: 1fr; margin-bottom: 8px; }}
    .controls input[type="text"], .controls select {{ min-width: 0; padding: 6px; }}
    .controls button {{ padding: 6px 8px; }}
    .detail-grid {{ display: grid; gap: 14px; }}
    .detail-section {{ border-top: 1px solid var(--border); padding-top: 12px; }}
    pre {{ max-height: 320px; overflow: auto; white-space: pre-wrap; }}
    @media (max-width: 820px) {{ th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5) {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="muted">Updated <code>{generated_at}</code></div>
  </header>
  <main>
    {message_html}
    <section>
      <h2>Approval Queue</h2>
      {approval_queue}
    </section>
    <section>
      <h2>Runs</h2>
      {runs_table}
    </section>
    <section>
      <h2>Automatic Orchestration</h2>
      {auto_table}
    </section>
    <section>
      <h2>Goals</h2>
      {goals_table}
    </section>
    <section>
      <h2>Routines</h2>
      {routines_table}
    </section>
    <section>
      <h2>Routine Service</h2>
      {routine_service_table}
    </section>
    <section>
      <h2>Routine Supervisor</h2>
      {routine_supervisor_table}
    </section>
    <section>
      <h2>Benchmarks</h2>
      {reports_table}
    </section>
  </main>
</body>
</html>
""".format(
        title=escape(title),
        generated_at=escape(generated_at),
        message_html=message_html,
        approval_queue=_render_approval_queue(runs),
        runs_table=_render_runs_table(runs),
        auto_table=_render_auto_orchestrations_table(auto_runs),
        goals_table=_render_goals_table(goals),
        routines_table=_render_routines_table(routines),
        routine_service_table=_render_routine_service(routine_service),
        routine_supervisor_table=_render_routine_supervisor(routine_supervisor),
        reports_table=_render_reports_table(reports),
    )


def render_live_run_detail(detail: Dict, title: str = "Codex Conductor Live", message: str = "") -> str:
    handle = _string(detail.get("run_handle") or run_handle(str(detail.get("id") or "")))
    detail = _public_value(detail)
    run_id = _string(detail.get("id"))
    generated_at = utc_now().isoformat(timespec="seconds") + "Z"
    message_html = (
        '<section class="notice">%s</section>' % escape(message)
        if message
        else ""
    )
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}: {run_id}</title>
  <style>
    :root {{ color-scheme: light dark; --border: #b8c0cc; --muted: #5f6875; --ok: #0f7b3e; --warn: #a05a00; --bad: #a32222; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.4; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
    main {{ padding: 18px 24px 32px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin: 26px 0 10px; font-size: 17px; }}
    h3 {{ margin: 18px 0 8px; font-size: 15px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0; color: var(--muted); }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    pre {{ max-height: 320px; overflow: auto; white-space: pre-wrap; border: 1px solid var(--border); padding: 8px; }}
    .muted {{ color: var(--muted); }}
    .warn {{ color: var(--warn); }}
    .status-completed, .status-active {{ color: var(--ok); font-weight: 650; }}
    .status-planned, .status-running, .status-pending, .status-needs_resume, .status-paused, .status-pause_requested, .status-stop_requested, .status-restart_requested, .status-max_iterations_exhausted, .status-goal_not_satisfied {{ color: var(--warn); font-weight: 650; }}
    .status-failed, .status-blocked, .status-invalid, .status-workflow_failed, .status-needs-attention {{ color: var(--bad); font-weight: 650; }}
    .notice {{ padding: 10px 12px; border: 1px solid var(--border); margin-bottom: 14px; background: rgba(128,128,128,.08); }}
    .controls form {{ display: grid; gap: 6px; grid-template-columns: minmax(110px, 1fr) minmax(120px, 2fr) auto auto auto auto; align-items: center; margin-bottom: 8px; }}
    .controls input[type="text"], .controls select {{ min-width: 0; padding: 6px; }}
    .controls button {{ padding: 6px 8px; }}
    .transcript-open {{ display: flex; gap: 6px; align-items: center; }}
    .transcript-open input {{ min-width: 120px; padding: 6px; }}
    .transcript-open button {{ padding: 6px 8px; }}
    @media (max-width: 820px) {{ .controls form {{ grid-template-columns: 1fr; }} .transcript-open {{ align-items: stretch; flex-direction: column; }} table, thead, tbody, tr, th, td {{ display: block; }} thead {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{title}: <code>{run_id}</code></h1>
    <div><a href="/">Back</a> <span class="muted">Updated <code>{generated_at}</code></span></div>
  </header>
  <main>
    {message_html}
    {summary}
    {approvals}
    {runtime}
    {diagnostics}
    {phases}
    {steps}
    {hooks}
    {session_traces}
    {agent_teams}
    {agent_maps}
    {standard_files}
    {artifacts}
    {logs}
  </main>
</body>
</html>
""".format(
        title=escape(title),
        run_id=escape(run_id),
        generated_at=escape(generated_at),
        message_html=message_html,
        summary=_render_live_detail_summary(detail),
        approvals=_render_live_detail_approvals(detail.get("approvals") if isinstance(detail.get("approvals"), dict) else {}),
        runtime=_render_live_detail_runtime(detail.get("runtime") if isinstance(detail.get("runtime"), dict) else {}),
        diagnostics=_render_live_detail_diagnostics(detail.get("diagnostics") if isinstance(detail.get("diagnostics"), dict) else {}),
        phases=_render_live_detail_phases(detail.get("phases") if isinstance(detail.get("phases"), list) else []),
        steps=_render_live_detail_steps(handle, detail.get("steps") if isinstance(detail.get("steps"), list) else []),
        hooks=_render_live_detail_hooks(detail.get("hook_events") if isinstance(detail.get("hook_events"), list) else []),
        session_traces=_render_live_session_traces(
            detail.get("session_trace_events") if isinstance(detail.get("session_trace_events"), list) else [],
            detail.get("session_trace_summary") if isinstance(detail.get("session_trace_summary"), dict) else {},
        ),
        agent_teams=_render_live_agent_teams(
            handle,
            detail.get("agent_teams") if isinstance(detail.get("agent_teams"), list) else []
        ),
        agent_maps=_render_live_agent_maps(handle, detail.get("agent_maps") if isinstance(detail.get("agent_maps"), list) else []),
        standard_files=_render_live_file_table("Standard Artifacts", detail.get("standard_files") if isinstance(detail.get("standard_files"), list) else []),
        artifacts=_render_live_group("Artifacts", detail.get("artifacts") if isinstance(detail.get("artifacts"), dict) else {}),
        logs=_render_live_group("Logs", detail.get("logs") if isinstance(detail.get("logs"), dict) else {}),
    )


def render_private_team_transcript(
    view: Dict,
    run_handle_value: str,
    step_handle_value: str,
    *,
    title: str = "Codex Conductor Live",
    message: str = "",
    follow: bool = True,
) -> str:
    transcript = view.get("transcript") if isinstance(view, dict) else None
    if not isinstance(transcript, dict):
        raise ValidationError("private teammate transcript view is invalid")
    transcript_id = _string(transcript.get("transcript_id"))
    private_path = _private_transcript_path(
        run_handle_value,
        step_handle_value,
        transcript_id,
    )
    is_active = transcript.get("status") == "active"
    follow = bool(follow and is_active)
    refresh_meta = (
        '<meta http-equiv="refresh" content="%d">'
        % PRIVATE_TRANSCRIPT_REFRESH_SECONDS
        if follow
        else ""
    )
    refresh_control = (
        '<a href="%s?follow=0">Pause refresh</a>' % escape(private_path)
        if follow
        else (
            '<a href="%s">Resume refresh</a>' % escape(private_path)
            if is_active
            else ""
        )
    )
    generated_at = utc_now().isoformat(timespec="seconds") + "Z"
    notice = (
        '<section class="notice">%s</section>' % escape(redact_text(message))
        if message
        else ""
    )
    event_rows = []
    events = transcript.get("events") if isinstance(transcript.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = _string(event.get("type") or "unknown")
        if event_type == "assistant-message":
            detail = '<pre>%s</pre>' % escape(redact_text(_string(event.get("text"))))
            if event.get("truncated"):
                detail += '<div class="warn">Message truncated</div>'
            label = "assistant"
        elif event_type == "activity":
            label = _string(event.get("item_type") or "activity").replace("_", " ")
            detail = escape(_string(event.get("status") or "unknown"))
        else:
            label = event_type.replace("-", " ")
            detail = ""
        event_rows.append(
            "<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(event.get("sequence") or 0)),
                escape(_string(event.get("at_utc") or "unknown")),
                escape(label),
                detail,
            )
        )
    if not event_rows:
        event_rows.append('<tr><td colspan="4" class="muted">No retained events yet.</td></tr>')
    control = _render_private_team_transcript_control(
        view,
        run_handle_value,
        step_handle_value,
        private_path,
    )
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>{title}: Teammate transcript</title>
  <style>
    :root {{ color-scheme: light dark; --border: #b8c0cc; --muted: #5f6875; --ok: #0f7b3e; --warn: #a05a00; --bad: #a32222; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.4; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
    main {{ padding: 18px 24px 32px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin: 26px 0 10px; font-size: 17px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0; color: var(--muted); }}
    code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    pre {{ margin: 0; max-height: 420px; overflow: auto; white-space: pre-wrap; }}
    .muted {{ color: var(--muted); }}
    .warn {{ color: var(--warn); }}
    .status-active, .status-completed {{ color: var(--ok); font-weight: 650; }}
    .status-interrupted, .status-timed-out {{ color: var(--warn); font-weight: 650; }}
    .status-failed {{ color: var(--bad); font-weight: 650; }}
    .notice {{ padding: 10px 12px; border: 1px solid var(--border); margin-bottom: 14px; background: rgba(128,128,128,.08); }}
    .controls form {{ display: grid; gap: 8px; grid-template-columns: minmax(130px, 1fr) minmax(280px, 3fr) minmax(150px, 1fr) auto; align-items: center; }}
    .controls input, .controls select, .controls button {{ min-width: 0; padding: 7px; }}
    @media (max-width: 820px) {{ header {{ align-items: flex-start; flex-direction: column; }} .controls form {{ grid-template-columns: 1fr; }} table, thead, tbody, tr, th, td {{ display: block; }} thead {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Teammate <code>{member}</code></h1>
    <div><a href="/run/{run_handle}">Back</a> {refresh_control} <span class="muted">Updated <code>{generated_at}</code></span></div>
  </header>
  <main>
    {notice}
    <section>
      <h2>Turn</h2>
      <table><tbody>
        <tr><th>Status</th><td><span class="status-{status_class}">{status}</span></td><th>Task</th><td><code>{task}</code></td></tr>
        <tr><th>Round</th><td>{round_number}</td><th>Attempt</th><td>{attempt}</td></tr>
        <tr><th>Events</th><td>{event_count}</td><th>Dropped</th><td>{events_dropped}</td></tr>
        <tr><th>Transcript</th><td colspan="3"><code>{transcript_id}</code></td></tr>
      </tbody></table>
    </section>
    {control}
    <section>
      <h2>Activity</h2>
      <table><thead><tr><th>Seq</th><th>Time</th><th>Type</th><th>Detail</th></tr></thead><tbody>{events}</tbody></table>
    </section>
  </main>
</body>
</html>
""".format(
        refresh_meta=refresh_meta,
        title=escape(title),
        member=escape(_string(transcript.get("member_id") or "unknown")),
        run_handle=quote(run_handle_value, safe=""),
        refresh_control=refresh_control,
        generated_at=escape(generated_at),
        notice=notice,
        status_class=escape(_status_class(_string(transcript.get("status") or "unknown"))),
        status=escape(_string(transcript.get("status") or "unknown")),
        task=escape(_string(transcript.get("task_id") or "unknown")),
        round_number=escape(_string(transcript.get("round") or 0)),
        attempt=escape(_string(transcript.get("attempt") or 0)),
        event_count=escape(_string(len(events))),
        events_dropped=escape(_string(transcript.get("events_dropped") or 0)),
        transcript_id=escape(transcript_id),
        control=control,
        events="".join(event_rows),
    )


def _render_private_team_transcript_control(
    view: Dict,
    run_handle_value: str,
    step_handle_value: str,
    return_to: str,
) -> str:
    transcript = view.get("transcript") if isinstance(view.get("transcript"), dict) else {}
    can_redirect = (
        transcript.get("status") == "active"
        and view.get("team_status") == "running"
        and view.get("member_status") == "working"
        and view.get("current_task_id") == transcript.get("task_id")
        and isinstance(view.get("max_operator_tasks"), int)
        and isinstance(view.get("operator_tasks_added"), int)
        and isinstance(view.get("pending_operator_tasks"), int)
        and view["operator_tasks_added"] + view["pending_operator_tasks"]
        < view["max_operator_tasks"]
    )
    if not can_redirect:
        return ""
    return (
        '<section class="controls"><h2>Direct teammate</h2><form method="post" action="/control">'
        '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
        '<input type="text" name="instruction" maxlength="%d" autocomplete="off" '
        'placeholder="Instruction" required>'
        '<select name="delivery" aria-label="Delivery">'
        '<option value="interrupt-current">Interrupt current turn</option>'
        '<option value="next-turn">Queue for next turn</option></select>'
        '<input type="hidden" name="run_handle" value="%s">'
        '<input type="hidden" name="step_handle" value="%s">'
        '<input type="hidden" name="member" value="%s">'
        '<input type="hidden" name="action" value="queue-team-task">%s'
        '<button type="submit">Send</button></form></section>'
        % (
            MAX_AGENT_TEAM_MESSAGE_CHARS,
            escape(run_handle_value),
            escape(step_handle_value),
            escape(_string(transcript.get("member_id"))),
            _return_to_input(return_to),
        )
    )


def render_live_goal_detail(detail: Dict, title: str = "Codex Conductor Live", message: str = "") -> str:
    detail = _public_value(detail)
    goal_id = _string(detail.get("goal_id") or detail.get("name"))
    generated_at = utc_now().isoformat(timespec="seconds") + "Z"
    message_html = (
        '<section class="notice">%s</section>' % escape(message)
        if message
        else ""
    )
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}: {goal_id}</title>
  <style>
    :root {{ color-scheme: light dark; --border: #b8c0cc; --muted: #5f6875; --ok: #0f7b3e; --warn: #a05a00; --bad: #a32222; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.4; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
    main {{ padding: 18px 24px 32px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin: 26px 0 10px; font-size: 17px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0; color: var(--muted); }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .muted {{ color: var(--muted); }}
    .warn {{ color: var(--warn); }}
    .status-completed, .status-goal_satisfied, .status-passed, .status-active {{ color: var(--ok); font-weight: 650; }}
    .status-planned, .status-running, .status-pending, .status-needs_resume, .status-paused, .status-pause_requested, .status-stop_requested, .status-restart_requested, .status-max_iterations_exhausted, .status-goal_not_satisfied, .status-failed {{ color: var(--warn); font-weight: 650; }}
    .status-blocked, .status-invalid, .status-workflow_failed, .status-timed_out, .status-needs-attention {{ color: var(--bad); font-weight: 650; }}
    .notice {{ padding: 10px 12px; border: 1px solid var(--border); margin-bottom: 14px; background: rgba(128,128,128,.08); }}
    @media (max-width: 820px) {{ table, thead, tbody, tr, th, td {{ display: block; }} thead {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{title}: <code>{goal_id}</code></h1>
    <div><a href="/">Back</a> <span class="muted">Updated <code>{generated_at}</code></span></div>
  </header>
  <main>
    {message_html}
    {summary}
    {iterations}
  </main>
</body>
</html>
""".format(
        title=escape(title),
        goal_id=escape(goal_id),
        generated_at=escape(generated_at),
        message_html=message_html,
        summary=_render_live_goal_detail_summary(detail),
        iterations=_render_live_goal_detail_iterations(detail),
    )


def apply_live_control(
    runs_dir: Path,
    form: Dict[str, str],
    control_token: str,
    routines_dir: Optional[Path] = None,
) -> Dict:
    token = _field(form, "token")
    if not secrets.compare_digest(token, control_token):
        raise ValidationError("invalid live control token")
    action = _field(form, "action")
    reason = _field(form, "reason", required=False)
    if len(reason) > MAX_REASON_CHARS:
        raise ValidationError("reason must be at most %d characters" % MAX_REASON_CHARS)
    return_to = _validated_return_to(runs_dir, _field(form, "return_to", required=False))
    if action in {"pause-routine", "resume-routine", "run-routine-now"}:
        resolved_routines_dir = routines_dir or (Path(runs_dir).parent / "routines")
        routine_path = resolve_live_routine_handle(
            resolved_routines_dir,
            _field(form, "routine_handle"),
        )
        with _control_lock(routine_path):
            if action == "pause-routine":
                result = set_routine_paused(routine_path, paused=True, reason=reason)
                result["status"] = "paused"
                return _with_return_to(result, return_to)
            manifest = load_routine_manifest(routine_path)
            launch_approval = routine_launch_approval(manifest["name"])
            if action == "resume-routine":
                result = set_routine_paused(
                    routine_path,
                    paused=False,
                    approvals={launch_approval},
                    reason=reason,
                )
                result["status"] = "active"
                return _with_return_to(result, return_to)
            extra_approvals = _routine_form_approvals(_field(form, "approvals", required=False))
            extra_approvals.add(launch_approval)
            execution = execute_routine_manifest(
                routine_path,
                approvals=extra_approvals,
                output_limit_bytes=DEFAULT_OUTPUT_LIMIT_BYTES,
            )
            return _with_return_to(
                {
                    "action": "run-routine-now",
                    "routine": manifest["name"],
                    "status": execution["status"],
                    "returncode": execution["returncode"],
                    "approval_values_persisted": False,
                },
                return_to,
            )
    handle = _field(form, "run_handle", required=False)
    run_id = _field(form, "run_id", required=False)
    step_id = _field(form, "step_id", required=False)
    step_handle_value = _field(form, "step_handle", required=False)
    cascade = _field(form, "cascade", required=False) == "1"
    if handle:
        run_dir = resolve_live_run_handle(runs_dir, handle)
    else:
        run_dir = resolve_live_run_dir(runs_dir, run_id)
    with _control_lock(run_dir):
        if action == "pause-run":
            return _with_return_to(pause_run(run_dir, reason=reason), return_to)
        if action == "resume-run":
            return _with_return_to(resume_run(run_dir, reason=reason), return_to)
        if action == "stop-run":
            return _with_return_to(stop_run(run_dir, reason=reason), return_to)
        if action == "restart-run":
            return _with_return_to(restart_run(run_dir, reason=reason), return_to)
        if action == "terminate-run":
            return _with_return_to(terminate_run(run_dir, reason=reason), return_to)
        if action == "recover-run":
            return _with_return_to(
                recover_run(run_dir, reason=reason, retry_running=True),
                return_to,
            )
        if action == "recover-codex":
            return _with_return_to(
                recover_run(run_dir, reason=reason, resume_codex=True),
                return_to,
            )
        if action == "retry":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            return _with_return_to(retry_step(run_dir, step_id, reason=reason, cascade=cascade), return_to)
        if action == "retry-packet":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            packet_index = _live_packet_index(_field(form, "packet_index"))
            return _with_return_to(
                retry_packet(
                    run_dir,
                    step_id,
                    packet_index,
                    reason=reason,
                    cascade=cascade,
                ),
                return_to,
            )
        if action == "queue-team-task":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            delivery = _field(form, "delivery", required=False) or "next-turn"
            if delivery not in {"next-turn", "interrupt-current"}:
                raise ValidationError("unsupported teammate follow-up delivery mode")
            entry = queue_team_task(
                run_dir,
                step_id,
                _field(form, "member"),
                _field(form, "instruction"),
                interrupt_current=delivery == "interrupt-current",
            )
            return _with_return_to(
                {
                    "action": "queue-team-task",
                    "requested_step": step_id,
                    "status": entry["status"],
                    "task_id": entry["task_id"],
                    "member_id": entry["member_id"],
                    "delivery": entry["delivery"],
                    "interrupt_round": entry["interrupt_round"],
                    "interrupt_task_id": entry["interrupt_task_id"],
                    "interrupted_at_utc": entry["interrupted_at_utc"],
                    "instruction_sha256": entry["instruction_sha256"],
                },
                return_to,
            )
        if action == "reply-team-question":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            entry = reply_team_question(
                run_dir,
                step_id,
                _field(form, "question_id"),
                _field(form, "reply"),
            )
            return _with_return_to(
                {
                    "action": "reply-team-question",
                    "requested_step": step_id,
                    "status": entry["status"],
                    "question_id": entry["id"],
                    "member_id": entry["member_id"],
                    "reply_sha256": entry["reply_sha256"],
                    "response_task_id": entry["response_task_id"],
                },
                return_to,
            )
        if action == "review-team-plan":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            value = review_team_plan(
                run_dir,
                step_id,
                _field(form, "task_id"),
                _field(form, "decision"),
                _field(form, "feedback"),
            )
            return _with_return_to(
                {
                    "action": "review-team-plan",
                    "requested_step": step_id,
                    "status": value["status"],
                    "task_id": value["task_id"],
                    "decision": value["latest_decision"],
                    "review_sha256": value["latest_review_output_sha256"],
                },
                return_to,
            )
        if action == "reset":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            return _with_return_to(reset_step(run_dir, step_id, reason=reason, cascade=cascade), return_to)
        if action == "skip":
            step_id = _resolve_control_step_id(run_dir, step_id, step_handle_value)
            return _with_return_to(skip_step(run_dir, step_id, reason=reason, cascade=cascade), return_to)
    raise ValidationError("unsupported live control action: %s" % action)


def resolve_live_run_dir(runs_dir: Path, run_id: str) -> Path:
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise ValidationError("run_id must be a single run directory name")
    require_no_path_escape(run_id)
    reject_symlink_path(runs_dir, "runs_dir")
    runs_fd = open_dir_no_follow(runs_dir, "runs_dir")
    run_fd = None
    try:
        run_fd = _open_child_dir_no_follow(runs_fd, run_id, "run_dir")
        return runs_dir / run_id
    finally:
        if run_fd is not None:
            os.close(run_fd)
        os.close(runs_fd)


def resolve_live_run_handle(runs_dir: Path, handle: str) -> Path:
    if (
        not isinstance(handle, str)
        or len(handle) != 24
        or not handle.startswith("run-")
        or any(char not in "0123456789abcdef" for char in handle[4:])
    ):
        raise ValidationError("run_handle is invalid")
    reject_symlink_path(runs_dir, "runs_dir")
    if not runs_dir.exists():
        raise ValidationError("runs_dir does not exist: %s" % runs_dir)
    if not runs_dir.is_dir():
        raise ValidationError("runs_dir is not a directory: %s" % runs_dir)
    runs_fd = open_dir_no_follow(runs_dir, "runs_dir")
    try:
        for name in os.listdir(runs_fd):
            try:
                info = os.stat(name, dir_fd=runs_fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            if run_handle(name) == handle:
                return resolve_live_run_dir(runs_dir, name)
    finally:
        os.close(runs_fd)
    raise ValidationError("run_handle does not exist")


def resolve_live_routine_handle(routines_dir: Path, handle: str) -> Path:
    if (
        not isinstance(handle, str)
        or len(handle) != 28
        or not handle.startswith("routine-")
        or any(char not in "0123456789abcdef" for char in handle[8:])
    ):
        raise ValidationError("routine_handle is invalid")
    reject_symlink_path(routines_dir, "routines_dir")
    if not routines_dir.exists() or not routines_dir.is_dir():
        raise ValidationError("routines_dir does not exist or is not a directory")
    routines_fd = open_dir_no_follow(routines_dir, "routines_dir")
    try:
        for name in os.listdir(routines_fd):
            if not name.endswith(".json"):
                continue
            try:
                info = os.stat(name, dir_fd=routines_fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if routine_handle(name) == handle:
                require_no_path_escape(name)
                path = routines_dir / name
                reject_symlink_path(path, "routine manifest")
                return path
    finally:
        os.close(routines_fd)
    raise ValidationError("routine_handle does not exist")


def _routine_form_approvals(value: str) -> set:
    if not value.strip():
        return set()
    tokens = [token for token in re.split(r"[\s,]+", value.strip()) if token]
    return validate_approval_tokens(tokens)


def resolve_live_step_handle(run_dir: Path, handle: str) -> str:
    if not _valid_step_handle(handle):
        raise ValidationError("step_handle is invalid")
    reject_symlink_path(run_dir, "run_dir")
    if not run_dir.exists():
        raise ValidationError("run_dir does not exist")
    if not run_dir.is_dir():
        raise ValidationError("run_dir is not a directory")
    matches = []
    for step_id in _live_control_step_ids(run_dir):
        if step_handle(step_id) == handle:
            matches.append(step_id)
    unique = sorted(set(matches))
    if len(unique) > 1:
        raise ValidationError("step_handle is ambiguous")
    if not unique:
        raise ValidationError("step_handle does not exist")
    return unique[0]


def _valid_step_handle(handle: str) -> bool:
    return (
        isinstance(handle, str)
        and len(handle) == len(STEP_HANDLE_PREFIX) + 20
        and handle.startswith(STEP_HANDLE_PREFIX)
        and all(char in "0123456789abcdef" for char in handle[len(STEP_HANDLE_PREFIX) :])
    )


def _live_control_step_ids(run_dir: Path) -> list:
    ids = []
    workflow = _read_live_control_json(run_dir / "workflow.json", "workflow metadata")
    steps = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("id"), str):
            ids.append(step["id"])
    state = _read_live_control_json(run_dir / "state.json", "run state")
    state_steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    ids.extend(step_id for step_id in state_steps if isinstance(step_id, str))
    return ids


def _read_live_control_json(path: Path, label: str) -> Dict:
    try:
        text = read_regular_text_file_no_follow(path, label, max_bytes=LIVE_CONTROL_METADATA_MAX_BYTES)
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValidationError("%s must be valid JSON" % label)
    return data if isinstance(data, dict) else {}


def _resolve_control_step_id(run_dir: Path, step_id: str, step_handle_value: str) -> str:
    if step_handle_value:
        return resolve_live_step_handle(run_dir, step_handle_value)
    if step_id:
        return step_id
    raise ValidationError("missing form field: step_id")


def _open_child_dir_no_follow(parent_fd: int, name: str, label: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise ValidationError("%s does not exist: %s" % (label, name))
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, name, exc.__class__.__name__))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError("%s must be a directory: %s" % (label, name))
        return fd
    except Exception:
        os.close(fd)
        raise


def _private_transcript_path(
    run_handle_value: str,
    step_handle_value: str,
    transcript_id: str,
) -> str:
    if (
        not isinstance(run_handle_value, str)
        or len(run_handle_value) != 24
        or not run_handle_value.startswith("run-")
        or any(char not in "0123456789abcdef" for char in run_handle_value[4:])
    ):
        raise ValidationError("private transcript run_handle is invalid")
    if not _valid_step_handle(step_handle_value):
        raise ValidationError("private transcript step_handle is invalid")
    if (
        not isinstance(transcript_id, str)
        or len(transcript_id) != 35
        or not transcript_id.startswith("transcript-")
        or any(char not in "0123456789abcdef" for char in transcript_id[11:])
    ):
        raise ValidationError("private transcript id is invalid")
    return "%s%s/%s/%s" % (
        PRIVATE_TRANSCRIPT_PATH_PREFIX,
        run_handle_value,
        step_handle_value,
        transcript_id,
    )


def _parse_private_transcript_path(path: str) -> Tuple[str, str, str]:
    if not isinstance(path, str) or not path.startswith(PRIVATE_TRANSCRIPT_PATH_PREFIX):
        raise ValidationError("private transcript path is invalid")
    raw_parts = path[len(PRIVATE_TRANSCRIPT_PATH_PREFIX) :].split("/")
    if len(raw_parts) != 3 or any(not value for value in raw_parts):
        raise ValidationError("private transcript path is invalid")
    values = tuple(unquote(value) for value in raw_parts)
    canonical = _private_transcript_path(*values)
    if path != canonical:
        raise ValidationError("private transcript path is not canonical")
    return values


def _private_transcript_cookie_name(transcript_id: str) -> str:
    _private_transcript_path("run-" + "0" * 20, "step-" + "0" * 20, transcript_id)
    return "cc_team_%s" % transcript_id[11:]


def _private_transcript_grant_value(
    grant_secret: bytes,
    run_handle_value: str,
    step_handle_value: str,
    transcript_id: str,
    *,
    now: Optional[int] = None,
) -> str:
    _private_transcript_path(run_handle_value, step_handle_value, transcript_id)
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + PRIVATE_TRANSCRIPT_COOKIE_SECONDS
    material = "\0".join(
        [str(expires_at), run_handle_value, step_handle_value, transcript_id]
    ).encode("utf-8")
    if not isinstance(grant_secret, bytes) or len(grant_secret) < 32:
        raise ValidationError("private transcript grant secret is invalid")
    key = hashlib.sha256(
        b"codex-conductor-private-transcript\0" + grant_secret
    ).digest()
    signature = hmac.new(key, material, hashlib.sha256).hexdigest()
    return "%d.%s" % (expires_at, signature)


def _private_transcript_set_cookie(
    grant_secret: bytes,
    run_handle_value: str,
    step_handle_value: str,
    transcript_id: str,
    *,
    now: Optional[int] = None,
) -> str:
    path = _private_transcript_path(
        run_handle_value,
        step_handle_value,
        transcript_id,
    )
    return "%s=%s; Path=%s; Max-Age=%d; HttpOnly; SameSite=Strict" % (
        _private_transcript_cookie_name(transcript_id),
        _private_transcript_grant_value(
            grant_secret,
            run_handle_value,
            step_handle_value,
            transcript_id,
            now=now,
        ),
        path,
        PRIVATE_TRANSCRIPT_COOKIE_SECONDS,
    )


def _validate_private_transcript_cookie(
    headers,
    grant_secret: bytes,
    run_handle_value: str,
    step_handle_value: str,
    transcript_id: str,
    *,
    now: Optional[int] = None,
) -> None:
    raw_cookie = headers.get("Cookie", "")
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except CookieError:
        raise ValidationError("private transcript read grant is invalid or expired")
    morsel = cookie.get(_private_transcript_cookie_name(transcript_id))
    value = morsel.value if morsel is not None else ""
    parts = value.split(".")
    if len(parts) != 2 or not parts[0].isdigit() or not re.fullmatch(r"[0-9a-f]{64}", parts[1]):
        raise ValidationError("private transcript read grant is invalid or expired")
    current = int(time.time()) if now is None else int(now)
    expires_at = int(parts[0])
    if (
        expires_at < current - PRIVATE_TRANSCRIPT_CLOCK_SKEW_SECONDS
        or expires_at
        > current
        + PRIVATE_TRANSCRIPT_COOKIE_SECONDS
        + PRIVATE_TRANSCRIPT_CLOCK_SKEW_SECONDS
    ):
        raise ValidationError("private transcript read grant is invalid or expired")
    material = "\0".join(
        [parts[0], run_handle_value, step_handle_value, transcript_id]
    ).encode("utf-8")
    if not isinstance(grant_secret, bytes) or len(grant_secret) < 32:
        raise ValidationError("private transcript grant secret is invalid")
    key = hashlib.sha256(
        b"codex-conductor-private-transcript\0" + grant_secret
    ).digest()
    expected = hmac.new(key, material, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[1], expected):
        raise ValidationError("private transcript read grant is invalid or expired")


def _resolve_private_transcript_view(
    runs_dir: Path,
    run_handle_value: str,
    step_handle_value: str,
    transcript_id: str,
) -> Tuple[Path, str, Dict]:
    _private_transcript_path(run_handle_value, step_handle_value, transcript_id)
    run_dir = resolve_live_run_handle(runs_dir, run_handle_value)
    step_id = resolve_live_step_handle(run_dir, step_handle_value)
    try:
        view = read_team_transcript_view(run_dir, step_id, transcript_id)
    except FileNotFoundError:
        raise ValidationError("private teammate transcript is unavailable")
    return run_dir, step_id, view


def _handler_factory(
    runs_dir: Path,
    benchmarks_dir: Path,
    max_runs: int,
    max_reports: int,
    max_text_bytes: int,
    title: str,
    control_token: str,
    goals_dir: Optional[Path] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    routines_dir: Optional[Path] = None,
    generated_dir: Optional[Path] = None,
    auto_dir: Optional[Path] = None,
    max_auto: int = DEFAULT_MAX_AUTO,
):
    resolved_goals_dir = goals_dir or (Path(runs_dir).parent / "goals")
    resolved_routines_dir = routines_dir or (Path(runs_dir).parent / "routines")
    resolved_generated_dir = generated_dir or (Path(runs_dir).parent / "generated")
    resolved_auto_dir = auto_dir or (Path(resolved_generated_dir) / "auto")
    private_transcript_grant_secret = secrets.token_bytes(32)

    class LiveHandler(BaseHTTPRequestHandler):
        server_version = "ConductorLive/1"

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    query = parse_qs(parsed.query)
                    payload = build_live_payload(
                        runs_dir,
                        benchmarks_dir,
                        max_runs=max_runs,
                        max_reports=max_reports,
                        max_text_bytes=max_text_bytes,
                        goals_dir=resolved_goals_dir,
                        max_goals=max_goals,
                        routines_dir=resolved_routines_dir,
                        generated_dir=resolved_generated_dir,
                        auto_dir=resolved_auto_dir,
                        max_auto=max_auto,
                    )
                    html = render_live_dashboard(
                        payload,
                        title=title,
                        message=_first(query.get("message", [])),
                    )
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                if parsed.path.startswith(PRIVATE_TRANSCRIPT_PATH_PREFIX):
                    run_handle_value, step_handle_value, transcript_id = (
                        _parse_private_transcript_path(parsed.path)
                    )
                    _validate_private_transcript_cookie(
                        self.headers,
                        private_transcript_grant_secret,
                        run_handle_value,
                        step_handle_value,
                        transcript_id,
                    )
                    _run_dir, _step_id, view = _resolve_private_transcript_view(
                        runs_dir,
                        run_handle_value,
                        step_handle_value,
                        transcript_id,
                    )
                    query = parse_qs(parsed.query)
                    html = render_private_team_transcript(
                        view,
                        run_handle_value,
                        step_handle_value,
                        title=title,
                        message=_first(query.get("message", [])),
                        follow=_first(query.get("follow", [])) != "0",
                    )
                    self._send(
                        200,
                        "text/html; charset=utf-8",
                        html,
                        extra_headers={
                            "Set-Cookie": _private_transcript_set_cookie(
                                private_transcript_grant_secret,
                                run_handle_value,
                                step_handle_value,
                                transcript_id,
                            )
                        },
                    )
                    return
                if parsed.path == "/api/state":
                    payload = build_live_payload(
                        runs_dir,
                        benchmarks_dir,
                        max_runs=max_runs,
                        max_reports=max_reports,
                        max_text_bytes=max_text_bytes,
                        goals_dir=resolved_goals_dir,
                        max_goals=max_goals,
                        routines_dir=resolved_routines_dir,
                        generated_dir=resolved_generated_dir,
                        auto_dir=resolved_auto_dir,
                        max_auto=max_auto,
                    )
                    self._send_json(200, payload)
                    return
                if parsed.path.startswith("/api/goal/"):
                    handle = unquote(parsed.path[len("/api/goal/") :])
                    detail = collect_goal_detail(resolved_goals_dir, handle)
                    self._send_json(200, detail)
                    return
                if parsed.path.startswith("/api/run/"):
                    handle = unquote(parsed.path[len("/api/run/") :])
                    run_dir = resolve_live_run_handle(runs_dir, handle)
                    detail = collect_run_detail(runs_dir, run_dir.name, max_text_bytes=max_text_bytes)
                    detail["run_handle"] = run_handle(run_dir.name)
                    self._send_json(200, public_run_detail(detail))
                    return
                if parsed.path.startswith("/goal/"):
                    handle = unquote(parsed.path[len("/goal/") :])
                    query = parse_qs(parsed.query)
                    detail = collect_goal_detail(resolved_goals_dir, handle)
                    html = render_live_goal_detail(
                        detail,
                        title=title,
                        message=_first(query.get("message", [])),
                    )
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                if parsed.path.startswith("/run/") and parsed.path.endswith("/workflow.json"):
                    handle = unquote(parsed.path[len("/run/") : -len("/workflow.json")])
                    run_dir = resolve_live_run_handle(runs_dir, handle)
                    workflow_text = export_run_workflow_text(run_dir)
                    self._send(
                        200,
                        "application/json; charset=utf-8",
                        workflow_text,
                        extra_headers={
                            "Content-Disposition": 'attachment; filename="workflow-%s.json"' % handle,
                        },
                    )
                    return
                if parsed.path.startswith("/run/"):
                    handle = unquote(parsed.path[len("/run/") :])
                    query = parse_qs(parsed.query)
                    run_dir = resolve_live_run_handle(runs_dir, handle)
                    detail = collect_run_detail(runs_dir, run_dir.name, max_text_bytes=max_text_bytes)
                    detail["run_handle"] = run_handle(run_dir.name)
                    html = render_live_run_detail(
                        detail,
                        title=title,
                        message=_first(query.get("message", [])),
                    )
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                self._send(404, "text/plain; charset=utf-8", "not found\n")
            except Exception as exc:  # noqa: BLE001 - HTTP boundary turns errors into responses.
                self._send_error(exc)

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            if parsed.path not in {"/control", "/private/team-transcript"}:
                self._send(404, "text/plain; charset=utf-8", "not found\n")
                return
            try:
                _validate_post_origin(self.headers)
                form = _parse_control_form(self.headers.get("Content-Length", "0"), self.rfile)
                if parsed.path == "/private/team-transcript":
                    token = _field(form, "token")
                    if not secrets.compare_digest(token, control_token):
                        raise ValidationError("invalid live control token")
                    run_handle_value = _field(form, "run_handle")
                    step_handle_value = _field(form, "step_handle")
                    transcript_id = _field(form, "transcript_id")
                    _resolve_private_transcript_view(
                        runs_dir,
                        run_handle_value,
                        step_handle_value,
                        transcript_id,
                    )
                    location = _private_transcript_path(
                        run_handle_value,
                        step_handle_value,
                        transcript_id,
                    )
                    self._redirect(
                        location,
                        extra_headers={
                            "Set-Cookie": _private_transcript_set_cookie(
                                private_transcript_grant_secret,
                                run_handle_value,
                                step_handle_value,
                                transcript_id,
                            )
                        },
                    )
                    return
                result = apply_live_control(
                    runs_dir,
                    form,
                    control_token,
                    routines_dir=resolved_routines_dir,
                )
                message = _control_redirect_message(result)
                return_to = result.get("return_to") or "/"
                separator = "&" if "?" in return_to else "?"
                self._redirect("%s%smessage=%s" % (return_to, separator, quote(message)))
            except Exception as exc:  # noqa: BLE001 - HTTP boundary turns errors into responses.
                self._send_error(exc)

        def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API.
            return

        def _send_json(self, status: int, data: Dict) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps(_public_value(data), sort_keys=True) + "\n")

        def _send_error(self, exc: Exception) -> None:
            status = 400 if isinstance(exc, ValidationError) else 500
            self._send_json(status, {"error": _public_error_message(exc), "type": exc.__class__.__name__})

        def _redirect(
            self,
            location: str,
            extra_headers: Optional[Dict[str, str]] = None,
        ) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self._send_security_headers()
            self.end_headers()

        def _send(self, status: int, content_type: str, text: str, extra_headers: Optional[Dict[str, str]] = None) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_security_headers(self) -> None:
            self.send_header("Content-Security-Policy", LIVE_CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

    return LiveHandler


_PATH_LIKE = re.compile(r"(?P<prefix>(?:^|[\s:]))(?:/|\.{1,2}/)[^\s,;]+")
RUN_DETAIL_API_SCHEMA = "conductor.run_detail.v1"
RUN_DETAIL_HOOK_EVENT_LIMIT = 100


def _public_error_message(exc: Exception) -> str:
    message = redact_text(str(exc))
    return _PATH_LIKE.sub(lambda match: "%s<path>" % match.group("prefix"), message)


def public_run_detail(detail: Dict) -> Dict:
    hook_events = detail.get("hook_events") if isinstance(detail.get("hook_events"), list) else []
    public_hooks = hook_events[-RUN_DETAIL_HOOK_EVENT_LIMIT:]
    payload = {
        "schema": RUN_DETAIL_API_SCHEMA,
        "id": detail.get("id"),
        "run_handle": detail.get("run_handle"),
        "status": detail.get("status"),
        "workflow": detail.get("workflow"),
        "state_error": detail.get("state_error"),
        "workflow_error": detail.get("workflow_error"),
        "started_at_utc": detail.get("started_at_utc"),
        "finished_at_utc": detail.get("finished_at_utc"),
        "duration_ms": detail.get("duration_ms"),
        "runner": detail.get("runner") if isinstance(detail.get("runner"), dict) else {},
        "background": detail.get("background") if isinstance(detail.get("background"), dict) else {},
        "launch": detail.get("launch") if isinstance(detail.get("launch"), dict) else {},
        "usage": detail.get("usage") if isinstance(detail.get("usage"), dict) else {},
        "approvals": detail.get("approvals") if isinstance(detail.get("approvals"), dict) else {},
        "runtime": detail.get("runtime") if isinstance(detail.get("runtime"), dict) else {},
        "diagnostics": detail.get("diagnostics") if isinstance(detail.get("diagnostics"), dict) else {},
        "phases": detail.get("phases") if isinstance(detail.get("phases"), list) else [],
        "steps": detail.get("steps") if isinstance(detail.get("steps"), list) else [],
        "agent_teams": detail.get("agent_teams") if isinstance(detail.get("agent_teams"), list) else [],
        "agent_maps": _without_excerpt_text(detail.get("agent_maps") if isinstance(detail.get("agent_maps"), list) else []),
        "hook_events": public_hooks,
        "hook_event_count": len(hook_events),
        "hook_event_display_count": len(public_hooks),
        "hook_events_truncated": len(hook_events) > len(public_hooks),
        "session_trace_events": detail.get("session_trace_events") if isinstance(detail.get("session_trace_events"), list) else [],
        "session_trace_summary": detail.get("session_trace_summary") if isinstance(detail.get("session_trace_summary"), dict) else {},
        "session_trace_count": detail.get("session_trace_count"),
        "session_trace_counts": detail.get("session_trace_counts") if isinstance(detail.get("session_trace_counts"), dict) else {},
        "session_trace_status_counts": detail.get("session_trace_status_counts") if isinstance(detail.get("session_trace_status_counts"), dict) else {},
        "session_trace_display_count": detail.get("session_trace_display_count"),
        "standard_files": _without_excerpt_text(detail.get("standard_files") if isinstance(detail.get("standard_files"), list) else []),
        "artifacts": _without_excerpt_text(detail.get("artifacts") if isinstance(detail.get("artifacts"), dict) else {}),
        "logs": _without_excerpt_text(detail.get("logs") if isinstance(detail.get("logs"), dict) else {}),
    }
    return _public_value(payload)


def _public_routine(routine: Dict) -> Dict:
    fields = {
        "routine_handle",
        "name",
        "schedule",
        "target_kind",
        "workflow",
        "evidence_status",
        "no_process_started",
        "policy",
        "created_at_utc",
        "paused",
        "control_updated_at_utc",
        "control_error",
        "last_status",
        "last_trigger",
        "last_finished_at_utc",
        "executions",
        "history_count",
        "state_error",
        "error",
    }
    return _public_value({key: routine[key] for key in fields if key in routine})


def _without_excerpt_text(value):
    if isinstance(value, list):
        return [_without_excerpt_text(item) for item in value]
    if isinstance(value, tuple):
        return [_without_excerpt_text(item) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key == "text":
                continue
            cleaned[key] = _without_excerpt_text(item)
        if "present" in value and "truncated" in value and "text" in value:
            cleaned["text_omitted"] = bool(value.get("text"))
        return cleaned
    return value


def _render_runs_table(runs) -> str:
    if not runs:
        return '<p class="muted">No runs found.</p>'
    rows = []
    for run in runs:
        steps = run.get("steps") if isinstance(run.get("steps"), list) else []
        rows.append(
            "<tr>"
            "<td><a href=\"/run/%s\"><code>%s</code></a><div class=\"muted\">%s</div></td>"
            "<td class=\"status-%s\">%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td class=\"controls\">%s</td>"
            "</tr>"
            % (
                quote(_string(run.get("run_handle")), safe=""),
                escape(_string(run.get("id"))),
                escape(_string(run.get("started_at_utc") or run.get("mtime_utc"))),
                escape(_status_class(run.get("status"))),
                escape(_string(run.get("status"))),
                _render_run_timing(run),
                escape(_string(run.get("workflow"))),
                _render_phase_step_summary(run, steps),
                _render_final_report_summary(run.get("final_report_excerpt")),
                _render_controls(run, steps),
            )
        )
    return (
        "<table><thead><tr><th>Run</th><th>Status</th><th>Timing</th><th>Workflow</th><th>Steps</th><th>Final Report</th><th>Controls</th></tr></thead>"
        "<tbody>%s</tbody></table>" % "".join(rows)
    )


def _render_auto_orchestrations_table(auto_runs) -> str:
    if not auto_runs:
        return '<p class="muted">No auto receipts found.</p>'
    rows = []
    for item in auto_runs:
        child = item.get("child") if isinstance(item.get("child"), dict) else {}
        child_text = "%s / %s" % (
            _string(child.get("resolution") or "unknown"),
            _string(item.get("liveness") or "unknown"),
        )
        if child.get("kind"):
            child_text += " / %s" % _string(child.get("kind"))
        if child.get("error"):
            child_text += " / %s" % _string(child.get("error"))
        bounds_text = "%s worker(s) / %s iteration(s)" % (
            _string(item.get("max_workers") or 0),
            _string(item.get("max_iterations") or 0),
        )
        if item.get("native_agent_max_threads") is not None:
            bounds_text += " / native max %s children, depth 1, %s shared weighted rollout tokens" % (
                _string(item.get("native_agent_max_threads")),
                _string(item.get("native_agent_max_total_tokens") or "unknown"),
            )
        rows.append(
            "<tr><td><code>%s</code><div class=\"muted\">%s</div></td>"
            "<td><span class=\"status-%s\">%s</span></td>"
            "<td>%s<div class=\"muted\">%s verifier</div></td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(item.get("name") or "unknown")),
                escape(_string(item.get("updated_at_utc") or item.get("mtime_utc") or "unknown")),
                escape(_status_class(_string(item.get("effective_status") or "unknown"))),
                escape(_string(item.get("effective_status") or "unknown")),
                escape(_string(item.get("route") or "unknown")),
                escape(_string(item.get("verifier_kind") or "none")),
                escape(_string(item.get("execution_mode") or "unknown")),
                escape(child_text),
                escape(bounds_text),
            )
        )
    return (
        "<table><thead><tr><th>Receipt</th><th>Effective</th><th>Route</th><th>Mode</th>"
        "<th>Child</th><th>Bounds</th></tr></thead><tbody>%s</tbody></table>" % "".join(rows)
    )


def _render_approval_queue(runs) -> str:
    rows = []
    for run in runs:
        approvals = run.get("approvals") if isinstance(run.get("approvals"), dict) else {}
        pending_count = approvals.get("pending_count")
        if not isinstance(pending_count, int) or isinstance(pending_count, bool) or pending_count < 1:
            continue
        handle = _string(run.get("run_handle") or run_handle(str(run.get("id") or "")))
        guidance = _string(approvals.get("guidance") or "Resume from the CLI with the required approval token.")
        rows.append(
            "<tr>"
            '<td><a href="/run/%s"><code>%s</code></a></td>'
            '<td class="status-blocked">%s</td>'
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                quote(handle, safe=""),
                escape(_string(run.get("id"))),
                escape(_string(pending_count)),
                escape(_string(run.get("workflow"))),
                escape(guidance),
            )
        )
    if not rows:
        return '<p class="muted">No manual-gate approvals are pending.</p>'
    return (
        "<table><thead><tr><th>Run</th><th>Pending gates</th><th>Workflow</th><th>Guidance</th></tr></thead>"
        "<tbody>%s</tbody></table>" % "".join(rows)
    )


def _render_goals_table(goals) -> str:
    if not goals:
        return '<p class="muted">No goals found.</p>'
    rows = []
    for goal in goals:
        handle = _string(goal.get("goal_handle"))
        goal_label = escape(_string(goal.get("goal_id") or goal.get("name")))
        goal_link = (
            '<a href="/goal/%s"><code>%s</code></a>' % (quote(handle, safe=""), goal_label)
            if handle
            else "<code>%s</code>" % goal_label
        )
        latest = goal.get("last_iteration") if isinstance(goal.get("last_iteration"), dict) else {}
        latest_status = _string(latest.get("status") or goal.get("last_check_status") or "")
        latest_label = latest_status or "none"
        check_status = _string(goal.get("last_check_status"))
        check_html = ""
        if check_status:
            returncode = goal.get("last_check_returncode")
            suffix = " rc %s" % returncode if isinstance(returncode, int) and not isinstance(returncode, bool) else ""
            check_html = '<div class="muted">Verifier: %s%s</div>' % (escape(check_status), escape(suffix))
        background = goal.get("background") if isinstance(goal.get("background"), dict) else {}
        background_html = ""
        if background:
            background_html = '<div class="muted">Worker: %s</div>' % escape(_background_goal_label(background))
        planner_session = goal.get("planner_session") if isinstance(goal.get("planner_session"), dict) else {}
        planner_html = ""
        if goal.get("planning_mode") == "deterministic-direct":
            planner_html = '<div class="muted">Planning: %s</div>' % escape(_goal_planning_label(goal))
        elif goal.get("planning_mode") == "progressive":
            planner_html = '<div class="muted">Planning: %s</div>' % escape(_goal_planning_label(goal))
            if planner_session:
                planner_html += '<div class="muted">Planner: %s</div>' % escape(_planner_session_label(planner_session))
        elif planner_session:
            planner_html = '<div class="muted">Planner: %s</div>' % escape(_planner_session_label(planner_session))
        rows.append(
            "<tr>"
            "<td>%s<div class=\"muted\">Updated %s</div>%s</td>"
            "<td class=\"status-%s\">%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td><span class=\"status-%s\">%s</span>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                goal_link,
                escape(_string(goal.get("updated_at_utc") or goal.get("mtime_utc"))),
                _render_goal_error(goal) + background_html + planner_html,
                escape(_status_class(goal.get("status"))),
                escape(_string(goal.get("status"))),
                escape(_string(goal.get("workflow"))),
                escape(_goal_iterations_label(goal)),
                escape(_status_class(latest_label)),
                escape(latest_label),
                check_html,
                _render_goal_action(goal),
            )
        )
    return (
        "<table><thead><tr><th>Goal</th><th>Status</th><th>Workflow</th><th>Iterations</th><th>Latest</th><th>Operator Action</th></tr></thead>"
        "<tbody>%s</tbody></table>" % "".join(rows)
    )


def _render_routines_table(routines: List[Dict]) -> str:
    if not routines:
        return '<p class="muted">No routine manifests found.</p>'
    rows = []
    for routine in routines:
        control = "paused" if routine.get("paused") else "active"
        if routine.get("error") or routine.get("control_error") or routine.get("state_error"):
            control = "needs-attention"
        rows.append(
            "<tr><td>%s</td><td><span class=\"status status-%s\">%s</span></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(routine.get("name") or "unknown")),
                escape(_status_class(control)),
                escape(control),
                escape(_string(routine.get("schedule") or "unknown")),
                escape(_string(routine.get("workflow") or "unknown")),
                escape(_string(routine.get("last_status") or "never")),
                escape("%s runs / %s history" % (routine.get("executions") or 0, routine.get("history_count") or 0)),
                _render_routine_controls(routine),
            )
        )
    return (
        "<table><thead><tr><th>Routine</th><th>Control</th><th>Schedule</th><th>Workflow</th>"
        "<th>Latest</th><th>History</th><th>Actions</th></tr></thead><tbody>%s</tbody></table>" % "".join(rows)
    )


def _render_routine_controls(routine: Dict) -> str:
    handle = escape(_string(routine.get("routine_handle")))
    if not handle or routine.get("error") or routine.get("control_error") or routine.get("state_error"):
        return '<span class="muted">Unavailable</span>'
    control_action = "resume-routine" if routine.get("paused") else "pause-routine"
    control_label = "Resume" if routine.get("paused") else "Pause"
    control_form = (
        '<form method="post" action="/control">'
        '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
        '<input type="hidden" name="routine_handle" value="%s">'
        '<input type="hidden" name="return_to" value="/">'
        '<input type="text" name="reason" maxlength="%d" placeholder="reason">'
        '<button name="action" value="%s">%s</button>'
        '</form>'
        % (handle, MAX_REASON_CHARS, control_action, control_label)
    )
    run_form = (
        '<form method="post" action="/control">'
        '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
        '<input type="hidden" name="routine_handle" value="%s">'
        '<input type="hidden" name="return_to" value="/">'
        '<input type="password" name="approvals" autocomplete="off" placeholder="additional approvals">'
        '<button name="action" value="run-routine-now">Run now</button>'
        '</form>'
        % handle
    )
    return '<div class="controls">%s%s</div>' % (control_form, run_form)


def _render_routine_supervisor(supervisor: Dict) -> str:
    if not isinstance(supervisor, dict) or not supervisor:
        return '<p class="muted">No detached supervisor launches found.</p>'
    values = [
        ("Lifecycle", supervisor.get("status") or "unknown"),
        ("Liveness", supervisor.get("liveness") or "unknown"),
        ("Scheduler", supervisor.get("supervisor_status") or "unknown"),
        ("Worker", "pid %s" % (supervisor.get("pid") or "unknown")),
        ("Cycles", supervisor.get("cycles_completed") or 0),
        (
            "Results",
            "%s completed / %s blocked / %s failed"
            % (
                supervisor.get("completed_count") or 0,
                supervisor.get("blocked_count") or 0,
                supervisor.get("failed_count") or 0,
            ),
        ),
        ("Updated", supervisor.get("updated_at_utc") or "unknown"),
        ("Metadata", supervisor.get("metadata") or "unknown"),
    ]
    return "<table><tbody>%s</tbody></table>" % "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (escape(_string(label)), escape(_string(value)))
        for label, value in values
    )


def _render_routine_service(service: Dict) -> str:
    if not isinstance(service, dict) or service.get("effective_status") == "not-installed":
        return '<p class="muted">No restart-persistent routine service installed.</p>'
    state = service.get("state") if isinstance(service.get("state"), dict) else {}
    values = [
        ("Status", service.get("effective_status") or "unknown"),
        ("Liveness", service.get("liveness") or "unknown"),
        ("Platform", service.get("platform") or "unknown"),
        ("Bindings", "valid" if service.get("bindings_valid") else "invalid"),
        (
            "Descriptor",
            "valid" if service.get("descriptor_valid") else ("invalid" if service.get("descriptor_present") else "missing"),
        ),
        (
            "Staged descriptor",
            "valid"
            if service.get("staged_descriptor_valid")
            else ("invalid" if service.get("staged_descriptor_present") else "absent"),
        ),
        ("Expiration", service.get("expires_at_utc") or "unknown"),
        ("Cycles", state.get("cycles_completed") or 0),
        (
            "Results",
            "%s completed / %s failed"
            % (state.get("results_completed") or 0, state.get("results_failed") or 0),
        ),
        ("Heartbeat", state.get("heartbeat_at_utc") or "not recorded"),
    ]
    if service.get("error"):
        values.append(("Attention", service.get("error")))
    return "<table><tbody>%s</tbody></table>" % "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (escape(_string(label)), escape(_string(value)))
        for label, value in values
    )


def _render_goal_error(goal) -> str:
    error = _string(goal.get("error"))
    if not error:
        return ""
    return '<div class="muted">%s</div>' % escape(error)


def _goal_iterations_label(goal) -> str:
    iterations = goal.get("iterations") if isinstance(goal.get("iterations"), int) and not isinstance(goal.get("iterations"), bool) else 0
    max_iterations = goal.get("max_iterations")
    if isinstance(max_iterations, int) and not isinstance(max_iterations, bool):
        return "%s/%s iterations" % (iterations, max_iterations)
    return "%s iterations" % iterations


def _render_goal_action(goal) -> str:
    status = _string(goal.get("status"))
    background = goal.get("background") if isinstance(goal.get("background"), dict) else {}
    if background.get("liveness") == "alive":
        return '<span class="muted">Detached worker active; use status-background-model-goal or wait-background-model-goal.</span>'
    if status == "completed":
        return '<span class="muted">No action needed.</span>'
    if status == "invalid":
        return '<span class="muted">Inspect or replace the invalid goal artifact.</span>'
    if status == "max_iterations_exhausted":
        return '<span class="muted">Resume with a higher --max-iterations cap if more attempts are warranted.</span>'
    if goal.get("resume_available") is True:
        command = "run-model-goal" if _string(goal.get("workflow")) == "adaptive-model-workflow" else "run-goal"
        return '<span class="muted">Resume from CLI with %s --resume-goal.</span>' % escape(command)
    return '<span class="muted">Inspect goal artifact.</span>'


def _render_live_goal_detail_summary(detail) -> str:
    background = detail.get("background") if isinstance(detail.get("background"), dict) else {}
    rows = [
        ("Status", '<span class="status-%s">%s</span>' % (escape(_status_class(detail.get("status"))), escape(_string(detail.get("status"))))),
        ("Workflow", escape(_string(detail.get("workflow")))),
        ("Updated", escape(_string(detail.get("updated_at_utc") or detail.get("mtime_utc")))),
        ("Iterations", escape(_goal_iterations_label(detail))),
        ("Planning", escape(_goal_planning_label(detail))),
        (
            "Planner session",
            escape(
                "not used by deterministic direct planning"
                if detail.get("planning_mode") == "deterministic-direct"
                else _planner_session_label(detail.get("planner_session"))
            ),
        ),
        ("Verifier policy", escape(_goal_verifier_policy_label(detail.get("verifier_policy")))),
        ("Latest verifier", _render_live_goal_latest(detail)),
        ("Operator action", _render_goal_action(detail)),
        ("Verifier output", escape(_string(detail.get("output_policy") or "verifier stdout/stderr excerpts omitted"))),
    ]
    if background:
        rows.insert(4, ("Detached worker", escape(_background_goal_label(background))))
    if _string(detail.get("error")):
        rows.append(("Goal artifact", '<span class="warn">%s</span>' % escape(_string(detail.get("error")))))
    return "<section><h2>Summary</h2><table><tbody>%s</tbody></table></section>" % "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (escape(label), value) for label, value in rows
    )


def _background_goal_label(background: Dict) -> str:
    if not isinstance(background, dict) or not background:
        return "unavailable"
    parts = [
        _string(background.get("status") or "unknown"),
        _string(background.get("liveness") or "unknown"),
    ]
    goal_status = _string(background.get("goal_status"))
    if goal_status:
        parts.append("goal %s" % goal_status)
    pid = background.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parts.append("pid %s" % pid)
    return " / ".join(parts)


def _planner_session_label(raw) -> str:
    session = raw if isinstance(raw, dict) else {}
    if not session:
        return "unavailable"
    mode = _string(session.get("mode") or "unavailable")
    availability = "available" if session.get("available") is True else "unavailable"
    turns = session.get("turns")
    if not isinstance(turns, int) or isinstance(turns, bool):
        turns = 0
    return "%s / %s / %s turns" % (mode, availability, turns)


def _goal_planning_label(goal: Dict) -> str:
    calls = goal.get("planning_provider_calls")
    if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
        calls = 0
    call_text = "%s planner call%s" % (calls, "" if calls == 1 else "s")
    if goal.get("planning_mode") == "deterministic-direct":
        return "deterministic direct / 0 planner calls per iteration"
    if goal.get("planning_mode") == "model-authored":
        return "model-authored workflow per iteration / %s" % call_text
    if goal.get("planning_mode") == "progressive":
        return "direct first / model escalation after verifier failure / %s" % call_text
    return "unknown"


def _goal_verifier_policy_label(raw) -> str:
    policy = raw if isinstance(raw, dict) else {}
    if not policy:
        return "command-only or legacy model verifier"
    return "%s / max %s weighted tokens / %s / %s / %s" % (
        _string(policy.get("effort") or "unknown effort"),
        _string(policy.get("max_tokens") if policy.get("max_tokens") is not None else "unknown"),
        _string(policy.get("token_cap_enforcement") or "unknown enforcement"),
        "isolated" if policy.get("isolated_profile") is True else "standard profile",
        "ephemeral" if policy.get("ephemeral_session") is True else "persistent session",
    )


def _render_live_goal_latest(detail) -> str:
    latest = detail.get("last_iteration") if isinstance(detail.get("last_iteration"), dict) else {}
    if not latest:
        return '<span class="muted">No iterations recorded</span>'
    status = _string(latest.get("status") or detail.get("last_check_status") or "unknown")
    iteration = latest.get("iteration")
    label = "iteration %s" % iteration if iteration is not None else "latest iteration"
    rows = [
        '%s <span class="status-%s">%s</span>'
        % (escape(label), escape(_status_class(status)), escape(status))
    ]
    check_status = _string(detail.get("last_check_status"))
    if check_status:
        returncode = detail.get("last_check_returncode")
        suffix = " rc %s" % returncode if isinstance(returncode, int) and not isinstance(returncode, bool) else ""
        rows.append('<div class="muted">Verifier: %s%s</div>' % (escape(check_status), escape(suffix)))
    return "".join(rows)


def _render_live_goal_detail_iterations(detail) -> str:
    iterations = detail.get("iterations_detail") if isinstance(detail.get("iterations_detail"), list) else []
    if not iterations:
        return '<section><h2>Iterations</h2><p class="muted">No goal iterations recorded.</p></section>'
    rows = ["<section><h2>Iterations</h2>"]
    if detail.get("iterations_truncated"):
        rows.append(
            '<p class="muted">Showing the latest %s of %s iterations.</p>'
            % (escape(_string(detail.get("iteration_display_count"))), escape(_string(detail.get("iterations"))))
        )
    rows.extend(
        [
            "<table>",
            "<thead><tr><th>Iteration</th><th>Status</th><th>Run</th><th>Verifier</th><th>Timing</th><th>Output summary</th></tr></thead>",
            "<tbody>",
        ]
    )
    for iteration in iterations:
        check = iteration.get("check") if isinstance(iteration.get("check"), dict) else {}
        rows.append(
            "<tr>"
            "<td>%s</td>"
            "<td><span class=\"status-%s\">%s</span></td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                escape(_string(iteration.get("iteration") or "unknown")),
                escape(_status_class(iteration.get("status"))),
                escape(_string(iteration.get("status") or "unknown")),
                _render_live_goal_iteration_run(iteration),
                _render_live_goal_check(check),
                _render_live_goal_timing(iteration, check),
                _render_live_goal_output(check),
            )
        )
    rows.extend(["</tbody></table>", "</section>"])
    return "".join(rows)


def _render_live_goal_iteration_run(iteration) -> str:
    rows = []
    run_id = _string(iteration.get("run_id"))
    if run_id:
        rows.append("<code>%s</code>" % escape(run_id))
    run_status = _string(iteration.get("run_status"))
    if run_status:
        rows.append('<div class="muted">run status: %s</div>' % escape(run_status))
    return "".join(rows) or '<span class="muted">unknown</span>'


def _render_live_goal_check(check) -> str:
    status = _string(check.get("status") or "unknown")
    rows = ['<span class="status-%s">%s</span>' % (escape(_status_class(status)), escape(status))]
    kind = _string(check.get("kind"))
    if kind:
        rows.append('<div class="muted">%s verifier</div>' % escape(kind))
    returncode = check.get("returncode")
    if isinstance(returncode, int) and not isinstance(returncode, bool):
        rows.append('<div class="muted">return code %s</div>' % escape(_string(returncode)))
    if check.get("timed_out") is True:
        rows.append('<div class="warn">timed out</div>')
    if kind == "command+model":
        command = check.get("command") if isinstance(check.get("command"), dict) else {}
        model = check.get("model") if isinstance(check.get("model"), dict) else {}
        rows.append(
            '<div class="muted">command %s</div>'
            % escape(_string(command.get("status") or "unknown"))
        )
        rows.append(
            '<div class="muted">model %s</div>'
            % escape(
                _string(
                    model.get("status")
                    or ("skipped" if check.get("short_circuited") else "unknown")
                )
            )
        )
        calls = check.get("model_provider_calls")
        rows.append(
            '<div class="muted">model verifier calls: %s</div>'
            % escape(_string(calls if isinstance(calls, int) and not isinstance(calls, bool) else 0))
        )
    verdict = check.get("verdict") if isinstance(check.get("verdict"), dict) else {}
    confidence = _string(verdict.get("confidence"))
    if confidence:
        rows.append('<div class="muted">%s confidence</div>' % escape(confidence))
    error = _string(check.get("error"))
    if error:
        rows.append('<div class="warn">%s</div>' % escape(error))
    return "".join(rows)


def _render_live_goal_timing(iteration, check) -> str:
    rows = []
    if _string(iteration.get("started_at_utc")):
        rows.append("<div>Run started %s</div>" % escape(_string(iteration.get("started_at_utc"))))
    if _string(iteration.get("finished_at_utc")):
        rows.append("<div>Run finished %s</div>" % escape(_string(iteration.get("finished_at_utc"))))
    if _string(check.get("started_at_utc")):
        rows.append('<div class="muted">Check started %s</div>' % escape(_string(check.get("started_at_utc"))))
    if _string(check.get("finished_at_utc")):
        rows.append('<div class="muted">Check finished %s</div>' % escape(_string(check.get("finished_at_utc"))))
    return "".join(rows) or '<span class="muted">unknown</span>'


def _render_live_goal_output(check) -> str:
    parts = []
    stdout_bytes = check.get("stdout_bytes")
    stderr_bytes = check.get("stderr_bytes")
    if stdout_bytes is not None or stderr_bytes is not None:
        parts.append("%s/%s bytes out/err" % (_string(stdout_bytes or 0), _string(stderr_bytes or 0)))
    if check.get("stdout_truncated") is True or check.get("stderr_truncated") is True:
        parts.append("truncated")
    if check.get("output_excerpts_omitted") is True:
        parts.append("excerpts omitted")
    verdict = check.get("verdict") if isinstance(check.get("verdict"), dict) else {}
    feedback = _string(verdict.get("feedback") or check.get("feedback"))
    evidence = verdict.get("evidence") if isinstance(verdict.get("evidence"), list) else []
    usage = check.get("usage") if isinstance(check.get("usage"), dict) else {}
    if usage.get("total_tokens") is not None:
        parts.append("%s tokens" % _string(usage.get("total_tokens")))
    rows = ['<span class="muted">%s</span>' % escape(" / ".join(parts) if parts else "not recorded")]
    if feedback:
        rows.append('<div>%s</div>' % escape(feedback))
    if evidence:
        rows.append('<div class="muted">%s evidence item(s)</div>' % escape(_string(len(evidence))))
    return "".join(rows)


def _render_reports_table(reports) -> str:
    if not reports:
        return '<p class="muted">No benchmark reports found.</p>'
    rows = []
    for report in reports:
        rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(report.get("name"))),
                escape(_string(report.get("kind"))),
                _render_report_evidence_status(report),
                escape(_string(report.get("suite"))),
                escape(_string(report.get("system"))),
                escape(_string(report.get("result"))),
                _render_report_warnings(report),
            )
        )
    return "<table><thead><tr><th>Report</th><th>Kind</th><th>Evidence</th><th>Suite</th><th>System</th><th>Result</th><th>Warnings</th></tr></thead><tbody>%s</tbody></table>" % "".join(rows)


def _render_report_evidence_status(report) -> str:
    label = _string(report.get("evidence_label") or report.get("evidence_status") or "unknown report")
    status = _string(report.get("evidence_status") or "unknown-report")
    attention = report.get("evidence_attention") is True
    css = "status-failed" if attention else "status-completed"
    note = ""
    if status == "claim-blocked":
        note = "Not eligible for parity or superiority claims."
    elif status == "claim-eligible-report":
        note = "Report declares complete live claim-eligible provenance."
    elif status == "benchmark-evidence":
        note = "Single-system evidence."
    elif attention:
        note = "Review provenance before use."
    note_html = '<div class="muted">%s</div>' % escape(note) if note else ""
    return '<span class="%s">%s</span>%s' % (escape(css), escape(label), note_html)


def _render_report_warnings(report) -> str:
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    if not warnings:
        return '<span class="muted">None</span>'
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % escape(_string(item)) for item in warnings)


def _render_step_summary(steps) -> str:
    if not steps:
        return '<span class="muted">No recorded steps</span>'
    parts = []
    for step in steps[:8]:
        meta = []
        if step.get("kind"):
            meta.append(_string(step.get("kind")))
        duration = _format_duration(step.get("duration_ms"))
        if duration:
            meta.append(duration)
        if step.get("agent_items_total"):
            meta.append("%s item(s), %s cached" % (step.get("agent_items_total"), step.get("agent_items_cached") or 0))
        meta_html = ""
        if meta:
            meta_html = ' <span class="muted">%s</span>' % escape(" / ".join(meta))
        parts.append(
            '<div><code>%s</code> <span class="status-%s">%s</span>%s</div>'
            % (
                escape(_string(step.get("id"))),
                escape(_status_class(step.get("status"))),
                escape(_string(step.get("status"))),
                meta_html,
            )
        )
    if len(steps) > 8:
        parts.append('<div class="muted">+%d more</div>' % (len(steps) - 8))
    return "".join(parts)


def _render_phase_step_summary(run, steps) -> str:
    phases = run.get("phases") if isinstance(run.get("phases"), list) else []
    phase_html = _render_live_phase_summary(phases)
    step_html = _render_step_summary(steps)
    if phase_html:
        return phase_html + step_html
    return step_html


def _render_live_phase_summary(phases) -> str:
    if not phases:
        return ""
    counts = {}
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        status = _string(phase.get("status") or "unknown") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join("%s %s" % (count, status) for status, count in sorted(counts.items()))
    rows = ['<details><summary>Phases: %s</summary>' % escape(summary)]
    for phase in phases[:8]:
        if not isinstance(phase, dict):
            continue
        rows.append(
            '<div><code>%s</code> <span class="status-%s">%s</span> <span class="muted">%s steps / %s item(s) / %s</span></div>'
            % (
                escape(_string(phase.get("id") or "default")),
                escape(_status_class(phase.get("status"))),
                escape(_string(phase.get("status") or "unknown")),
                escape(_string(phase.get("steps_total") or 0)),
                escape(_string(phase.get("agent_items_total") or 0)),
                escape(_usage_label(phase.get("usage") if isinstance(phase.get("usage"), dict) else {})),
            )
        )
    if len(phases) > 8:
        rows.append('<div class="muted">+%d more phase(s)</div>' % (len(phases) - 8))
    rows.append("</details>")
    return "".join(rows)


def _render_run_timing(run) -> str:
    lines = []
    duration = _format_duration(run.get("duration_ms"))
    if duration:
        lines.append("<div>%s</div>" % escape(duration))
    else:
        lines.append('<div class="muted">Duration unavailable</div>')
    if run.get("finished_at_utc"):
        lines.append('<div class="muted">Finished <code>%s</code></div>' % escape(_string(run.get("finished_at_utc"))))
    runner_label = _runner_label(run.get("runner") if isinstance(run.get("runner"), dict) else {})
    if runner_label != "unavailable":
        lines.append('<div class="muted">Runner: %s</div>' % escape(runner_label))
    launch_label = _launch_label(run.get("launch") if isinstance(run.get("launch"), dict) else {})
    if launch_label != "unavailable":
        lines.append('<div class="muted">Launch: %s</div>' % escape(launch_label))
    agent = run.get("agent_metrics") if isinstance(run.get("agent_metrics"), dict) else {}
    if agent.get("agent_invocations") or agent.get("agent_items_total"):
        lines.append(
            '<div class="muted">Agents: %s calls, %s items, %s cached</div>'
            % (
                escape(_string(agent.get("agent_invocations", 0))),
                escape(_string(agent.get("agent_items_total", 0))),
                escape(_string(agent.get("agent_items_cached", 0))),
            )
        )
    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    lines.append('<div class="muted">Tokens/cost: %s</div>' % escape(_usage_label(usage)))
    hook_counts = run.get("hook_counts") if isinstance(run.get("hook_counts"), dict) else {}
    if hook_counts:
        summary = ", ".join("%s %s" % (count, status) for status, count in sorted(hook_counts.items()))
        lines.append('<div class="muted">Hooks: %s</div>' % escape(summary))
    trace_counts = run.get("session_trace_counts") if isinstance(run.get("session_trace_counts"), dict) else {}
    if run.get("session_trace_count"):
        lines.append('<div class="muted">Session traces: %s</div>' % escape(_session_trace_count_label(run.get("session_trace_count"), trace_counts)))
    approvals = run.get("approvals") if isinstance(run.get("approvals"), dict) else {}
    pending_approvals = approvals.get("pending_count")
    if isinstance(pending_approvals, int) and not isinstance(pending_approvals, bool) and pending_approvals > 0:
        lines.append('<div class="status-blocked">Approvals needed: %s manual gate(s)</div>' % escape(_string(pending_approvals)))
    runtime_label = _runtime_summary_label(run.get("runtime") if isinstance(run.get("runtime"), dict) else {})
    if runtime_label:
        lines.append('<div class="muted">Runtime actions: %s</div>' % escape(runtime_label))
    return "".join(lines)


def _runtime_summary_label(runtime: Dict) -> str:
    if not isinstance(runtime, dict) or not runtime.get("available"):
        return ""
    counts = runtime.get("counts") if isinstance(runtime.get("counts"), dict) else {}
    parts = []
    for key, label in [
        ("operator_action", "operator"),
        ("failed_recovery", "recovery"),
        ("ready", "ready"),
        ("waiting", "waiting"),
        ("active", "active"),
    ]:
        value = counts.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            parts.append("%s %s" % (value, label))
    return ", ".join(parts)


def _session_trace_count_label(total, counts) -> str:
    count = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else 0
    summary_parts = []
    if isinstance(counts, dict):
        for provider, provider_count in sorted(counts.items()):
            if not isinstance(provider_count, int) or isinstance(provider_count, bool) or provider_count < 0:
                continue
            summary_parts.append("%s %s" % (provider_count, _string(provider) or "generic"))
    if summary_parts:
        return "%s total (%s)" % (count, ", ".join(summary_parts))
    return "%s total" % count


def _render_controls(run, steps, return_to: str = "") -> str:
    handle = _string(run.get("run_handle") or run_handle(str(run.get("id") or "")))
    forms = [_render_run_controls(handle, return_to)]
    for step in steps:
        step_id = _string(step.get("id"))
        if step.get("kind") == "manual_gate":
            forms.append(_render_manual_gate_guidance(step))
            continue
        forms.append(
            '<form method="post" action="/control">'
            '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
            '<input type="hidden" name="run_handle" value="%s">'
            '<input type="hidden" name="step_handle" value="%s">'
            '%s'
            '<input type="text" name="reason" maxlength="%d" placeholder="reason">'
            '<label><input type="checkbox" name="cascade" value="1"> cascade</label>'
            '<button name="action" value="retry">Retry</button>'
            '<button name="action" value="reset">Reset</button>'
            '<button name="action" value="skip">Skip</button>'
            '</form>'
            % (
                escape(handle),
                escape(_string(step.get("step_handle") or step_handle(step_id))),
                _return_to_input(return_to),
                MAX_REASON_CHARS,
            )
        )
    return "".join(forms)


def _render_manual_gate_guidance(step) -> str:
    approval_id = _string(step.get("approval_id") or "<approval-id>")
    command = _string(step.get("approval_command") or "python3 -m conductor_extras run RUN_WORKFLOW --resume RUN_DIR --approve <approval-id>")
    return (
        '<div class="muted">'
        'Manual gate approval required: <code>%s</code>. '
        'Resume from CLI with <code>%s</code>. '
        'The live control token does not grant approval.'
        '</div>'
        % (escape(approval_id), escape(command))
    )


def _render_run_controls(handle: str, return_to: str = "") -> str:
    return (
        '<form method="post" action="/control">'
        '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
        '<input type="hidden" name="run_handle" value="%s">'
        '%s'
        '<input type="text" name="reason" maxlength="%d" placeholder="run reason">'
        '<button name="action" value="pause-run">Request pause</button>'
        '<button name="action" value="resume-run">Resume</button>'
        '<button name="action" value="stop-run">Request stop</button>'
        '<button name="action" value="terminate-run">Terminate runner</button>'
        '<button name="action" value="recover-run">Recover and retry</button>'
        '<button name="action" value="recover-codex">Resume Codex thread</button>'
        '<button name="action" value="restart-run">Restart</button>'
        '</form>'
        % (
            escape(handle),
            _return_to_input(return_to),
            MAX_REASON_CHARS,
        )
    )


def _render_live_detail_summary(detail: Dict) -> str:
    usage = detail.get("usage") if isinstance(detail.get("usage"), dict) else {}
    launch = detail.get("launch") if isinstance(detail.get("launch"), dict) else {}
    background = detail.get("background") if isinstance(detail.get("background"), dict) else {}
    duration = _format_duration(detail.get("duration_ms")) or "unavailable"
    handle = _string(detail.get("run_handle") or run_handle(str(detail.get("id") or "")))
    lines = [
        "<section>",
        "<h2>Summary</h2>",
        "<table><tbody>",
        "<tr><th>Status</th><td class=\"status-%s\">%s</td></tr>"
        % (escape(_status_class(detail.get("status"))), escape(_string(detail.get("status")))),
        "<tr><th>Workflow</th><td>%s</td></tr>" % escape(_string(detail.get("workflow"))),
        "<tr><th>Started</th><td><code>%s</code></td></tr>" % escape(_string(detail.get("started_at_utc"))),
        "<tr><th>Finished</th><td><code>%s</code></td></tr>" % escape(_string(detail.get("finished_at_utc"))),
        "<tr><th>Duration</th><td>%s</td></tr>" % escape(duration),
        "<tr><th>Runner</th><td>%s</td></tr>"
        % escape(_runner_label(detail.get("runner") if isinstance(detail.get("runner"), dict) else {})),
        "<tr><th>Background</th><td>%s</td></tr>" % escape(_background_label(background)),
        "<tr><th>Launch</th><td>%s</td></tr>" % _render_launch_summary_cell(launch),
        "<tr><th>Tokens/cost</th><td>%s</td></tr>" % escape(_usage_label(usage)),
        '<tr><th>Run detail API</th><td><a href="/api/run/%s">Download run detail JSON</a></td></tr>'
        % quote(handle, safe=""),
        '<tr><th>Workflow export</th><td><a href="/run/%s/workflow.json">Download workflow JSON</a></td></tr>'
        % quote(handle, safe=""),
        "</tbody></table>",
        "</section>",
    ]
    if detail.get("state_error") or detail.get("workflow_error"):
        lines.insert(
            -1,
            '<p class="warn">state.json: %s; workflow.json: %s</p>'
            % (escape(_string(detail.get("state_error"))), escape(_string(detail.get("workflow_error")))),
        )
    return "".join(lines)


def _render_live_detail_diagnostics(diagnostics: Dict) -> str:
    issues = diagnostics.get("issues") if isinstance(diagnostics.get("issues"), list) else []
    rows = [
        "<section>",
        "<h2>Diagnostics</h2>",
        '<p class="muted">%s</p>' % escape(_string(diagnostics.get("summary") or "No current diagnostic issues.")),
    ]
    if not issues:
        rows.append('<p class="muted">No current diagnostic issues.</p>')
        rows.append("</section>")
        return "".join(rows)
    rows.append(
        "<table><thead><tr><th>Severity</th><th>Code</th><th>Message</th><th>Recommendation</th><th>Command</th></tr></thead><tbody>"
    )
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        message = _string(issue.get("message"))
        detail = _string(issue.get("detail"))
        if detail:
            message = "%s %s" % (message, detail)
        command = _string(issue.get("command"))
        command_html = "<code>%s</code>" % escape(command) if command else '<span class="muted">none</span>'
        rows.append(
            "<tr><td class=\"status-%s\">%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_status_class(issue.get("severity"))),
                escape(_string(issue.get("severity") or "unknown")),
                escape(_string(issue.get("code") or "unknown")),
                escape(message),
                escape(_string(issue.get("recommendation"))),
                command_html,
            )
        )
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _render_live_detail_approvals(approvals: Dict) -> str:
    pending = approvals.get("pending") if isinstance(approvals.get("pending"), list) else []
    if not pending:
        return ""
    rows = [
        "<section>",
        "<h2>Approval Guidance</h2>",
        '<p class="muted">%s</p>' % escape(
            _string(
                approvals.get("guidance")
                or "Manual gates require an explicit CLI resume with --approve; the live control token does not grant approval."
            )
        ),
        "<table><thead><tr><th>Step</th><th>Status</th><th>Approval</th><th>Resume guidance</th></tr></thead><tbody>",
    ]
    for item in pending:
        if not isinstance(item, dict):
            continue
        command = _string(item.get("command") or "python3 -m conductor_extras run RUN_WORKFLOW --resume RUN_DIR --approve <approval-id>")
        rows.append(
            "<tr><td><code>%s</code></td><td class=\"status-%s\">%s</td><td><code>%s</code></td><td><code>%s</code></td></tr>"
            % (
                escape(_string(item.get("step_id") or "unknown")),
                escape(_status_class(item.get("status"))),
                escape(_string(item.get("status") or "unknown")),
                escape(_string(item.get("approval_id") or "<approval-id>")),
                escape(command),
            )
        )
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _render_live_detail_runtime(runtime: Dict) -> str:
    if not isinstance(runtime, dict) or not runtime.get("available"):
        return ""
    rows = [
        "<section>",
        "<h2>Runtime Actions</h2>",
        '<p class="muted">%s</p>' % escape(_string(runtime.get("guidance") or "No pending runtime actions detected.")),
        "<table><thead><tr><th>Category</th><th>Step</th><th>Status</th><th>Waiting/deps</th><th>Detail</th><th>Command</th></tr></thead><tbody>",
    ]
    rendered = 0
    for label, items in [
        ("Operator action", runtime.get("operator_actions")),
        ("Failed recovery", runtime.get("failed_recovery")),
        ("Ready", runtime.get("ready")),
        ("Waiting", runtime.get("waiting")),
        ("Active", runtime.get("active")),
    ]:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            rendered += 1
            rows.append(
                "<tr><td>%s</td><td><code>%s</code><div class=\"muted\">%s / %s</div></td><td class=\"status-%s\">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    escape(label),
                    escape(_string(item.get("step_id") or "unknown")),
                    escape(_string(item.get("phase") or "default")),
                    escape(_string(item.get("kind") or "unknown")),
                    escape(_status_class(item.get("status"))),
                    escape(_string(item.get("status") or "unknown")),
                    escape(_runtime_waiting_label(item)),
                    escape(_string(item.get("detail"))),
                    _runtime_command_html(item),
                )
            )
    if rendered == 0:
        rows.append('<tr><td colspan="6" class="muted">No pending runtime actions.</td></tr>')
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _runtime_waiting_label(item: Dict) -> str:
    waiting = item.get("waiting_on") if isinstance(item.get("waiting_on"), list) else []
    if waiting:
        parts = []
        for dependency in waiting[:6]:
            if isinstance(dependency, dict):
                parts.append("%s:%s" % (_string(dependency.get("step_id") or "unknown"), _string(dependency.get("status") or "unknown")))
        if len(waiting) > 6:
            parts.append("+%d more" % (len(waiting) - 6))
        return ", ".join(parts)
    depends_on = item.get("depends_on") if isinstance(item.get("depends_on"), list) else []
    if depends_on:
        return "depends on %s" % ", ".join(_string(dep or "unknown") for dep in depends_on[:6])
    return ""


def _runtime_command_html(item: Dict) -> str:
    command = _string(item.get("command"))
    if not command:
        return '<span class="muted">none</span>'
    return "<code>%s</code>" % escape(command)


def _render_live_detail_phases(phases) -> str:
    if not phases:
        return '<section><h2>Phases</h2><p class="muted">No phase summary available.</p></section>'
    rows = [
        "<section>",
        "<h2>Phases</h2>",
        "<table><thead><tr><th>Phase</th><th>Status</th><th>Steps</th><th>Agents</th><th>Timing</th><th>Tokens/cost</th></tr></thead><tbody>",
    ]
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        rows.append(
            "<tr><td><code>%s</code></td><td class=\"status-%s\">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(phase.get("id") or "default")),
                escape(_status_class(phase.get("status"))),
                escape(_string(phase.get("status") or "unknown")),
                escape(_live_phase_steps(phase)),
                escape(_live_phase_agents(phase)),
                escape(_live_phase_timing(phase)),
                escape(_usage_label(phase.get("usage") if isinstance(phase.get("usage"), dict) else {})),
            )
        )
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _render_live_detail_steps(handle: str, steps) -> str:
    if not steps:
        return '<section><h2>Steps</h2><p class="muted">No recorded steps.</p></section>'
    rows = [
        "<section>",
        "<h2>Steps</h2>",
        "<table><thead><tr><th>Step</th><th>Status</th><th>Phase</th><th>Kind</th><th>Timing</th><th>Detail</th><th>Controls</th></tr></thead><tbody>",
    ]
    for step in steps:
        rows.append(
            "<tr><td><code>%s</code></td><td class=\"status-%s\">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=\"controls\">%s</td></tr>"
            % (
                escape(_string(step.get("id"))),
                escape(_status_class(step.get("status"))),
                escape(_string(step.get("status"))),
                escape(_string(step.get("phase") or "default")),
                escape(_string(step.get("kind"))),
                escape(_live_step_timing(step)),
                escape(_live_step_detail(step)),
                _render_controls({"run_handle": handle}, [step], return_to="/run/%s" % quote(handle, safe="")),
            )
        )
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _live_step_detail(step: Dict) -> str:
    detail = _string(step.get("detail"))
    progress = step.get("codex_progress")
    if isinstance(progress, dict):
        if progress.get("status") == "invalid":
            progress_text = "Codex progress invalid (%s)" % _string(
                progress.get("error") or "unknown"
            )
        else:
            progress_text = "Codex progress %s, %s events, %s/%s items, last %s" % (
                _string(progress.get("status") or "unknown"),
                _string(progress.get("event_count") or 0),
                _string(progress.get("item_completed_count") or 0),
                _string(progress.get("item_started_count") or 0),
                _string(progress.get("last_event") or "unknown"),
            )
            native_count = progress.get("native_agent_count", 0)
            collab_count = progress.get("collab_tool_call_count", 0)
            if progress.get("native_agents_enabled_by_runner") is True:
                progress_text += ", native enabled max %s depth %s, %s" % (
                    _string(progress.get("native_agent_max_threads") or "unknown"),
                    _string(progress.get("native_agent_max_depth") or "unknown"),
                    _live_native_usage(progress),
                )
                checkpoint = step.get(
                    "native_agent_parent_completion_checkpoint_tokens"
                )
                if (
                    isinstance(checkpoint, int)
                    and not isinstance(checkpoint, bool)
                    and checkpoint > 0
                ):
                    integration = step.get(
                        "native_agent_parent_integration_checkpoint_tokens"
                    )
                    if (
                        isinstance(integration, int)
                        and not isinstance(integration, bool)
                        and integration > 0
                    ):
                        progress_text += ", parent integration checkpoint %s and synthesis checkpoint %s remaining" % (
                            integration,
                            checkpoint,
                        )
                    else:
                        progress_text += ", parent synthesis checkpoint %s remaining" % checkpoint
            if (
                isinstance(native_count, int)
                and not isinstance(native_count, bool)
                and isinstance(collab_count, int)
                and not isinstance(collab_count, bool)
                and (native_count or collab_count)
            ):
                progress_text += (
                    ", %s native agents (%s active, %s errors), %s collaboration calls%s"
                    % (
                        native_count,
                        progress.get("native_agent_active_count", 0),
                        progress.get("native_agent_error_count", 0),
                        collab_count,
                        (
                            ""
                            if progress.get("native_agents_enabled_by_runner") is True
                            else ", nested usage unattributed"
                        ),
                    )
                )
                if progress.get("native_agent_observation_truncated") is True:
                    progress_text += ", native-agent observation truncated"
        detail = "%s | %s" % (detail, progress_text) if detail else progress_text
    packet_progress = step.get("codex_packet_progress")
    if isinstance(packet_progress, list) and packet_progress:
        invalid = [value for value in packet_progress if value.get("status") == "invalid"]
        if invalid:
            packet_text = "Codex packet progress invalid (%s)" % _string(
                invalid[0].get("error") or "unknown"
            )
        else:
            active = sum(value.get("status") == "active" for value in packet_progress)
            completed = sum(value.get("status") == "completed" for value in packet_progress)
            failed = len(packet_progress) - active - completed
            events = sum(
                value.get("event_count", 0)
                for value in packet_progress
                if isinstance(value.get("event_count", 0), int)
                and not isinstance(value.get("event_count", 0), bool)
            )
            packet_text = "Codex packet progress %s active, %s completed, %s failed, %s events" % (
                active,
                completed,
                failed,
                events,
            )
            native_count = sum(
                value.get("native_agent_count", 0)
                for value in packet_progress
                if isinstance(value.get("native_agent_count", 0), int)
                and not isinstance(value.get("native_agent_count", 0), bool)
            )
            collab_count = sum(
                value.get("collab_tool_call_count", 0)
                for value in packet_progress
                if isinstance(value.get("collab_tool_call_count", 0), int)
                and not isinstance(value.get("collab_tool_call_count", 0), bool)
            )
            if native_count or collab_count:
                packet_text += (
                    ", %s native agents, %s collaboration calls, nested usage unattributed"
                    % (native_count, collab_count)
                )
                if any(
                    value.get("native_agent_observation_truncated") is True
                    for value in packet_progress
                ):
                    packet_text += ", native-agent observation truncated"
        detail = "%s | %s" % (detail, packet_text) if detail else packet_text
    terminal = step.get("codex_terminal")
    if not isinstance(terminal, dict):
        return detail
    if terminal.get("status") == "invalid":
        terminal_text = "Codex local terminal invalid (%s)" % _string(
            terminal.get("error") or "unknown"
        )
    else:
        terminal_text = (
            "Codex local terminal recovery pending, provider replay not required"
        )
    return "%s | %s" % (detail, terminal_text) if detail else terminal_text


def _live_native_usage(progress: Dict) -> str:
    if progress.get("native_agent_usage_status") == "complete":
        return "%s weighted/%s gross tokens across %s sessions/%s children" % (
            _string(progress.get("native_agent_usage_rollout_tokens") or 0),
            _string(progress.get("native_agent_usage_total_tokens") or 0),
            _string(progress.get("native_agent_usage_session_count") or 0),
            _string(progress.get("native_agent_usage_child_count") or 0),
        )
    return "nested usage %s" % _string(
        progress.get("native_agent_usage_status") or "unavailable"
    )


def _live_phase_steps(phase: Dict) -> str:
    counts = phase.get("step_counts") if isinstance(phase.get("step_counts"), dict) else {}
    summary = ", ".join("%s %s" % (count, status) for status, count in sorted(counts.items()))
    text = "%s/%s recorded" % (_string(phase.get("steps_recorded") or 0), _string(phase.get("steps_total") or 0))
    if summary:
        text += ": %s" % summary
    return text


def _live_phase_agents(phase: Dict) -> str:
    return "%s calls / %s item(s), %s cached / max %s worker(s)" % (
        _string(phase.get("agent_invocations") or 0),
        _string(phase.get("agent_items_total") or 0),
        _string(phase.get("agent_items_cached") or 0),
        _string(phase.get("agent_workers_max") or 0),
    )


def _live_phase_timing(phase: Dict) -> str:
    parts = []
    duration = _format_duration(phase.get("duration_ms"))
    if duration:
        parts.append(duration)
    if phase.get("started_at_utc"):
        parts.append("started %s" % _string(phase.get("started_at_utc")))
    if phase.get("finished_at_utc"):
        parts.append("finished %s" % _string(phase.get("finished_at_utc")))
    return " / ".join(parts) or "unknown"


def _render_live_detail_hooks(events) -> str:
    if not events:
        return '<section><h2>Hooks</h2><p class="muted">No hook events recorded.</p></section>'
    rows = [
        "<section>",
        "<h2>Hooks</h2>",
        "<table><thead><tr><th>Hook</th><th>Event</th><th>Status</th><th>Timing</th><th>Detail</th></tr></thead><tbody>",
    ]
    for event in events:
        rows.append(
            "<tr><td><code>%s</code>%s</td><td>%s</td><td class=\"status-%s\">%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(event.get("id"))),
                _live_hook_step(event),
                escape(_string(event.get("event"))),
                escape(_status_class(event.get("status"))),
                escape(_string(event.get("status"))),
                escape(_live_hook_timing(event)),
                escape(_string(event.get("detail"))),
            )
        )
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _render_live_session_traces(events, summary=None) -> str:
    summary = summary if isinstance(summary, dict) else {}
    total = summary.get("count") if isinstance(summary.get("count"), int) and not isinstance(summary.get("count"), bool) and summary.get("count") >= 0 else len(events)
    if not events and not total:
        return '<section><h2>Session Traces</h2><p class="muted">No imported session trace events recorded.</p></section>'
    by_provider = summary.get("by_provider") if isinstance(summary.get("by_provider"), dict) else {}
    rows = [
        "<section>",
        "<h2>Session Traces</h2>",
        '<p class="muted">%s%s</p>'
        % (
            escape(_session_trace_count_label(total, by_provider)),
            escape("; showing latest %s" % len(events)) if total > len(events) else "",
        ),
    ]
    if not events:
        rows.append('<p class="muted">No displayable session trace rows.</p></section>')
        return "".join(rows)
    rows.append("<table><thead><tr><th>Event</th><th>Status</th><th>Actor</th><th>Timing</th><th>Tokens/cost</th><th>Detail</th></tr></thead><tbody>")
    for event in events:
        if not isinstance(event, dict):
            continue
        rows.append(
            "<tr><td><code>%s</code><div class=\"muted\">%s</div></td><td class=\"status-%s\">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_string(event.get("event") or "event")),
                escape(_string(event.get("provider") or "generic")),
                escape(_status_class(event.get("status"))),
                escape(_string(event.get("status") or "unknown")),
                escape(_live_session_trace_actor(event)),
                escape(_live_session_trace_timing(event)),
                escape(_live_session_trace_usage(event)),
                escape(_string(event.get("detail"))),
            )
        )
    rows.append("</tbody></table></section>")
    return "".join(rows)


def _live_session_trace_actor(event: Dict) -> str:
    parts = []
    for label, field in [("model", "model"), ("session", "session_id"), ("agent", "agent_id"), ("step", "step_id"), ("role", "role"), ("tool", "tool"), ("source", "source")]:
        value = _string(event.get(field))
        if value:
            parts.append("%s %s" % (label, value))
    return " / ".join(parts) or "unknown"


def _live_session_trace_timing(event: Dict) -> str:
    parts = []
    duration = _format_duration(event.get("duration_ms"))
    if duration:
        parts.append(duration)
    if event.get("started_at_utc"):
        parts.append("started %s" % _string(event.get("started_at_utc")))
    if event.get("finished_at_utc"):
        parts.append("finished %s" % _string(event.get("finished_at_utc")))
    if event.get("recorded_at_utc"):
        parts.append("recorded %s" % _string(event.get("recorded_at_utc")))
    if event.get("imported_at_utc"):
        parts.append("imported %s" % _string(event.get("imported_at_utc")))
    return " / ".join(parts) or "unknown"


def _live_session_trace_usage(event: Dict) -> str:
    usage = {
        "status": "recorded",
        "input_tokens": event.get("input_tokens"),
        "output_tokens": event.get("output_tokens"),
        "total_tokens": event.get("total_tokens"),
        "cost_usd": event.get("cost_usd"),
    }
    if any(usage.get(field) is not None for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]):
        return _usage_label(usage)
    return "unavailable"


def _render_live_agent_teams(handle: str, agent_teams) -> str:
    if not agent_teams:
        return '<section><h2>Agent Teams</h2><p class="muted">No coordinated team state recorded.</p></section>'
    rows = ["<section>", "<h2>Agent Teams</h2>"]
    for team in agent_teams:
        if not isinstance(team, dict):
            continue
        status = _string(team.get("status") or "unknown")
        rows.append(
            '<h3><code>%s</code> <span class="status-%s">%s</span></h3>'
            % (
                escape(_string(team.get("step_id") or "unknown")),
                escape(_status_class(status)),
                escape(status),
            )
        )
        if team.get("error"):
            rows.append('<p class="muted">%s</p>' % escape(_string(team["error"])))
            continue
        counts = team.get("task_counts") if isinstance(team.get("task_counts"), dict) else {}
        rows.append(
            '<p class="muted">generation %s / lead %s / round %s of %s / %s members / %s of %s tasks / '
            '%s turns / %s interrupted / %s messages / %s of %s authorized tokens</p>'
            % (
                escape(_string(team.get("generation") or 0)),
                escape(_string(team.get("lead") or "unknown")),
                escape(_string(team.get("round") or 0)),
                escape(_string(team.get("max_rounds") or 0)),
                escape(_string(team.get("member_count") or 0)),
                escape(_string(counts.get("completed") or 0)),
                escape(_string(sum(value for value in counts.values() if isinstance(value, int)))),
                escape(_string(team.get("turn_count") or 0)),
                escape(_string(team.get("interruption_count") or 0)),
                escape(_string(team.get("message_count") or 0)),
                escape(_string(team.get("authorized_tokens") or 0)),
                escape(_string(team.get("max_total_tokens") or 0)),
            )
        )
        operator = team.get("operator_inbox") if isinstance(team.get("operator_inbox"), dict) else {}
        operator_counts = operator.get("status_counts") if isinstance(operator.get("status_counts"), dict) else {}
        if team.get("max_operator_tasks") or operator:
            rows.append(
                '<p class="muted">operator follow-ups %s of %s accepted / %s queued / %s superseded</p>'
                % (
                    escape(_string(team.get("operator_tasks_added") or 0)),
                    escape(_string(team.get("max_operator_tasks") or 0)),
                    escape(_string(operator_counts.get("pending") or 0)),
                    escape(_string(operator_counts.get("superseded") or 0)),
                )
            )
        if team.get("max_message_tasks"):
            rows.append(
                '<p class="muted">active teammate responses %s of %s message task(s)</p>'
                % (
                    escape(_string(team.get("message_tasks_added") or 0)),
                    escape(_string(team.get("max_message_tasks") or 0)),
                )
            )
        chat = team.get("operator_chat") if isinstance(team.get("operator_chat"), dict) else {}
        chat_counts = chat.get("status_counts") if isinstance(chat.get("status_counts"), dict) else {}
        if team.get("max_operator_questions") or chat:
            rows.append(
                '<p class="muted">operator questions %s of %s / %s pending / %s answered / %s delivered</p>'
                % (
                    escape(_string(team.get("operator_questions_added") or 0)),
                    escape(_string(team.get("max_operator_questions") or 0)),
                    escape(_string(chat_counts.get("pending") or 0)),
                    escape(_string(chat_counts.get("answered") or 0)),
                    escape(_string(chat_counts.get("delivered") or 0)),
                )
            )
        controls = _render_live_agent_team_controls(handle, team)
        if controls:
            rows.append(controls)
        transcript_controls = _render_live_agent_team_transcripts(handle, team)
        if transcript_controls:
            rows.append(transcript_controls)
        question_controls = _render_live_agent_team_question_controls(handle, team)
        if question_controls:
            rows.append(question_controls)
        plan_approval_count = team.get("plan_approval_count") or 0
        if plan_approval_count:
            rows.append(
                '<p class="muted">plan approvals %s of %s approved / %s rejection(s) before approval / '
                'in-flight provider replay after process loss: disabled</p>'
                % (
                    escape(_string(team.get("plan_approval_approved_count") or 0)),
                    escape(_string(plan_approval_count)),
                    escape(_string(team.get("plan_approval_rejection_count") or 0)),
                )
            )
            for approval in team.get("plan_approvals", [])[:16]:
                rows.append(
                    '<p class="muted">task <code>%s</code> / planner <code>%s</code> / '
                    'reviewer <code>%s</code> / status %s / revision %s of %s</p>'
                    % (
                        escape(_string(approval.get("task_id") or "unknown")),
                        escape(_string(approval.get("member_id") or "unknown")),
                        escape(_string(approval.get("reviewer") or "lead")),
                        escape(_string(approval.get("status") or "unknown")),
                        escape(_string(approval.get("revision") or 0)),
                        escape(_string((approval.get("max_revisions") or 0) + 1)),
                    )
                )
            plan_controls = _render_live_agent_team_plan_controls(handle, team)
            if plan_controls:
                rows.append(plan_controls)
        quality_retry_count = team.get("quality_retry_history_count") or 0
        quality_retry_pending_count = team.get("quality_retry_pending_count") or 0
        if quality_retry_count or quality_retry_pending_count:
            quality_counts = (
                team.get("quality_retry_status_counts")
                if isinstance(team.get("quality_retry_status_counts"), dict)
                else {}
            )
            quality_statuses = ", ".join(
                "%s %s" % (count, status)
                for status, count in sorted(quality_counts.items())
            ) or "no terminal outcomes"
            rows.append(
                '<p class="muted">automatic quality retries: %s (%s) / pending recovery: %s / '
                'automatic provider replay after process loss: disabled%s</p>'
                % (
                    escape(_string(quality_retry_count)),
                    escape(quality_statuses),
                    escape(_string(quality_retry_pending_count)),
                    " / history display truncated"
                    if team.get("quality_retry_history_truncated")
                    else "",
                )
            )
            pending_quality = (
                team.get("quality_retry_pending")
                if isinstance(team.get("quality_retry_pending"), list)
                else []
            )
            for checkpoint in pending_quality[:8]:
                rows.append(
                    '<p><strong>Quality retry recovery pending</strong> / status %s / '
                    'member/retry task <code>%s/%s</code>%s / retry %s of %s / hook <code>%s</code></p>'
                    % (
                        escape(_string(checkpoint.get("status") or "unknown")),
                        escape(_string(checkpoint.get("member_id") or "unknown")),
                        escape(_string(checkpoint.get("task_id") or "unknown")),
                        (
                            " / event task <code>%s</code>"
                            % escape(_string(checkpoint.get("event_task_id") or "unknown"))
                            if checkpoint.get("event_task_id") != checkpoint.get("task_id")
                            else ""
                        ),
                        escape(_string(checkpoint.get("retry_index") or 0)),
                        escape(_string(checkpoint.get("max_retries") or 0)),
                        escape(_string(checkpoint.get("hook_id") or "unknown")),
                    )
                )
        merge = team.get("merge") if isinstance(team.get("merge"), dict) else None
        if merge is not None:
            rows.append(
                '<p class="muted">isolated write / %s / %s accepted merges / %s serialized conflicts / '
                '%s applied files / %s deduplicated files</p>'
                % (
                    escape(_string(merge.get("strategy") or "unknown")),
                    escape(_string(merge.get("merge_count") or 0)),
                    escape(_string(merge.get("conflict_count") or 0)),
                    escape(_string(merge.get("applied_file_count") or 0)),
                    escape(_string(merge.get("deduplicated_file_count") or 0)),
                )
            )
        pending = team.get("recovery_pending") if isinstance(team.get("recovery_pending"), dict) else None
        if pending is not None:
            if pending.get("intent_sha256"):
                rows.append(
                    '<p><strong>Accepted turn recovery pending</strong> / intent <code>%s</code> / '
                    'member/task <code>%s/%s</code> / merge decision pending / provider replay not required</p>'
                    % (
                        escape(_string(pending.get("intent_sha256") or "unknown")),
                        escape(_string(pending.get("member_id") or "unknown")),
                        escape(_string(pending.get("task_id") or "unknown")),
                    )
                )
            else:
                rows.append(
                    '<p><strong>Merge recovery pending</strong> / transaction <code>%s</code> / '
                    'member/task <code>%s/%s</code> / %s changed files / provider replay not required</p>'
                    % (
                        escape(_string(pending.get("transaction_sha256") or "unknown")),
                        escape(_string(pending.get("member_id") or "unknown")),
                        escape(_string(pending.get("task_id") or "unknown")),
                        escape(_string(pending.get("changed_file_count") or 0)),
                    )
                )
        terminals = (
            team.get("turn_terminal_pending")
            if isinstance(team.get("turn_terminal_pending"), list)
            else []
        )
        for terminal in terminals:
            if not isinstance(terminal, dict):
                continue
            rows.append(
                '<p><strong>Local terminal turn recovery pending</strong> / terminal '
                '<code>%s</code> / member/task <code>%s/%s</code> / quality retry %s / '
                'provider replay not required</p>'
                % (
                    escape(_string(terminal.get("terminal_sha256") or "unknown")),
                    escape(_string(terminal.get("member_id") or "unknown")),
                    escape(_string(terminal.get("task_id") or "unknown")),
                    escape(_string(terminal.get("quality_retry_index") or 0)),
                )
            )
        completions = (
            team.get("turn_completion_pending")
            if isinstance(team.get("turn_completion_pending"), list)
            else []
        )
        for completion in completions:
            if not isinstance(completion, dict):
                continue
            rows.append(
                '<p><strong>Provider-completed turn recovery pending</strong> / completion '
                '<code>%s</code> / member/task <code>%s/%s</code> / quality retry %s / '
                'provider replay not required</p>'
                % (
                    escape(_string(completion.get("completion_sha256") or "unknown")),
                    escape(_string(completion.get("member_id") or "unknown")),
                    escape(_string(completion.get("task_id") or "unknown")),
                    escape(_string(completion.get("quality_retry_index") or 0)),
                )
            )
        if team.get("merge_recovery_count"):
            latest = team.get("latest_merge_recovery") if isinstance(team.get("latest_merge_recovery"), dict) else {}
            rows.append(
                '<p class="muted">%s exactly-once merge recoveries / latest source checkpoint %s / '
                'provider replayed: no</p>'
                % (
                    escape(_string(team.get("merge_recovery_count") or 0)),
                    escape(_string(latest.get("source_checkpoint") or "unknown")),
                )
            )
        tasks = team.get("tasks") if isinstance(team.get("tasks"), list) else []
        if tasks:
            task_rows = []
            for task in tasks:
                task_rows.append(
                    "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (
                        escape(_string(task.get("id") or "unknown")),
                        escape(_string(task.get("status") or "unknown")),
                        escape(_string(task.get("claimed_by") or task.get("assignee") or "unassigned")),
                        escape(_string(task.get("attempts") or 0)),
                    )
                )
            rows.append(
                "<table><thead><tr><th>Task</th><th>Status</th><th>Owner</th><th>Attempts</th></tr></thead>"
                "<tbody>%s</tbody></table>" % "".join(task_rows)
            )
    rows.append("</section>")
    return "".join(rows)


def _render_live_agent_team_transcripts(handle: str, team: Dict) -> str:
    console = (
        team.get("operator_console")
        if isinstance(team.get("operator_console"), dict)
        else None
    )
    if console is None:
        return ""
    transcripts = (
        console.get("transcripts")
        if isinstance(console.get("transcripts"), list)
        else []
    )
    if not transcripts:
        return '<h3>Teammate transcripts</h3><p class="muted">No retained turns yet.</p>'
    step_id = team.get("step_id")
    if not isinstance(step_id, str) or not step_id:
        return ""
    rows = []
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        transcript_id = transcript.get("transcript_id")
        if not isinstance(transcript_id, str):
            continue
        open_form = (
            '<form class="transcript-open" method="post" action="/private/team-transcript">'
            '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
            '<input type="hidden" name="run_handle" value="%s">'
            '<input type="hidden" name="step_handle" value="%s">'
            '<input type="hidden" name="transcript_id" value="%s">'
            '<button type="submit">Open</button></form>'
            % (
                escape(handle),
                escape(step_handle(step_id)),
                escape(transcript_id),
            )
        )
        rows.append(
            "<tr><td><code>%s</code><div class=\"muted\">round %s / attempt %s</div></td>"
            "<td><code>%s</code></td><td>%s</td><td>%s%s</td><td><code>%s</code></td><td>%s</td></tr>"
            % (
                escape(_string(transcript.get("member_id") or "unknown")),
                escape(_string(transcript.get("round") or 0)),
                escape(_string(transcript.get("attempt") or 0)),
                escape(_string(transcript.get("task_id") or "unknown")),
                escape(_string(transcript.get("status") or "unknown")),
                escape(_string(transcript.get("event_count") or 0)),
                " / truncated" if transcript.get("truncated") else "",
                escape(_string(transcript.get("file_sha256") or "unavailable")),
                open_form,
            )
        )
    return (
        '<h3>Teammate transcripts</h3><p class="muted">%s retained turn(s) / private content</p>'
        '<table><thead><tr><th>Member</th><th>Task</th><th>Status</th><th>Events</th>'
        '<th>File SHA-256</th><th>View</th></tr></thead><tbody>%s</tbody></table>'
        % (
            escape(_string(console.get("transcript_count") or len(transcripts))),
            "".join(rows),
        )
    )


def _render_live_agent_team_controls(handle: str, team: Dict) -> str:
    operator = (
        team.get("operator_inbox")
        if isinstance(team.get("operator_inbox"), dict)
        else {}
    )
    status_counts = (
        operator.get("status_counts")
        if isinstance(operator.get("status_counts"), dict)
        else {}
    )
    pending = status_counts.get("pending", 0)
    if (
        team.get("status") != "running"
        or not isinstance(team.get("max_operator_tasks"), int)
        or not isinstance(team.get("operator_tasks_added"), int)
        or not isinstance(pending, int)
        or team["max_operator_tasks"]
        <= team["operator_tasks_added"] + pending
    ):
        return ""
    step_id = team.get("step_id")
    members = team.get("members")
    if not isinstance(step_id, str) or not step_id or not isinstance(members, list):
        return ""
    member_options = []
    has_working_member = False
    for member in members:
        member_id = member.get("id") if isinstance(member, dict) else None
        if isinstance(member_id, str) and member_id:
            status = member.get("status") or "unknown"
            current_task_id = member.get("current_task_id")
            has_working_member = has_working_member or status == "working"
            label = "%s (%s%s)" % (
                member_id,
                status,
                " on %s" % current_task_id if current_task_id else "",
            )
            member_options.append(
                '<option value="%s">%s</option>'
                % (escape(member_id), escape(label))
            )
    if not member_options:
        return ""
    delivery_options = ['<option value="next-turn">Next turn</option>']
    if team.get("schema") in {
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    } and has_working_member:
        delivery_options.append(
            '<option value="interrupt-current">Interrupt current turn</option>'
        )
    return (
        '<div class="controls"><form method="post" action="/control">'
        '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
        '<select name="member" aria-label="Teammate">%s</select>'
        '<select name="delivery" aria-label="Delivery">%s</select>'
        '<input type="text" name="instruction" maxlength="%d" autocomplete="off" '
        'placeholder="Follow-up instruction" required>'
        '<input type="hidden" name="run_handle" value="%s">'
        '<input type="hidden" name="step_handle" value="%s">'
        '<input type="hidden" name="action" value="queue-team-task">%s'
        '<button type="submit">Queue follow-up</button>'
        '</form></div>'
        % (
            "".join(member_options),
            "".join(delivery_options),
            MAX_AGENT_TEAM_MESSAGE_CHARS,
            escape(handle),
            escape(step_handle(step_id)),
            _return_to_input("/run/%s" % quote(handle, safe="")),
        )
    )


def _render_live_agent_team_question_controls(handle: str, team: Dict) -> str:
    if team.get("status") != "running":
        return ""
    chat = team.get("operator_chat")
    step_id = team.get("step_id")
    if not isinstance(chat, dict) or not isinstance(step_id, str) or not step_id:
        return ""
    entries = chat.get("entries")
    generation = team.get("generation")
    if not isinstance(entries, list) or not isinstance(generation, int):
        return ""
    forms = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("generation") != generation
            or entry.get("status") != "pending"
        ):
            continue
        question_id = entry.get("id")
        member_id = entry.get("member_id")
        question_sha256 = entry.get("question_sha256")
        if not all(isinstance(value, str) and value for value in (question_id, member_id, question_sha256)):
            continue
        forms.append(
            '<form method="post" action="/control">'
            '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
            '<code>%s</code><span class="muted">%s / %s</span>'
            '<input type="text" name="reply" maxlength="%d" autocomplete="off" '
            'placeholder="Operator reply" required>'
            '<input type="hidden" name="run_handle" value="%s">'
            '<input type="hidden" name="step_handle" value="%s">'
            '<input type="hidden" name="question_id" value="%s">'
            '<input type="hidden" name="action" value="reply-team-question">%s'
            '<button type="submit">Reply</button>'
            '</form>'
            % (
                escape(question_id),
                escape(member_id),
                escape(question_sha256),
                MAX_AGENT_TEAM_MESSAGE_CHARS,
                escape(handle),
                escape(step_handle(step_id)),
                escape(question_id),
                _return_to_input("/run/%s" % quote(handle, safe="")),
            )
        )
    if not forms:
        return ""
    return '<div class="controls">%s</div>' % "".join(forms)


def _render_live_agent_team_plan_controls(handle: str, team: Dict) -> str:
    if team.get("status") != "running":
        return ""
    step_id = team.get("step_id")
    approvals = team.get("plan_approvals")
    if not isinstance(step_id, str) or not step_id or not isinstance(approvals, list):
        return ""
    forms = []
    for approval in approvals:
        if (
            not isinstance(approval, dict)
            or approval.get("reviewer") != "operator"
            or approval.get("status") != "plan-ready"
        ):
            continue
        task_id = approval.get("task_id")
        plan_sha256 = approval.get("latest_plan_output_sha256")
        if not all(isinstance(value, str) and value for value in (task_id, plan_sha256)):
            continue
        forms.append(
            '<form method="post" action="/control">'
            '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
            '<code>%s</code><span class="muted">revision %s / plan %s</span>'
            '<select name="decision" aria-label="Plan decision">'
            '<option value="approve">Approve</option><option value="reject">Reject</option>'
            '</select>'
            '<input type="text" name="feedback" maxlength="%d" autocomplete="off" '
            'placeholder="Review rationale or required changes" required>'
            '<input type="hidden" name="run_handle" value="%s">'
            '<input type="hidden" name="step_handle" value="%s">'
            '<input type="hidden" name="task_id" value="%s">'
            '<input type="hidden" name="action" value="review-team-plan">%s'
            '<button type="submit">Submit review</button>'
            '</form>'
            % (
                escape(task_id),
                escape(_string(approval.get("revision") or 0)),
                escape(plan_sha256),
                MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
                escape(handle),
                escape(step_handle(step_id)),
                escape(task_id),
                _return_to_input("/run/%s" % quote(handle, safe="")),
            )
        )
    if not forms:
        return ""
    return '<div class="controls">%s</div>' % "".join(forms)


def _render_live_agent_maps(handle: str, agent_maps) -> str:
    if not agent_maps:
        return '<section><h2>Agent Packets</h2><p class="muted">No agent_map packet details recorded.</p></section>'
    rows = ["<section>", "<h2>Agent Packets</h2>"]
    for group in agent_maps:
        if not isinstance(group, dict):
            continue
        rows.append(
            '<h3><code>%s</code> <span class="status-%s">%s</span></h3>'
            % (
                escape(_string(group.get("step_id") or "unknown")),
                escape(_status_class(group.get("status"))),
                escape(_string(group.get("status") or "unknown")),
            )
        )
        rows.append(
            '<p class="muted">%s</p>'
            % escape(
                " / ".join(
                    [
                        "phase %s" % _string(group.get("phase") or "default"),
                        "capture %s" % _string(group.get("capture_dir") or ""),
                        "%s calls" % _string(group.get("agent_invocations") or 0),
                        "%s item(s)" % _string(group.get("agent_items_total") or len(group.get("packets") or [])),
                        "%s cached" % _string(group.get("agent_items_cached") or 0),
                        "%s worker(s)" % _string(group.get("agent_workers") or 0),
                        "%s cache entries" % _string(group.get("cache_entries") or 0),
                        "%s trace entries" % _string(group.get("trace_entries") or 0),
                        "%s terminal recovery pending"
                        % _string(group.get("terminal_pending_count") or 0),
                    ]
                )
            )
        )
        if group.get("error"):
            rows.append('<p class="warn">%s</p>' % escape(_string(group.get("error"))))
        if group.get("cache_error"):
            rows.append('<p class="warn">cache: %s</p>' % escape(_string(group.get("cache_error"))))
        if group.get("trace_error"):
            rows.append('<p class="warn">trace: %s</p>' % escape(_string(group.get("trace_error"))))
        if group.get("terminal_error"):
            rows.append(
                '<p class="warn">terminal recovery: %s</p>'
                % escape(_string(group.get("terminal_error")))
            )
        if group.get("terminal_pending_count"):
            rows.append(
                '<p class="warn">Local packet terminal recovery pending; provider replay is not required.</p>'
            )
        if group.get("truncated"):
            rows.append('<p class="warn">Packet listing truncated.</p>')
        rows.append(_render_live_agent_packet_table(handle, group))
    rows.append("</section>")
    return "".join(rows)


def _render_live_agent_packet_table(handle: str, group: Dict) -> str:
    packets = group.get("packets") if isinstance(group.get("packets"), list) else []
    if not packets:
        return '<p class="muted">No packet rows available.</p>'
    rows = [
        "<table><thead><tr><th>Packet</th><th>Status</th><th>Trace</th><th>Cache</th><th>Controls</th><th>Output</th><th>Stdout</th><th>Stderr</th></tr></thead><tbody>",
    ]
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        rows.append(
            "<tr><td><code>%03d</code><div>%s</div></td><td class=\"status-%s\">%s</td><td>%s</td><td>%s</td><td class=\"controls\">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                packet.get("index") or 0,
                escape(_string(packet.get("item"))),
                escape(_status_class(packet.get("status"))),
                escape(_string(packet.get("status") or "unknown")),
                escape(_live_agent_packet_trace_text(packet)),
                escape(_live_agent_packet_cache_text(packet)),
                _render_live_agent_packet_controls(handle, group, packet),
                _live_excerpt((packet.get("output") if isinstance(packet.get("output"), dict) else {}).get("excerpt") or {}),
                _live_excerpt((packet.get("stdout") if isinstance(packet.get("stdout"), dict) else {}).get("excerpt") or {}),
                _live_excerpt((packet.get("stderr") if isinstance(packet.get("stderr"), dict) else {}).get("excerpt") or {}),
            )
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_live_agent_packet_controls(handle: str, group: Dict, packet: Dict) -> str:
    actions = _live_agent_packet_control_actions(packet)
    step_handle_value = _string(group.get("step_handle"))
    if not actions or not step_handle_value or not handle:
        return '<span class="muted">Use step controls</span>'
    packet_index = _packet_index_text(packet)
    packet_index_value = packet.get("index")
    if not isinstance(packet_index_value, int) or isinstance(packet_index_value, bool) or packet_index_value < 1:
        return '<span class="muted">Use step controls</span>'
    forms = []
    for action, label, reason_suffix in actions:
        reason = ("agent_map packet %s %s" % (packet_index, reason_suffix))[:MAX_REASON_CHARS]
        forms.append(
            '<form method="post" action="/control">'
            '<input type="password" name="token" autocomplete="off" placeholder="control token" required>'
            '<input type="hidden" name="run_handle" value="%s">'
            '<input type="hidden" name="step_handle" value="%s">'
            '<input type="hidden" name="packet_index" value="%s">'
            '<input type="hidden" name="return_to" value="/run/%s">'
            '<input type="hidden" name="reason" value="%s">'
            '<button name="action" value="%s">%s</button>'
            '</form>'
            % (
                escape(handle),
                escape(step_handle_value),
                escape(str(packet_index_value)),
                escape(quote(handle, safe="")),
                escape(reason),
                escape(action),
                escape(label),
            )
        )
    return "".join(forms)


def _live_agent_packet_control_actions(packet: Dict) -> list:
    status = _string(packet.get("status") or "")
    trace = packet.get("trace") if isinstance(packet.get("trace"), dict) else {}
    trace_status = _string(trace.get("status") or "")
    if status == "cached-output-missing":
        return [("retry-packet", "Retry packet", "cached output missing")]
    if trace_status in {"failed", "timed_out", "no_output"}:
        return [("retry-packet", "Retry packet", trace_status)]
    if status == "ran-without-output":
        return [("retry-packet", "Retry packet", "ran without output")]
    if status == "completed" or trace_status in {"completed", "cached"}:
        return [("retry-packet", "Rerun packet", "operator requested rerun")]
    return []


def _packet_index_text(packet: Dict) -> str:
    value = packet.get("index")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return "%03d" % value
    return "unknown"


def _live_agent_packet_cache_text(packet: Dict) -> str:
    generation = packet.get("retry_generation")
    generation_text = " / packet retry %s" % generation if isinstance(generation, int) and generation > 0 else ""
    if not packet.get("cached"):
        return "not cached%s" % generation_text
    updated = _string(packet.get("cache_updated_at_utc"))
    return "cached%s%s" % ((" %s" % updated) if updated else "", generation_text)


def _live_agent_packet_trace_text(packet: Dict) -> str:
    trace = packet.get("trace") if isinstance(packet.get("trace"), dict) else {}
    if not trace:
        return "not recorded"
    parts = [_string(trace.get("status") or "unknown")]
    duration = trace.get("duration_ms")
    if duration is not None:
        parts.append(_format_duration(duration))
    if trace.get("cached"):
        parts.append("cache reuse")
    returncode = trace.get("returncode")
    if returncode is not None:
        parts.append("rc %s" % _string(returncode))
    if trace.get("timed_out"):
        parts.append("timed out")
    stdout_bytes = trace.get("stdout_bytes")
    stderr_bytes = trace.get("stderr_bytes")
    if stdout_bytes is not None or stderr_bytes is not None:
        parts.append("%s/%s bytes out/err" % (_string(stdout_bytes or 0), _string(stderr_bytes or 0)))
    detail = _string(trace.get("detail"))
    if detail and detail not in {"completed", "reused cached output"}:
        parts.append(detail)
    return " / ".join(parts)


def _render_live_group(title: str, group: Dict) -> str:
    files = group.get("files") if isinstance(group.get("files"), list) else []
    prefix = ""
    if group.get("error"):
        prefix = '<p class="warn">%s: %s</p>' % (escape(title), escape(_string(group.get("error"))))
    suffix = ""
    if group.get("truncated"):
        suffix = '<p class="warn">%s listing truncated.</p>' % escape(title)
    return "<section><h2>%s</h2>%s%s%s</section>" % (
        escape(title),
        prefix,
        _render_live_file_table("", files),
        suffix,
    )


def _render_live_file_table(title: str, files) -> str:
    heading = "<section><h2>%s</h2>" % escape(title) if title else ""
    closing = "</section>" if title else ""
    if not files:
        return '%s<p class="muted">No files found.</p>%s' % (heading, closing)
    rows = [
        heading,
        "<table><thead><tr><th>File</th><th>Bytes</th><th>Updated</th><th>Excerpt</th></tr></thead><tbody>",
    ]
    for item in files:
        rows.append(
            "<tr><td><code>%s</code>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td></tr>"
            % (
                escape(_string(item.get("name"))),
                _live_file_error(item),
                escape(_string(item.get("bytes"))),
                escape(_string(item.get("mtime_utc"))),
                _live_excerpt(item.get("excerpt") if isinstance(item.get("excerpt"), dict) else {}),
            )
        )
    rows.append("</tbody></table>%s" % closing)
    return "".join(rows)


def _live_file_error(item: Dict) -> str:
    if not item.get("error"):
        return ""
    return '<div class="warn">%s</div>' % escape(_string(item.get("error")))


def _live_excerpt(excerpt: Dict) -> str:
    if excerpt.get("error"):
        return '<span class="muted">%s</span>' % escape(_string(excerpt.get("error")))
    text = _string(excerpt.get("text")).strip()
    if not text:
        return '<span class="muted">No excerpt</span>'
    return "<details><summary>View excerpt</summary><pre>%s</pre></details>" % escape(text)


def _live_step_timing(step: Dict) -> str:
    parts = []
    duration = _format_duration(step.get("duration_ms"))
    if duration:
        parts.append(duration)
    if step.get("started_at_utc"):
        parts.append("started %s" % _string(step.get("started_at_utc")))
    if step.get("finished_at_utc"):
        parts.append("finished %s" % _string(step.get("finished_at_utc")))
    if step.get("agent_items_total"):
        parts.append(
            "%s item(s), %s cached" % (_string(step.get("agent_items_total")), _string(step.get("agent_items_cached") or 0))
        )
    return " / ".join(parts)


def _live_hook_timing(event: Dict) -> str:
    parts = []
    duration = _format_duration(event.get("duration_ms"))
    if duration:
        parts.append(duration)
    if event.get("returncode") is not None:
        parts.append("exit %s" % _string(event.get("returncode")))
    if event.get("timed_out"):
        parts.append("timed out")
    if event.get("on_failure"):
        parts.append("on failure: %s" % _string(event.get("on_failure")))
    return " / ".join(parts)


def _live_hook_step(event: Dict) -> str:
    lines = []
    if event.get("step_id"):
        lines.append("step %s" % _string(event.get("step_id")))
    if event.get("member_id") or event.get("task_id"):
        identity = "%s / %s" % (
            _string(event.get("member_id")) or "member",
            _string(event.get("task_id")) or "task",
        )
        position = []
        if event.get("generation") is not None:
            position.append("generation %s" % _string(event.get("generation")))
        if event.get("round") is not None:
            position.append("round %s" % _string(event.get("round")))
        if isinstance(event.get("quality_retry_index"), int) and event["quality_retry_index"] > 0:
            position.append("quality retry %s" % _string(event["quality_retry_index"]))
        if position:
            identity += " (%s)" % ", ".join(position)
        lines.append(identity)
    if event.get("hook_input_sha256"):
        lines.append("input %s" % _string(event.get("hook_input_sha256"))[:12])
    return "".join('<div class="muted">%s</div>' % escape(line) for line in lines)


def _public_value(value):
    if isinstance(value, Path):
        return redact_text(str(value))
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        public = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in {"path", "workflow_source"} or key_text.endswith("_path"):
                continue
            public[redact_text(key_text)] = _public_value(item)
        return public
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def _public_run(run: Dict) -> Dict:
    public = _public_value(run)
    if isinstance(public, dict):
        public["run_handle"] = run_handle(str(run.get("id") or ""))
    return public


def _render_final_report_summary(excerpt) -> str:
    if not isinstance(excerpt, dict):
        return '<span class="muted">No final report yet</span>'
    if excerpt.get("error"):
        return '<span class="muted">%s</span>' % escape(_string(excerpt.get("error")))
    text = _string(excerpt.get("text")).strip()
    if not text:
        return '<span class="muted">No final report yet</span>'
    return escape(text[:300])


def _return_to_input(return_to: str) -> str:
    if not return_to:
        return ""
    return '<input type="hidden" name="return_to" value="%s">' % escape(return_to)


def _validated_return_to(runs_dir: Path, value: str) -> str:
    if not value:
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        raise ValidationError("return_to must be a local path without query or fragment")
    if value == "/":
        return "/"
    if value.startswith(PRIVATE_TRANSCRIPT_PATH_PREFIX):
        run_handle_value, step_handle_value, transcript_id = (
            _parse_private_transcript_path(value)
        )
        _resolve_private_transcript_view(
            runs_dir,
            run_handle_value,
            step_handle_value,
            transcript_id,
        )
        return _private_transcript_path(
            run_handle_value,
            step_handle_value,
            transcript_id,
        )
    if value.startswith("/run/"):
        handle = unquote(value[len("/run/") :])
        run_dir = resolve_live_run_handle(runs_dir, handle)
        return "/run/%s" % quote(run_handle(run_dir.name), safe="")
    raise ValidationError(
        "return_to must be /, /run/<handle>, or a private transcript path"
    )


def _with_return_to(result: Dict, return_to: str) -> Dict:
    result = dict(result)
    result["return_to"] = return_to
    return result


def _control_redirect_message(result: Dict) -> str:
    return "%s %s: %s" % (
        _string(result.get("action")),
        _string(result.get("routine") or result.get("requested_step") or "run"),
        _string(result.get("status")),
    )


def _field(form: Dict[str, str], name: str, required: bool = True) -> str:
    value = form.get(name, "")
    if required and not value:
        raise ValidationError("missing form field: %s" % name)
    return value


def _live_packet_index(value: str) -> int:
    if not isinstance(value, str) or not value.isdigit() or value.startswith("0"):
        raise ValidationError("packet_index must be a positive integer")
    index = int(value)
    if index < 1:
        raise ValidationError("packet_index must be a positive integer")
    return index


def _parse_control_form(content_length: str, reader) -> Dict[str, str]:
    try:
        length = int(content_length)
    except ValueError:
        raise ValidationError("control request has invalid Content-Length")
    if length < 0 or length > MAX_CONTROL_REQUEST_BYTES:
        raise ValidationError("control request is too large")
    try:
        body = reader.read(length).decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("control request body must be utf-8")
    return {key: _first(values) for key, values in parse_qs(body, keep_blank_values=True).items()}


def _validate_post_origin(headers) -> None:
    origin = headers.get("Origin")
    if not origin:
        return
    host = headers.get("Host", "")
    parsed = urlparse(origin)
    if parsed.scheme != "http" or not host or parsed.netloc != host:
        raise ValidationError("control request Origin does not match server")


def _control_lock(run_dir: Path) -> threading.Lock:
    key = str(run_dir.resolve())
    with _CONTROL_LOCKS_GUARD:
        lock = _CONTROL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CONTROL_LOCKS[key] = lock
        return lock


def _first(values) -> str:
    return values[0] if values else ""


def _string(value) -> str:
    if value is None:
        return ""
    return redact_text(str(value))


def _status_class(value) -> str:
    status = _string(value).replace(" ", "_")
    return "".join(char if char.isalnum() or char in "_-" else "_" for char in status) or "unknown"


def _format_duration(value) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return ""
    if value < 1000:
        return "%d ms" % value
    seconds = value / 1000.0
    if seconds < 60:
        return "%.1f s" % seconds
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return "%dm %02ds" % (minutes, remaining)


def _usage_label(usage: Dict) -> str:
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        label = "%s tokens" % total
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            label += ", $%.4f" % cost
        return redact_text(label)
    return redact_text(_string(usage.get("status") or "unavailable"))


def _runner_label(runner: Dict) -> str:
    status = _string(runner.get("status") if isinstance(runner, dict) else "") or "unavailable"
    if status == "unavailable":
        return "unavailable"
    parts = [status]
    liveness = _string(runner.get("liveness"))
    if liveness and liveness not in {"unavailable", "finished"}:
        parts.append(liveness)
    heartbeat_age = runner.get("heartbeat_age_seconds")
    if isinstance(heartbeat_age, int) and not isinstance(heartbeat_age, bool):
        parts.append("age %s" % _format_duration(heartbeat_age * 1000))
    mode = _string(runner.get("mode"))
    if mode:
        parts.append(mode)
    pid = runner.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parts.append("pid %s" % pid)
    heartbeat = _string(runner.get("heartbeat_at_utc"))
    if heartbeat:
        parts.append("heartbeat %s" % heartbeat)
    return redact_text(", ".join(parts))


def _launch_label(launch: Dict) -> str:
    if not isinstance(launch, dict):
        return "unavailable"
    status = _string(launch.get("status")) or "unavailable"
    if status == "unavailable":
        if not launch.get("error") or launch.get("error") == "missing":
            return "unavailable"
    parts = [status]
    preflight = _string(launch.get("preflight_status"))
    if preflight:
        parts.append("preflight %s" % preflight)
    if launch.get("no_process_started") is True:
        parts.append("no process started")
    policy = launch.get("policy") if isinstance(launch.get("policy"), dict) else {}
    approval_count = policy.get("approval_count")
    if isinstance(approval_count, int) and not isinstance(approval_count, bool) and approval_count > 0:
        parts.append("%s approval(s)" % approval_count)
    external = launch.get("external_supervisor") if isinstance(launch.get("external_supervisor"), dict) else {}
    if external.get("approval_tokens_required_at_launch") is True:
        parts.append("approval tokens required")
    if launch.get("error"):
        parts.append(_string(launch.get("error")) or "error")
    return redact_text(", ".join(parts))


def _background_label(background: Dict) -> str:
    if not isinstance(background, dict):
        return "unavailable"
    status = _string(background.get("status")) or "unavailable"
    if status == "unavailable" and background.get("error") in {None, "", "missing"}:
        return "unavailable"
    parts = [status]
    run_status = _string(background.get("run_status"))
    if run_status and run_status != "unknown":
        parts.append("run %s" % run_status)
    pid = background.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parts.append("pid %s" % pid)
    if background.get("detached_session") is True:
        parts.append("isolated")
    if background.get("error_class"):
        parts.append(_string(background.get("error_class")))
    return redact_text(", ".join(parts))


def _render_launch_summary_cell(launch: Dict) -> str:
    label = _launch_label(launch)
    if label == "unavailable":
        return '<span class="muted">unavailable</span>'
    rows = [escape(label)]
    command = _string(launch.get("resume_command")) if isinstance(launch, dict) else ""
    if command:
        suffix = " (truncated)" if launch.get("resume_command_truncated") else ""
        rows.append('<div class="muted">Resume command%s</div><code>%s</code>' % (escape(suffix), escape(command)))
    command_error = _string(launch.get("resume_command_error")) if isinstance(launch, dict) else ""
    if command_error:
        rows.append('<div class="warn">Resume command: %s</div>' % escape(command_error))
    return "".join(rows)


def _validate_bind(host: str, port: int, allow_remote: bool) -> None:
    if not isinstance(host, str) or not host:
        raise ValidationError("serve host must be non-empty")
    if not isinstance(port, int) or port < 0 or port > 65535:
        raise ValidationError("serve port must be an integer from 0 to 65535")
    if not allow_remote and host not in LOOPBACK_HOSTS:
        raise ValidationError("serve host must be loopback unless --allow-remote is supplied")
