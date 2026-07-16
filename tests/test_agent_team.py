import hashlib
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlencode

from conductor_extras.runtime.agent_profiles import AGENT_PROFILE_SCHEMA
from conductor_extras.runtime.agent_lifecycle_context import (
    AGENT_LIFECYCLE_CONTEXT_BEGIN,
    load_agent_lifecycle_context_receipt,
)
from conductor_extras.runtime.agent_team import (
    AGENT_TEAM_STATE_SCHEMA,
    AGENT_TEAM_STATE_SCHEMA_V5,
    AGENT_TEAM_STATE_SCHEMA_V4,
    AGENT_TEAM_STATE_SCHEMA_V3,
    AGENT_TEAM_STATE_SCHEMA_V2,
    AGENT_TEAM_STATE_SCHEMA_V1,
    AGENT_TEAM_TURN_SCHEMA,
    AGENT_TEAM_TURN_SCHEMA_V1,
    apply_agent_team_interruption,
    agent_team_state_summary,
    assign_agent_team_tasks,
    build_agent_team_interruption,
    initial_agent_team_state,
    load_agent_team_state,
    parse_agent_team_turn,
    prepare_agent_team_operator_tasks,
    prepare_agent_team_task_proposals,
    validate_agent_team_state,
    verify_agent_team_state_outputs,
    write_agent_team_state,
)
from conductor_extras.runtime.agent_team_chat import (
    AGENT_TEAM_OPERATOR_CHAT_SCHEMA,
    agent_team_operator_chat_summary,
    load_agent_team_operator_chat,
    validate_agent_team_operator_chat,
    verify_agent_team_operator_chat,
)
from conductor_extras.runtime.agent_team_transcript import (
    AGENT_TEAM_TRANSCRIPT_SCHEMA,
    AgentTeamTranscriptWriter,
    load_agent_team_transcript,
    validate_agent_team_transcript,
)
from conductor_extras.runtime.agent_team_operator import (
    AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
    AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
    append_agent_team_operator_entry,
    agent_team_operator_inbox_summary,
    initial_agent_team_operator_inbox,
    load_agent_team_operator_inbox,
    reconcile_agent_team_operator_interruptions,
    validate_agent_team_operator_inbox,
    verify_agent_team_operator_inbox,
)
from conductor_extras.runtime.agent_team_merge import (
    AGENT_TEAM_MERGE_INTENT_SCHEMA,
    AGENT_TEAM_MERGE_LEDGER_SCHEMA,
    AGENT_TEAM_MERGE_RECOVERY_SCHEMA,
    AGENT_TEAM_MERGE_TRANSACTION_SCHEMA,
    agent_team_merge_intent_summary,
    agent_team_merge_summary,
    load_agent_team_merge_intent,
    load_agent_team_merge_recovery,
    load_agent_team_merge_transaction,
    load_agent_team_merge_ledger,
)
from conductor_extras.runtime.agent_team_hooks import (
    AGENT_TEAM_HOOK_INPUT_SCHEMA,
    agent_team_hook_input_json,
    agent_team_hook_input_sha256,
    build_agent_team_hook_input,
    validate_agent_team_hook_input,
)
from conductor_extras.runtime.agent_team_quality_retry import (
    AGENT_TEAM_QUALITY_RETRY_SCHEMA,
    build_agent_team_quality_retry,
    finalize_agent_team_quality_retry,
    list_agent_team_quality_retry_history,
    list_agent_team_quality_retry_pending,
    load_agent_team_quality_retry,
    validate_agent_team_quality_retry,
)
from conductor_extras.runtime.agent_team_turn_completion import (
    AGENT_TEAM_TURN_COMPLETION_SCHEMA,
    agent_team_turn_completion_summary,
    list_agent_team_turn_completion_paths,
    load_agent_team_turn_completion,
)
from conductor_extras.runtime.agent_team_turn_terminal import (
    AGENT_TEAM_TURN_TERMINAL_SCHEMA,
    agent_team_turn_terminal_summary,
    list_agent_team_turn_terminal_paths,
    load_agent_team_turn_terminal,
)
from conductor_extras.runtime.agent_team_plan_approval import (
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1,
    AGENT_TEAM_PLAN_REVIEW_SCHEMA,
    AGENT_TEAM_PLAN_SCHEMA,
    agent_team_plan_approval_summary,
    load_agent_team_plan_approval,
    validate_agent_team_plan_approval,
    verify_agent_team_plan_approval_outputs,
)
from conductor_runtime.errors import PolicyError, StepExecutionError, ValidationError
from conductor_extras.cli import main as cli_main
from conductor_extras.runtime.dashboard import (
    collect_run_detail,
    run_handle,
    step_handle,
    write_dashboard,
)
from conductor_extras.runtime.live_server import (
    _handler_factory,
    apply_live_control,
    render_live_run_detail,
)
from conductor_extras.runtime.runner import ProcessResult, WorkflowRunner
from conductor_extras.runtime.run_control import (
    list_team_inbox,
    list_team_plans,
    list_team_questions,
    list_team_transcripts,
    queue_team_task,
    read_team_console_snapshot,
    read_team_question,
    read_team_plan,
    read_team_transcript,
    reset_step,
    review_team_plan,
    retry_step,
)
from conductor_extras.runtime.team_console import (
    render_team_console,
    team_console_is_terminal,
    validate_team_console_snapshot,
)
from conductor_extras.runtime.schemas import get_schema
from conductor_extras.runtime.security import RuntimePolicy, assess_command
from conductor_extras.runtime.staged_workspace import snapshot_workspace
from conductor_extras.runtime.workflow import validate_workflow, workflow_fingerprint, workflow_summary


SESSION_IDS = {
    "lead": "11111111-1111-4111-8111-111111111111",
    "security": "22222222-2222-4222-8222-222222222222",
    "correctness": "33333333-3333-4333-8333-333333333333",
    "worker": "44444444-4444-4444-8444-444444444444",
}


def team_profile(name):
    return {
        "schema": AGENT_PROFILE_SCHEMA,
        "name": name + "-profile",
        "instructions": "Inspect only assigned team work and report concrete evidence.",
        "sandbox": "read-only",
        "risk": "medium",
    }


def write_team_profile(name):
    profile = team_profile(name)
    profile["sandbox"] = "workspace-write"
    return profile


def team_member(member_id, role, lead=False):
    return {
        "id": member_id,
        "role": role,
        "instructions": "Perform the %s role and communicate material evidence." % role,
        "agent_profile": member_id + "-profile",
        "lead": lead,
    }


def coordinated_workflow():
    return {
        "schema": "conductor.workflow.v1",
        "name": "coordinated-review",
        "mode": "read_only",
        "max_workers": 3,
        "agent_profiles": [
            team_profile("lead"),
            team_profile("security"),
            team_profile("correctness"),
        ],
        "steps": [
            {
                "id": "team-review",
                "kind": "agent_team",
                "risk": "medium",
                "members": [
                    team_member("lead", "synthesis lead", lead=True),
                    team_member("security", "security reviewer"),
                    team_member("correctness", "correctness reviewer"),
                ],
                "tasks": [
                    {
                        "id": "security-audit",
                        "description": "Audit authentication boundaries and report concrete risks.",
                        "assignee": "security",
                        "depends_on": [],
                    },
                    {
                        "id": "correctness-audit",
                        "description": "Audit retry behavior and boundary handling.",
                        "assignee": "correctness",
                        "depends_on": [],
                    },
                    {
                        "id": "synthesize",
                        "description": "Synthesize and challenge both audits into a prioritized report.",
                        "assignee": "lead",
                        "depends_on": ["security-audit", "correctness-audit"],
                    },
                ],
                "max_workers": 3,
                "max_rounds": 4,
                "max_tokens": 1000,
                "max_total_tokens": 12000,
                "capture_dir": "team-review-internal",
                "report": "team-review.md",
            }
        ],
    }


def continuation_workflow():
    workflow = {
        "schema": "conductor.workflow.v1",
        "name": "continuing-team",
        "mode": "read_only",
        "max_workers": 1,
        "agent_profiles": [team_profile("lead"), team_profile("worker")],
        "steps": [
            {
                "id": "team-debug",
                "kind": "agent_team",
                "risk": "medium",
                "members": [
                    team_member("lead", "lead", lead=True),
                    team_member("worker", "debugger"),
                ],
                "tasks": [
                    {
                        "id": "debug",
                        "description": "Investigate the failure and complete after a second pass.",
                        "assignee": "worker",
                        "depends_on": [],
                    }
                ],
                "max_workers": 1,
                "max_rounds": 3,
                "max_tokens": 1000,
                "max_total_tokens": 4000,
                "capture_dir": "team-debug-internal",
                "report": "team-debug.md",
            }
        ],
    }
    return workflow


def active_messaging_workflow(*, max_tasks=3, max_depth=2, allow_broadcast=False):
    return {
        "schema": "conductor.workflow.v1",
        "name": "active-messaging-team",
        "mode": "read_only",
        "max_workers": 1,
        "agent_profiles": [team_profile("lead"), team_profile("worker")],
        "steps": [
            {
                "id": "team-debate",
                "kind": "agent_team",
                "risk": "medium",
                "members": [
                    team_member("lead", "challenge lead", lead=True),
                    team_member("worker", "primary investigator"),
                ],
                "tasks": [
                    {
                        "id": "investigate",
                        "description": "Investigate one hypothesis and ask for a challenge when useful.",
                        "assignee": "worker",
                        "depends_on": [],
                    }
                ],
                "max_workers": 1,
                "max_rounds": 4,
                "max_tokens": 2000,
                "max_total_tokens": 8000,
                "active_messaging": {
                    "max_tasks": max_tasks,
                    "max_depth": max_depth,
                    "max_tokens": 1000,
                    "allow_broadcast": allow_broadcast,
                },
                "capture_dir": "team-debate-internal",
                "report": "team-debate.md",
            }
        ],
    }


def operator_chat_workflow(*, max_questions=2, reply_timeout_seconds=30):
    workflow = continuation_workflow()
    workflow["name"] = "operator-chat-team"
    step = workflow["steps"][0]
    step["max_rounds"] = 4
    step["max_total_tokens"] = 8000
    step["operator_chat"] = {
        "max_questions": max_questions,
        "max_tokens": 500,
        "reply_timeout_seconds": reply_timeout_seconds,
    }
    return workflow


def operator_console_workflow(*, max_events=64, max_bytes=64 * 1024):
    workflow = continuation_workflow()
    workflow["name"] = "operator-console-team"
    workflow["steps"][0]["operator_console"] = {
        "max_events": max_events,
        "max_bytes": max_bytes,
    }
    return workflow


def adaptive_workflow():
    return {
        "schema": "conductor.workflow.v1",
        "name": "adaptive-team",
        "mode": "read_only",
        "max_workers": 2,
        "agent_profiles": [team_profile("lead"), team_profile("worker")],
        "steps": [
            {
                "id": "adaptive-review",
                "kind": "agent_team",
                "risk": "medium",
                "members": [
                    team_member("lead", "adaptive lead", lead=True),
                    team_member("worker", "focused investigator"),
                ],
                "tasks": [
                    {
                        "id": "plan-adaptive",
                        "description": "Inspect the bounded review target and plan only material follow-up work.",
                        "assignee": "lead",
                        "depends_on": [],
                    }
                ],
                "max_workers": 2,
                "max_rounds": 4,
                "max_dynamic_tasks": 2,
                "max_tokens": 1000,
                "max_total_tokens": 8000,
                "capture_dir": "adaptive-review-internal",
                "report": "adaptive-review.md",
            }
        ],
    }


def write_team_workflow():
    return {
        "schema": "conductor.workflow.v1",
        "name": "isolated-write-team",
        "mode": "workspace_write",
        "max_workers": 2,
        "agent_profiles": [write_team_profile("lead"), write_team_profile("worker")],
        "steps": [
            {
                "id": "team-implementation",
                "kind": "agent_team",
                "risk": "medium",
                "sandbox": "workspace-write",
                "members": [
                    team_member("lead", "implementation lead", lead=True),
                    team_member("worker", "implementation worker"),
                ],
                "tasks": [
                    {
                        "id": "lead-edit",
                        "description": "Apply the lead-owned bounded edit.",
                        "assignee": "lead",
                        "depends_on": [],
                    },
                    {
                        "id": "worker-edit",
                        "description": "Apply the worker-owned bounded edit.",
                        "assignee": "worker",
                        "depends_on": [],
                    },
                ],
                "max_workers": 2,
                "max_rounds": 3,
                "max_tokens": 1000,
                "max_total_tokens": 6000,
                "capture_dir": "team-implementation-internal",
                "report": "team-implementation.md",
            }
        ],
    }


def single_write_team_workflow():
    workflow = write_team_workflow()
    workflow["name"] = "single-isolated-write-team"
    workflow["max_workers"] = 1
    step = workflow["steps"][0]
    step["members"] = step["members"]
    step["tasks"] = [step["tasks"][0]]
    step["max_workers"] = 1
    step["max_rounds"] = 2
    step["max_total_tokens"] = 3000
    return workflow


def plan_approval_workflow():
    return {
        "schema": "conductor.workflow.v1",
        "name": "plan-approved-write-team",
        "mode": "workspace_write",
        "max_workers": 1,
        "agent_profiles": [write_team_profile("lead"), write_team_profile("worker")],
        "steps": [
            {
                "id": "team-planned-implementation",
                "kind": "agent_team",
                "risk": "medium",
                "sandbox": "workspace-write",
                "members": [
                    team_member("lead", "plan approval lead", lead=True),
                    team_member("worker", "implementation worker"),
                ],
                "tasks": [
                    {
                        "id": "worker-edit",
                        "description": "Apply one bounded worker-owned edit after plan approval.",
                        "assignee": "worker",
                        "depends_on": [],
                    }
                ],
                "plan_approval": {
                    "task_ids": ["worker-edit"],
                    "criteria": "Approve only a minimal edit with a deterministic verification step.",
                    "max_revisions": 1,
                    "plan_max_tokens": 100,
                    "review_max_tokens": 100,
                },
                "max_workers": 1,
                "max_rounds": 2,
                "max_tokens": 1000,
                "max_total_tokens": 4000,
                "capture_dir": "team-planned-implementation-internal",
                "report": "team-planned-implementation.md",
            }
        ],
    }


def operator_plan_approval_workflow(*, reply_timeout_seconds=30):
    workflow = plan_approval_workflow()
    workflow["name"] = "operator-plan-approved-write-team"
    policy = workflow["steps"][0]["plan_approval"]
    policy.pop("review_max_tokens")
    policy["reviewer"] = "operator"
    policy["reply_timeout_seconds"] = reply_timeout_seconds
    return workflow


class SimulatedHardCrash(BaseException):
    pass


class FakeTeamRunner(WorkflowRunner):
    def __init__(
        self,
        *args,
        continue_once=False,
        fail_once_tasks=None,
        legacy_turns=False,
        invalid_proposals=False,
        quality_gate_failures=0,
        quality_gate_exit_code=2,
        quality_gate_timed_out=False,
        quality_retry_crash_phase=None,
        turn_completion_crash=False,
        turn_completion_crash_phase=None,
        turn_completion_crash_member=None,
        turn_terminal_crash_phase=None,
        turn_terminal_crash_member=None,
        turn_terminal_crash_retry_index=None,
        first_turn_started=None,
        release_first_turn=None,
        interrupt_first_turn=False,
        interrupt_member_id=None,
        message_overrides=None,
        **kwargs,
    ):
        self.calls = []
        self.hook_calls = []
        self._calls_lock = threading.Lock()
        self.continue_once = continue_once
        self.fail_once_tasks = set(fail_once_tasks or [])
        self.legacy_turns = legacy_turns
        self.invalid_proposals = invalid_proposals
        self.quality_gate_failures = quality_gate_failures
        self.quality_gate_exit_code = quality_gate_exit_code
        self.quality_gate_timed_out = quality_gate_timed_out
        self.quality_retry_crash_phase = quality_retry_crash_phase
        self.turn_completion_crash = turn_completion_crash
        self.turn_completion_crash_phase = (
            turn_completion_crash_phase
            or ("written" if turn_completion_crash else None)
        )
        self.turn_completion_crash_member = turn_completion_crash_member
        self.turn_terminal_crash_phase = turn_terminal_crash_phase
        self.turn_terminal_crash_member = turn_terminal_crash_member
        self.turn_terminal_crash_retry_index = turn_terminal_crash_retry_index
        self.first_turn_started = first_turn_started
        self.release_first_turn = release_first_turn
        self.interrupt_first_turn = interrupt_first_turn
        self.interrupt_member_id = interrupt_member_id
        self.message_overrides = {
            task_id: [dict(message) for message in messages]
            for task_id, messages in (message_overrides or {}).items()
        }
        self.quality_retry_crashed = False
        self.turn_completion_crashed = False
        self.turn_terminal_crashed = False
        self.quality_gate_calls = 0
        self.task_attempts = {}
        super().__init__(*args, **kwargs)

    def _run_process(
        self,
        argv,
        cwd,
        timeout,
        input_text=None,
        output_limit_bytes=1024 * 1024,
        env=None,
        process_event_callback=None,
        stdout_line_callback=None,
        interrupt_check=None,
    ):
        if "--output-last-message" not in argv:
            self.hook_calls.append(
                {"argv": list(argv), "cwd": Path(cwd), "input": input_text or ""}
            )
            if argv[:2] == ["grep", "QUALITY_GATE_SIMULATED"]:
                self.quality_gate_calls += 1
                if self.quality_gate_timed_out:
                    return ProcessResult(
                        returncode=124,
                        stdout="",
                        stderr="quality gate timed out",
                        timed_out=True,
                    )
                if self.quality_gate_calls <= self.quality_gate_failures:
                    return ProcessResult(
                        returncode=self.quality_gate_exit_code,
                        stdout="",
                        stderr="repair the quality boundary before completion",
                    )
                return ProcessResult(returncode=0, stdout="quality accepted\n", stderr="")
            return super()._run_process(
                argv,
                cwd=Path(cwd),
                timeout=timeout,
                input_text=input_text,
                output_limit_bytes=output_limit_bytes,
                stdout_line_callback=stdout_line_callback,
                interrupt_check=interrupt_check,
            )
        del cwd, output_limit_bytes, env, process_event_callback
        output = Path(argv[argv.index("--output-last-message") + 1])
        stem = output.stem
        member_id, task_id = stem.split("--", 1)
        with self._calls_lock:
            attempt = self.task_attempts.get(task_id, 0) + 1
            self.task_attempts[task_id] = attempt
            self.calls.append(
                {
                    "argv": list(argv),
                    "prompt": input_text or "",
                    "member": member_id,
                    "task": task_id,
                    "interrupt_polling": interrupt_check is not None,
                    "stream_callback": stdout_line_callback is not None,
                }
            )
            call_count = len(self.calls)
            member_call_count = sum(
                call["member"] == member_id for call in self.calls
            )
        if call_count == 1 and self.first_turn_started is not None:
            self.first_turn_started.set()
            if self.release_first_turn is not None:
                self.release_first_turn.wait(timeout=5)
        if task_id in self.fail_once_tasks and attempt == 1:
            return ProcessResult(returncode=17, stdout="", stderr="simulated teammate failure")
        if "resume" in argv:
            session_id = argv[argv.index("resume") + 1]
        else:
            session_id = SESSION_IDS[member_id]
        started = json.dumps({"type": "thread.started", "thread_id": session_id})
        interrupt_target = self.interrupt_first_turn and (
            (self.interrupt_member_id is None and call_count == 1)
            or (self.interrupt_member_id == member_id and member_call_count == 1)
        )
        if interrupt_target:
            if stdout_line_callback is None or interrupt_check is None:
                raise AssertionError("interruptible turn omitted runtime callbacks")
            stdout_line_callback(started)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if interrupt_check():
                    return ProcessResult(
                        returncode=-9,
                        stdout=started + "\n",
                        stderr="",
                        interrupted=True,
                    )
                time.sleep(0.01)
            raise AssertionError("interrupt request was not observed")
        if task_id == "security-audit":
            summary = "Security found an authentication boundary issue."
            messages = [{"to": "lead", "body": "Prioritize the authentication boundary finding."}]
            status = "completed"
        elif task_id == "correctness-audit":
            summary = "Correctness found a retry boundary issue."
            messages = [{"to": "lead", "body": "Cross-check the retry boundary with security impact."}]
            status = "completed"
        elif task_id == "synthesize":
            summary = "Lead synthesized authentication and retry findings."
            messages = []
            status = "completed"
        elif task_id == "plan-adaptive":
            summary = "Lead identified one investigation and a dependent synthesis task."
            messages = []
            status = "completed"
        elif task_id == "investigate-adaptive":
            summary = "Worker verified the adaptive boundary with concrete evidence."
            messages = [{"to": "lead", "body": "The adaptive boundary is verified."}]
            status = "completed"
        elif task_id == "synthesize-adaptive":
            summary = "Lead synthesized the verified adaptive finding."
            messages = []
            status = "completed"
        else:
            summary = "Debugger needs another pass." if attempt == 1 else "Debugger confirmed the root cause."
            messages = []
            status = "continue" if self.continue_once and attempt == 1 else "completed"
        if task_id in self.message_overrides:
            messages = [dict(message) for message in self.message_overrides[task_id]]
        turn = {
            "schema": AGENT_TEAM_TURN_SCHEMA_V1 if self.legacy_turns else AGENT_TEAM_TURN_SCHEMA,
            "task_id": task_id,
            "status": status,
            "summary": summary,
            "messages": messages,
            "claim_task_ids": [],
        }
        if not self.legacy_turns:
            turn["task_proposals"] = []
            if task_id == "plan-adaptive":
                turn["task_proposals"] = [
                    {
                        "id": "investigate-adaptive",
                        "description": "Verify the adaptive boundary and capture concrete evidence.",
                        "assignee": "worker",
                        "depends_on": ["plan-adaptive"],
                    },
                    {
                        "id": "synthesize-adaptive",
                        "description": "Challenge and synthesize the verified adaptive evidence.",
                        "assignee": "lead",
                        "depends_on": ["investigate-adaptive"],
                    },
                ]
                if self.invalid_proposals:
                    turn["task_proposals"].append(
                        {
                            "id": "excess-adaptive",
                            "description": "This proposal exceeds the configured dynamic-task allowance.",
                            "assignee": "worker",
                            "depends_on": ["plan-adaptive"],
                        }
                    )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(turn), encoding="utf-8")
        completed = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            }
        )
        streamed = [
            started,
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-tool",
                        "type": "command_execution",
                        "command": "PRIVATE_CONSOLE_COMMAND",
                        "aggregated_output": "PRIVATE_CONSOLE_TOOL_OUTPUT",
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-message",
                        "type": "agent_message",
                        "text": "Private console teammate update.",
                    },
                }
            ),
            completed,
        ]
        if stdout_line_callback is not None:
            for line in streamed:
                stdout_line_callback(line)
        return ProcessResult(
            returncode=0,
            stdout="\n".join(streamed) + "\n",
            stderr="",
        )

    def _agent_team_quality_retry_checkpoint(self, phase, checkpoint):
        del checkpoint
        if not self.quality_retry_crashed and phase == self.quality_retry_crash_phase:
            self.quality_retry_crashed = True
            raise SimulatedHardCrash(phase)

    def _agent_team_turn_completion_checkpoint(self, phase, completion):
        if (
            not self.turn_completion_crashed
            and self.turn_completion_crash_phase is not None
            and phase == self.turn_completion_crash_phase
            and (
                self.turn_completion_crash_member is None
                or completion["member_id"] == self.turn_completion_crash_member
            )
        ):
            self.turn_completion_crashed = True
            raise SimulatedHardCrash(phase)

    def _agent_team_turn_terminal_checkpoint(self, phase, terminal):
        if (
            not self.turn_terminal_crashed
            and self.turn_terminal_crash_phase is not None
            and phase == self.turn_terminal_crash_phase
            and (
                self.turn_terminal_crash_member is None
                or terminal["member_id"] == self.turn_terminal_crash_member
            )
            and (
                self.turn_terminal_crash_retry_index is None
                or terminal["quality_retry_index"]
                == self.turn_terminal_crash_retry_index
            )
        ):
            self.turn_terminal_crashed = True
            raise SimulatedHardCrash(phase)


class FakeWriteTeamRunner(WorkflowRunner):
    def __init__(
        self,
        *args,
        identical=False,
        fail_once_tasks=None,
        crash_phase=None,
        multi_file=False,
        quality_gate_failures=0,
        quality_gate_exit_code=2,
        first_turn_started=None,
        interrupt_first_turn=False,
        message_overrides=None,
        crash_member=None,
        **kwargs,
    ):
        self.calls = []
        self.hook_calls = []
        self.identical = identical
        self.fail_once_tasks = set(fail_once_tasks or [])
        self.crash_phase = crash_phase
        self.crash_member = crash_member
        self.multi_file = multi_file
        self.quality_gate_failures = quality_gate_failures
        self.quality_gate_exit_code = quality_gate_exit_code
        self.first_turn_started = first_turn_started
        self.interrupt_first_turn = interrupt_first_turn
        self.message_overrides = {
            task_id: [dict(message) for message in messages]
            for task_id, messages in (message_overrides or {}).items()
        }
        self.quality_gate_calls = 0
        self.crashed = False
        self.task_attempts = {}
        self._calls_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _run_process(
        self,
        argv,
        cwd,
        timeout,
        input_text=None,
        output_limit_bytes=1024 * 1024,
        env=None,
        process_event_callback=None,
        stdout_line_callback=None,
        interrupt_check=None,
    ):
        if "--output-last-message" not in argv:
            self.hook_calls.append(
                {"argv": list(argv), "cwd": Path(cwd), "input": input_text or ""}
            )
            if argv[:2] == ["grep", "QUALITY_GATE_SIMULATED"]:
                self.quality_gate_calls += 1
                if self.quality_gate_calls <= self.quality_gate_failures:
                    return ProcessResult(
                        returncode=self.quality_gate_exit_code,
                        stdout="",
                        stderr="repair the staged quality boundary",
                    )
                return ProcessResult(returncode=0, stdout="quality accepted\n", stderr="")
            return super()._run_process(
                argv,
                cwd=Path(cwd),
                timeout=timeout,
                input_text=input_text,
                output_limit_bytes=output_limit_bytes,
                stdout_line_callback=stdout_line_callback,
                interrupt_check=interrupt_check,
            )
        del output_limit_bytes, env, process_event_callback
        output = Path(argv[argv.index("--output-last-message") + 1])
        member_id, task_id = output.stem.split("--", 1)
        with self._calls_lock:
            attempt = self.task_attempts.get(task_id, 0) + 1
            self.task_attempts[task_id] = attempt
            call_record = {
                "argv": list(argv),
                "cwd": Path(cwd),
                "prompt": input_text or "",
                "member": member_id,
                "task": task_id,
                "attempt": attempt,
            }
            self.calls.append(call_record)
        self.assert_external_team_workspace(Path(cwd), argv)
        if task_id in self.fail_once_tasks and attempt == 1:
            return ProcessResult(returncode=17, stdout="", stderr="simulated isolated teammate failure")
        target = Path(cwd) / "shared.txt"
        call_record["source_before"] = target.read_text(encoding="utf-8")
        if "resume" in argv:
            session_id = argv[argv.index("resume") + 1]
        else:
            session_id = SESSION_IDS[member_id]
        started = json.dumps({"type": "thread.started", "thread_id": session_id})
        if len(self.calls) == 1 and self.interrupt_first_turn:
            target.write_text("partial-interrupted\n", encoding="utf-8")
            if stdout_line_callback is None or interrupt_check is None:
                raise AssertionError("interruptible write turn omitted runtime callbacks")
            stdout_line_callback(started)
            if self.first_turn_started is not None:
                self.first_turn_started.set()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if interrupt_check():
                    return ProcessResult(
                        returncode=-9,
                        stdout=started + "\n",
                        stderr="",
                        interrupted=True,
                    )
                time.sleep(0.01)
            raise AssertionError("write-team interrupt request was not observed")
        if task_id == "lead-edit":
            target.write_text("lead\n", encoding="utf-8")
            if self.multi_file:
                (Path(cwd) / "second.txt").write_text("second-lead\n", encoding="utf-8")
            summary = "Lead applied the first isolated edit."
        elif task_id.startswith("operator-g"):
            summary = "Worker acknowledged the bounded operator redirect."
        elif self.identical:
            target.write_text("lead\n", encoding="utf-8")
            summary = "Worker independently produced the identical edit."
        elif attempt == 1 and target.read_text(encoding="utf-8") == "base\n":
            target.write_text("worker\n", encoding="utf-8")
            summary = "Worker produced a conflicting first edit."
        else:
            target.write_text(target.read_text(encoding="utf-8") + "worker\n", encoding="utf-8")
            summary = "Worker rebased its edit on the deterministic merged source."
        turn = {
            "schema": AGENT_TEAM_TURN_SCHEMA,
            "task_id": task_id,
            "status": "completed",
            "summary": summary,
            "messages": [
                dict(message) for message in self.message_overrides.get(task_id, [])
            ],
            "claim_task_ids": [],
            "task_proposals": [],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(turn), encoding="utf-8")
        completed = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            }
        )
        return ProcessResult(returncode=0, stdout=started + "\n" + completed + "\n", stderr="")

    def _agent_team_merge_checkpoint(self, phase, evidence):
        matches_member = (
            self.crash_member is None
            or evidence.get("member_id") == self.crash_member
        )
        if not self.crashed and phase == self.crash_phase and matches_member:
            self.crashed = True
            raise SimulatedHardCrash(phase)

    def _agent_team_turn_completion_checkpoint(self, phase, completion):
        matches_member = (
            self.crash_member is None
            or completion.get("member_id") == self.crash_member
        )
        if (
            not self.crashed
            and self.crash_phase
            in {"turn-completion-written", "turn-telemetry-recorded"}
            and phase
            == (
                "written"
                if self.crash_phase == "turn-completion-written"
                else "telemetry-recorded"
            )
            and matches_member
        ):
            self.crashed = True
            raise SimulatedHardCrash(phase)

    def _agent_team_turn_terminal_checkpoint(self, phase, terminal):
        matches_member = (
            self.crash_member is None
            or terminal.get("member_id") == self.crash_member
        )
        expected_phase = {
            "turn-terminal-written": "written",
            "turn-terminal-promoted": "promoted-to-completion",
        }.get(self.crash_phase)
        if (
            not self.crashed
            and expected_phase is not None
            and phase == expected_phase
            and matches_member
        ):
            self.crashed = True
            raise SimulatedHardCrash(phase)

    def assert_external_team_workspace(self, cwd, argv):
        if cwd == self.workspace or self.workspace in cwd.parents:
            raise AssertionError("write teammate ran inside the source workspace")
        command_cwd = Path(argv[argv.index("--cd") + 1])
        if command_cwd != cwd:
            raise AssertionError("Codex --cd did not bind the isolated teammate workspace")


class FakePlanApprovalRunner(FakeWriteTeamRunner):
    def __init__(
        self,
        *args,
        reject_reviews=0,
        mutate_plan_workspace=False,
        crash_kind=None,
        **kwargs,
    ):
        self.plan_calls = []
        self.reject_reviews = reject_reviews
        self.mutate_plan_workspace = mutate_plan_workspace
        self.crash_kind = crash_kind
        self.plan_crashed = False
        self.review_count = 0
        super().__init__(*args, **kwargs)

    def _run_process(
        self,
        argv,
        cwd,
        timeout,
        input_text=None,
        output_limit_bytes=1024 * 1024,
        env=None,
        process_event_callback=None,
        stdout_line_callback=None,
        interrupt_check=None,
    ):
        if "--output-last-message" not in argv:
            return super()._run_process(
                argv,
                cwd=cwd,
                timeout=timeout,
                input_text=input_text,
                output_limit_bytes=output_limit_bytes,
                env=env,
                process_event_callback=process_event_callback,
                stdout_line_callback=stdout_line_callback,
                interrupt_check=interrupt_check,
            )
        output = Path(argv[argv.index("--output-last-message") + 1])
        if not (output.name.endswith(".plan.json") or output.name.endswith(".review.json")):
            return super()._run_process(
                argv,
                cwd=cwd,
                timeout=timeout,
                input_text=input_text,
                output_limit_bytes=output_limit_bytes,
                env=env,
                process_event_callback=process_event_callback,
                stdout_line_callback=stdout_line_callback,
                interrupt_check=interrupt_check,
            )
        del timeout, output_limit_bytes, env, process_event_callback
        if stdout_line_callback is not None or interrupt_check is not None:
            raise AssertionError("plan approval call unexpectedly enabled interruption polling")
        kind = "review" if output.name.endswith(".review.json") else "plan"
        cwd = Path(cwd)
        self.assert_external_team_workspace(cwd, argv)
        if argv[argv.index("--sandbox") + 1] != "read-only":
            raise AssertionError("plan approval call was not read-only")
        revision = int(output.name.split("-")[1].split(".")[0])
        source_before = (cwd / "shared.txt").read_text(encoding="utf-8")
        call = {
            "kind": kind,
            "argv": list(argv),
            "cwd": cwd,
            "prompt": input_text or "",
            "revision": revision,
            "source_before": source_before,
        }
        self.plan_calls.append(call)
        if kind == "plan" and self.mutate_plan_workspace:
            (cwd / "shared.txt").write_text("illegal-plan-write\n", encoding="utf-8")
        if "resume" in argv:
            session_id = argv[argv.index("resume") + 1]
        else:
            session_id = SESSION_IDS["worker" if kind == "plan" else "lead"]
        if kind == "plan":
            value = {
                "schema": AGENT_TEAM_PLAN_SCHEMA,
                "task_id": "worker-edit",
                "revision": revision,
                "plan": "Edit shared.txt once, preserve scope, then verify its exact content.",
                "risks": ["Avoid unrelated files."],
                "verification": ["Read shared.txt and compare the exact expected text."],
            }
        else:
            self.review_count += 1
            rejected = self.review_count <= self.reject_reviews
            value = {
                "schema": AGENT_TEAM_PLAN_REVIEW_SCHEMA,
                "task_id": "worker-edit",
                "revision": revision,
                "decision": "reject" if rejected else "approve",
                "feedback": (
                    "Add an exact deterministic content check before resubmitting."
                    if rejected
                    else "The plan is minimal and has deterministic verification."
                ),
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value), encoding="utf-8")
        if self.crash_kind == kind and not self.plan_crashed:
            self.plan_crashed = True
            raise SimulatedHardCrash("plan-%s" % kind)
        started = json.dumps({"type": "thread.started", "thread_id": session_id})
        completed = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            }
        )
        return ProcessResult(returncode=0, stdout=started + "\n" + completed + "\n", stderr="")


class AgentTeamTests(unittest.TestCase):
    def test_lifecycle_context_injects_into_plan_review_and_resumed_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = plan_approval_workflow()
            workflow["hooks"] = [
                {
                    "id": "planner-context",
                    "event": "agent_start",
                    "command": ["printf", "planner lifecycle context\n"],
                    "scopes": ["agent_team_plan"],
                    "members": ["worker"],
                    "tasks": ["worker-edit"],
                    "inject_context": True,
                },
                {
                    "id": "review-context",
                    "event": "agent_start",
                    "command": ["printf", "review lifecycle context\n"],
                    "scopes": ["agent_team_review"],
                    "members": ["lead"],
                    "tasks": ["worker-edit"],
                    "inject_context": True,
                },
                {
                    "id": "turn-context",
                    "event": "agent_start",
                    "command": ["printf", "implementation lifecycle context\n"],
                    "scopes": ["agent_team_turn"],
                    "members": ["worker"],
                    "tasks": ["worker-edit"],
                    "inject_context": True,
                },
            ]
            validate_workflow(workflow)
            runner = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            run = runner.execute()

            self.assertEqual([call["kind"] for call in runner.plan_calls], ["plan", "review"])
            self.assertTrue(
                all(
                    AGENT_LIFECYCLE_CONTEXT_BEGIN in call["prompt"]
                    for call in runner.plan_calls + runner.calls
                )
            )
            self.assertIn("planner lifecycle context", runner.plan_calls[0]["prompt"])
            self.assertIn("review lifecycle context", runner.plan_calls[1]["prompt"])
            self.assertIn("implementation lifecycle context", runner.calls[0]["prompt"])
            self.assertIn("resume", runner.calls[0]["argv"])

            receipt_paths = sorted(
                (run.artifacts_dir / ".agent-lifecycle-context").glob("*.json")
            )
            self.assertEqual(len(receipt_paths), 3)
            receipts = [load_agent_lifecycle_context_receipt(path) for path in receipt_paths]
            by_scope = {receipt["scope"]: receipt for receipt in receipts}
            self.assertEqual(
                set(by_scope),
                {"agent_team_plan", "agent_team_review", "agent_team_turn"},
            )
            self.assertEqual(by_scope["agent_team_plan"]["member_id"], "worker")
            self.assertEqual(by_scope["agent_team_review"]["member_id"], "lead")
            self.assertEqual(by_scope["agent_team_turn"]["session_mode"], "resume")
            self.assertEqual(by_scope["agent_team_turn"]["task_id"], "worker-edit")

            approval_path = (
                run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            attempt = load_agent_team_plan_approval(approval_path)["attempts"][0]
            self.assertRegex(attempt["plan_lifecycle_context_receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(attempt["review_lifecycle_context_receipt_sha256"], r"^[0-9a-f]{64}$")
            legacy_v2 = load_agent_team_plan_approval(approval_path)
            legacy_v2["schema"] = AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2
            for legacy_attempt in legacy_v2["attempts"]:
                for field in (
                    "plan_session_mode",
                    "plan_base_prompt_sha256",
                    "plan_effective_prompt_sha256",
                    "plan_lifecycle_context_receipt_sha256",
                    "review_session_mode",
                    "review_base_prompt_sha256",
                    "review_effective_prompt_sha256",
                    "review_lifecycle_context_receipt_sha256",
                ):
                    legacy_attempt.pop(field)
            validate_agent_team_plan_approval(
                legacy_v2,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
                generation=0,
            )

    def test_quality_retry_context_terminal_recovery_replays_neither_hook_nor_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "retry-context",
                    "event": "agent_start",
                    "command": ["printf", "retry lifecycle context\n"],
                    "scopes": ["agent_team_turn", "agent_team_quality_retry"],
                    "members": ["worker"],
                    "tasks": ["debug"],
                    "inject_context": True,
                },
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                },
            ]
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                quality_gate_failures=1,
                turn_terminal_crash_phase="written",
                turn_terminal_crash_retry_index=1,
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 2)
            self.assertTrue(
                all(AGENT_LIFECYCLE_CONTEXT_BEGIN in call["prompt"] for call in crashed.calls)
            )
            receipt_paths = sorted(
                (crashed.run.artifacts_dir / ".agent-lifecycle-context").glob("*.json")
            )
            receipts = [load_agent_lifecycle_context_receipt(path) for path in receipt_paths]
            self.assertEqual(
                {receipt["scope"] for receipt in receipts},
                {"agent_team_turn", "agent_team_quality_retry"},
            )
            retry_receipt = next(
                receipt
                for receipt in receipts
                if receipt["scope"] == "agent_team_quality_retry"
            )
            self.assertEqual(retry_receipt["session_mode"], "resume")
            self.assertEqual(retry_receipt["quality_retry_index"], 1)
            retry_receipt_path = next(
                path
                for path in receipt_paths
                if load_agent_lifecycle_context_receipt(path)["scope"]
                == "agent_team_quality_retry"
            )
            receipt_text = retry_receipt_path.read_text(encoding="utf-8")
            terminal = load_agent_team_turn_terminal(
                list_agent_team_turn_terminal_paths(
                    crashed.run,
                    workflow["steps"][0],
                )[0]
            )
            retry_receipt_path.unlink()
            with self.assertRaisesRegex(ValidationError, "evidence receipt is missing"):
                crashed._verify_agent_team_turn_terminal_artifacts(
                    workflow["steps"][0],
                    terminal,
                )
            retry_receipt_path.write_text(receipt_text, encoding="utf-8")
            lifecycle_events_before = sum(
                event["event"] == "agent_start"
                for event in crashed.run.read_state()["hook_events"]
            )

            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover context-bound quality retry terminal",
            )
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual(
                sum(
                    event["event"] == "agent_start"
                    for event in resumed.run.read_state()["hook_events"]
                ),
                lifecycle_events_before,
            )
            self.assertEqual(
                len(
                    list(
                        (resumed.run.artifacts_dir / ".agent-lifecycle-context").glob(
                            "*.json"
                        )
                    )
                ),
                2,
            )

    def _invoke_live_handler(self, handler_cls, method, path, body="", headers=None):
        handler = object.__new__(handler_cls)
        handler.path = path
        handler.rfile = BytesIO(body.encode("utf-8"))
        handler.wfile = BytesIO()
        handler.headers = Message()
        for key, value in (headers or {}).items():
            handler.headers[key] = value
        sent_headers = {}

        def send_response(status, message=None):
            del message
            handler._test_status = status

        def send_header(key, value):
            sent_headers[key] = value

        handler.send_response = send_response
        handler.send_header = send_header
        handler.end_headers = lambda: None
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        else:
            raise AssertionError("unsupported method %s" % method)
        return (
            handler._test_status,
            sent_headers,
            handler.wfile.getvalue().decode("utf-8"),
        )

    def test_operator_console_policy_is_strict_bounded_and_schema_exported(self):
        workflow = operator_console_workflow()
        validate_workflow(workflow)
        team_schema = next(
            item
            for item in get_schema("workflow")["properties"]["steps"]["items"]["oneOf"]
            if item["properties"]["kind"].get("const") == "agent_team"
        )
        self.assertIn("operator_console", team_schema["properties"])
        self.assertEqual(
            get_schema("agent-team-transcript")["properties"]["schema"]["const"],
            AGENT_TEAM_TRANSCRIPT_SCHEMA,
        )

        cases = []
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_console"]["unknown"] = True
        cases.append((bad, "operator_console has invalid fields"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_console"]["max_events"] = 7
        cases.append((bad, "max_events must be an integer"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_console"]["max_bytes"] = 4095
        cases.append((bad, "max_bytes must be an integer"))
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_workflow(value)

        read_assessment = assess_command(
            [
                "python3",
                "-m",
                "conductor_extras",
                "read-team-transcript",
                "/tmp/run",
                "team-debug",
                "transcript-0123456789abcdef01234567",
                "--follow",
            ]
        )
        self.assertFalse(read_assessment.writes)
        self.assertFalse(read_assessment.network)

    def test_operator_console_writer_redacts_bounds_and_rejects_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = operator_console_workflow(max_events=8, max_bytes=4096)
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="console-writer",
            )
            writer = AgentTeamTranscriptWriter(
                run=runner.run,
                step=workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(workflow),
                generation=0,
                round_number=1,
                member_id="worker",
                task_id="debug",
                attempt=9,
            )
            writer.observe_line(
                json.dumps(
                    {
                        "type": "thread.started",
                        "thread_id": SESSION_IDS["worker"],
                    }
                )
            )
            writer.observe_line(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "echo PRIVATE_TOOL_ARGUMENT",
                            "aggregated_output": "PRIVATE_TOOL_OUTPUT",
                        },
                    }
                )
            )
            writer.observe_line(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "Finished safely; api_key=abcdefghijklmnop.",
                        },
                    }
                )
            )
            for _ in range(12):
                writer.observe_line(json.dumps({"type": "turn.started"}))
            writer.finish("failed", error_class="SyntheticFailure")

            text = writer.path.read_text(encoding="utf-8")
            value = load_agent_team_transcript(writer.path)
            validate_agent_team_transcript(value)
            self.assertEqual(writer.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(value["status"], "failed")
            self.assertEqual(len(value["events"]), 8)
            self.assertTrue(value["truncated"])
            self.assertGreater(value["events_dropped"], 0)
            self.assertNotIn("PRIVATE_TOOL_ARGUMENT", text)
            self.assertNotIn("PRIVATE_TOOL_OUTPUT", text)
            self.assertNotIn("abcdefghijklmnop", text)
            self.assertIn("<redacted>", text)
            self.assertNotIn(SESSION_IDS["worker"], text)

            tampered = json.loads(text)
            message = next(
                event
                for event in tampered["events"]
                if event["type"] == "assistant-message"
            )
            message["text"] = "tampered"
            with self.assertRaisesRegex(ValidationError, "message hash changed"):
                validate_agent_team_transcript(tampered)

    def test_operator_console_is_private_and_omission_adds_no_console_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = operator_console_workflow()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="console-enabled",
            )
            run = runner.execute()
            self.assertTrue(runner.calls[0]["stream_callback"])
            summary = list_team_transcripts(run.run_dir, "team-debug")
            self.assertEqual(len(summary["transcripts"]), 1)
            serialized_summary = json.dumps(summary, sort_keys=True)
            self.assertNotIn("Private console teammate update.", serialized_summary)
            transcript_id = summary["transcripts"][0]["transcript_id"]
            private = read_team_transcript(run.run_dir, "team-debug", transcript_id)
            private_text = json.dumps(private, sort_keys=True)
            self.assertIn("Private console teammate update.", private_text)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", private_text)
            self.assertNotIn("PRIVATE_CONSOLE_TOOL_OUTPUT", private_text)
            self.assertNotIn(SESSION_IDS["worker"], private_text)
            self.assertEqual(private["status"], "completed")

            detail = collect_run_detail(root / "runs", run.run_dir.name)
            public_text = json.dumps(detail, sort_keys=True)
            self.assertNotIn("Private console teammate update.", public_text)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", public_text)
            self.assertNotIn(SESSION_IDS["worker"], public_text)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "list-team-transcripts",
                            str(run.run_dir),
                            "team-debug",
                        ]
                    ),
                    0,
                )
            self.assertNotIn("Private console teammate update.", output.getvalue())
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "read-team-transcript",
                            str(run.run_dir),
                            "team-debug",
                            transcript_id,
                        ]
                    ),
                    0,
                )
            self.assertIn("Private console teammate update.", output.getvalue())

            omitted_workspace = root / "omitted-workspace"
            omitted_workspace.mkdir()
            omitted = FakeTeamRunner(
                workflow=continuation_workflow(),
                workspace=omitted_workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="console-omitted",
            )
            omitted_run = omitted.execute()
            self.assertTrue(omitted.calls[0]["stream_callback"])
            self.assertFalse(
                (
                    omitted_run.artifacts_dir
                    / "team-debug-internal"
                    / "operator-console"
                ).exists()
            )

    def test_team_console_snapshot_layouts_filters_and_private_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = operator_console_workflow()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="terminal-team-console",
            )
            run = runner.execute()

            snapshot = read_team_console_snapshot(run.run_dir, "team-debug")
            validate_team_console_snapshot(snapshot)
            self.assertTrue(team_console_is_terminal(snapshot))
            self.assertEqual(
                [member["id"] for member in snapshot["members"]],
                ["lead", "worker"],
            )
            self.assertEqual(snapshot["transcript_count"], 1)
            private_json = json.dumps(snapshot, sort_keys=True)
            self.assertIn("Private console teammate update.", private_json)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", private_json)
            self.assertNotIn("PRIVATE_CONSOLE_TOOL_OUTPUT", private_json)
            self.assertNotIn(SESSION_IDS["worker"], private_json)

            columns = render_team_console(
                snapshot,
                width=120,
                layout="columns",
                history=1,
                max_events=8,
                max_message_chars=1200,
            )
            self.assertIn("lead * [idle]", columns)
            self.assertIn("worker [idle]", columns)
            self.assertIn("Private console teammate update.", columns)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", columns)
            self.assertNotIn(SESSION_IDS["worker"], columns)
            self.assertTrue(all(len(line) <= 120 for line in columns.splitlines()))

            stacked = render_team_console(
                snapshot,
                width=50,
                layout="stack",
                max_events=2,
            )
            self.assertTrue(all(len(line) <= 50 for line in stacked.splitlines()))
            self.assertEqual(stacked.count("TEAM team-debug"), 1)
            with self.assertRaisesRegex(ValidationError, "columns layout needs width"):
                render_team_console(snapshot, width=70, layout="columns")

            worker_only = read_team_console_snapshot(
                run.run_dir,
                "team-debug",
                member_ids=["worker"],
            )
            self.assertEqual(
                [member["id"] for member in worker_only["members"]],
                ["worker"],
            )
            self.assertEqual(worker_only["transcript_count"], 1)
            with self.assertRaisesRegex(ValidationError, "contains duplicates"):
                read_team_console_snapshot(
                    run.run_dir,
                    "team-debug",
                    member_ids=["worker", "worker"],
                )
            with self.assertRaisesRegex(ValidationError, "not part of the team"):
                read_team_console_snapshot(
                    run.run_dir,
                    "team-debug",
                    member_ids=["missing"],
                )

            tampered = json.loads(json.dumps(snapshot))
            tampered["transcripts"][0]["events"][-2]["text"] = "tampered"
            with self.assertRaisesRegex(ValidationError, "message hash changed"):
                validate_team_console_snapshot(tampered)

            controlled = json.loads(json.dumps(snapshot))
            controlled_message = next(
                event
                for event in controlled["transcripts"][0]["events"]
                if event["type"] == "assistant-message"
            )
            controlled_message["text"] = (
                "visible\x1b[2Jhidden\roverwrite\u202ereversed"
            )
            controlled_message["text_sha256"] = hashlib.sha256(
                controlled_message["text"].encode("utf-8")
            ).hexdigest()
            validate_team_console_snapshot(controlled)
            controlled_render = render_team_console(
                controlled,
                width=80,
                layout="stack",
            )
            self.assertNotIn("\x1b", controlled_render)
            self.assertNotIn("\r", controlled_render)
            self.assertNotIn("\u202e", controlled_render)
            self.assertIn("\\x1b[2J", controlled_render)
            self.assertIn("\\x0d", controlled_render)
            self.assertIn("\\u202e", controlled_render)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "team-console",
                            str(run.run_dir),
                            "team-debug",
                            "--layout",
                            "stack",
                            "--width",
                            "80",
                        ]
                    ),
                    0,
                )
            terminal_text = output.getvalue()
            self.assertIn("Private console teammate update.", terminal_text)
            self.assertNotIn("\x1b", terminal_text)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", terminal_text)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "team-console",
                            str(run.run_dir),
                            "team-debug",
                            "--member",
                            "worker",
                            "--json",
                        ]
                    ),
                    0,
                )
            console_json = json.loads(output.getvalue())
            self.assertEqual(console_json["schema"], "conductor.team_console_snapshot.v1")
            self.assertEqual(
                [member["id"] for member in console_json["members"]],
                ["worker"],
            )
            self.assertIn(
                "Private console teammate update.",
                json.dumps(console_json, sort_keys=True),
            )

            assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "team-console",
                    str(run.run_dir),
                    "team-debug",
                    "--follow",
                ]
            )
            self.assertFalse(assessment.writes)
            self.assertFalse(assessment.network)

    def test_team_console_follow_tracks_active_turn_and_exits_at_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            first_turn_started = threading.Event()
            release_first_turn = threading.Event()
            runner = FakeTeamRunner(
                workflow=operator_console_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="follow-team-console",
                first_turn_started=first_turn_started,
                release_first_turn=release_first_turn,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - return thread failure.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))

            def release_turn():
                time.sleep(0.2)
                release_first_turn.set()

            releaser = threading.Thread(target=release_turn)
            releaser.start()
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "team-console",
                            str(runner.run.run_dir),
                            "team-debug",
                            "--follow",
                            "--no-clear",
                            "--layout",
                            "stack",
                            "--refresh-ms",
                            "100",
                            "--timeout-seconds",
                            "5",
                            "--width",
                            "80",
                        ]
                    ),
                    0,
                )
            releaser.join(timeout=5)
            thread.join(timeout=10)
            self.assertFalse(releaser.is_alive())
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            followed = output.getvalue()
            self.assertGreaterEqual(followed.count("TEAM team-debug"), 2)
            self.assertIn("running", followed)
            self.assertIn("completed", followed)
            self.assertIn("Private console teammate update.", followed)
            self.assertNotIn("\x1b", followed)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", followed)
            self.assertNotIn(SESSION_IDS["worker"], followed)

    def test_live_operator_console_private_grants_and_interrupt_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = operator_console_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            first_turn_started = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="live-private-console",
                first_turn_started=first_turn_started,
                interrupt_first_turn=True,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            deadline = time.monotonic() + 5
            active = []
            while time.monotonic() < deadline:
                active = list_team_transcripts(
                    runner.run.run_dir,
                    "team-debug",
                )["transcripts"]
                if active and active[0]["event_count"]:
                    break
                time.sleep(0.01)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["status"], "active")

            run_handle_value = run_handle(runner.run.run_dir.name)
            step_handle_value = step_handle("team-debug")
            transcript_id = active[0]["transcript_id"]
            private_path = "/private/team-transcript/%s/%s/%s" % (
                run_handle_value,
                step_handle_value,
                transcript_id,
            )
            handler_cls = _handler_factory(
                runs_dir=root / "runs",
                benchmarks_dir=root / "benchmarks",
                max_runs=50,
                max_reports=50,
                max_text_bytes=4096,
                title="Unit Live",
                control_token="unit-token",
            )

            status, _headers, public_html = self._invoke_live_handler(
                handler_cls,
                "GET",
                "/run/%s" % run_handle_value,
            )
            self.assertEqual(status, 200)
            self.assertIn('action="/private/team-transcript"', public_html)
            self.assertIn(active[0]["file_sha256"], public_html)
            self.assertNotIn("Private console teammate update.", public_html)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", public_html)
            self.assertNotIn(SESSION_IDS["worker"], public_html)

            status, _headers, api_body = self._invoke_live_handler(
                handler_cls,
                "GET",
                "/api/run/%s" % run_handle_value,
            )
            self.assertEqual(status, 200)
            self.assertIn(transcript_id, api_body)
            self.assertNotIn("Private console teammate update.", api_body)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", api_body)
            self.assertNotIn(SESSION_IDS["worker"], api_body)

            status, _headers, error_body = self._invoke_live_handler(
                handler_cls,
                "GET",
                private_path,
            )
            self.assertEqual(status, 400)
            self.assertIn("invalid or expired", error_body)

            def private_post(token, selected_transcript_id):
                body = urlencode(
                    {
                        "token": token,
                        "run_handle": run_handle_value,
                        "step_handle": step_handle_value,
                        "transcript_id": selected_transcript_id,
                    }
                )
                return self._invoke_live_handler(
                    handler_cls,
                    "POST",
                    "/private/team-transcript",
                    body=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Content-Length": str(len(body)),
                        "Host": "127.0.0.1:8765",
                        "Origin": "http://127.0.0.1:8765",
                    },
                )

            status, _headers, error_body = private_post("wrong-token", transcript_id)
            self.assertEqual(status, 400)
            self.assertIn("invalid live control token", error_body)

            status, headers, body = private_post("unit-token", transcript_id)
            self.assertEqual(status, 303)
            self.assertEqual(body, "")
            self.assertEqual(headers["Location"], private_path)
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", headers["Set-Cookie"])
            self.assertIn("Path=%s" % private_path, headers["Set-Cookie"])
            cookie_pair = headers["Set-Cookie"].split(";", 1)[0]

            restarted_handler_cls = _handler_factory(
                runs_dir=root / "runs",
                benchmarks_dir=root / "benchmarks",
                max_runs=50,
                max_reports=50,
                max_text_bytes=4096,
                title="Unit Live",
                control_token="unit-token",
            )
            status, _headers, _body = self._invoke_live_handler(
                restarted_handler_cls,
                "GET",
                private_path,
                headers={"Cookie": cookie_pair},
            )
            self.assertEqual(status, 400)

            cookie_name, cookie_value = cookie_pair.split("=", 1)
            tampered_value = cookie_value[:-1] + (
                "0" if cookie_value[-1] != "0" else "1"
            )
            status, _headers, _body = self._invoke_live_handler(
                handler_cls,
                "GET",
                private_path,
                headers={"Cookie": "%s=%s" % (cookie_name, tampered_value)},
            )
            self.assertEqual(status, 400)

            with patch(
                "conductor_extras.runtime.live_server.time.time",
                return_value=time.time() + 3600,
            ):
                status, _headers, _body = self._invoke_live_handler(
                    handler_cls,
                    "GET",
                    private_path,
                    headers={"Cookie": cookie_pair},
                )
            self.assertEqual(status, 400)

            wrong_transcript_id = "transcript-" + "f" * 24
            wrong_path = "/private/team-transcript/%s/%s/%s" % (
                run_handle_value,
                step_handle_value,
                wrong_transcript_id,
            )
            status, _headers, wrong_body = self._invoke_live_handler(
                handler_cls,
                "GET",
                wrong_path,
                headers={
                    "Cookie": "cc_team_%s=%s"
                    % (wrong_transcript_id[11:], cookie_value)
                },
            )
            self.assertEqual(status, 400)
            self.assertIn("invalid or expired", wrong_body)

            status, refreshed_headers, active_html = self._invoke_live_handler(
                handler_cls,
                "GET",
                private_path,
                headers={"Cookie": cookie_pair},
            )
            self.assertEqual(status, 200)
            self.assertIn("Set-Cookie", refreshed_headers)
            self.assertIn('<meta http-equiv="refresh" content="2">', active_html)
            self.assertIn("Direct teammate", active_html)
            self.assertIn('value="interrupt-current"', active_html)
            self.assertNotIn(SESSION_IDS["worker"], active_html)

            status, _headers, paused_html = self._invoke_live_handler(
                handler_cls,
                "GET",
                private_path + "?follow=0",
                headers={"Cookie": cookie_pair},
            )
            self.assertEqual(status, 200)
            self.assertNotIn("http-equiv=\"refresh\"", paused_html)
            self.assertIn("Resume refresh", paused_html)
            self.assertIn("Direct teammate", paused_html)

            rejected_body = urlencode(
                {
                    "token": "wrong-token",
                    "action": "queue-team-task",
                    "run_handle": run_handle_value,
                    "step_handle": step_handle_value,
                    "member": "worker",
                    "instruction": "This must not be accepted.",
                    "delivery": "interrupt-current",
                    "return_to": private_path,
                }
            )
            status, _headers, _body = self._invoke_live_handler(
                handler_cls,
                "POST",
                "/control",
                body=rejected_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(rejected_body)),
                    "Host": "127.0.0.1:8765",
                    "Origin": "http://127.0.0.1:8765",
                    "Cookie": cookie_pair,
                },
            )
            self.assertEqual(status, 400)

            instruction = "Stop the broad search and inspect only the retry boundary."
            redirect_body = urlencode(
                {
                    "token": "unit-token",
                    "action": "queue-team-task",
                    "run_handle": run_handle_value,
                    "step_handle": step_handle_value,
                    "member": "worker",
                    "instruction": instruction,
                    "delivery": "interrupt-current",
                    "return_to": private_path,
                }
            )
            status, redirect_headers, _body = self._invoke_live_handler(
                handler_cls,
                "POST",
                "/control",
                body=redirect_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(redirect_body)),
                    "Host": "127.0.0.1:8765",
                    "Origin": "http://127.0.0.1:8765",
                    "Cookie": cookie_pair,
                },
            )
            self.assertEqual(status, 303)
            self.assertTrue(redirect_headers["Location"].startswith(private_path + "?message="))

            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            final_transcripts = list_team_transcripts(
                runner.run.run_dir,
                "team-debug",
            )["transcripts"]
            self.assertEqual(final_transcripts[0]["status"], "interrupted")

            status, _headers, interrupted_html = self._invoke_live_handler(
                handler_cls,
                "GET",
                private_path,
                headers={"Cookie": cookie_pair},
            )
            self.assertEqual(status, 200)
            self.assertIn("interrupted", interrupted_html)
            self.assertNotIn("http-equiv=\"refresh\"", interrupted_html)
            self.assertNotIn("Direct teammate", interrupted_html)

            completed = next(
                value for value in final_transcripts if value["status"] == "completed"
            )
            status, completed_headers, _body = private_post(
                "unit-token",
                completed["transcript_id"],
            )
            self.assertEqual(status, 303)
            completed_path = completed_headers["Location"]
            completed_cookie = completed_headers["Set-Cookie"].split(";", 1)[0]
            status, _headers, completed_html = self._invoke_live_handler(
                handler_cls,
                "GET",
                completed_path,
                headers={"Cookie": completed_cookie},
            )
            self.assertEqual(status, 200)
            self.assertIn("Private console teammate update.", completed_html)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", completed_html)
            self.assertNotIn("PRIVATE_CONSOLE_TOOL_OUTPUT", completed_html)
            self.assertNotIn(SESSION_IDS["worker"], completed_html)
            self.assertNotIn("http-equiv=\"refresh\"", completed_html)

            status, _headers, final_public_html = self._invoke_live_handler(
                handler_cls,
                "GET",
                "/run/%s" % run_handle_value,
            )
            self.assertEqual(status, 200)
            self.assertNotIn("Private console teammate update.", final_public_html)
            self.assertNotIn(instruction, final_public_html)

            dashboard_path = root / "dashboard.html"
            write_dashboard(root / "runs", root / "benchmarks", dashboard_path)
            static_html = dashboard_path.read_text(encoding="utf-8")
            self.assertIn(completed["file_sha256"], static_html)
            self.assertNotIn("Private console teammate update.", static_html)
            self.assertNotIn("PRIVATE_CONSOLE_COMMAND", static_html)
            self.assertNotIn(SESSION_IDS["worker"], static_html)

    def test_operator_chat_policy_is_strict_bounded_and_reserves_routing(self):
        workflow = operator_chat_workflow()
        validate_workflow(workflow)
        state = initial_agent_team_state(
            workflow["steps"][0],
            workflow_fingerprint(workflow),
        )
        self.assertEqual(state["schema"], AGENT_TEAM_STATE_SCHEMA)
        self.assertEqual(state["max_operator_questions"], 2)
        self.assertEqual(state["operator_questions_added"], 0)

        cases = []
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_chat"]["unknown"] = True
        cases.append((bad, "operator_chat has invalid fields"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_chat"]["max_questions"] = 0
        cases.append((bad, "max_questions must be an integer"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_chat"]["max_tokens"] = 2000
        cases.append((bad, "max_tokens cannot exceed"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["operator_chat"]["reply_timeout_seconds"] = 0
        cases.append((bad, "reply_timeout_seconds must be an integer"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["members"][1]["id"] = "operator"
        cases.append((bad, "reserved for operator routing"))
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_workflow(value)
        reply_assessment = assess_command(
            [
                "python3",
                "-m",
                "conductor_extras",
                "reply-team-question",
                "/tmp/run",
                "team-debug",
                "operator-question-g000000000-0001",
                "--reply",
                "bounded answer",
            ]
        )
        self.assertTrue(reply_assessment.writes)
        self.assertFalse(reply_assessment.network)
        read_assessment = assess_command(
            [
                "python3",
                "-m",
                "conductor_extras",
                "read-team-question",
                "/tmp/run",
                "team-debug",
                "operator-question-g000000000-0001",
            ]
        )
        self.assertFalse(read_assessment.writes)
        self.assertFalse(read_assessment.network)

    def test_operator_question_reply_wakes_same_session_with_private_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = operator_chat_workflow()
            workflow["hooks"] = [
                {
                    "id": "operator-reply-created",
                    "event": "team_task_created",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "block",
                }
            ]
            question = "Should the compatibility branch preserve the legacy retry result?"
            reply = "Yes. Preserve it and add an exact regression check within the existing scope."
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="operator-chat",
                message_overrides={"debug": [{"to": "operator", "body": question}]},
            )
            errors = []

            def execute():
                try:
                    runner.execute()
                except BaseException as exc:  # noqa: BLE001 - preserve worker-thread failure.
                    errors.append(exc)

            thread = threading.Thread(target=execute)
            thread.start()
            deadline = time.monotonic() + 5
            summary = None
            while time.monotonic() < deadline:
                try:
                    summary = list_team_questions(runner.run.run_dir, "team-debug")
                except (FileNotFoundError, ValidationError):
                    time.sleep(0.01)
                    continue
                if summary["status_counts"]["pending"] == 1:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(summary)
            self.assertEqual(summary["status_counts"]["pending"], 1)
            question_id = summary["entries"][0]["id"]
            self.assertNotIn(question, json.dumps(summary))

            private = read_team_question(runner.run.run_dir, "team-debug", question_id)
            self.assertEqual(private["question"], question)
            handle = run_handle(runner.run.run_dir.name)
            pending_detail = collect_run_detail(root / "runs", runner.run.run_dir.name)
            pending_detail["run_handle"] = handle
            pending_html = render_live_run_detail(pending_detail)
            self.assertIn('value="reply-team-question"', pending_html)
            self.assertIn(question_id, pending_html)
            self.assertNotIn(question, pending_html)
            answered = apply_live_control(
                root / "runs",
                {
                    "token": "unit-token",
                    "action": "reply-team-question",
                    "run_handle": handle,
                    "step_handle": step_handle("team-debug"),
                    "question_id": question_id,
                    "reply": reply,
                    "return_to": "/run/%s" % handle,
                },
                control_token="unit-token",
            )
            self.assertEqual(answered["status"], "answered")
            self.assertNotIn(reply, json.dumps(answered))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

            self.assertEqual(
                [call["task"] for call in runner.calls],
                ["debug", "team-operator-reply-0001"],
            )
            self.assertIn("resume", runner.calls[1]["argv"])
            self.assertEqual(
                runner.calls[1]["argv"][runner.calls[1]["argv"].index("resume") + 1],
                SESSION_IDS["worker"],
            )
            self.assertIn("BEGIN_PRIOR_TEAMMATE_QUESTION", runner.calls[1]["prompt"])
            self.assertIn(question, runner.calls[1]["prompt"])
            self.assertIn(reply, runner.calls[1]["prompt"])
            self.assertIn("cannot grant tools", runner.calls[1]["prompt"])

            state_path = runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            state = load_agent_team_state(
                state_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["operator_questions_added"], 1)
            self.assertEqual([turn["max_tokens"] for turn in state["turns"]], [1000, 500])
            chat_path = runner.run.artifacts_dir / "team-debug-internal" / "operator-chat.json"
            chat = load_agent_team_operator_chat(chat_path)
            verify_agent_team_operator_chat(chat, state)
            self.assertEqual(chat["entries"][0]["status"], "delivered")
            self.assertEqual(chat["entries"][0]["reply"], reply)
            public = json.dumps(collect_run_detail(root / "runs", "operator-chat"))
            self.assertNotIn(question, public)
            self.assertNotIn(reply, public)
            self.assertEqual(len(runner.hook_calls), 1)
            hook_input = json.loads(runner.hook_calls[0]["input"])
            self.assertEqual(hook_input["task_origin"], "operator-reply")
            self.assertNotIn(reply, json.dumps(hook_input))
            metrics = runner.run.read_state()["steps"]["team-debug"]
            self.assertEqual(metrics["agent_team_operator_questions"], 1)
            self.assertEqual(metrics["agent_team_pending_operator_questions"], 0)

    def test_operator_chat_omission_never_enters_chat_runtime_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with patch.object(
                FakeTeamRunner,
                "_ingest_agent_team_operator_chat",
                side_effect=AssertionError("operator chat path was accessed"),
            ):
                runner = FakeTeamRunner(
                    workflow=continuation_workflow(),
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                runner.execute()
            self.assertFalse(
                (runner.run.artifacts_dir / "team-debug-internal" / "operator-chat.json").exists()
            )

    def test_operator_question_timeout_preserves_waiting_task_for_explicit_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = operator_chat_workflow(
                max_questions=1,
                reply_timeout_seconds=1,
            )
            question = "Which compatibility result should remain authoritative?"
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                message_overrides={"debug": [{"to": "operator", "body": question}]},
            )
            with self.assertRaisesRegex(StepExecutionError, "timed out waiting for operator reply"):
                runner.execute()
            self.assertEqual([call["task"] for call in runner.calls], ["debug"])
            state_path = runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            state = load_agent_team_state(state_path)
            waiting = [task for task in state["tasks"] if task["status"] == "waiting"]
            self.assertEqual([task["id"] for task in waiting], ["team-operator-reply-0001"])
            summary = list_team_questions(runner.run.run_dir, "team-debug")
            question_id = summary["entries"][0]["id"]
            cli_output = StringIO()
            with redirect_stdout(cli_output):
                self.assertEqual(
                    cli_main(
                        [
                            "reply-team-question",
                            str(runner.run.run_dir),
                            "team-debug",
                            question_id,
                            "--reply",
                            "Preserve the legacy result and verify the exact boundary.",
                        ]
                    ),
                    0,
                )
            self.assertIn("Status: answered", cli_output.getvalue())
            retry_step(runner.run.run_dir, "team-debug", reason="operator answered")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=runner.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(
                [call["task"] for call in resumed.calls],
                ["team-operator-reply-0001"],
            )
            self.assertIn("resume", resumed.calls[0]["argv"])
            final_chat = load_agent_team_operator_chat(
                runner.run.artifacts_dir / "team-debug-internal" / "operator-chat.json"
            )
            self.assertEqual(final_chat["entries"][0]["status"], "delivered")

    def test_peer_and_operator_messages_share_one_atomic_capacity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = active_messaging_workflow(max_tasks=1)
            step = workflow["steps"][0]
            step["max_rounds"] = 2
            step["operator_chat"] = {
                "max_questions": 1,
                "max_tokens": 1000,
                "reply_timeout_seconds": 30,
            }
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                message_overrides={
                    "investigate": [
                        {"to": "lead", "body": "Challenge this finding."},
                        {"to": "operator", "body": "Choose the compatibility boundary."},
                    ]
                },
            )
            with self.assertRaisesRegex(
                StepExecutionError,
                "message and operator reply tasks exceed remaining round capacity",
            ):
                runner.execute()
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-debate-internal" / "team-state.json"
            )
            self.assertEqual(state["messages"], [])
            self.assertEqual(state["message_tasks_added"], 0)
            self.assertEqual(state["operator_questions_added"], 0)
            self.assertEqual(len(state["tasks"]), 1)

    def test_operator_chat_tamper_is_rejected_without_public_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.json"
            value = json.loads(
                Path("benchmark-suites/agent-team-operator-chat-template.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_agent_team_operator_chat(value)
            summary = agent_team_operator_chat_summary(value)
            self.assertNotIn(value["entries"][0]["question"], json.dumps(summary))
            tampered = json.loads(json.dumps(value))
            tampered["entries"][0]["question"] += " changed"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "question hash is invalid"):
                load_agent_team_operator_chat(path)

    def test_active_messaging_workflow_policy_is_strict_and_bounded(self):
        workflow = active_messaging_workflow()
        validate_workflow(workflow)
        state = initial_agent_team_state(
            workflow["steps"][0],
            workflow_fingerprint(workflow),
        )
        self.assertEqual(state["schema"], AGENT_TEAM_STATE_SCHEMA)
        self.assertEqual(state["max_message_tasks"], 3)
        self.assertEqual(state["message_tasks_added"], 0)
        self.assertTrue(all(task["message_id"] is None for task in state["tasks"]))

        cases = []
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["active_messaging"]["unknown"] = True
        cases.append((bad, "active_messaging has invalid fields"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["active_messaging"]["max_tasks"] = 0
        cases.append((bad, "max_tasks must be an integer"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["active_messaging"]["max_depth"] = 5
        cases.append((bad, "max_depth must be an integer"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["active_messaging"]["max_tokens"] = 3000
        cases.append((bad, "max_tokens cannot exceed max_tokens"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["active_messaging"]["allow_broadcast"] = "yes"
        cases.append((bad, "allow_broadcast must be a boolean"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["max_rounds"] = 1
        cases.append((bad, "requires at least two rounds"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["tasks"][0]["id"] = "team-message-task-0001-01"
        cases.append((bad, "reserves task ids beginning"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["members"][1]["id"] = "all"
        cases.append((bad, "reserved for broadcast routing"))
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_workflow(value)

    def test_agent_team_hook_input_is_strict_bounded_and_hash_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            result = root / "result"
            base.mkdir()
            result.mkdir()
            (base / "shared.txt").write_text("base\n", encoding="utf-8")
            (result / "shared.txt").write_text("changed\n", encoding="utf-8")
            (result / "new.txt").write_text("new\n", encoding="utf-8")
            payload = build_agent_team_hook_input(
                event="team_task_completed",
                workflow_fingerprint="a" * 64,
                step_id="team",
                generation=2,
                round_number=3,
                member_id="lead",
                task={
                    "id": "review",
                    "description": "Inspect a private implementation detail.",
                    "origin": "static",
                    "assignee": "lead",
                },
                turn_output_sha256="b" * 64,
                turn_summary="A private completion summary.",
                turn_status="completed",
                workspace_mode="isolated-write",
                workspace_base_snapshot=snapshot_workspace(base),
                workspace_result_snapshot=snapshot_workspace(result),
            )
            validate_agent_team_hook_input(payload)
            serialized = agent_team_hook_input_json(payload)
            self.assertEqual(json.loads(serialized), payload)
            self.assertEqual(payload["schema"], AGENT_TEAM_HOOK_INPUT_SCHEMA)
            self.assertEqual(payload["changed_files"], ["new.txt", "shared.txt"])
            self.assertEqual(payload["changed_file_count"], 2)
            self.assertNotIn("private", serialized)
            self.assertEqual(len(agent_team_hook_input_sha256(payload)), 64)
            self.assertEqual(
                get_schema("agent-team-hook-input")["properties"]["schema"]["const"],
                AGENT_TEAM_HOOK_INPUT_SCHEMA,
            )

            tampered = dict(payload)
            tampered["unexpected"] = True
            with self.assertRaisesRegex(ValidationError, "invalid fields"):
                validate_agent_team_hook_input(tampered)
            tampered = dict(payload)
            tampered["changed_file_count"] = 1
            with self.assertRaisesRegex(ValidationError, "count is inconsistent"):
                validate_agent_team_hook_input(tampered)

    def test_team_quality_hook_workflow_contract_rejects_unsafe_shapes(self):
        workflow = adaptive_workflow()
        workflow["hooks"] = [
            {
                "id": "created",
                "event": "team_task_created",
                "steps": ["adaptive-review"],
                "tasks": ["future-task"],
                "command": ["true"],
            },
            {
                "id": "completed",
                "event": "team_task_completed",
                "members": ["lead"],
                "command": ["true"],
            },
            {
                "id": "idle",
                "event": "team_member_idle",
                "command": ["true"],
            },
        ]
        validate_workflow(workflow)

        cases = []
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["capture"] = "created.txt"
        cases.append((bad, "cannot use capture"))
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["network"] = True
        cases.append((bad, "cannot enable network"))
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["command"] = ["curl", "https://example.com"]
        cases.append((bad, "cannot enable network"))
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["writes"] = True
        cases.append((bad, "require workspace-write teams"))
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["command"] = ["touch", "unexpected.txt"]
        cases.append((bad, "require workspace-write teams"))
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][1]["members"] = ["missing"]
        cases.append((bad, "unknown team member"))
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][1]["members"] = None
        cases.append((bad, "members must be a non-empty safe id array"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"].append(
            {
                "id": "ordinary",
                "kind": "write_artifact",
                "output": "ordinary.txt",
                "content": "ordinary",
            }
        )
        bad["hooks"][0]["steps"] = ["ordinary"]
        cases.append((bad, "only agent_team steps"))
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_workflow(value)

    def test_team_quality_hooks_dry_run_preflights_without_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = adaptive_workflow()
            workflow["hooks"] = [
                {"id": "created", "event": "team_task_created", "command": ["true"]},
                {"id": "completed", "event": "team_task_completed", "command": ["true"]},
                {"id": "idle", "event": "team_member_idle", "command": ["true"]},
            ]
            runner = WorkflowRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                dry_run=True,
            )
            run = runner.execute()
            events = run.read_state()["hook_events"]
            self.assertEqual(len(events), 3)
            self.assertEqual({event["status"] for event in events}, {"planned"})
            self.assertEqual(
                {event["event"] for event in events},
                {"team_task_created", "team_task_completed", "team_member_idle"},
            )
            self.assertFalse((run.logs_dir / "hooks").exists())
            self.assertFalse(
                (run.artifacts_dir / "adaptive-review-internal" / "team-state.json").exists()
            )

    def test_team_quality_hooks_execute_filters_and_publish_hash_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = adaptive_workflow()
            workflow["hooks"] = [
                {
                    "id": "created",
                    "event": "team_task_created",
                    "tasks": ["investigate-adaptive"],
                    "command": ["grep", '"event": "team_task_created"'],
                },
                {
                    "id": "completed",
                    "event": "team_task_completed",
                    "members": ["worker"],
                    "command": ["grep", '"event": "team_task_completed"'],
                },
                {
                    "id": "idle",
                    "event": "team_member_idle",
                    "tasks": ["synthesize-adaptive"],
                    "command": ["grep", '"event": "team_member_idle"'],
                },
            ]
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True)
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                run_id="team-hooks",
            )
            run = runner.execute()
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(len(runner.hook_calls), 3)
            payloads = [json.loads(call["input"]) for call in runner.hook_calls]
            for call, payload in zip(runner.hook_calls, payloads):
                validate_agent_team_hook_input(payload)
                self.assertEqual(payload["workspace_mode"], "read-only")
                self.assertNotEqual(call["cwd"], workspace.resolve())
                self.assertNotIn(workspace.resolve(), call["cwd"].parents)
            self.assertEqual(
                [(payload["event"], payload["task_id"]) for payload in payloads],
                [
                    ("team_task_created", "investigate-adaptive"),
                    ("team_task_completed", "investigate-adaptive"),
                    ("team_member_idle", "synthesize-adaptive"),
                ],
            )
            events = run.read_state()["hook_events"]
            self.assertEqual(len(events), 3)
            for payload, event in zip(payloads, events):
                self.assertEqual(event["status"], "completed")
                self.assertEqual(
                    event["hook_input_sha256"],
                    agent_team_hook_input_sha256(payload),
                )
                self.assertEqual(event["member_id"], payload["member_id"])
                self.assertEqual(event["task_id"], payload["task_id"])
            public_events = json.dumps(events)
            self.assertNotIn("Verify the adaptive boundary", public_events)
            hook_logs = list((run.logs_dir / "hooks").rglob("*.stdout.log"))
            self.assertEqual(len(hook_logs), 3)
            self.assertEqual(len({path.relative_to(run.logs_dir) for path in hook_logs}), 3)
            detail = collect_run_detail(root / "runs", "team-hooks")
            self.assertEqual(detail["hook_events"][0]["member_id"], "lead")
            live_html = render_live_run_detail(detail)
            self.assertIn("lead / investigate-adaptive", live_html)
            self.assertIn(events[0]["hook_input_sha256"][:12], live_html)

            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual(resumed.hook_calls, [])

    def test_blocking_team_quality_hook_rejects_turn_and_explicit_retry_gets_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "completion-gate",
                    "event": "team_task_completed",
                    "command": ["grep", "approved", "gate-feedback.txt"],
                    "on_failure": "block",
                }
            ]
            policy = RuntimePolicy(allow_agent=True)
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
            )
            with self.assertRaisesRegex(StepExecutionError, "completion-gate"):
                first.execute()
            state_path = first.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            failed = load_agent_team_state(state_path)
            self.assertEqual(failed["tasks"][0]["status"], "pending")
            self.assertEqual(failed["turns"], [])
            self.assertIn("completion-gate", failed["members"][1]["last_error"])
            self.assertIn("gate-feedback.txt", failed["members"][1]["last_error"])
            self.assertEqual(first.run.read_state()["hook_events"][0]["status"], "failed")

            (workspace / "gate-feedback.txt").write_text("approved\n", encoding="utf-8")
            retry_step(first.run.run_dir, "team-debug", reason="quality gate corrected")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=first.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(len(resumed.calls), 1)
            self.assertIn("completion-gate", resumed.calls[0]["prompt"])
            completed = load_agent_team_state(state_path)
            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(completed["members"][1]["last_error"])
            self.assertEqual(
                [event["status"] for event in resumed.run.read_state()["hook_events"]],
                ["failed", "completed"],
            )

    def test_warning_team_quality_hook_records_failure_and_accepts_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "advisory",
                    "event": "team_task_completed",
                    "command": ["false"],
                    "on_failure": "warn",
                }
            ]
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            state = load_agent_team_state(
                run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(run.read_state()["hook_events"][0]["status"], "failed")

    def test_quality_retry_checkpoint_is_strict_hash_only_and_schema_exported(self):
        checkpoint = build_agent_team_quality_retry(
            step_id="team",
            workflow_fingerprint="a" * 64,
            generation=0,
            round_number=1,
            member_id="worker",
            task_id="repair",
            event_task_id="repair",
            event="team_task_completed",
            hook_id="quality",
            hook_input_sha256="b" * 64,
            retry_index=1,
            max_retries=4,
            hook_retry_index=1,
            hook_max_retries=2,
            max_tokens=1000,
            session_id_sha256="c" * 64,
            feedback_sha256="d" * 64,
            workspace_mode="read-only",
            workspace_fingerprint_sha256="e" * 64,
            rejected_output="team-internal/quality-retries/rejected/rejected.json",
            rejected_output_sha256="f" * 64,
            started_at_utc="2026-07-09T12:00:00Z",
        )
        validate_agent_team_quality_retry(checkpoint)
        self.assertEqual(checkpoint["schema"], AGENT_TEAM_QUALITY_RETRY_SCHEMA)
        self.assertNotIn("repair the quality boundary", json.dumps(checkpoint))
        failed = finalize_agent_team_quality_retry(
            checkpoint,
            "failed",
            timestamp="2026-07-09T12:00:01Z",
            error_class="ProviderFailure",
        )
        validate_agent_team_quality_retry(failed)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            get_schema("agent-team-quality-retry")["properties"]["schema"]["const"],
            AGENT_TEAM_QUALITY_RETRY_SCHEMA,
        )
        tampered = dict(checkpoint)
        tampered["hook_retry_index"] = 3
        with self.assertRaisesRegex(ValidationError, "hook retry index exceeds"):
            validate_agent_team_quality_retry(tampered)
        tampered = dict(checkpoint)
        tampered["step_id"] = "t\u00e9am"
        with self.assertRaisesRegex(ValidationError, "step_id is invalid"):
            validate_agent_team_quality_retry(tampered)
        tampered = dict(checkpoint)
        tampered["updated_at_utc"] = "2026-07-09T11:59:59Z"
        with self.assertRaisesRegex(ValidationError, "timestamps are out of order"):
            validate_agent_team_quality_retry(tampered)
        tampered = dict(failed)
        tampered["error_class"] = "secret-bearing free text"
        with self.assertRaisesRegex(ValidationError, "error class is invalid"):
            validate_agent_team_quality_retry(tampered)
        tampered = dict(checkpoint)
        tampered["event_task_id"] = "different-task"
        with self.assertRaisesRegex(ValidationError, "event task is inconsistent"):
            validate_agent_team_quality_retry(tampered)

    def test_retry_hook_policy_is_bounded_and_team_only(self):
        workflow = continuation_workflow()
        workflow["hooks"] = [
            {
                "id": "quality",
                "event": "team_task_completed",
                "command": ["grep", "QUALITY_GATE_SIMULATED"],
                "on_failure": "retry",
                "max_retries": 2,
                "retry_exit_codes": [2, 3],
                "retry_max_tokens": 1000,
            }
        ]
        validate_workflow(workflow)

        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["event"] = "after_step"
        with self.assertRaisesRegex(ValidationError, "only valid for agent-team events"):
            validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["max_retries"] = 0
        with self.assertRaisesRegex(ValidationError, "max_retries"):
            validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["retry_exit_codes"] = [2, 2]
        with self.assertRaisesRegex(ValidationError, "unique exit codes"):
            validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["on_failure"] = "block"
        with self.assertRaisesRegex(ValidationError, "require on_failure retry"):
            validate_workflow(bad)

    def test_retry_team_quality_hook_resumes_same_session_with_bounded_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            static_profile_text = "WORKER_STATIC_PROFILE_SENTINEL " + ("x" * 2048)
            next(
                profile
                for profile in workflow["agent_profiles"]
                if profile["name"] == "worker-profile"
            )["instructions"] = static_profile_text
            workflow["hooks"] = [
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                }
            ]
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                quality_gate_failures=1,
            )
            run = runner.execute()
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(len(runner.hook_calls), 2)
            self.assertNotIn("resume", runner.calls[0]["argv"])
            self.assertIn("resume", runner.calls[1]["argv"])
            self.assertIn(SESSION_IDS["worker"], runner.calls[1]["argv"])
            self.assertIn(static_profile_text, runner.calls[0]["prompt"])
            self.assertNotIn(static_profile_text, runner.calls[1]["prompt"])
            self.assertIn(
                "BEGIN_CONDUCTOR_AGENT_PROFILE_CONTINUATION",
                runner.calls[1]["prompt"],
            )
            self.assertIn("repair the quality boundary", runner.calls[1]["prompt"])
            self.assertIn("BEGIN_CONDUCTOR_TEAM_QUALITY_FEEDBACK", runner.calls[1]["prompt"])

            state = load_agent_team_state(
                run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["round"], 1)
            self.assertEqual(len(state["turns"]), 1)
            self.assertEqual(state["tasks"][0]["attempts"], 1)
            self.assertEqual(state["members"][1]["turns"], 1)
            self.assertEqual(state["members"][1]["session_id"], SESSION_IDS["worker"])
            self.assertEqual(state["authorized_tokens"], 2000)

            history = list_agent_team_quality_retry_history(
                run,
                runner.workflow["steps"][0],
            )
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "accepted")
            self.assertEqual(history[0]["retry_index"], 1)
            self.assertNotIn(SESSION_IDS["worker"], json.dumps(history[0]))
            self.assertNotIn("repair the quality boundary", json.dumps(history[0]))
            self.assertEqual(
                list_agent_team_quality_retry_pending(run, runner.workflow["steps"][0]),
                [],
            )
            rejected = list(
                (run.artifacts_dir / "team-debug-internal" / "quality-retries" / "rejected").glob("*.json")
            )
            self.assertEqual(len(rejected), 1)
            hook_events = run.read_state()["hook_events"]
            self.assertEqual([event["status"] for event in hook_events], ["failed", "completed"])
            self.assertEqual([event["quality_retry_index"] for event in hook_events], [0, 1])
            step_state = run.read_state()["steps"]["team-debug"]
            self.assertEqual(step_state["agent_team_quality_retries"], 1)
            self.assertEqual(step_state["agent_team_quality_retries_accepted"], 1)
            self.assertEqual(step_state["agent_invocations"], 2)
            self.assertEqual(step_state["total_tokens"], 20)
            report = (run.artifacts_dir / "team-debug.md").read_text(encoding="utf-8")
            self.assertIn("Automatic quality retries: 1", report)

            history_path = next(
                (run.artifacts_dir / "team-debug-internal" / "quality-retries" / "history").glob("*.json")
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(["validate-agent-team-quality-retry", str(history_path)]),
                    0,
                )
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-team-quality-retry",
                            str(history_path),
                            "--json",
                        ]
                    ),
                    0,
                )
            inspected_value = json.loads(inspected.getvalue())
            self.assertEqual(inspected_value["status"], "accepted")
            self.assertFalse(inspected_value["automatic_provider_replay_allowed"])
            self.assertNotIn(SESSION_IDS["worker"], inspected.getvalue())
            self.assertNotIn("repair the quality boundary", inspected.getvalue())
            for command in (
                "validate-agent-team-quality-retry",
                "inspect-agent-team-quality-retry",
            ):
                assessment = assess_command(
                    ["python3", "-m", "conductor_extras", command, str(history_path)]
                )
                self.assertFalse(assessment.writes)
                self.assertFalse(assessment.network)

            detail = collect_run_detail(root / "runs", run.run_dir.name)
            public_team = json.dumps(detail["agent_teams"][0])
            self.assertEqual(detail["agent_teams"][0]["quality_retry_history_count"], 1)
            self.assertEqual(
                detail["agent_teams"][0]["quality_retry_status_counts"],
                {"accepted": 1},
            )
            self.assertEqual(detail["agent_teams"][0]["quality_retry_pending_count"], 0)
            self.assertEqual(
                [event["quality_retry_index"] for event in detail["hook_events"]],
                [0, 1],
            )
            self.assertNotIn(SESSION_IDS["worker"], public_team)
            self.assertNotIn("repair the quality boundary", public_team)
            live_html = render_live_run_detail(detail)
            self.assertIn("automatic quality retries: 1", live_html)
            self.assertIn("quality retry 1", live_html)

    def test_quality_retry_exhaustion_and_budget_shortage_fail_without_hidden_calls(self):
        for case in ("exhausted", "budget", "wrong-exit", "timeout"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                workflow = continuation_workflow()
                workflow["hooks"] = [
                    {
                        "id": "quality",
                        "event": "team_task_completed",
                        "command": ["grep", "QUALITY_GATE_SIMULATED"],
                        "on_failure": "retry",
                        "max_retries": 1,
                        "retry_max_tokens": 1000,
                    }
                ]
                failures = 2
                expected_calls = 2
                expected_history = "rejected"
                if case == "budget":
                    workflow["steps"][0]["max_total_tokens"] = 1000
                    failures = 1
                    expected_calls = 1
                    expected_history = None
                gate_exit_code = 1 if case == "wrong-exit" else 2
                if case == "wrong-exit":
                    failures = 1
                    expected_calls = 1
                    expected_history = None
                timed_out = case == "timeout"
                if timed_out:
                    failures = 0
                    expected_calls = 1
                    expected_history = None
                runner = FakeTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    quality_gate_failures=failures,
                    quality_gate_exit_code=gate_exit_code,
                    quality_gate_timed_out=timed_out,
                )
                with self.assertRaises(StepExecutionError):
                    runner.execute()
                self.assertEqual(len(runner.calls), expected_calls)
                state = load_agent_team_state(
                    runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
                )
                self.assertEqual(state["tasks"][0]["status"], "pending")
                self.assertEqual(state["authorized_tokens"], expected_calls * 1000)
                history = list_agent_team_quality_retry_history(
                    runner.run,
                    runner.workflow["steps"][0],
                )
                self.assertEqual(
                    [value["status"] for value in history],
                    [] if expected_history is None else [expected_history],
                )
                self.assertEqual(
                    list_agent_team_quality_retry_pending(
                        runner.run,
                        runner.workflow["steps"][0],
                    ),
                    [],
                )

    def test_created_task_quality_retry_distinguishes_event_task_from_retry_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = adaptive_workflow()
            workflow["hooks"] = [
                {
                    "id": "created-quality",
                    "event": "team_task_created",
                    "tasks": ["investigate-adaptive"],
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                }
            ]
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                quality_gate_failures=1,
            )
            run = runner.execute()
            self.assertEqual([call["task"] for call in runner.calls[:2]], ["plan-adaptive", "plan-adaptive"])
            self.assertIn("resume", runner.calls[1]["argv"])
            self.assertIn(SESSION_IDS["lead"], runner.calls[1]["argv"])

            history = list_agent_team_quality_retry_history(
                run,
                runner.workflow["steps"][0],
            )
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "accepted")
            self.assertEqual(history[0]["event"], "team_task_created")
            self.assertEqual(history[0]["task_id"], "plan-adaptive")
            self.assertEqual(history[0]["event_task_id"], "investigate-adaptive")
            self.assertEqual(
                [(event["task_id"], event["quality_retry_index"]) for event in run.read_state()["hook_events"]],
                [("investigate-adaptive", 0), ("investigate-adaptive", 1)],
            )

    def test_quality_retry_history_and_archives_are_generation_scoped_after_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                }
            ]
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                quality_gate_failures=1,
            )
            run = first.execute()
            reset_step(run.run_dir, "team-debug", reason="new quality generation")
            second = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                quality_gate_failures=1,
                resume_dir=run.run_dir,
            )
            second.execute()

            history = list_agent_team_quality_retry_history(
                second.run,
                second.workflow["steps"][0],
            )
            self.assertEqual(
                [(value["generation"], value["status"]) for value in history],
                [(0, "accepted"), (1, "accepted")],
            )
            retry_root = run.artifacts_dir / "team-debug-internal" / "quality-retries"
            history_names = sorted(path.name for path in (retry_root / "history").glob("*.json"))
            archive_names = sorted(path.name for path in (retry_root / "rejected").glob("*.json"))
            self.assertEqual(len(history_names), 2)
            self.assertEqual(len(archive_names), 2)
            self.assertIn("generation-000000000", history_names[0])
            self.assertIn("generation-000000001", history_names[1])
            self.assertIn("generation-000000000", archive_names[0])
            self.assertIn("generation-000000001", archive_names[1])

    def test_write_team_quality_hook_runs_in_stage_and_mutation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            workflow["hooks"] = [
                {
                    "id": "stage-check",
                    "event": "team_task_completed",
                    "command": ["grep", "lead", "shared.txt"],
                }
            ]
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            run = runner.execute()
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            self.assertNotEqual(runner.hook_calls[0]["cwd"], workspace)
            event = run.read_state()["hook_events"][0]
            self.assertEqual(event["status"], "completed")
            self.assertEqual(event["changed_file_count"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            workflow["hooks"] = [
                {
                    "id": "mutating-gate",
                    "event": "team_task_completed",
                    "command": ["touch", "hook-mutated.txt"],
                    "on_failure": "retry",
                    "risk": "medium",
                    "writes": True,
                }
            ]
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            with self.assertRaisesRegex(StepExecutionError, "guarded team workspace"):
                runner.execute()
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "base\n")
            self.assertFalse((workspace / "hook-mutated.txt").exists())
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-implementation-internal" / "team-state.json"
            )
            self.assertEqual(state["tasks"][0]["status"], "pending")
            self.assertEqual(state["turns"], [])
            ledger = load_agent_team_merge_ledger(
                runner.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual(ledger["events"], [])
            self.assertEqual(runner.run.read_state()["hook_events"][0]["status"], "failed")

    def test_write_team_quality_retry_reuses_session_and_stage_before_one_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            workflow["hooks"] = [
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                }
            ]
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                quality_gate_failures=1,
            )
            run = runner.execute()
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(runner.calls[0]["cwd"], runner.calls[1]["cwd"])
            self.assertIn("resume", runner.calls[1]["argv"])
            self.assertIn("repair the staged quality boundary", runner.calls[1]["prompt"])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            state = load_agent_team_state(
                run.artifacts_dir / "team-implementation-internal" / "team-state.json"
            )
            ledger = load_agent_team_merge_ledger(
                run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual(len(state["turns"]), 1)
            self.assertEqual(state["authorized_tokens"], 2000)
            self.assertEqual(agent_team_merge_summary(ledger)["merge_count"], 1)
            history = list_agent_team_quality_retry_history(
                run,
                runner.workflow["steps"][0],
            )
            self.assertEqual([value["status"] for value in history], ["accepted"])

    def test_write_team_quality_retry_accepted_intent_recovers_without_third_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            workflow["hooks"] = [
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                }
            ]
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                quality_gate_failures=1,
                crash_phase="intent-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 2)
            pending = list_agent_team_quality_retry_pending(
                crashed.run,
                workflow["steps"][0],
            )
            self.assertEqual(len(pending), 1)
            self.assertEqual(load_agent_team_quality_retry(pending[0])[0]["status"], "completed")

            retry_step(crashed.run.run_dir, "team-implementation", reason="recover accepted retry")
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            history = list_agent_team_quality_retry_history(
                resumed.run,
                workflow["steps"][0],
            )
            self.assertEqual([value["status"] for value in history], ["accepted"])
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["authorized_tokens"], 2000)
            self.assertEqual(len(state["turns"]), 1)

    def test_quality_retry_hard_loss_recovers_only_provider_completed_turn(self):
        for phase, first_call_count, pending_status, resumed_call_count in [
            ("prepared", 1, "active", 1),
            ("provider-completed", 2, "completed", 0),
        ]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                workflow = continuation_workflow()
                workflow["hooks"] = [
                    {
                        "id": "quality",
                        "event": "team_task_completed",
                        "command": ["grep", "QUALITY_GATE_SIMULATED"],
                        "on_failure": "retry",
                        "max_retries": 1,
                        "retry_max_tokens": 1000,
                    }
                ]
                crashed = FakeTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    quality_gate_failures=1,
                    quality_retry_crash_phase=phase,
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                self.assertEqual(len(crashed.calls), first_call_count)
                pending_paths = list_agent_team_quality_retry_pending(
                    crashed.run,
                    crashed.workflow["steps"][0],
                )
                self.assertEqual(len(pending_paths), 1)
                pending, _pending_sha256 = load_agent_team_quality_retry(pending_paths[0])
                self.assertEqual(pending["status"], pending_status)
                pending_detail = collect_run_detail(root / "runs", crashed.run.run_dir.name)
                pending_team = pending_detail["agent_teams"][0]
                self.assertEqual(pending_team["quality_retry_pending_count"], 1)
                self.assertEqual(pending_team["quality_retry_pending"][0]["status"], pending_status)
                self.assertIn(
                    "Quality retry recovery pending",
                    render_live_run_detail(pending_detail),
                )
                interrupted = load_agent_team_state(
                    crashed.run.artifacts_dir / "team-debug-internal" / "team-state.json"
                )
                self.assertEqual(interrupted["tasks"][0]["status"], "claimed")

                retry_step(crashed.run.run_dir, "team-debug", reason="recover quality retry")
                resumed = FakeTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=crashed.run.run_dir,
                )
                resumed.execute()
                self.assertEqual(len(resumed.calls), resumed_call_count)
                if resumed.calls:
                    self.assertNotIn("resume", resumed.calls[0]["argv"])
                    self.assertIn("QualityRetryRecovery", resumed.calls[0]["prompt"])
                self.assertEqual(
                    list_agent_team_quality_retry_pending(
                        resumed.run,
                        resumed.workflow["steps"][0],
                    ),
                    [],
                )
                history = list_agent_team_quality_retry_history(
                    resumed.run,
                    resumed.workflow["steps"][0],
                )
                expected_status = "abandoned" if phase == "prepared" else "accepted"
                self.assertEqual([value["status"] for value in history], [expected_status])
                self.assertEqual(
                    history[0]["error_class"],
                    "QualityRetryOutcomeNotCommitted" if phase == "prepared" else None,
                )
                completed = load_agent_team_state(
                    resumed.run.artifacts_dir / "team-debug-internal" / "team-state.json"
                )
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(
                    completed["authorized_tokens"],
                    3000 if phase == "prepared" else 2000,
                )

    def test_quality_retry_recovery_rejects_tampered_retained_outputs_without_provider_calls(self):
        for phase, output_field in [
            ("prepared", "rejected_output"),
            ("provider-completed", "retry_output"),
        ]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                workflow = continuation_workflow()
                workflow["hooks"] = [
                    {
                        "id": "quality",
                        "event": "team_task_completed",
                        "command": ["grep", "QUALITY_GATE_SIMULATED"],
                        "on_failure": "retry",
                        "max_retries": 1,
                        "retry_max_tokens": 1000,
                    }
                ]
                crashed = FakeTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    quality_gate_failures=1,
                    quality_retry_crash_phase=phase,
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                pending_path = list_agent_team_quality_retry_pending(
                    crashed.run,
                    crashed.workflow["steps"][0],
                )[0]
                checkpoint, _checkpoint_sha256 = load_agent_team_quality_retry(pending_path)
                retained_path = crashed.run.artifacts_dir / checkpoint[output_field]
                retained_path.write_text("tampered retained output\n", encoding="utf-8")
                retry_step(crashed.run.run_dir, "team-debug", reason="test tamper recovery")
                resumed = FakeTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=crashed.run.run_dir,
                )
                with self.assertRaisesRegex(ValidationError, "hash changed"):
                    resumed.execute()
                self.assertEqual(resumed.calls, [])

    def test_coordinated_team_delivers_messages_to_lead_and_resumes_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            context_text = "Review authentication against SECURITY-CONTEXT-BOUNDARY-17."
            (workspace / "security-context.md").write_text(context_text, encoding="utf-8")
            workflow["agent_profiles"][1]["context_files"] = ["security-context.md"]
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True)
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                run_id="coordinated",
            )
            run = runner.execute()
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual({call["task"] for call in runner.calls[:2]}, {"security-audit", "correctness-audit"})
            security_call = next(call for call in runner.calls if call["task"] == "security-audit")
            self.assertIn("SECURITY-CONTEXT-BOUNDARY-17", security_call["prompt"])
            self.assertEqual(
                runner.workflow["agent_profile_context_bindings"]["profiles"][0]["name"],
                "security-profile",
            )
            lead_call = next(call for call in runner.calls if call["task"] == "synthesize")
            self.assertIn("Prioritize the authentication boundary", lead_call["prompt"])
            self.assertIn("retry boundary issue", lead_call["prompt"])

            state_path = run.artifacts_dir / "team-review-internal" / "team-state.json"
            self.assertFalse(
                (run.artifacts_dir / "team-review-internal" / "operator-inbox.json").exists()
            )
            self.assertFalse(
                (run.artifacts_dir / "team-review-internal" / ".operator-inbox.lock").exists()
            )
            state = load_agent_team_state(
                state_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
            )
            summary = agent_team_state_summary(state)
            self.assertEqual(state["schema"], AGENT_TEAM_STATE_SCHEMA)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["round"], 2)
            self.assertEqual(summary["turn_count"], 3)
            self.assertEqual(summary["message_count"], 2)
            self.assertEqual(summary["task_counts"]["completed"], 3)
            report = (run.artifacts_dir / "team-review.md").read_text(encoding="utf-8")
            self.assertIn("Lead synthesized", report)
            self.assertIn("Message Ledger", report)
            self.assertIn("Generation: 0", report)
            step_state = run.read_state()["steps"]["team-review"]
            self.assertEqual(step_state["agent_team_members"], 3)
            self.assertEqual(step_state["agent_team_tasks_completed"], 3)
            detail = collect_run_detail(root / "runs", "coordinated")
            self.assertEqual(detail["agent_teams"][0]["status"], "completed")
            self.assertEqual(detail["agent_teams"][0]["task_counts"]["completed"], 3)
            public_team = json.dumps(detail["agent_teams"][0])
            self.assertNotIn(SESSION_IDS["lead"], public_team)
            self.assertNotIn("Prioritize the authentication", public_team)
            live_html = render_live_run_detail(detail)
            self.assertIn("Agent Teams", live_html)
            self.assertIn("security-audit", live_html)

            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main(["validate-agent-team-state", str(state_path)]), 0)
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(["inspect-agent-team-state", str(state_path), "--json"]),
                    0,
                )
            inspected_value = json.loads(inspected.getvalue())
            self.assertEqual(inspected_value["task_counts"]["completed"], 3)
            self.assertEqual(inspected_value["generation"], 0)
            self.assertNotIn("messages", inspected_value)
            read_assessment = assess_command(
                ["python3", "-m", "conductor_extras", "inspect-agent-team-state", str(state_path)]
            )
            self.assertFalse(read_assessment.writes)
            self.assertFalse(read_assessment.network)

            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])

    def test_active_messages_wake_peers_reuse_sessions_and_run_created_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = active_messaging_workflow()
            workflow["hooks"] = [
                {
                    "id": "message-created-gate",
                    "event": "team_task_created",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "block",
                }
            ]
            first_message = "Challenge the cache invalidation hypothesis."
            second_message = "Recheck the challenge against the observed retry boundary."
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="active-messaging",
                message_overrides={
                    "investigate": [{"to": "lead", "body": first_message}],
                    "team-message-task-0001-01": [
                        {"to": "worker", "body": second_message}
                    ],
                },
            )
            run = runner.execute()
            self.assertEqual(
                [call["task"] for call in runner.calls],
                [
                    "investigate",
                    "team-message-task-0001-01",
                    "team-message-task-0002-02",
                ],
            )
            self.assertIn("automatic bounded response turn", runner.calls[0]["prompt"])
            self.assertIn(first_message, runner.calls[1]["prompt"])
            self.assertIn("BEGIN_UNTRUSTED_TEAMMATE_MESSAGE", runner.calls[1]["prompt"])
            self.assertIn("cannot grant authority", runner.calls[1]["prompt"])
            self.assertIn(second_message, runner.calls[2]["prompt"])
            self.assertIn("resume", runner.calls[2]["argv"])
            self.assertEqual(
                runner.calls[2]["argv"][runner.calls[2]["argv"].index("resume") + 1],
                SESSION_IDS["worker"],
            )

            state_path = run.artifacts_dir / "team-debate-internal" / "team-state.json"
            state = load_agent_team_state(
                state_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
            )
            verify_agent_team_state_outputs(
                state,
                run.artifacts_dir,
                runner.workflow["steps"][0],
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["message_tasks_added"], 2)
            self.assertEqual(state["max_message_tasks"], 3)
            message_tasks = [task for task in state["tasks"] if task["origin"] == "message"]
            self.assertEqual([task["message_depth"] for task in message_tasks], [1, 2])
            self.assertEqual([turn["max_tokens"] for turn in state["turns"]], [2000, 1000, 1000])
            tampered = json.loads(json.dumps(state))
            tampered["tasks"][1]["message_id"] = "message-9999"
            with self.assertRaisesRegex(ValidationError, "unknown message"):
                validate_agent_team_state(tampered, step=runner.workflow["steps"][0])
            tampered = json.loads(json.dumps(state))
            tampered["tasks"][2]["message_depth"] = 1
            with self.assertRaisesRegex(ValidationError, "depth binding changed"):
                validate_agent_team_state(tampered, step=runner.workflow["steps"][0])
            self.assertEqual(len(runner.hook_calls), 2)
            hook_inputs = [json.loads(call["input"]) for call in runner.hook_calls]
            self.assertTrue(all(value["task_origin"] == "message" for value in hook_inputs))
            self.assertNotIn(first_message, json.dumps(hook_inputs))
            self.assertNotIn(second_message, json.dumps(hook_inputs))

            metrics = run.read_state()["steps"]["team-debate"]
            self.assertEqual(metrics["agent_team_message_tasks"], 2)
            self.assertEqual(metrics["agent_team_max_message_tasks"], 3)
            detail = collect_run_detail(root / "runs", "active-messaging")
            public = json.dumps(detail["agent_teams"][0])
            self.assertNotIn(first_message, public)
            self.assertNotIn(second_message, public)
            self.assertIn("active teammate responses 2 of 3", render_live_run_detail(detail))

    def test_active_message_depth_failure_is_not_committed_or_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = active_messaging_workflow(max_depth=1)
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                message_overrides={
                    "investigate": [{"to": "lead", "body": "Challenge this finding."}],
                    "team-message-task-0001-01": [
                        {"to": "worker", "body": "Attempt an unauthorized second hop."}
                    ],
                },
            )
            with self.assertRaisesRegex(StepExecutionError, "active message depth limit exceeded"):
                runner.execute()
            state_path = runner.run.artifacts_dir / "team-debate-internal" / "team-state.json"
            failed = load_agent_team_state(state_path)
            self.assertEqual(failed["message_tasks_added"], 1)
            self.assertEqual(len(failed["messages"]), 1)
            self.assertEqual(failed["tasks"][1]["status"], "pending")

            retry_step(runner.run.run_dir, "team-debate", reason="retry without excess message")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=runner.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(len(resumed.calls), 1)
            self.assertEqual(resumed.calls[0]["task"], "team-message-task-0001-01")
            self.assertNotIn("resume", resumed.calls[0]["argv"])

    def test_active_message_broadcast_fails_closed_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = active_messaging_workflow(allow_broadcast=False)
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                message_overrides={
                    "investigate": [{"to": "all", "body": "Wake every peer."}],
                },
            )
            with self.assertRaisesRegex(StepExecutionError, "active message broadcast is disabled"):
                runner.execute()
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-debate-internal" / "team-state.json"
            )
            self.assertEqual(state["message_tasks_added"], 0)
            self.assertEqual(state["messages"], [])
            self.assertEqual(state["tasks"][0]["status"], "pending")

    def test_active_message_broadcast_expansion_respects_aggregate_task_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = active_messaging_workflow(
                max_tasks=1,
                allow_broadcast=True,
            )
            workflow["agent_profiles"].append(team_profile("correctness"))
            workflow["steps"][0]["members"].append(
                team_member("correctness", "independent challenger")
            )
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                message_overrides={
                    "investigate": [{"to": "all", "body": "Challenge this in parallel."}],
                },
            )
            with self.assertRaisesRegex(StepExecutionError, "active message task limit exceeded"):
                runner.execute()
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-debate-internal" / "team-state.json"
            )
            self.assertEqual(state["message_tasks_added"], 0)
            self.assertEqual(state["messages"], [])

    def test_passive_messages_do_not_enter_active_delivery_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with patch(
                "conductor_extras.runtime.agent_team.prepare_agent_team_message_tasks",
                side_effect=AssertionError("active messaging path was accessed"),
            ):
                runner = FakeTeamRunner(
                    workflow=coordinated_workflow(),
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                )
                runner.execute()
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-review-internal" / "team-state.json"
            )
            self.assertEqual(state["max_message_tasks"], 0)
            self.assertEqual(state["message_tasks_added"], 0)
            self.assertFalse(any(task["origin"] == "message" for task in state["tasks"]))
            self.assertEqual(len(runner.calls), 3)

    def test_operator_task_queues_during_active_turn_and_resumes_target_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            workflow["hooks"] = [
                {
                    "id": "operator-created-gate",
                    "event": "team_task_created",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "block",
                }
            ]
            first_turn_started = threading.Event()
            release_first_turn = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="operator-steering",
                first_turn_started=first_turn_started,
                release_first_turn=release_first_turn,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            instruction = "Recheck the retry boundary against malformed boolean inputs."
            queued = StringIO()
            with redirect_stdout(queued):
                self.assertEqual(
                    cli_main(
                        [
                            "queue-team-task",
                            str(runner.run.run_dir),
                            "team-debug",
                            "--member",
                            "worker",
                            "--instruction",
                            instruction,
                        ]
                    ),
                    0,
                )
            self.assertIn("Status: pending", queued.getvalue())
            self.assertNotIn(instruction, queued.getvalue())
            pending = list_team_inbox(runner.run.run_dir, "team-debug")
            self.assertEqual(pending["status_counts"]["pending"], 1)
            self.assertNotIn(instruction, json.dumps(pending))
            release_first_turn.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(all(call["interrupt_polling"] for call in runner.calls))
            operator_call = next(
                call for call in runner.calls if call["task"].startswith("operator-g")
            )
            self.assertEqual(operator_call["member"], "worker")
            self.assertIn(instruction, operator_call["prompt"])
            self.assertIn("resume", operator_call["argv"])
            self.assertIn(SESSION_IDS["worker"], operator_call["argv"])
            operator_hook = next(
                call
                for call in runner.hook_calls
                if json.loads(call["input"])["task_origin"] == "operator"
            )
            operator_hook_input = json.loads(operator_hook["input"])
            self.assertEqual(operator_hook_input["task_id"], operator_call["task"])
            self.assertNotIn(instruction, operator_hook["input"])

            state_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            state = load_agent_team_state(state_path, step=workflow["steps"][0])
            self.assertEqual(state["schema"], AGENT_TEAM_STATE_SCHEMA)
            self.assertEqual(state["operator_tasks_added"], 1)
            operator_task = next(task for task in state["tasks"] if task["origin"] == "operator")
            self.assertEqual(operator_task["assignee"], "worker")
            self.assertEqual(operator_task["status"], "completed")
            inbox_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            inbox = load_agent_team_operator_inbox(inbox_path)
            validate_agent_team_operator_inbox(inbox)
            inbox_summary = agent_team_operator_inbox_summary(inbox)
            self.assertEqual(inbox_summary["status_counts"]["accepted"], 1)
            self.assertNotIn(instruction, json.dumps(inbox_summary))
            listed = StringIO()
            with redirect_stdout(listed):
                self.assertEqual(
                    cli_main(
                        [
                            "list-team-inbox",
                            str(runner.run.run_dir),
                            "team-debug",
                            "--json",
                        ]
                    ),
                    0,
                )
            self.assertNotIn(instruction, listed.getvalue())
            self.assertEqual(json.loads(listed.getvalue())["operator_tasks_added"], 1)
            detail = collect_run_detail(root / "runs", "operator-steering")
            operator_detail = detail["agent_teams"][0]["operator_inbox"]
            self.assertEqual(operator_detail["status_counts"]["accepted"], 1)
            self.assertNotIn(instruction, json.dumps(detail))
            self.assertIn("operator follow-ups", render_live_run_detail(detail))

            queue_assessment = assess_command(
                ["python3", "-m", "conductor_extras", "queue-team-task", "run", "team"]
            )
            self.assertTrue(queue_assessment.writes)
            self.assertFalse(queue_assessment.network)
            list_assessment = assess_command(
                ["python3", "-m", "conductor_extras", "list-team-inbox", "run", "team"]
            )
            self.assertFalse(list_assessment.writes)
            self.assertFalse(list_assessment.network)

    def test_live_team_control_queues_named_follow_up_without_instruction_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            first_turn_started = threading.Event()
            release_first_turn = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="live-operator-steering",
                first_turn_started=first_turn_started,
                release_first_turn=release_first_turn,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            handle = run_handle(runner.run.run_dir.name)
            detail = collect_run_detail(root / "runs", runner.run.run_dir.name)
            detail["run_handle"] = handle
            html = render_live_run_detail(detail)
            self.assertIn('value="queue-team-task"', html)
            self.assertIn('name="member"', html)
            self.assertIn(
                '<option value="worker">worker (working on debug)</option>',
                html,
            )
            self.assertIn(
                '<option value="interrupt-current">Interrupt current turn</option>',
                html,
            )
            self.assertIn('name="instruction" maxlength="4000"', html)
            self.assertIn(
                'name="step_handle" value="%s"' % step_handle("team-debug"),
                html,
            )

            instruction = "Recheck the malformed retry path from the live team view."
            result = apply_live_control(
                root / "runs",
                {
                    "token": "unit-token",
                    "action": "queue-team-task",
                    "run_handle": handle,
                    "step_handle": step_handle("team-debug"),
                    "member": "worker",
                    "instruction": instruction,
                    "return_to": "/run/%s" % handle,
                },
                control_token="unit-token",
            )
            self.assertEqual(result["action"], "queue-team-task")
            self.assertEqual(result["requested_step"], "team-debug")
            self.assertEqual(result["member_id"], "worker")
            self.assertEqual(result["status"], "pending")
            self.assertNotIn(instruction, json.dumps(result))
            queued_detail = collect_run_detail(root / "runs", runner.run.run_dir.name)
            queued_detail["run_handle"] = handle
            queued_html = render_live_run_detail(queued_detail)
            self.assertNotIn('value="queue-team-task"', queued_html)
            self.assertNotIn(instruction, queued_html)

            release_first_turn.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(runner.calls), 2)
            final_detail = collect_run_detail(root / "runs", runner.run.run_dir.name)
            final_detail["run_handle"] = handle
            final_html = render_live_run_detail(final_detail)
            self.assertNotIn('value="queue-team-task"', final_html)
            self.assertNotIn(instruction, final_html)

    def test_operator_interrupt_stops_only_target_turn_and_resumes_same_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            workflow["steps"][0]["operator_console"] = {
                "max_events": 64,
                "max_bytes": 64 * 1024,
            }
            first_turn_started = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="operator-interrupt",
                first_turn_started=first_turn_started,
                interrupt_first_turn=True,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            deadline = time.monotonic() + 5
            active_transcripts = []
            while time.monotonic() < deadline:
                active_transcripts = list_team_transcripts(
                    runner.run.run_dir,
                    "team-debug",
                )["transcripts"]
                if (
                    active_transcripts
                    and active_transcripts[0]["session_id_sha256"] is not None
                ):
                    break
                time.sleep(0.01)
            self.assertEqual(len(active_transcripts), 1)
            self.assertEqual(active_transcripts[0]["status"], "active")
            self.assertIsNotNone(active_transcripts[0]["session_id_sha256"])
            self.assertNotIn(
                SESSION_IDS["worker"],
                json.dumps(active_transcripts, sort_keys=True),
            )
            instruction = "Stop the broad search and inspect only the malformed retry boundary."
            queued = StringIO()
            with redirect_stdout(queued):
                self.assertEqual(
                    cli_main(
                        [
                            "queue-team-task",
                            str(runner.run.run_dir),
                            "team-debug",
                            "--member",
                            "worker",
                            "--instruction",
                            instruction,
                            "--interrupt-current",
                        ]
                    ),
                    0,
                )
            self.assertIn("Delivery: interrupt-current", queued.getvalue())
            self.assertIn("Interrupt task: debug", queued.getvalue())
            self.assertNotIn(instruction, queued.getvalue())
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

            self.assertEqual([call["task"] for call in runner.calls][0], "debug")
            self.assertEqual(len(runner.calls), 3)
            operator_call = runner.calls[1]
            self.assertTrue(operator_call["task"].startswith("operator-g"))
            self.assertIn(instruction, operator_call["prompt"])
            self.assertIn("accepted no output or workspace changes", operator_call["prompt"])
            self.assertEqual(runner.calls[2]["task"], "debug")
            for call in runner.calls[1:]:
                self.assertIn("resume", call["argv"])
                self.assertIn(SESSION_IDS["worker"], call["argv"])

            state_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            state = load_agent_team_state(state_path, step=workflow["steps"][0])
            verify_agent_team_state_outputs(
                state,
                runner.run.artifacts_dir,
                workflow["steps"][0],
            )
            self.assertEqual(
                [turn["status"] for turn in state["turns"]],
                ["interrupted", "completed", "completed"],
            )
            self.assertEqual(state["members"][1]["session_id"], SESSION_IDS["worker"])
            debug_task = next(task for task in state["tasks"] if task["id"] == "debug")
            self.assertEqual(debug_task["attempts"], 2)
            self.assertEqual(debug_task["status"], "completed")
            transcripts = list_team_transcripts(
                runner.run.run_dir,
                "team-debug",
            )["transcripts"]
            self.assertEqual(len(transcripts), 3)
            self.assertEqual(
                [value["status"] for value in transcripts],
                ["interrupted", "completed", "completed"],
            )
            self.assertEqual(
                [value["task_id"] for value in transcripts],
                ["debug", runner.calls[1]["task"], "debug"],
            )
            interrupted_turn = state["turns"][0]
            self.assertEqual(
                interrupted_turn["interruption_instruction_sha256"],
                hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            )
            interruption_text = (
                runner.run.artifacts_dir / interrupted_turn["output"]
            ).read_text(encoding="utf-8")
            self.assertNotIn(instruction, interruption_text)
            self.assertNotIn(SESSION_IDS["worker"], interruption_text)
            interruption_path = runner.run.artifacts_dir / interrupted_turn["output"]
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-team-interruption",
                            str(interruption_path),
                            "--json",
                        ]
                    ),
                    0,
                )
            inspected_value = json.loads(inspected.getvalue())
            self.assertEqual(inspected_value["status"], "interrupted")
            self.assertNotIn(instruction, inspected.getvalue())
            self.assertNotIn(SESSION_IDS["worker"], inspected.getvalue())
            interruption_assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "inspect-agent-team-interruption",
                    str(interruption_path),
                ]
            )
            self.assertFalse(interruption_assessment.writes)
            self.assertFalse(interruption_assessment.network)

            inbox_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            inbox = load_agent_team_operator_inbox(inbox_path)
            entry = inbox["entries"][0]
            self.assertEqual(entry["delivery"], "interrupt-current")
            self.assertEqual(entry["interrupt_round"], 1)
            self.assertEqual(entry["interrupt_task_id"], "debug")
            self.assertIsNotNone(entry["interrupted_at_utc"])
            self.assertEqual(entry["status"], "accepted")
            public = json.dumps(agent_team_operator_inbox_summary(inbox))
            self.assertNotIn(instruction, public)
            detail = collect_run_detail(root / "runs", "operator-interrupt")
            self.assertEqual(detail["agent_teams"][0]["interruption_count"], 1)
            self.assertNotIn(instruction, json.dumps(detail))
            self.assertIn("1 interrupted", render_live_run_detail(detail))

    def test_interruption_state_first_window_reconciles_and_instruction_tamper_fails(self):
        workflow = continuation_workflow()
        workflow["steps"][0]["max_operator_tasks"] = 1
        step = workflow["steps"][0]
        state = initial_agent_team_state(step, "a" * 64)
        assignments = assign_agent_team_tasks(step, state, max_workers=1)
        self.assertEqual(assignments, [{"member_id": "worker", "task_id": "debug"}])
        state["round"] = 1
        state["authorized_tokens"] = 1000
        instruction = "Redirect the active teammate to the exact malformed retry boundary."
        inbox = initial_agent_team_operator_inbox(step["id"], state["workflow_fingerprint"])
        entry = append_agent_team_operator_entry(
            inbox,
            generation=state["generation"],
            member_id="worker",
            instruction=instruction,
            delivery="interrupt-current",
            interrupt_round=1,
            interrupt_task_id="debug",
        )
        interruption = build_agent_team_interruption(
            task_id="debug",
            operator_entry_id=entry["id"],
            instruction_sha256=entry["instruction_sha256"],
            session_id=SESSION_IDS["worker"],
        )
        apply_agent_team_interruption(
            state,
            member_id="worker",
            task_id="debug",
            interruption=interruption,
            output="team-debug-internal/round-001/worker--debug.json",
            output_sha256="b" * 64,
            session_id=SESSION_IDS["worker"],
            started_at_utc="2026-07-10T20:00:00.000Z",
            finished_at_utc="2026-07-10T20:00:01.000Z",
            max_tokens=1000,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
        )
        validate_agent_team_state(state, step=step)
        with self.assertRaisesRegex(ValidationError, "not reconciled"):
            verify_agent_team_operator_inbox(inbox, state)
        self.assertEqual(reconcile_agent_team_operator_interruptions(inbox, state), 1)
        verify_agent_team_operator_inbox(inbox, state)
        self.assertEqual(
            inbox["entries"][0]["interrupted_at_utc"],
            state["turns"][0]["finished_at_utc"],
        )
        self.assertEqual(reconcile_agent_team_operator_interruptions(inbox, state), 0)

        tampered = json.loads(json.dumps(inbox))
        tampered["entries"][0]["instruction"] = "A different redirect."
        tampered["entries"][0]["instruction_sha256"] = hashlib.sha256(
            tampered["entries"][0]["instruction"].encode("utf-8")
        ).hexdigest()
        validate_agent_team_operator_inbox(tampered)
        with self.assertRaisesRegex(ValidationError, "binding changed"):
            verify_agent_team_operator_inbox(tampered, state)

        legacy_inbox = initial_agent_team_operator_inbox(
            step["id"],
            state["workflow_fingerprint"],
            schema=AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
        )
        append_agent_team_operator_entry(
            legacy_inbox,
            generation=0,
            member_id="worker",
            instruction="Retained v1 next-turn instruction.",
        )
        validate_agent_team_operator_inbox(legacy_inbox)
        self.assertEqual(
            agent_team_operator_inbox_summary(legacy_inbox)["entries"][0]["delivery"],
            "next-turn",
        )
        with self.assertRaisesRegex(ValidationError, "legacy.*cannot interrupt"):
            append_agent_team_operator_entry(
                legacy_inbox,
                generation=0,
                member_id="worker",
                instruction="Unsupported legacy interrupt.",
                delivery="interrupt-current",
                interrupt_round=1,
                interrupt_task_id="debug",
            )

    def test_duplicate_interrupt_rejects_and_completed_turn_degrades_to_next_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 2
            first_turn_started = threading.Event()
            release_first_turn = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                first_turn_started=first_turn_started,
                release_first_turn=release_first_turn,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            first = queue_team_task(
                runner.run.run_dir,
                "team-debug",
                "worker",
                "Redirect if the active call has not already completed.",
                interrupt_current=True,
            )
            self.assertEqual(first["delivery"], "interrupt-current")
            with self.assertRaisesRegex(ValidationError, "already has a pending interrupt"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "A duplicate redirect for the same active turn.",
                    interrupt_current=True,
                )
            release_first_turn.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-debug-internal" / "team-state.json",
                step=workflow["steps"][0],
            )
            self.assertEqual([turn["status"] for turn in state["turns"]], ["completed", "completed"])
            inbox = load_agent_team_operator_inbox(
                runner.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            self.assertEqual(inbox["entries"][0]["status"], "accepted")
            self.assertIsNone(inbox["entries"][0]["interrupted_at_utc"])

    def test_parallel_interrupt_targets_one_member_and_preserves_successful_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            first_turn_started = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                first_turn_started=first_turn_started,
                interrupt_first_turn=True,
                interrupt_member_id="security",
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            instruction = "Redirect only the security teammate to the cookie boundary."
            queue_team_task(
                runner.run.run_dir,
                "team-review",
                "security",
                instruction,
                interrupt_current=True,
            )
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            correctness_calls = [
                call for call in runner.calls if call["task"] == "correctness-audit"
            ]
            self.assertEqual(len(correctness_calls), 1)
            security_calls = [
                call for call in runner.calls if call["member"] == "security"
            ]
            self.assertEqual(len(security_calls), 3)
            self.assertIn(instruction, security_calls[1]["prompt"])
            self.assertNotIn(instruction, correctness_calls[0]["prompt"])
            state = load_agent_team_state(
                runner.run.artifacts_dir / "team-review-internal" / "team-state.json",
                step=workflow["steps"][0],
            )
            interrupted = [turn for turn in state["turns"] if turn["status"] == "interrupted"]
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(interrupted[0]["member_id"], "security")
            correctness_task = next(
                task for task in state["tasks"] if task["id"] == "correctness-audit"
            )
            self.assertEqual(correctness_task["attempts"], 1)
            self.assertEqual(correctness_task["status"], "completed")

    def test_interrupted_write_turn_discards_its_isolated_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            step = workflow["steps"][0]
            step["max_operator_tasks"] = 1
            step["max_rounds"] = 3
            step["max_total_tokens"] = 4000
            first_turn_started = threading.Event()
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                first_turn_started=first_turn_started,
                interrupt_first_turn=True,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            queue_team_task(
                runner.run.run_dir,
                "team-implementation",
                "lead",
                "Discard the broad attempt and apply only the bounded lead edit.",
                interrupt_current=True,
            )
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(runner.calls[0]["source_before"], "base\n")
            self.assertEqual(runner.calls[1]["source_before"], "base\n")
            self.assertNotIn(
                "partial-interrupted",
                (workspace / "shared.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (workspace / "shared.txt").read_text(encoding="utf-8"),
                "lead\n",
            )
            state = load_agent_team_state(
                runner.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json",
                step=step,
            )
            self.assertEqual(
                [turn["status"] for turn in state["turns"]],
                ["interrupted", "completed", "completed"],
            )
            ledger = load_agent_team_merge_ledger(
                runner.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual(ledger["event_count"], 2)
            metrics = runner.run.read_state()["steps"]["team-implementation"]
            self.assertEqual(metrics["agent_invocations"], 3)
            self.assertEqual(metrics["agent_team_interruptions"], 1)

    def test_enabled_parallel_team_shares_one_bounded_interrupt_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = coordinated_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            runner = WorkflowRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            step = workflow["steps"][0]
            state = initial_agent_team_state(step, workflow_fingerprint(workflow))
            state["round"] = 1
            calls = []

            def missing_inbox(*args, **kwargs):
                calls.append((args, kwargs))
                raise FileNotFoundError

            with patch.object(
                runner,
                "_agent_team_operator_inbox_path",
                return_value=root / "missing-inbox.json",
            ), patch("conductor_extras.runtime.runner.os.stat", side_effect=missing_inbox):
                for _index in range(64):
                    self.assertEqual(
                        runner._agent_team_interrupt_requests(step, state),
                        [],
                    )
            self.assertEqual(len(calls), 1)

    def test_interrupt_requires_future_capacity_and_leaves_no_request_on_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            step = workflow["steps"][0]
            step["max_operator_tasks"] = 1
            step["max_rounds"] = 1
            step["max_total_tokens"] = 1000
            first_turn_started = threading.Event()
            release_first_turn = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                first_turn_started=first_turn_started,
                release_first_turn=release_first_turn,
            )
            failures = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    failures.append(exc)

            thread = threading.Thread(target=execute_team)
            thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            with self.assertRaisesRegex(ValidationError, "remaining round capacity"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "Redirect this final-round turn.",
                    interrupt_current=True,
                )
            inbox_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            self.assertFalse(inbox_path.exists())
            release_first_turn.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

    def test_operator_task_authority_capacity_tamper_and_reset_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            disabled = continuation_workflow()
            disabled_runner = FakeTeamRunner(
                workflow=disabled,
                workspace=workspace,
                base_run_dir=root / "disabled-runs",
                policy=RuntimePolicy(allow_agent=True),
                fail_once_tasks={"debug"},
            )
            with self.assertRaises(StepExecutionError):
                disabled_runner.execute()
            self.assertTrue(disabled_runner.calls)
            self.assertFalse(
                any(call["interrupt_polling"] for call in disabled_runner.calls)
            )
            with self.assertRaisesRegex(ValidationError, "does not allow operator tasks"):
                queue_team_task(
                    disabled_runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "Review one more boundary.",
                )

            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                fail_once_tasks={"debug"},
            )
            with self.assertRaises(StepExecutionError):
                runner.execute()
            with self.assertRaisesRegex(ValidationError, "currently working member"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "Interrupt an idle teammate.",
                    interrupt_current=True,
                )
            with self.assertRaisesRegex(ValidationError, "secret-like"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "Use token=super-secret-value for the check.",
                )
            with self.assertRaisesRegex(ValidationError, "not part of the team"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "unknown",
                    "Review one more boundary.",
                )
            state_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            original_state_text = state_path.read_text(encoding="utf-8")
            fingerprint_tamper = json.loads(original_state_text)
            fingerprint_tamper["workflow_fingerprint"] = "0" * 64
            state_path.write_text(json.dumps(fingerprint_tamper), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "workflow fingerprint"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "Review the workflow-bound retry boundary.",
                )
            state_path.write_text(original_state_text, encoding="utf-8")
            queued = queue_team_task(
                runner.run.run_dir,
                "team-debug",
                "worker",
                "Review malformed retry values before rerunning.",
            )
            self.assertEqual(queued["status"], "pending")
            with self.assertRaisesRegex(ValidationError, "operator task limit"):
                queue_team_task(
                    runner.run.run_dir,
                    "team-debug",
                    "worker",
                    "Review a second extra boundary.",
                )

            inbox_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            tampered_path = root / "tampered-inbox.json"
            tampered = json.loads(inbox_path.read_text(encoding="utf-8"))
            tampered["entries"][0]["instruction"] = "Changed after queueing."
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "instruction hash"):
                load_agent_team_operator_inbox(tampered_path)

            reset_step(runner.run.run_dir, "team-debug", reason="start a fresh team generation")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=runner.run.run_dir,
            )
            resumed.execute()
            state_path = (
                runner.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            state = load_agent_team_state(state_path, step=workflow["steps"][0])
            self.assertEqual(state["generation"], 1)
            self.assertEqual(state["operator_tasks_added"], 0)
            inbox = load_agent_team_operator_inbox(inbox_path)
            self.assertEqual(inbox["entries"][0]["status"], "superseded")
            self.assertIsNotNone(inbox["entries"][0]["superseded_at_utc"])

    def test_operator_task_blocked_hook_stays_pending_until_a_clean_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            workflow["hooks"] = [
                {
                    "id": "operator-created-gate",
                    "event": "team_task_created",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "block",
                }
            ]
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                fail_once_tasks={"debug"},
            )
            with self.assertRaises(StepExecutionError):
                first.execute()
            retry_step(first.run.run_dir, "team-debug", reason="retry failed teammate")
            instruction = "Recheck the retry boundary before the teammate resumes."
            queue_team_task(
                first.run.run_dir,
                "team-debug",
                "worker",
                instruction,
            )

            blocked = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                quality_gate_failures=1,
                resume_dir=first.run.run_dir,
            )
            with self.assertRaises(StepExecutionError):
                blocked.execute()
            inbox_path = (
                first.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            inbox = load_agent_team_operator_inbox(inbox_path)
            self.assertEqual(inbox["entries"][0]["status"], "pending")
            state_path = (
                first.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            blocked_state = load_agent_team_state(state_path, step=workflow["steps"][0])
            self.assertEqual(blocked_state["operator_tasks_added"], 0)
            self.assertNotIn(
                instruction,
                json.dumps(agent_team_state_summary(blocked_state)),
            )

            retry_step(first.run.run_dir, "team-debug", reason="quality gate repaired")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=first.run.run_dir,
            )
            resumed.execute()
            final_state = load_agent_team_state(state_path, step=workflow["steps"][0])
            self.assertEqual(final_state["status"], "completed")
            self.assertEqual(final_state["operator_tasks_added"], 1)
            self.assertEqual(
                load_agent_team_operator_inbox(inbox_path)["entries"][0]["status"],
                "accepted",
            )

    def test_operator_queue_and_team_completion_race_has_no_lost_pending_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            first_turn_started = threading.Event()
            release_first_turn = threading.Event()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="operator-completion-race",
                first_turn_started=first_turn_started,
                release_first_turn=release_first_turn,
            )
            run_failures = []
            queue_results = []

            def execute_team():
                try:
                    runner.execute()
                except Exception as exc:  # noqa: BLE001 - test thread returns failures to the parent.
                    run_failures.append(exc)

            def queue_follow_up():
                try:
                    queue_results.append(
                        queue_team_task(
                            runner.run.run_dir,
                            "team-debug",
                            "worker",
                            "Race completion while preserving this bounded follow-up.",
                        )
                    )
                except ValidationError as exc:
                    queue_results.append(exc)

            run_thread = threading.Thread(target=execute_team)
            run_thread.start()
            self.assertTrue(first_turn_started.wait(timeout=5))
            queue_thread = threading.Thread(target=queue_follow_up)
            queue_thread.start()
            release_first_turn.set()
            queue_thread.join(timeout=10)
            run_thread.join(timeout=10)
            self.assertFalse(queue_thread.is_alive())
            self.assertFalse(run_thread.is_alive())
            self.assertEqual(run_failures, [])
            self.assertEqual(len(queue_results), 1)

            inbox = list_team_inbox(runner.run.run_dir, "team-debug")
            self.assertEqual(inbox["team_status"], "completed")
            self.assertEqual(inbox["status_counts"]["pending"], 0)
            if isinstance(queue_results[0], ValidationError):
                self.assertIn("completed agent team", str(queue_results[0]))
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(inbox["entry_count"], 0)
            else:
                self.assertEqual(queue_results[0]["status"], "pending")
                self.assertEqual(len(runner.calls), 2)
                self.assertEqual(inbox["status_counts"]["accepted"], 1)

    def test_operator_state_first_crash_window_reconciles_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["steps"][0]["max_operator_tasks"] = 1
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                fail_once_tasks={"debug"},
            )
            with self.assertRaises(StepExecutionError):
                first.execute()
            queue_team_task(
                first.run.run_dir,
                "team-debug",
                "worker",
                "Reconcile exactly once after the state-first checkpoint.",
            )
            state_path = (
                first.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            inbox_path = (
                first.run.artifacts_dir / "team-debug-internal" / "operator-inbox.json"
            )
            state = load_agent_team_state(state_path, step=workflow["steps"][0])
            inbox = load_agent_team_operator_inbox(inbox_path)
            prepared = prepare_agent_team_operator_tasks(
                workflow["steps"][0],
                state,
                [inbox["entries"][0]],
                max_workers=1,
            )
            state["tasks"].extend(prepared)
            state["operator_tasks_added"] += 1
            write_agent_team_state(state_path, state)
            self.assertEqual(
                load_agent_team_operator_inbox(inbox_path)["entries"][0]["status"],
                "pending",
            )

            retry_step(first.run.run_dir, "team-debug", reason="resume after state checkpoint")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=first.run.run_dir,
            )
            resumed.execute()
            final_state = load_agent_team_state(state_path, step=workflow["steps"][0])
            operator_tasks = [
                task for task in final_state["tasks"] if task["origin"] == "operator"
            ]
            self.assertEqual(len(operator_tasks), 1)
            self.assertEqual(operator_tasks[0]["status"], "completed")
            self.assertEqual(final_state["operator_tasks_added"], 1)
            self.assertEqual(
                load_agent_team_operator_inbox(inbox_path)["entries"][0]["status"],
                "accepted",
            )
            self.assertEqual(
                sum(call["task"] == operator_tasks[0]["id"] for call in resumed.calls),
                1,
            )

    def test_retry_preserves_successful_teammate_and_resumes_unfinished_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True)
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                fail_once_tasks={"correctness-audit"},
            )
            with self.assertRaises(StepExecutionError):
                first.execute()

            state_path = first.run.artifacts_dir / "team-review-internal" / "team-state.json"
            failed_state = load_agent_team_state(state_path)
            task_statuses = {task["id"]: task["status"] for task in failed_state["tasks"]}
            self.assertEqual(task_statuses["security-audit"], "completed")
            self.assertEqual(task_statuses["correctness-audit"], "pending")
            self.assertEqual(task_statuses["synthesize"], "pending")
            self.assertEqual(failed_state["generation"], 0)

            retry_step(first.run.run_dir, "team-review", reason="retry unfinished teammate")
            run_state = first.run.read_state()
            self.assertNotIn("agent_team_generations", run_state)
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=first.run.run_dir,
            )
            resumed.execute()
            self.assertEqual([call["task"] for call in resumed.calls], ["correctness-audit", "synthesize"])
            self.assertNotIn("security-audit", [call["task"] for call in resumed.calls])
            lead_call = next(call for call in resumed.calls if call["task"] == "synthesize")
            self.assertIn("authentication boundary issue", lead_call["prompt"])
            completed_state = load_agent_team_state(state_path)
            self.assertEqual(completed_state["generation"], 0)
            self.assertEqual(completed_state["status"], "completed")

    def test_reset_step_advances_generation_and_reruns_every_team_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True)
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
            )
            run = first.execute()
            reset_step(run.run_dir, "team-review", reason="fresh coordinated review")
            self.assertEqual(run.read_state()["agent_team_generations"]["team-review"], 1)

            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=run.run_dir,
            )
            resumed.execute()
            self.assertEqual(len(resumed.calls), 3)
            self.assertEqual(
                {call["task"] for call in resumed.calls},
                {"security-audit", "correctness-audit", "synthesize"},
            )
            self.assertTrue(all("resume" not in call["argv"] for call in resumed.calls))
            state = load_agent_team_state(
                run.artifacts_dir / "team-review-internal" / "team-state.json"
            )
            self.assertEqual(state["generation"], 1)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(resumed.run.read_state()["steps"]["team-review"]["agent_team_generation"], 1)

    def test_continuing_turn_reuses_the_same_member_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            runner = FakeTeamRunner(
                workflow=continuation_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                continue_once=True,
            )
            run = runner.execute()
            self.assertEqual(len(runner.calls), 2)
            first, second = runner.calls
            self.assertNotIn("resume", first["argv"])
            self.assertIn("resume", second["argv"])
            self.assertIn(SESSION_IDS["worker"], second["argv"])
            state = load_agent_team_state(
                run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            self.assertEqual(state["round"], 2)
            self.assertEqual(state["members"][1]["turns"], 2)
            self.assertEqual(state["tasks"][0]["attempts"], 2)
            self.assertEqual(len({turn["session_id_sha256"] for turn in state["turns"]}), 1)

    def test_lead_can_add_bounded_tasks_and_complete_an_adaptive_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = adaptive_workflow()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                run_id="adaptive",
            )
            run = runner.execute()

            self.assertEqual(
                [call["task"] for call in runner.calls],
                ["plan-adaptive", "investigate-adaptive", "synthesize-adaptive"],
            )
            investigation = runner.calls[1]
            self.assertIn("Verify the adaptive boundary", investigation["prompt"])
            synthesis = runner.calls[2]
            self.assertIn("resume", synthesis["argv"])
            self.assertIn(SESSION_IDS["lead"], synthesis["argv"])
            self.assertIn("adaptive boundary with concrete evidence", synthesis["prompt"])

            state_path = run.artifacts_dir / "adaptive-review-internal" / "team-state.json"
            state = load_agent_team_state(
                state_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
            )
            self.assertEqual(state["dynamic_tasks_added"], 2)
            self.assertEqual([task["origin"] for task in state["tasks"]], ["static", "proposed", "proposed"])
            self.assertEqual(state["tasks"][1]["proposed_by"], "lead")
            self.assertEqual(state["tasks"][1]["proposed_round"], 1)
            self.assertEqual(state["round"], 3)
            self.assertEqual(state["status"], "completed")
            report = (run.artifacts_dir / "adaptive-review.md").read_text(encoding="utf-8")
            self.assertIn("Dynamic tasks: 2/2", report)
            self.assertIn("Origin: `proposed`", report)
            metrics = run.read_state()["steps"]["adaptive-review"]
            self.assertEqual(metrics["agent_team_dynamic_tasks"], 2)
            self.assertEqual(metrics["agent_team_tasks_completed"], 3)
            detail = collect_run_detail(root / "runs", "adaptive")
            self.assertEqual(detail["agent_teams"][0]["dynamic_tasks_added"], 2)

    def test_dynamic_task_authority_and_capacity_are_enforced(self):
        workflow = adaptive_workflow()
        step = workflow["steps"][0]
        state = initial_agent_team_state(step, "d" * 64)
        assign_agent_team_tasks(step, state)
        state["round"] = 1
        state["authorized_tokens"] = 1000
        proposal = {
            "schema": AGENT_TEAM_TURN_SCHEMA,
            "task_id": "plan-adaptive",
            "status": "completed",
            "summary": "Planned bounded follow-up work.",
            "messages": [],
            "claim_task_ids": [],
            "task_proposals": [
                {
                    "id": "follow-up",
                    "description": "Perform one bounded follow-up.",
                    "assignee": "worker",
                    "depends_on": ["plan-adaptive"],
                }
            ],
        }
        with self.assertRaisesRegex(ValidationError, "only the configured.*lead"):
            prepare_agent_team_task_proposals(
                step,
                state,
                "worker",
                "plan-adaptive",
                proposal,
            )

        forward = json.loads(json.dumps(proposal))
        forward["task_proposals"] = [
            {
                "id": "first",
                "description": "First proposed task.",
                "assignee": "worker",
                "depends_on": ["second"],
            },
            {
                "id": "second",
                "description": "Second proposed task.",
                "assignee": "lead",
                "depends_on": ["plan-adaptive"],
            },
        ]
        with self.assertRaisesRegex(ValidationError, "out of order"):
            parse_agent_team_turn(
                json.dumps(forward),
                "plan-adaptive",
                ["lead", "worker"],
                ["plan-adaptive"],
            )

        too_many = json.loads(json.dumps(proposal))
        too_many["task_proposals"] = [
            {
                "id": "follow-up-%d" % index,
                "description": "Bounded follow-up %d." % index,
                "assignee": None,
                "depends_on": ["plan-adaptive"],
            }
            for index in range(3)
        ]
        with self.assertRaisesRegex(ValidationError, "dynamic task limit"):
            prepare_agent_team_task_proposals(
                step,
                state,
                "lead",
                "plan-adaptive",
                too_many,
            )

        shallow_workflow = adaptive_workflow()
        shallow_step = shallow_workflow["steps"][0]
        shallow_step["max_rounds"] = 2
        shallow_state = initial_agent_team_state(shallow_step, "e" * 64)
        assign_agent_team_tasks(shallow_step, shallow_state)
        shallow_state["round"] = 1
        shallow_state["authorized_tokens"] = 1000
        chain = json.loads(json.dumps(proposal))
        chain["task_proposals"].append(
            {
                "id": "dependent-follow-up",
                "description": "Synthesize the first follow-up.",
                "assignee": "lead",
                "depends_on": ["follow-up"],
            }
        )
        with self.assertRaisesRegex(ValidationError, "dependency depth"):
            prepare_agent_team_task_proposals(
                shallow_step,
                shallow_state,
                "lead",
                "plan-adaptive",
                chain,
            )

        token_workflow = adaptive_workflow()
        token_step = token_workflow["steps"][0]
        token_step["max_total_tokens"] = 1050
        token_state = initial_agent_team_state(token_step, "f" * 64)
        assign_agent_team_tasks(token_step, token_state)
        token_state["round"] = 1
        token_state["authorized_tokens"] = 1000
        with self.assertRaisesRegex(ValidationError, "minimum token funding"):
            prepare_agent_team_task_proposals(
                token_step,
                token_state,
                "lead",
                "plan-adaptive",
                proposal,
            )

    def test_dynamic_task_metadata_is_bound_to_the_lead_turn_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            runner = FakeTeamRunner(
                workflow=adaptive_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            run = runner.execute()
            state = load_agent_team_state(
                run.artifacts_dir / "adaptive-review-internal" / "team-state.json"
            )
            state["tasks"][1]["description"] = "Tampered proposed scope."
            with self.assertRaisesRegex(ValidationError, "dynamic tasks do not match"):
                verify_agent_team_state_outputs(
                    state,
                    run.artifacts_dir,
                    runner.workflow["steps"][0],
                )

    def test_rejected_lead_proposal_preserves_successful_sibling_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = adaptive_workflow()
            workflow["steps"][0]["tasks"].append(
                {
                    "id": "baseline-review",
                    "description": "Complete an independent baseline review.",
                    "assignee": "worker",
                    "depends_on": [],
                }
            )
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                invalid_proposals=True,
            )
            with self.assertRaises(StepExecutionError):
                first.execute()

            state_path = first.run.artifacts_dir / "adaptive-review-internal" / "team-state.json"
            failed = load_agent_team_state(state_path)
            statuses = {task["id"]: task["status"] for task in failed["tasks"]}
            self.assertEqual(statuses["plan-adaptive"], "pending")
            self.assertEqual(statuses["baseline-review"], "completed")
            self.assertEqual(failed["dynamic_tasks_added"], 0)
            self.assertEqual([turn["task_id"] for turn in failed["turns"]], ["baseline-review"])

            retry_step(first.run.run_dir, "adaptive-review", reason="retry rejected lead proposal")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                resume_dir=first.run.run_dir,
            )
            resumed.execute()
            self.assertNotIn("baseline-review", [call["task"] for call in resumed.calls])
            completed = load_agent_team_state(state_path)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["dynamic_tasks_added"], 2)

    def test_legacy_v1_team_state_resumes_with_v1_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                fail_once_tasks={"debug"},
            )
            with self.assertRaises(StepExecutionError):
                first.execute()
            state_path = first.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            legacy = load_agent_team_state(state_path)
            legacy_v5 = json.loads(json.dumps(legacy))
            legacy_v5["schema"] = AGENT_TEAM_STATE_SCHEMA_V5
            legacy_v5.pop("max_operator_questions")
            legacy_v5.pop("operator_questions_added")
            for task in legacy_v5["tasks"]:
                task.pop("operator_reply_sha256")
            validate_agent_team_state(legacy_v5, step=first.workflow["steps"][0])
            legacy_v4 = json.loads(json.dumps(legacy_v5))
            legacy_v4["schema"] = AGENT_TEAM_STATE_SCHEMA_V4
            legacy_v4.pop("max_message_tasks")
            legacy_v4.pop("message_tasks_added")
            for task in legacy_v4["tasks"]:
                task.pop("message_id")
                task.pop("message_depth")
            validate_agent_team_state(legacy_v4, step=first.workflow["steps"][0])
            legacy_v3 = json.loads(json.dumps(legacy_v4))
            legacy_v3["schema"] = AGENT_TEAM_STATE_SCHEMA_V3
            for turn in legacy_v3["turns"]:
                turn.pop("interruption_entry_id")
                turn.pop("interruption_instruction_sha256")
            validate_agent_team_state(legacy_v3, step=first.workflow["steps"][0])
            legacy_v2 = json.loads(json.dumps(legacy_v3))
            legacy_v2["schema"] = AGENT_TEAM_STATE_SCHEMA_V2
            legacy_v2.pop("max_operator_tasks")
            legacy_v2.pop("operator_tasks_added")
            for task in legacy_v2["tasks"]:
                task.pop("operator_entry_id")
            validate_agent_team_state(legacy_v2, step=first.workflow["steps"][0])
            legacy = json.loads(json.dumps(legacy_v2))
            legacy["schema"] = AGENT_TEAM_STATE_SCHEMA_V1
            legacy.pop("max_dynamic_tasks")
            legacy.pop("dynamic_tasks_added")
            for task in legacy["tasks"]:
                for field in (
                    "description",
                    "origin",
                    "proposed_by",
                    "proposed_round",
                    "operator_entry_id",
                ):
                    task.pop(field, None)
            validate_agent_team_state(legacy, step=first.workflow["steps"][0])
            state_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
            retry_step(first.run.run_dir, "team-debug", reason="resume legacy team state")

            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=first.run.run_dir,
                legacy_turns=True,
            )
            resumed.execute()
            restored = load_agent_team_state(
                state_path,
                step=resumed.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(resumed.workflow),
            )
            self.assertEqual(restored["schema"], AGENT_TEAM_STATE_SCHEMA_V1)
            self.assertEqual(restored["status"], "completed")
            self.assertEqual(restored["turns"][0]["round"], 2)
            output = first.run.artifacts_dir / restored["turns"][0]["output"]
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["schema"], AGENT_TEAM_TURN_SCHEMA_V1)

    def test_dry_run_preflights_team_without_launching_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            runner = FakeTeamRunner(
                workflow=coordinated_workflow(),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                dry_run=True,
            )
            run = runner.execute()
            self.assertEqual(runner.calls, [])
            self.assertEqual(run.read_state()["steps"]["team-review"]["status"], "planned")
            self.assertFalse((run.artifacts_dir / "team-review-internal" / "team-state.json").exists())

    def test_runtime_worker_override_limits_team_round_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                max_workers=1,
            )
            run = runner.execute()
            state = load_agent_team_state(
                run.artifacts_dir / "team-review-internal" / "team-state.json"
            )
            self.assertEqual(state["round"], 3)
            self.assertEqual(run.read_state()["steps"]["team-review"]["agent_workers"], 1)

    def test_plan_approval_policy_is_strict_bounded_and_pre_funded(self):
        workflow = plan_approval_workflow()
        validate_workflow(workflow)
        operator_workflow = operator_plan_approval_workflow()
        validate_workflow(operator_workflow)
        schema = get_schema("workflow")
        team_schema = next(
            item
            for item in schema["properties"]["steps"]["items"]["oneOf"]
            if item["properties"]["kind"].get("const") == "agent_team"
        )
        self.assertIn("plan_approval", team_schema["properties"])
        self.assertEqual(
            get_schema("agent-team-plan")["properties"]["schema"]["const"],
            AGENT_TEAM_PLAN_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-plan-review")["properties"]["schema"]["const"],
            AGENT_TEAM_PLAN_REVIEW_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-plan-approval")["properties"]["schema"]["const"],
            AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-plan-approval-v1")["properties"]["schema"]["const"],
            AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1,
        )

        cases = []
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["sandbox"] = "read-only"
        cases.append((bad, "requires workspace-write"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["plan_approval"]["task_ids"] = ["missing"]
        cases.append((bad, "unknown task"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["plan_approval"]["task_ids"] = ["worker-edit", "worker-edit"]
        cases.append((bad, "unique ids"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["tasks"][0]["assignee"] = "lead"
        cases.append((bad, "fixed non-lead assignee"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["plan_approval"]["plan_max_tokens"] = 1001
        cases.append((bad, "cannot exceed max_tokens"))
        bad = json.loads(json.dumps(workflow))
        bad["steps"][0]["max_total_tokens"] = 499
        cases.append((bad, "cannot fund worst-case"))
        bad = json.loads(json.dumps(operator_workflow))
        bad["steps"][0]["plan_approval"]["review_max_tokens"] = 100
        cases.append((bad, "plan_approval has invalid fields"))
        bad = json.loads(json.dumps(operator_workflow))
        bad["steps"][0]["plan_approval"]["reply_timeout_seconds"] = 0
        cases.append((bad, "reply_timeout_seconds must be an integer"))
        bad = json.loads(json.dumps(operator_workflow))
        bad["steps"][0]["plan_approval"]["reviewer"] = "peer"
        cases.append((bad, "reviewer must be lead or operator"))
        for candidate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_workflow(candidate)
        review_assessment = assess_command(
            [
                "python3",
                "-m",
                "conductor_extras",
                "review-team-plan",
                "/tmp/run",
                "team-planned-implementation",
                "worker-edit",
                "--decision",
                "approve",
                "--feedback",
                "bounded rationale",
            ]
        )
        self.assertTrue(review_assessment.writes)
        self.assertFalse(review_assessment.network)
        read_assessment = assess_command(
            [
                "python3",
                "-m",
                "conductor_extras",
                "read-team-plan",
                "/tmp/run",
                "team-planned-implementation",
                "worker-edit",
            ]
        )
        self.assertFalse(read_assessment.writes)
        self.assertFalse(read_assessment.network)

    def test_plan_approval_runs_read_only_lead_review_then_same_session_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = plan_approval_workflow()
            runner = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                run_id="planned-write",
            )
            with patch.object(
                runner,
                "_wait_for_agent_team_operator_plan_review",
                side_effect=AssertionError("lead review entered the operator wait path"),
            ):
                run = runner.execute()

            self.assertEqual([call["kind"] for call in runner.plan_calls], ["plan", "review"])
            self.assertTrue(all(call["source_before"] == "base\n" for call in runner.plan_calls))
            self.assertTrue(all(call["cwd"] != workspace for call in runner.plan_calls))
            self.assertEqual(len(runner.calls), 1)
            implementation = runner.calls[0]
            self.assertIn("resume", implementation["argv"])
            self.assertEqual(
                implementation["argv"][implementation["argv"].index("resume") + 1],
                SESSION_IDS["worker"],
            )
            self.assertIn("BEGIN_CONDUCTOR_APPROVED_TEAM_PLAN", implementation["prompt"])
            self.assertEqual(
                (workspace / "shared.txt").read_text(encoding="utf-8"),
                "worker\n",
            )

            approval_path = (
                run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            approval = load_agent_team_plan_approval(
                approval_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
                generation=0,
            )
            validate_agent_team_plan_approval(approval)
            verify_agent_team_plan_approval_outputs(approval, run.artifacts_dir)
            summary = agent_team_plan_approval_summary(approval)
            self.assertEqual(approval["schema"], AGENT_TEAM_PLAN_APPROVAL_SCHEMA)
            self.assertEqual(summary["status"], "approved")
            self.assertEqual(summary["attempt_count"], 1)
            self.assertEqual(summary["rejection_count"], 0)
            public = json.dumps(summary)
            self.assertNotIn(SESSION_IDS["worker"], public)
            self.assertNotIn("Edit shared.txt", public)
            self.assertNotIn("minimal and has", public)
            tampered = json.loads(json.dumps(approval))
            tampered["planner_session_id"] = SESSION_IDS["lead"]
            with self.assertRaisesRegex(ValidationError, "session hash changed"):
                validate_agent_team_plan_approval(tampered)
            tampered = json.loads(json.dumps(approval))
            tampered["attempts"][0]["feedback_sha256"] = "f" * 64
            with self.assertRaisesRegex(ValidationError, "review output binding changed"):
                verify_agent_team_plan_approval_outputs(tampered, run.artifacts_dir)

            attempt = approval["attempts"][0]
            plan_path = run.artifacts_dir / attempt["plan_output"]
            review_path = run.artifacts_dir / attempt["review_output"]
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main(["validate-agent-team-plan", str(plan_path)]), 0)
                self.assertEqual(
                    cli_main(["validate-agent-team-plan-review", str(review_path)]),
                    0,
                )
                self.assertEqual(
                    cli_main(["validate-agent-team-plan-approval", str(approval_path)]),
                    0,
                )
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-team-plan-approval",
                            str(approval_path),
                            "--json",
                        ]
                    ),
                    0,
                )
            inspected_text = inspected.getvalue()
            self.assertNotIn(SESSION_IDS["worker"], inspected_text)
            self.assertNotIn("Edit shared.txt", inspected_text)
            assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "inspect-agent-team-plan-approval",
                    str(approval_path),
                ]
            )
            self.assertFalse(assessment.writes)
            self.assertFalse(assessment.network)

            state = load_agent_team_state(
                run.artifacts_dir
                / "team-planned-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["authorized_tokens"], 1200)
            metrics = run.read_state()["steps"]["team-planned-implementation"]
            self.assertEqual(metrics["agent_team_plan_approval_tasks"], 1)
            self.assertEqual(metrics["agent_team_plan_approvals"], 1)
            self.assertEqual(metrics["agent_team_plan_approval_calls"], 2)
            self.assertEqual(metrics["agent_invocations"], 3)
            self.assertEqual(metrics["total_tokens"], 30)
            detail = collect_run_detail(root / "runs", "planned-write")
            team = detail["agent_teams"][0]
            self.assertEqual(team["plan_approval_approved_count"], 1)
            self.assertEqual(team["plan_approval_rejection_count"], 0)
            self.assertNotIn(SESSION_IDS["worker"], json.dumps(team["plan_approvals"]))
            live_html = render_live_run_detail(detail)
            self.assertIn("plan approvals 1 of 1 approved", live_html)
            self.assertNotIn(SESSION_IDS["worker"], live_html)

    def test_operator_plan_review_approves_without_lead_call_and_reuses_planner_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            runner = FakePlanApprovalRunner(
                workflow=operator_plan_approval_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                run_id="operator-plan-review",
            )
            errors = []

            def execute():
                try:
                    runner.execute()
                except BaseException as exc:  # noqa: BLE001 - preserve worker-thread failure.
                    errors.append(exc)

            thread = threading.Thread(target=execute)
            thread.start()
            deadline = time.monotonic() + 5
            summary = None
            while time.monotonic() < deadline:
                try:
                    summary = list_team_plans(
                        runner.run.run_dir,
                        "team-planned-implementation",
                    )
                except (FileNotFoundError, ValidationError):
                    time.sleep(0.01)
                    continue
                if summary["plans"] and summary["plans"][0]["status"] == "plan-ready":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(summary)
            self.assertEqual(summary["reviewer"], "operator")
            pending = summary["plans"][0]
            self.assertEqual(pending["status"], "plan-ready")
            self.assertEqual(pending["provider_call_count"], 1)
            self.assertNotIn("Edit shared.txt", json.dumps(summary))

            private = read_team_plan(
                runner.run.run_dir,
                "team-planned-implementation",
                "worker-edit",
            )
            self.assertIn("Edit shared.txt", private["plan"]["plan"])
            self.assertEqual(private["reviewer"], "operator")
            handle = run_handle(runner.run.run_dir.name)
            detail = collect_run_detail(root / "runs", runner.run.run_dir.name)
            detail["run_handle"] = handle
            html = render_live_run_detail(detail)
            self.assertIn('value="review-team-plan"', html)
            self.assertIn(pending["latest_plan_output_sha256"], html)
            self.assertNotIn(private["plan"]["plan"], html)
            with self.assertRaisesRegex(ValidationError, "secret-like"):
                review_team_plan(
                    runner.run.run_dir,
                    "team-planned-implementation",
                    "worker-edit",
                    "approve",
                    "Use token=abcdefghijklmnop in the implementation.",
                )
            feedback = "Approved because the plan is minimal and has an exact verification step."
            result = apply_live_control(
                root / "runs",
                {
                    "token": "unit-token",
                    "action": "review-team-plan",
                    "run_handle": handle,
                    "step_handle": step_handle("team-planned-implementation"),
                    "task_id": "worker-edit",
                    "decision": "approve",
                    "feedback": feedback,
                    "return_to": "/run/%s" % handle,
                },
                control_token="unit-token",
            )
            self.assertEqual(result["status"], "approved")
            self.assertNotIn(feedback, json.dumps(result))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

            self.assertEqual([call["kind"] for call in runner.plan_calls], ["plan"])
            self.assertEqual(len(runner.calls), 1)
            implementation = runner.calls[0]
            self.assertIn("resume", implementation["argv"])
            self.assertEqual(
                implementation["argv"][implementation["argv"].index("resume") + 1],
                SESSION_IDS["worker"],
            )
            self.assertIn("human operator approved", implementation["prompt"])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "worker\n")
            approval = load_agent_team_plan_approval(
                runner.run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            public_summary = agent_team_plan_approval_summary(approval)
            self.assertEqual(approval["schema"], AGENT_TEAM_PLAN_APPROVAL_SCHEMA)
            self.assertEqual(public_summary["reviewer"], "operator")
            self.assertEqual(public_summary["provider_call_count"], 1)
            self.assertEqual(public_summary["operator_review_count"], 1)
            self.assertIsNone(approval["lead_session_id"])
            self.assertIsNone(approval["attempts"][0]["review_total_tokens"])
            tampered = json.loads(json.dumps(approval))
            tampered["attempts"][0]["review_total_tokens"] = 1
            with self.assertRaisesRegex(ValidationError, "provider usage"):
                validate_agent_team_plan_approval(tampered)
            tampered = json.loads(json.dumps(approval))
            tampered["lead_session_id"] = SESSION_IDS["lead"]
            tampered["lead_session_id_sha256"] = hashlib.sha256(
                SESSION_IDS["lead"].encode("utf-8")
            ).hexdigest()
            with self.assertRaisesRegex(ValidationError, "cannot bind a lead session"):
                validate_agent_team_plan_approval(tampered)
            self.assertNotIn(feedback, json.dumps(public_summary))
            self.assertNotIn(feedback, json.dumps(collect_run_detail(root / "runs", "operator-plan-review")))
            state = load_agent_team_state(
                runner.run.artifacts_dir
                / "team-planned-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["authorized_tokens"], 1100)
            metrics = runner.run.read_state()["steps"]["team-planned-implementation"]
            self.assertEqual(metrics["agent_team_plan_approval_calls"], 1)
            self.assertEqual(metrics["agent_team_operator_plan_reviews"], 1)
            self.assertEqual(metrics["agent_invocations"], 2)

    def test_operator_plan_rejection_revises_same_session_and_timeout_never_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            runner = FakePlanApprovalRunner(
                workflow=operator_plan_approval_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                run_id="operator-plan-revision",
            )
            errors = []

            def execute():
                try:
                    runner.execute()
                except BaseException as exc:  # noqa: BLE001 - preserve worker-thread failure.
                    errors.append(exc)

            thread = threading.Thread(target=execute)
            thread.start()

            def wait_for_revision(revision):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        value = list_team_plans(
                            runner.run.run_dir,
                            "team-planned-implementation",
                        )
                    except (FileNotFoundError, ValidationError):
                        time.sleep(0.01)
                        continue
                    if (
                        value["plans"]
                        and value["plans"][0]["status"] == "plan-ready"
                        and value["plans"][0]["revision"] == revision
                    ):
                        return value["plans"][0]
                    time.sleep(0.01)
                self.fail("operator-reviewed plan revision did not become ready")

            wait_for_revision(1)
            rejection = "Add an exact deterministic content check before resubmitting."
            rejected = review_team_plan(
                runner.run.run_dir,
                "team-planned-implementation",
                "worker-edit",
                "reject",
                rejection,
            )
            self.assertEqual(rejected["status"], "revision-required")
            wait_for_revision(2)
            self.assertEqual([call["kind"] for call in runner.plan_calls], ["plan", "plan"])
            self.assertIn("The operator rejected", runner.plan_calls[1]["prompt"])
            self.assertIn(rejection, runner.plan_calls[1]["prompt"])
            self.assertIn("resume", runner.plan_calls[1]["argv"])
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "review-team-plan",
                            str(runner.run.run_dir),
                            "team-planned-implementation",
                            "worker-edit",
                            "--decision",
                            "approve",
                            "--feedback",
                            "The revised plan now has the required exact check.",
                        ]
                    ),
                    0,
                )
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            approval = load_agent_team_plan_approval(
                runner.run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            self.assertEqual([item["decision"] for item in approval["attempts"]], ["reject", "approve"])
            self.assertEqual(agent_team_plan_approval_summary(approval)["provider_call_count"], 2)
            state = load_agent_team_state(
                runner.run.artifacts_dir
                / "team-planned-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["authorized_tokens"], 1200)

            timeout_workspace = root / "timeout-workspace"
            timeout_workspace.mkdir()
            (timeout_workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            timeout_runner = FakePlanApprovalRunner(
                workflow=operator_plan_approval_workflow(reply_timeout_seconds=1),
                workspace=timeout_workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                run_id="operator-plan-timeout",
            )
            with self.assertRaisesRegex(StepExecutionError, "operator plan review timed out"):
                timeout_runner.execute()
            self.assertEqual([call["kind"] for call in timeout_runner.plan_calls], ["plan"])
            reviewed = review_team_plan(
                timeout_runner.run.run_dir,
                "team-planned-implementation",
                "worker-edit",
                "approve",
                "Approve the retained bounded plan after the timeout.",
            )
            self.assertEqual(reviewed["status"], "approved")
            retry_step(timeout_runner.run.run_dir, "team-planned-implementation")
            resumed = FakePlanApprovalRunner(
                workflow=timeout_runner.workflow,
                workspace=timeout_workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                resume_dir=timeout_runner.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.plan_calls, [])
            self.assertEqual(len(resumed.calls), 1)
            self.assertIn("resume", resumed.calls[0]["argv"])
            self.assertEqual(
                resumed.calls[0]["argv"][resumed.calls[0]["argv"].index("resume") + 1],
                SESSION_IDS["worker"],
            )

    def test_plan_rejection_revises_in_same_sessions_before_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = plan_approval_workflow()
            worker_static = "WORKER_PLAN_PROFILE_SENTINEL " + ("w" * 2048)
            lead_static = "LEAD_REVIEW_PROFILE_SENTINEL " + ("l" * 2048)
            for profile in workflow["agent_profiles"]:
                if profile["name"] == "worker-profile":
                    profile["instructions"] = worker_static
                elif profile["name"] == "lead-profile":
                    profile["instructions"] = lead_static
            runner = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                reject_reviews=1,
            )
            run = runner.execute()
            self.assertEqual(
                [call["kind"] for call in runner.plan_calls],
                ["plan", "review", "plan", "review"],
            )
            self.assertIn("BEGIN_CONDUCTOR_PLAN_FEEDBACK", runner.plan_calls[2]["prompt"])
            self.assertIn("resume", runner.plan_calls[2]["argv"])
            self.assertIn("resume", runner.plan_calls[3]["argv"])
            self.assertIn("resume", runner.calls[0]["argv"])
            self.assertIn(worker_static, runner.plan_calls[0]["prompt"])
            self.assertIn(lead_static, runner.plan_calls[1]["prompt"])
            self.assertNotIn(worker_static, runner.plan_calls[2]["prompt"])
            self.assertNotIn(lead_static, runner.plan_calls[3]["prompt"])
            self.assertNotIn(worker_static, runner.calls[0]["prompt"])
            for call in runner.plan_calls[2:] + runner.calls:
                self.assertIn(
                    "BEGIN_CONDUCTOR_AGENT_PROFILE_CONTINUATION",
                    call["prompt"],
                )
            approval = load_agent_team_plan_approval(
                run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            summary = agent_team_plan_approval_summary(approval)
            self.assertEqual(summary["status"], "approved")
            self.assertEqual(summary["revision"], 2)
            self.assertEqual(summary["rejection_count"], 1)
            self.assertEqual(
                [attempt["decision"] for attempt in approval["attempts"]],
                ["reject", "approve"],
            )
            self.assertEqual(
                run.read_state()["steps"]["team-planned-implementation"]["total_tokens"],
                50,
            )

    def test_plan_workspace_mutation_is_discarded_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            runner = FakePlanApprovalRunner(
                workflow=plan_approval_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                mutate_plan_workspace=True,
            )
            with self.assertRaisesRegex(ValidationError, "read-only plan approval mutated"):
                runner.execute()
            self.assertEqual(
                (workspace / "shared.txt").read_text(encoding="utf-8"),
                "base\n",
            )
            self.assertEqual(runner.calls, [])
            approval = load_agent_team_plan_approval(
                runner.run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            self.assertEqual(approval["status"], "failed")
            self.assertEqual(approval["error_class"], "ValidationError")
            self.assertFalse((runner.run.run_dir / "team-plan-workspaces").exists())

    def test_uncertain_plan_call_never_replays_without_explicit_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = plan_approval_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_kind="plan",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            approval_path = (
                crashed.run.artifacts_dir
                / "team-planned-implementation-internal"
                / "plan-approvals"
                / "generation-000000000"
                / "worker-edit"
                / "approval.json"
            )
            self.assertEqual(load_agent_team_plan_approval(approval_path)["status"], "planning")
            self.assertFalse((crashed.run.run_dir / "team-plan-workspaces").exists())
            resumed = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            with self.assertRaisesRegex(ValidationError, "run retry-step"):
                resumed.execute()
            self.assertEqual(resumed.plan_calls, [])
            self.assertEqual(resumed.calls, [])

            reset_step(crashed.run.run_dir, "team-planned-implementation", reason="fresh plan retry")
            retried = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            retried.execute()
            self.assertEqual([call["kind"] for call in retried.plan_calls], ["plan", "review"])
            self.assertEqual(len(retried.calls), 1)
            self.assertEqual(
                (workspace / "shared.txt").read_text(encoding="utf-8"),
                "worker\n",
            )

    def test_teams_without_plan_approval_touch_no_approval_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with patch(
                "conductor_extras.runtime.runner.agent_team_plan_approval_path",
                side_effect=AssertionError("disabled teams must not inspect approval state"),
            ):
                runner = FakeTeamRunner(
                    workflow=continuation_workflow(),
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                run = runner.execute()
            self.assertFalse(
                (run.artifacts_dir / "team-debug-internal" / "plan-approvals").exists()
            )

    def test_active_message_response_uses_the_ordinary_isolated_write_merge_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            workflow["steps"][0]["active_messaging"] = {
                "max_tasks": 1,
                "max_depth": 1,
                "max_tokens": 1000,
                "allow_broadcast": False,
            }
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                message_overrides={
                    "lead-edit": [
                        {"to": "worker", "body": "Review and extend the accepted isolated edit."}
                    ]
                },
            )
            run = runner.execute()
            self.assertEqual(
                [call["task"] for call in runner.calls],
                ["lead-edit", "team-message-task-0001-02"],
            )
            self.assertTrue(all(call["cwd"] != workspace for call in runner.calls))
            self.assertEqual(
                runner.calls[1]["source_before"],
                "lead\n",
            )
            self.assertEqual(
                (workspace / "shared.txt").read_text(encoding="utf-8"),
                "lead\nworker\n",
            )
            state = load_agent_team_state(
                run.artifacts_dir / "team-implementation-internal" / "team-state.json",
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
            )
            self.assertEqual(state["message_tasks_added"], 1)
            ledger = load_agent_team_merge_ledger(
                run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
                generation=0,
                state=state,
            )
            self.assertEqual(agent_team_merge_summary(ledger)["merge_count"], 2)

    def test_write_team_isolates_parallel_edits_and_serializes_only_the_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True, allow_writes=True)
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                run_id="isolated-write",
            )
            run = runner.execute()

            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\nworker\n")
            self.assertEqual(len(runner.calls), 3)
            first_round = [call for call in runner.calls if call["attempt"] == 1]
            self.assertEqual({call["task"] for call in first_round}, {"lead-edit", "worker-edit"})
            worker_calls = [call for call in runner.calls if call["task"] == "worker-edit"]
            self.assertEqual([call["attempt"] for call in worker_calls], [1, 2])
            self.assertTrue(all(call["cwd"] != workspace for call in runner.calls))
            self.assertTrue(all("isolated-write" in call["prompt"] for call in runner.calls))

            state_path = run.artifacts_dir / "team-implementation-internal" / "team-state.json"
            state = load_agent_team_state(
                state_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["round"], 2)
            self.assertEqual([turn["task_id"] for turn in state["turns"]], ["lead-edit", "worker-edit"])
            ledger_path = run.artifacts_dir / "team-implementation-internal" / "team-merge-ledger.json"
            ledger = load_agent_team_merge_ledger(
                ledger_path,
                step=runner.workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(runner.workflow),
                generation=0,
                state=state,
            )
            summary = agent_team_merge_summary(ledger)
            self.assertEqual(summary["merge_count"], 2)
            self.assertEqual(summary["conflict_count"], 1)
            self.assertEqual(summary["applied_file_count"], 2)
            self.assertEqual([event["kind"] for event in ledger["events"]], ["merge", "conflict", "merge"])
            self.assertEqual(ledger["events"][1]["conflicting_files"], ["shared.txt"])
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(["validate-agent-team-merge-ledger", str(ledger_path)]),
                    0,
                )
            inspected_merge = StringIO()
            with redirect_stdout(inspected_merge):
                self.assertEqual(
                    cli_main(["inspect-agent-team-merge-ledger", str(ledger_path), "--json"]),
                    0,
                )
            self.assertEqual(json.loads(inspected_merge.getvalue())["conflict_count"], 1)
            merge_assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "inspect-agent-team-merge-ledger",
                    str(ledger_path),
                ]
            )
            self.assertFalse(merge_assessment.writes)
            self.assertFalse(merge_assessment.network)
            self.assertFalse((run.run_dir / "team-workspaces" / "team-implementation" / "generation-000000000" / "lead").exists())
            metrics = run.read_state()["steps"]["team-implementation"]
            self.assertEqual(metrics["agent_team_workspace_mode"], "isolated-write")
            self.assertEqual(metrics["agent_team_merge_conflicts"], 1)
            self.assertEqual(metrics["agent_team_merges"], 2)
            self.assertEqual(metrics["agent_invocations"], 3)
            self.assertEqual(metrics["total_tokens"], 30)
            report = (run.artifacts_dir / "team-implementation.md").read_text(encoding="utf-8")
            self.assertIn("Automatically serialized conflicts: 1", report)
            detail = collect_run_detail(root / "runs", "isolated-write")
            self.assertEqual(detail["agent_teams"][0]["merge"]["conflict_count"], 1)
            live_html = render_live_run_detail(detail)
            self.assertIn("isolated write", live_html)
            self.assertIn("serialized conflicts", live_html)

            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])

            (workspace / "shared.txt").write_text("outside drift\n", encoding="utf-8")
            drifted = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=run.run_dir,
            )
            with self.assertRaisesRegex(ValidationError, "outside its deterministic merge ledger"):
                drifted.execute()
            self.assertEqual(drifted.calls, [])

    def test_write_team_deduplicates_identical_parallel_edits_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            runner = FakeWriteTeamRunner(
                workflow=write_team_workflow(),
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True, allow_writes=True),
                identical=True,
            )
            run = runner.execute()
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            self.assertEqual(len(runner.calls), 2)
            state = load_agent_team_state(
                run.artifacts_dir / "team-implementation-internal" / "team-state.json"
            )
            ledger = load_agent_team_merge_ledger(
                run.artifacts_dir / "team-implementation-internal" / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual([event["status"] for event in ledger["events"]], ["applied", "deduplicated"])
            summary = agent_team_merge_summary(ledger)
            self.assertEqual(summary["conflict_count"], 0)
            self.assertEqual(summary["deduplicated_file_count"], 1)

    def test_write_team_retry_preserves_accepted_merge_and_only_reruns_failed_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True, allow_writes=True)
            first = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                fail_once_tasks={"worker-edit"},
            )
            with self.assertRaisesRegex(StepExecutionError, "worker-edit"):
                first.execute()
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            state_path = first.run.artifacts_dir / "team-implementation-internal" / "team-state.json"
            failed_state = load_agent_team_state(state_path)
            self.assertEqual(
                {task["id"]: task["status"] for task in failed_state["tasks"]},
                {"lead-edit": "completed", "worker-edit": "pending"},
            )
            failed_ledger = load_agent_team_merge_ledger(
                first.run.artifacts_dir / "team-implementation-internal" / "team-merge-ledger.json",
                state=failed_state,
            )
            self.assertEqual(agent_team_merge_summary(failed_ledger)["merge_count"], 1)

            retry_step(first.run.run_dir, "team-implementation", reason="retry isolated teammate")
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=first.run.run_dir,
            )
            resumed.execute()
            self.assertEqual([call["task"] for call in resumed.calls], ["worker-edit"])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\nworker\n")
            completed = load_agent_team_state(state_path)
            ledger = load_agent_team_merge_ledger(
                first.run.artifacts_dir / "team-implementation-internal" / "team-merge-ledger.json",
                state=completed,
            )
            self.assertEqual(agent_team_merge_summary(ledger)["merge_count"], 2)

    def test_write_team_recovers_accepted_turn_intent_without_provider_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="intent-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "base\n")
            intent_path = (
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-intent.json"
            )
            transaction_path = intent_path.with_name("team-merge-pending.json")
            intent = load_agent_team_merge_intent(
                intent_path,
                step=workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(workflow),
                generation=0,
            )
            self.assertEqual(intent["schema"], AGENT_TEAM_MERGE_INTENT_SCHEMA)
            self.assertEqual(intent["member_id"], "lead")
            self.assertEqual(intent["source_expected_sha256"], intent["ledger_before"]["source_current_sha256"])
            self.assertFalse(transaction_path.exists())
            summary = agent_team_merge_intent_summary(intent)
            self.assertEqual(summary["status"], "accepted-turn-recovery-pending")
            self.assertFalse(summary["provider_replay_required"])

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(["validate-agent-team-merge-intent", str(intent_path)]),
                    0,
                )
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-team-merge-intent",
                            str(intent_path),
                            "--json",
                        ]
                    ),
                    0,
                )
            public_intent = inspected.getvalue()
            self.assertIn(intent["intent_sha256"], public_intent)
            self.assertNotIn(SESSION_IDS["lead"], public_intent)
            self.assertNotIn(intent["turn_output"], public_intent)
            assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "inspect-agent-team-merge-intent",
                    str(intent_path),
                ]
            )
            self.assertFalse(assessment.writes)
            self.assertFalse(assessment.network)

            pending_detail = collect_run_detail(root / "runs", crashed.run.run_dir.name)
            pending_team = pending_detail["agent_teams"][0]
            self.assertEqual(pending_team["status"], "recovery-pending")
            self.assertEqual(
                pending_team["recovery_pending"]["intent_sha256"],
                intent["intent_sha256"],
            )
            pending_html = render_live_run_detail(pending_detail)
            self.assertIn("Accepted turn recovery pending", pending_html)
            self.assertIn("provider replay not required", pending_html)
            self.assertNotIn(SESSION_IDS["lead"], pending_html)

            retry_step(crashed.run.run_dir, "team-implementation", reason="recover accepted turn")
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            self.assertFalse(intent_path.exists())
            self.assertFalse(transaction_path.exists())
            state = load_agent_team_state(
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(state["turns"]), 1)
            self.assertEqual(state["turns"][0]["total_tokens"], 10)
            recovery_dir = intent_path.parent / "merge-recoveries"
            receipts = list(recovery_dir.glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertFalse(load_agent_team_merge_recovery(receipts[0])["provider_replayed"])

    def test_read_team_recovers_provider_completed_turn_without_replay_or_duplicate_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                turn_completion_crash=True,
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            completion_paths = list_agent_team_turn_completion_paths(
                crashed.run,
                workflow["steps"][0],
            )
            self.assertEqual(len(completion_paths), 1)
            completion = load_agent_team_turn_completion(
                completion_paths[0],
                step=workflow["steps"][0],
                workflow_fingerprint=workflow_fingerprint(workflow),
            )
            self.assertEqual(completion["schema"], AGENT_TEAM_TURN_COMPLETION_SCHEMA)
            self.assertEqual(completion["workspace_mode"], "read-only")
            self.assertEqual(completion["telemetry"]["total_tokens"], 10)
            summary = agent_team_turn_completion_summary(completion)
            self.assertEqual(summary["status"], "provider-completed-recovery-pending")
            self.assertFalse(summary["provider_replay_required"])

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "validate-agent-team-turn-completion",
                            str(completion_paths[0]),
                        ]
                    ),
                    0,
                )
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-team-turn-completion",
                            str(completion_paths[0]),
                            "--json",
                        ]
                    ),
                    0,
                )
            public_completion = inspected.getvalue()
            self.assertIn(completion["completion_sha256"], public_completion)
            self.assertNotIn(completion["session_id"], public_completion)
            self.assertNotIn(completion["output"], public_completion)
            assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "inspect-agent-team-turn-completion",
                    str(completion_paths[0]),
                ]
            )
            self.assertFalse(assessment.writes)
            self.assertFalse(assessment.network)

            detail = collect_run_detail(root / "runs", crashed.run.run_dir.name)
            pending_team = detail["agent_teams"][0]
            self.assertEqual(pending_team["status"], "recovery-pending")
            self.assertEqual(pending_team["turn_completion_pending_count"], 1)
            self.assertEqual(
                pending_team["turn_completion_pending"][0]["completion_sha256"],
                completion["completion_sha256"],
            )
            live_html = render_live_run_detail(detail)
            self.assertIn("Provider-completed turn recovery pending", live_html)
            self.assertNotIn(completion["session_id"], live_html)

            retry_step(crashed.run.run_dir, "team-debug", reason="recover completed turn")
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual(
                list_agent_team_turn_completion_paths(
                    resumed.run,
                    workflow["steps"][0],
                ),
                [],
            )
            state = load_agent_team_state(
                resumed.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(state["turns"]), 1)
            run_state = resumed.run.read_state()
            self.assertEqual(
                run_state["telemetry_receipts"].count(completion["completion_sha256"]),
                1,
            )
            provider_usage_events = [
                event
                for event in run_state["usage_events"]
                if event.get("source") == "provider-runtime:codex"
            ]
            self.assertEqual(len(provider_usage_events), 1)
            self.assertEqual(run_state["steps"]["team-debug"]["total_tokens"], 10)

    def test_read_team_recovers_local_terminal_event_before_completion_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                turn_terminal_crash_phase="written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            terminal_paths = list_agent_team_turn_terminal_paths(
                crashed.run,
                workflow["steps"][0],
            )
            self.assertEqual(len(terminal_paths), 1)
            self.assertEqual(
                list_agent_team_turn_completion_paths(
                    crashed.run,
                    workflow["steps"][0],
                ),
                [],
            )
            terminal = load_agent_team_turn_terminal(terminal_paths[0])
            self.assertEqual(terminal["schema"], AGENT_TEAM_TURN_TERMINAL_SCHEMA)
            self.assertEqual(terminal["telemetry"]["total_tokens"], 10)
            summary = agent_team_turn_terminal_summary(terminal)
            self.assertEqual(summary["status"], "provider-terminal-recovery-pending")
            self.assertFalse(summary["provider_replay_required"])
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "validate-agent-team-turn-terminal",
                            str(terminal_paths[0]),
                        ]
                    ),
                    0,
                )
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-team-turn-terminal",
                            str(terminal_paths[0]),
                            "--json",
                        ]
                    ),
                    0,
                )
            public_terminal = inspected.getvalue()
            self.assertIn(terminal["terminal_sha256"], public_terminal)
            self.assertNotIn(terminal["session_id"], public_terminal)
            self.assertNotIn(terminal["output"], public_terminal)
            assessment = assess_command(
                [
                    "python3",
                    "-m",
                    "conductor_extras",
                    "inspect-agent-team-turn-terminal",
                    str(terminal_paths[0]),
                ]
            )
            self.assertFalse(assessment.writes)
            self.assertFalse(assessment.network)
            detail = collect_run_detail(root / "runs", crashed.run.run_dir.name)
            pending_team = detail["agent_teams"][0]
            self.assertEqual(pending_team["status"], "recovery-pending")
            self.assertEqual(pending_team["turn_terminal_pending_count"], 1)
            self.assertEqual(
                pending_team["turn_terminal_pending"][0]["terminal_sha256"],
                terminal["terminal_sha256"],
            )
            live_html = render_live_run_detail(detail)
            self.assertIn("Local terminal turn recovery pending", live_html)
            self.assertNotIn(terminal["session_id"], live_html)
            self.assertEqual(
                get_schema("agent-team-turn-terminal")["properties"]["schema"]["const"],
                AGENT_TEAM_TURN_TERMINAL_SCHEMA,
            )

            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover local terminal event",
            )
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual(
                list_agent_team_turn_terminal_paths(
                    resumed.run,
                    workflow["steps"][0],
                ),
                [],
            )
            self.assertEqual(
                list_agent_team_turn_completion_paths(
                    resumed.run,
                    workflow["steps"][0],
                ),
                [],
            )
            state = load_agent_team_state(
                resumed.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(state["turns"]), 1)
            run_state = resumed.run.read_state()
            provider_usage_events = [
                event
                for event in run_state["usage_events"]
                if event.get("source") == "provider-runtime:codex"
            ]
            self.assertEqual(len(provider_usage_events), 1)
            self.assertEqual(run_state["steps"]["team-debug"]["total_tokens"], 10)

    def test_write_team_recovers_provider_completed_turn_before_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="turn-completion-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "base\n")
            completion_path = list_agent_team_turn_completion_paths(
                crashed.run,
                workflow["steps"][0],
            )[0]
            completion = load_agent_team_turn_completion(completion_path)
            self.assertEqual(completion["workspace_mode"], "isolated-write")
            self.assertIsNotNone(completion["workspace_base_manifest"])
            self.assertFalse(
                (completion_path.parent.parent / "team-merge-intent.json").exists()
            )

            retry_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover provider-completed write turn",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            self.assertFalse(completion_path.exists())
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            recoveries = list(
                (completion_path.parent.parent / "merge-recoveries").glob("*.json")
            )
            self.assertEqual(len(recoveries), 1)
            self.assertFalse(load_agent_team_merge_recovery(recoveries[0])["provider_replayed"])

    def test_write_team_recovers_local_terminal_event_before_completion_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="turn-terminal-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(), "base\n")
            terminal_path = list_agent_team_turn_terminal_paths(
                crashed.run,
                workflow["steps"][0],
            )[0]
            terminal = load_agent_team_turn_terminal(terminal_path)
            self.assertEqual(terminal["workspace_mode"], "isolated-write")
            self.assertIsNotNone(terminal["workspace_base_sha256"])
            self.assertIsNotNone(terminal["workspace_result_sha256"])
            self.assertNotEqual(
                terminal["output_raw_sha256"],
                "0" * 64,
            )
            self.assertEqual(
                list_agent_team_turn_completion_paths(
                    crashed.run,
                    workflow["steps"][0],
                ),
                [],
            )

            retry_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover terminal write turn",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual((workspace / "shared.txt").read_text(), "lead\n")
            self.assertFalse(terminal_path.exists())
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(state["turns"]), 1)

    def test_parallel_terminal_recovery_preserves_siblings_and_rebases_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = write_team_workflow()
            policy = RuntimePolicy(
                allow_agent=True,
                allow_parallel=True,
                allow_writes=True,
            )
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="turn-terminal-written",
                crash_member="worker",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 2)
            self.assertGreaterEqual(
                len(
                    list_agent_team_turn_terminal_paths(
                        crashed.run,
                        workflow["steps"][0],
                    )
                ),
                1,
            )

            retry_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover parallel terminal turns",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual([call["task"] for call in resumed.calls], ["worker-edit"])
            self.assertEqual((workspace / "shared.txt").read_text(), "lead\nworker\n")
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            ledger = load_agent_team_merge_ledger(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual(
                [event["status"] for event in ledger["events"]],
                ["applied", "conflict", "applied"],
            )

    def test_quality_retry_recovers_local_terminal_event_without_third_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                }
            ]
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                quality_gate_failures=1,
                turn_terminal_crash_phase="written",
                turn_terminal_crash_retry_index=1,
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 2)
            terminal = load_agent_team_turn_terminal(
                list_agent_team_turn_terminal_paths(
                    crashed.run,
                    workflow["steps"][0],
                )[0]
            )
            self.assertEqual(terminal["quality_retry_index"], 1)
            pending = list_agent_team_quality_retry_pending(
                crashed.run,
                workflow["steps"][0],
            )
            self.assertEqual(load_agent_team_quality_retry(pending[0])[0]["status"], "active")

            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover terminal quality retry",
            )
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            history = list_agent_team_quality_retry_history(
                resumed.run,
                workflow["steps"][0],
            )
            self.assertEqual([value["status"] for value in history], ["accepted"])
            state = load_agent_team_state(
                resumed.run.artifacts_dir / "team-debug-internal" / "team-state.json"
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["authorized_tokens"], 2000)

    def test_terminal_promotion_crash_recovers_once_without_duplicate_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                turn_terminal_crash_phase="promoted-to-completion",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            self.assertEqual(
                len(list_agent_team_turn_terminal_paths(crashed.run, workflow["steps"][0])),
                1,
            )
            self.assertEqual(
                len(list_agent_team_turn_completion_paths(crashed.run, workflow["steps"][0])),
                1,
            )

            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover terminal promotion",
            )
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            run_state = resumed.run.read_state()
            provider_usage_events = [
                event
                for event in run_state["usage_events"]
                if event.get("source") == "provider-runtime:codex"
            ]
            self.assertEqual(len(provider_usage_events), 1)
            self.assertEqual(run_state["steps"]["team-debug"]["total_tokens"], 10)

    def test_turn_completion_recovery_deduplicates_already_recorded_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                turn_completion_crash_phase="telemetry-recorded",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            completion = load_agent_team_turn_completion(
                list_agent_team_turn_completion_paths(
                    crashed.run,
                    workflow["steps"][0],
                )[0]
            )
            before = crashed.run.read_state()
            self.assertEqual(
                before["telemetry_receipts"].count(completion["completion_sha256"]),
                1,
            )
            self.assertEqual(
                len(
                    [
                        event
                        for event in before["usage_events"]
                        if event.get("source") == "provider-runtime:codex"
                    ]
                ),
                1,
            )
            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover after telemetry checkpoint",
            )
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            after = resumed.run.read_state()
            self.assertEqual(
                after["telemetry_receipts"].count(completion["completion_sha256"]),
                1,
            )
            self.assertEqual(
                len(
                    [
                        event
                        for event in after["usage_events"]
                        if event.get("source") == "provider-runtime:codex"
                    ]
                ),
                1,
            )
            self.assertEqual(after["steps"]["team-debug"]["total_tokens"], 10)

    def test_parallel_write_completion_recovery_preserves_both_results_and_rebases_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = write_team_workflow()
            policy = RuntimePolicy(
                allow_agent=True,
                allow_parallel=True,
                allow_writes=True,
            )
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="turn-completion-written",
                crash_member="worker",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 2)
            self.assertGreaterEqual(
                len(
                    list_agent_team_turn_completion_paths(
                        crashed.run,
                        workflow["steps"][0],
                    )
                ),
                1,
            )

            retry_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover parallel completed turns",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual([call["task"] for call in resumed.calls], ["worker-edit"])
            self.assertEqual(
                (workspace / "shared.txt").read_text(encoding="utf-8"),
                "lead\nworker\n",
            )
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            ledger = load_agent_team_merge_ledger(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual(
                [event["status"] for event in ledger["events"]],
                ["applied", "conflict", "applied"],
            )

    def test_reset_recovers_provider_completed_turn_before_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="turn-completion-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            reset_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover completed old generation before reset",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(len(resumed.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["generation"], 1)
            self.assertEqual(state["status"], "completed")

    def test_reset_recovers_terminal_turn_before_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="turn-terminal-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            reset_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover terminal old generation before reset",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(len(resumed.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(), "lead\n")
            state = load_agent_team_state(
                resumed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["generation"], 1)
            self.assertEqual(state["status"], "completed")

    def test_terminal_recovery_rejects_artifact_output_workspace_and_source_tamper(self):
        for case in ["artifact", "output", "workspace", "source"]:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
                workflow = single_write_team_workflow()
                policy = RuntimePolicy(allow_agent=True, allow_writes=True)
                crashed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    crash_phase="turn-terminal-written",
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                terminal_path = list_agent_team_turn_terminal_paths(
                    crashed.run,
                    workflow["steps"][0],
                )[0]
                terminal = load_agent_team_turn_terminal(terminal_path)
                if case == "artifact":
                    value = json.loads(terminal_path.read_text(encoding="utf-8"))
                    value["telemetry"]["cost_usd"] = 1.0
                    terminal_path.write_text(json.dumps(value), encoding="utf-8")
                    expected = "hash is invalid"
                elif case == "output":
                    (crashed.run.artifacts_dir / terminal["output"]).write_text(
                        json.dumps(
                            {
                                "schema": AGENT_TEAM_TURN_SCHEMA,
                                "task_id": terminal["task_id"],
                                "status": "completed",
                                "summary": "tampered terminal result",
                                "messages": [],
                                "claim_task_ids": [],
                                "task_proposals": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    expected = "output changed"
                elif case == "workspace":
                    retained = crashed.run.run_dir / terminal["workspace_relative"]
                    (retained / "shared.txt").write_text(
                        "tampered workspace\n",
                        encoding="utf-8",
                    )
                    expected = "workspace changed"
                else:
                    (workspace / "shared.txt").write_text(
                        "outside source drift\n",
                        encoding="utf-8",
                    )
                    expected = "source"
                retry_step(
                    crashed.run.run_dir,
                    "team-implementation",
                    reason="reject terminal tamper",
                )
                resumed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=crashed.run.run_dir,
                )
                with self.assertRaisesRegex(ValidationError, expected):
                    resumed.execute()
                self.assertEqual(resumed.calls, [])

    def test_turn_completion_recovery_rejects_artifact_output_workspace_and_source_tamper(self):
        cases = ["artifact", "output", "workspace", "source"]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
                workflow = single_write_team_workflow()
                policy = RuntimePolicy(allow_agent=True, allow_writes=True)
                crashed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    crash_phase="turn-completion-written",
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                completion_path = list_agent_team_turn_completion_paths(
                    crashed.run,
                    workflow["steps"][0],
                )[0]
                completion = load_agent_team_turn_completion(completion_path)
                if case == "artifact":
                    value = json.loads(completion_path.read_text(encoding="utf-8"))
                    value["telemetry"]["cost_usd"] = 1.0
                    completion_path.write_text(json.dumps(value), encoding="utf-8")
                    expected = "hash is invalid"
                elif case == "output":
                    (crashed.run.artifacts_dir / completion["output"]).write_text(
                        "tampered output\n",
                        encoding="utf-8",
                    )
                    expected = "output hash changed"
                elif case == "workspace":
                    retained = crashed.run.run_dir / completion["workspace_relative"]
                    (retained / "shared.txt").write_text(
                        "tampered workspace\n",
                        encoding="utf-8",
                    )
                    expected = "workspace changed"
                else:
                    (workspace / "shared.txt").write_text(
                        "unrelated source drift\n",
                        encoding="utf-8",
                    )
                    expected = "source drift"
                retry_step(
                    crashed.run.run_dir,
                    "team-implementation",
                    reason="reject completed-turn tamper",
                )
                resumed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=crashed.run.run_dir,
                )
                with self.assertRaisesRegex(ValidationError, expected):
                    resumed.execute()
                self.assertEqual(resumed.calls, [])
                if case == "source":
                    self.assertTrue(
                        (completion_path.parent.parent / "team-merge-intent.json").exists()
                    )
                else:
                    self.assertTrue(completion_path.exists())

    def test_write_team_reset_recovers_intent_before_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="intent-written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            intent_path = (
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-intent.json"
            )
            reset_step(
                crashed.run.run_dir,
                "team-implementation",
                reason="recover old intent before reset",
            )
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(len(resumed.calls), 1)
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            self.assertFalse(intent_path.exists())
            state = load_agent_team_state(
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            self.assertEqual(state["generation"], 1)
            self.assertEqual(state["status"], "completed")
            receipts = list((intent_path.parent / "merge-recoveries").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertFalse(load_agent_team_merge_recovery(receipts[0])["provider_replayed"])

    def test_write_team_intent_conflict_recovery_handles_partial_checkpoints(self):
        for phase in ["intent-conflict-state-written", "intent-conflict-ledger-written"]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
                workflow = write_team_workflow()
                policy = RuntimePolicy(
                    allow_agent=True,
                    allow_parallel=True,
                    allow_writes=True,
                )
                first = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    crash_phase="intent-written",
                    crash_member="worker",
                )
                with self.assertRaises(SimulatedHardCrash):
                    first.execute()
                self.assertEqual(len(first.calls), 2)
                self.assertEqual(
                    (workspace / "shared.txt").read_text(encoding="utf-8"),
                    "lead\n",
                )
                intent_path = (
                    first.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-merge-intent.json"
                )
                self.assertEqual(load_agent_team_merge_intent(intent_path)["member_id"], "worker")

                retry_step(first.run.run_dir, "team-implementation", reason="begin intent recovery")
                partial = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=first.run.run_dir,
                    crash_phase=phase,
                    crash_member="worker",
                )
                with self.assertRaises(SimulatedHardCrash):
                    partial.execute()
                self.assertEqual(partial.calls, [])
                self.assertTrue(intent_path.exists())

                retry_step(first.run.run_dir, "team-implementation", reason="finish intent recovery")
                finished = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=first.run.run_dir,
                )
                finished.execute()
                self.assertEqual(
                    [(call["member"], call["task"]) for call in finished.calls],
                    [("worker", "worker-edit")],
                )
                self.assertEqual(
                    (workspace / "shared.txt").read_text(encoding="utf-8"),
                    "lead\nworker\n",
                )
                self.assertFalse(intent_path.exists())
                state = load_agent_team_state(
                    first.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-state.json"
                )
                ledger = load_agent_team_merge_ledger(
                    first.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-merge-ledger.json",
                    state=state,
                )
                self.assertEqual(
                    [event["status"] for event in ledger["events"]],
                    ["applied", "conflict", "applied"],
                )

    def test_write_team_intent_recovery_rejects_tamper_and_drift_without_calls(self):
        for mutation, expected in [
            ("intent", "intent hash is invalid"),
            ("workspace", "intent workspace changed"),
            ("source", "source drift before recovery"),
        ]:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
                workflow = single_write_team_workflow()
                policy = RuntimePolicy(allow_agent=True, allow_writes=True)
                crashed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    crash_phase="intent-written",
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                intent_path = (
                    crashed.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-merge-intent.json"
                )
                intent = load_agent_team_merge_intent(intent_path)
                if mutation == "intent":
                    intent["candidate_state"]["tasks"][0]["summary"] = "tampered"
                    intent_path.write_text(
                        json.dumps(intent, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                elif mutation == "workspace":
                    member_workspace = crashed.run.run_dir / intent["workspace_relative"]
                    (member_workspace / "shared.txt").write_text("tampered\n", encoding="utf-8")
                else:
                    (workspace / "outside.txt").write_text("drift\n", encoding="utf-8")
                retry_step(crashed.run.run_dir, "team-implementation", reason="reject intent drift")
                resumed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=crashed.run.run_dir,
                )
                with self.assertRaisesRegex(ValidationError, expected):
                    resumed.execute()
                self.assertEqual(resumed.calls, [])
                self.assertTrue(intent_path.exists())

    def test_write_team_recovers_every_durable_merge_checkpoint_without_provider_replay(self):
        for phase in ["prepared", "source-applied", "state-written", "ledger-written"]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
                workflow = single_write_team_workflow()
                policy = RuntimePolicy(allow_agent=True, allow_writes=True)
                crashed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    crash_phase=phase,
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                pending = (
                    crashed.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-merge-pending.json"
                )
                transaction = load_agent_team_merge_transaction(
                    pending,
                    step=crashed.workflow["steps"][0],
                    workflow_fingerprint=workflow_fingerprint(crashed.workflow),
                    generation=0,
                )
                self.assertEqual(transaction["member_id"], "lead")
                expected_after_crash = "base\n" if phase == "prepared" else "lead\n"
                self.assertEqual(
                    (workspace / "shared.txt").read_text(encoding="utf-8"),
                    expected_after_crash,
                )

                with redirect_stdout(StringIO()):
                    self.assertEqual(
                        cli_main(
                            [
                                "validate-agent-team-merge-transaction",
                                str(pending),
                            ]
                        ),
                        0,
                    )
                transaction_stdout = StringIO()
                with redirect_stdout(transaction_stdout):
                    self.assertEqual(
                        cli_main(
                            [
                                "inspect-agent-team-merge-transaction",
                                str(pending),
                                "--json",
                            ]
                        ),
                        0,
                    )
                transaction_summary = json.loads(transaction_stdout.getvalue())
                self.assertEqual(transaction_summary["status"], "recovery-pending")
                self.assertFalse(transaction_summary["provider_replay_required"])
                pending_detail = collect_run_detail(
                    root / "runs",
                    crashed.run.run_dir.name,
                )
                self.assertEqual(
                    pending_detail["agent_teams"][0]["status"],
                    "recovery-pending",
                )
                self.assertEqual(
                    pending_detail["agent_teams"][0]["recovery_pending"][
                        "transaction_sha256"
                    ],
                    transaction["transaction_sha256"],
                )
                pending_html = render_live_run_detail(pending_detail)
                self.assertIn("Merge recovery pending", pending_html)
                self.assertIn("provider replay not required", pending_html)
                transaction_assessment = assess_command(
                    [
                        "python3",
                        "-m",
                        "conductor_extras",
                        "inspect-agent-team-merge-transaction",
                        str(pending),
                    ]
                )
                self.assertFalse(transaction_assessment.writes)
                self.assertFalse(transaction_assessment.network)

                retry_step(crashed.run.run_dir, "team-implementation", reason="recover merge transaction")
                resumed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=crashed.run.run_dir,
                )
                resumed.execute()
                self.assertEqual(resumed.calls, [])
                self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
                self.assertFalse(pending.exists())
                state = load_agent_team_state(
                    crashed.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-state.json"
                )
                ledger = load_agent_team_merge_ledger(
                    crashed.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-merge-ledger.json",
                    state=state,
                )
                self.assertEqual(state["status"], "completed")
                self.assertEqual(agent_team_merge_summary(ledger)["merge_count"], 1)
                receipts = list(
                    (
                        crashed.run.artifacts_dir
                        / "team-implementation-internal"
                        / "merge-recoveries"
                    ).glob("*.json")
                )
                self.assertEqual(len(receipts), 1)
                receipt = load_agent_team_merge_recovery(receipts[0])
                self.assertFalse(receipt["provider_replayed"])
                with redirect_stdout(StringIO()):
                    self.assertEqual(
                        cli_main(
                            [
                                "validate-agent-team-merge-recovery",
                                str(receipts[0]),
                            ]
                        ),
                        0,
                    )
                recovery_stdout = StringIO()
                with redirect_stdout(recovery_stdout):
                    self.assertEqual(
                        cli_main(
                            [
                                "inspect-agent-team-merge-recovery",
                                str(receipts[0]),
                                "--json",
                            ]
                        ),
                        0,
                    )
                self.assertFalse(
                    json.loads(recovery_stdout.getvalue())["provider_replayed"]
                )
                recovered_detail = collect_run_detail(
                    root / "runs",
                    crashed.run.run_dir.name,
                )
                recovered_team = recovered_detail["agent_teams"][0]
                self.assertEqual(recovered_team["merge_recovery_count"], 1)
                self.assertIsNone(recovered_team["recovery_pending"])
                recovered_html = render_live_run_detail(recovered_detail)
                self.assertIn("1 exactly-once merge recoveries", recovered_html)
                self.assertIn("provider replayed: no", recovered_html)
                recovery_assessment = assess_command(
                    [
                        "python3",
                        "-m",
                        "conductor_extras",
                        "inspect-agent-team-merge-recovery",
                        str(receipts[0]),
                    ]
                )
                self.assertFalse(recovery_assessment.writes)
                self.assertFalse(recovery_assessment.network)
                step_metrics = crashed.run.read_state()["steps"]["team-implementation"]
                self.assertEqual(step_metrics["agent_team_merge_recoveries"], 1)
                report = (
                    crashed.run.artifacts_dir / "team-implementation.md"
                ).read_text(encoding="utf-8")
                self.assertIn("Exactly-once merge recoveries: 1", report)
                self.assertEqual(
                    step_metrics["total_tokens"],
                    10,
                )

    def test_write_team_recovers_a_partially_applied_multi_file_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="prepared",
                multi_file=True,
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            pending = (
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-pending.json"
            )
            transaction = load_agent_team_merge_transaction(pending)
            self.assertEqual(transaction["merge_plan"]["apply_files"], ["second.txt", "shared.txt"])
            (workspace / "shared.txt").write_text("lead\n", encoding="utf-8")

            retry_step(crashed.run.run_dir, "team-implementation", reason="recover partial merge")
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            self.assertEqual((workspace / "second.txt").read_text(encoding="utf-8"), "second-lead\n")
            self.assertFalse(pending.exists())

    def test_write_team_merge_recovery_rejects_unrelated_source_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="prepared",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            pending = (
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-pending.json"
            )
            (workspace / "outside.txt").write_text("unrelated\n", encoding="utf-8")
            retry_step(crashed.run.run_dir, "team-implementation", reason="reject drift")
            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            with self.assertRaisesRegex(ValidationError, "unrelated drift"):
                resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertTrue(pending.exists())

    def test_write_team_reset_recovers_pending_merge_before_starting_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = single_write_team_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_writes=True)
            crashed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_phase="prepared",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            reset_step(crashed.run.run_dir, "team-implementation", reason="fresh generation after recovery")

            resumed = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual([call["task"] for call in resumed.calls], ["lead-edit"])
            self.assertEqual((workspace / "shared.txt").read_text(encoding="utf-8"), "lead\n")
            state = load_agent_team_state(
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-state.json"
            )
            ledger = load_agent_team_merge_ledger(
                crashed.run.artifacts_dir
                / "team-implementation-internal"
                / "team-merge-ledger.json",
                state=state,
            )
            self.assertEqual(state["generation"], 1)
            self.assertEqual(ledger["generation"], 1)
            self.assertEqual(ledger["source_initial_sha256"], ledger["source_current_sha256"])
            self.assertEqual(ledger["events"][0]["status"], "no-change")
            self.assertEqual(
                len(
                    list(
                        (
                            crashed.run.artifacts_dir
                            / "team-implementation-internal"
                            / "merge-recoveries"
                        ).glob("*.json")
                    )
                ),
                1,
            )

    def test_write_team_merge_transaction_and_retained_workspace_tampering_fail_closed(self):
        for target in ["transaction", "workspace"]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
                workflow = single_write_team_workflow()
                policy = RuntimePolicy(allow_agent=True, allow_writes=True)
                crashed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    crash_phase="prepared",
                )
                with self.assertRaises(SimulatedHardCrash):
                    crashed.execute()
                pending = (
                    crashed.run.artifacts_dir
                    / "team-implementation-internal"
                    / "team-merge-pending.json"
                )
                transaction = json.loads(pending.read_text(encoding="utf-8"))
                if target == "transaction":
                    transaction["candidate_state"]["tasks"][0]["summary"] = "tampered"
                    pending.write_text(json.dumps(transaction), encoding="utf-8")
                    expected = "transaction hash|candidate state"
                else:
                    retained = crashed.run.run_dir / transaction["workspace_relative"] / "shared.txt"
                    retained.write_text("tampered\n", encoding="utf-8")
                    expected = "recovery workspace changed"
                retry_step(crashed.run.run_dir, "team-implementation", reason="reject tampering")
                resumed = FakeWriteTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=crashed.run.run_dir,
                )
                with self.assertRaisesRegex(ValidationError, expected):
                    resumed.execute()
                self.assertEqual(resumed.calls, [])
                self.assertTrue(pending.exists())

    def test_write_team_requires_write_policy_and_write_capable_member_profiles(self):
        workflow = write_team_workflow()
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            runner = FakeWriteTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            with self.assertRaisesRegex(PolicyError, "allow-writes"):
                runner.execute()
            self.assertEqual(runner.calls, [])

        narrowed = write_team_workflow()
        narrowed["agent_profiles"][1]["sandbox"] = "read-only"
        with self.assertRaisesRegex(ValidationError, "cannot widen read-only"):
            validate_workflow(narrowed)

    def test_claim_conflicts_are_deterministic_and_dependency_aware(self):
        workflow = coordinated_workflow()
        step = workflow["steps"][0]
        state = initial_agent_team_state(step, "a" * 64)
        state["tasks"][0]["assignee"] = None
        state["tasks"][1]["assignee"] = None
        state["members"][0]["claim_task_ids"] = ["security-audit"]
        state["members"][1]["claim_task_ids"] = ["security-audit"]
        assignments = assign_agent_team_tasks(step, state)
        self.assertEqual(assignments[0], {"member_id": "lead", "task_id": "security-audit"})
        self.assertNotIn("synthesize", [value["task_id"] for value in assignments])
        self.assertEqual(len({value["member_id"] for value in assignments}), len(assignments))

    def test_turn_and_state_validation_reject_tampering(self):
        workflow = continuation_workflow()
        step = workflow["steps"][0]
        state = initial_agent_team_state(step, "b" * 64)
        state["members"][0]["session_id"] = "not-a-uuid"
        with self.assertRaisesRegex(ValidationError, "session_id"):
            validate_agent_team_state(state)

        changed_binding = initial_agent_team_state(step, "c" * 64)
        changed_binding["tasks"][0]["assignee"] = "lead"
        with self.assertRaisesRegex(ValidationError, "task definitions changed"):
            validate_agent_team_state(changed_binding, step=step)

        turn = {
            "schema": AGENT_TEAM_TURN_SCHEMA,
            "task_id": "debug",
            "status": "continue",
            "summary": "Need another pass.",
            "messages": [{"to": "unknown", "body": "bad route"}],
            "claim_task_ids": [],
            "task_proposals": [],
        }
        with self.assertRaisesRegex(ValidationError, "recipient"):
            parse_agent_team_turn(json.dumps(turn), "debug", ["lead", "worker"], ["debug"])
        turn["messages"] = []
        turn["claim_task_ids"] = ["debug"]
        with self.assertRaisesRegex(ValidationError, "cannot claim"):
            parse_agent_team_turn(json.dumps(turn), "debug", ["lead", "worker"], ["debug"])
        duplicate = '{"schema":"conductor.agent_team_turn.v1","schema":"x"}'
        with self.assertRaisesRegex(ValidationError, "invalid JSON"):
            parse_agent_team_turn(duplicate, "debug", ["lead", "worker"], ["debug"])

    def test_retry_rejects_tampered_retained_turn_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            policy = RuntimePolicy(allow_agent=True, allow_parallel=True)
            first = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                fail_once_tasks={"correctness-audit"},
            )
            with self.assertRaises(StepExecutionError):
                first.execute()
            state_path = first.run.artifacts_dir / "team-review-internal" / "team-state.json"
            state = load_agent_team_state(state_path)
            retained_output = first.run.artifacts_dir / state["turns"][0]["output"]
            retained_output.write_text("{}\n", encoding="utf-8")
            retry_step(first.run.run_dir, "team-review", reason="attempt tampered resume")

            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=first.run.run_dir,
            )
            with self.assertRaisesRegex(ValidationError, "output hash changed"):
                resumed.execute()
            self.assertEqual(resumed.calls, [])

    def test_workflow_and_public_schemas_reject_unsafe_team_shapes(self):
        workflow = coordinated_workflow()
        validate_workflow(workflow)
        self.assertIn("team=3", workflow_summary(workflow))
        workflow_schema = get_schema("workflow")
        serialized = json.dumps(workflow_schema)
        self.assertIn('"agent_team"', serialized)
        self.assertEqual(
            get_schema("agent-team-state")["properties"]["schema"]["const"],
            AGENT_TEAM_STATE_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-turn")["properties"]["schema"]["const"],
            AGENT_TEAM_TURN_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-state-v5")["properties"]["schema"]["const"],
            AGENT_TEAM_STATE_SCHEMA_V5,
        )
        self.assertEqual(
            get_schema("agent-team-state-v4")["properties"]["schema"]["const"],
            AGENT_TEAM_STATE_SCHEMA_V4,
        )
        self.assertEqual(
            get_schema("agent-team-state-v3")["properties"]["schema"]["const"],
            AGENT_TEAM_STATE_SCHEMA_V3,
        )
        self.assertEqual(
            get_schema("agent-team-state-v2")["properties"]["schema"]["const"],
            AGENT_TEAM_STATE_SCHEMA_V2,
        )
        self.assertEqual(
            get_schema("agent-team-state-v1")["properties"]["schema"]["const"],
            AGENT_TEAM_STATE_SCHEMA_V1,
        )
        self.assertEqual(
            get_schema("agent-team-operator-inbox")["properties"]["schema"]["const"],
            AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-operator-inbox-v1")["properties"]["schema"]["const"],
            AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
        )
        self.assertEqual(
            get_schema("agent-team-operator-chat")["properties"]["schema"]["const"],
            AGENT_TEAM_OPERATOR_CHAT_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-turn-v1")["properties"]["schema"]["const"],
            AGENT_TEAM_TURN_SCHEMA_V1,
        )
        self.assertEqual(
            get_schema("agent-team-merge-ledger")["properties"]["schema"]["const"],
            AGENT_TEAM_MERGE_LEDGER_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-turn-completion")["properties"]["schema"]["const"],
            AGENT_TEAM_TURN_COMPLETION_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-merge-intent")["properties"]["schema"]["const"],
            AGENT_TEAM_MERGE_INTENT_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-merge-transaction")["properties"]["schema"]["const"],
            AGENT_TEAM_MERGE_TRANSACTION_SCHEMA,
        )
        self.assertEqual(
            get_schema("agent-team-merge-recovery")["properties"]["schema"]["const"],
            AGENT_TEAM_MERGE_RECOVERY_SCHEMA,
        )
        team_schema = next(
            item
            for item in workflow_schema["properties"]["steps"]["items"]["oneOf"]
            if item["properties"]["kind"]["const"] == "agent_team"
        )
        self.assertIn("max_dynamic_tasks", team_schema["properties"])
        self.assertIn("max_operator_tasks", team_schema["properties"])
        self.assertIn("active_messaging", team_schema["properties"])
        self.assertIn("operator_chat", team_schema["properties"])

        two_leads = coordinated_workflow()
        two_leads["steps"][0]["members"][1]["lead"] = True
        with self.assertRaisesRegex(ValidationError, "exactly one lead"):
            validate_workflow(two_leads)
        write_team = coordinated_workflow()
        write_team["steps"][0]["sandbox"] = "workspace-write"
        with self.assertRaisesRegex(ValidationError, "read-only"):
            validate_workflow(write_team)
        too_many_workers = coordinated_workflow()
        too_many_workers["steps"][0]["max_workers"] = 4
        with self.assertRaisesRegex(ValidationError, "member count"):
            validate_workflow(too_many_workers)
        insufficient_rounds = coordinated_workflow()
        insufficient_rounds["steps"][0]["max_rounds"] = 1
        with self.assertRaisesRegex(ValidationError, "dependency depth"):
            validate_workflow(insufficient_rounds)
        forward_dependency = coordinated_workflow()
        forward_dependency["steps"][0]["tasks"][0]["depends_on"] = ["synthesize"]
        with self.assertRaisesRegex(ValidationError, "appear earlier"):
            validate_workflow(forward_dependency)
        unknown_profile = coordinated_workflow()
        unknown_profile["steps"][0]["members"][0]["agent_profile"] = "missing"
        with self.assertRaisesRegex(ValidationError, "unknown agent profile"):
            validate_workflow(unknown_profile)
        invalid_operator_limit = coordinated_workflow()
        invalid_operator_limit["steps"][0]["max_operator_tasks"] = 33
        with self.assertRaisesRegex(ValidationError, "max_operator_tasks"):
            validate_workflow(invalid_operator_limit)


if __name__ == "__main__":
    unittest.main()
