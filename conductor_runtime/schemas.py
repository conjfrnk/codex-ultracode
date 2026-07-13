import copy
import json
from typing import Dict, List

from .agent_profiles import (
    AGENT_PROFILE_CONTEXT_BINDING_SCHEMA,
    AGENT_PROFILE_MEMORY_BINDING_SCHEMA,
    AGENT_PROFILE_SCHEMA,
    AGENT_PROFILE_SKILL_BINDING_SCHEMA,
    MAX_AGENT_PROFILES,
    MAX_AGENT_PROFILE_CONTEXT_BYTES,
    MAX_AGENT_PROFILE_CONTEXT_FILE_BYTES,
    MAX_AGENT_PROFILE_CONTEXT_FILES,
    MAX_AGENT_PROFILE_DESCRIPTION_CHARS,
    MAX_AGENT_PROFILE_INSTRUCTIONS_BYTES,
    MAX_AGENT_PROFILE_MEMORY_BYTES,
    MAX_AGENT_PROFILE_MEMORY_ENTRIES,
    MAX_AGENT_PROFILE_MODEL_CHARS,
    MAX_AGENT_PROFILE_SKILL_BYTES,
    MAX_AGENT_PROFILE_SKILL_FILE_BYTES,
    MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
    MAX_AGENT_PROFILE_SKILL_RESOURCE_BYTES,
    MAX_AGENT_PROFILE_SKILL_RESOURCE_FILE_BYTES,
    MAX_AGENT_PROFILE_SKILL_RESOURCE_FILES,
    MAX_AGENT_PROFILE_SKILL_SCRIPT_RULES,
    MAX_AGENT_PROFILE_SKILLS,
    MAX_AGENT_PROFILE_TIMEOUT_SECONDS,
    MAX_AGENT_PROFILE_WORKERS,
)
from .agent_skill_mcp import (
    MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS,
    MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES,
    MAX_AGENT_PROFILE_SKILL_MCP_ENV_VAR_CHARS,
    MAX_AGENT_PROFILE_SKILL_MCP_HEADER_NAME_CHARS,
    MAX_AGENT_PROFILE_SKILL_MCP_NAME_CHARS,
    MAX_AGENT_PROFILE_SKILL_MCP_SCRIPT_PATH_CHARS,
    MAX_AGENT_PROFILE_SKILL_MCP_TOOL_NAME_CHARS,
    MAX_AGENT_PROFILE_SKILL_MCP_TOOLS,
    MAX_AGENT_PROFILE_SKILL_MCP_URL_CHARS,
    SAFE_MCP_NAME,
    SAFE_MCP_ENV_VAR,
    SAFE_MCP_HEADER_NAME,
    SAFE_MCP_TOOL_NAME,
)
from .agent_memory import (
    AGENT_MEMORY_MODES,
    AGENT_MEMORY_SELECTIONS,
    AGENT_MEMORY_SCHEMA,
    AGENT_MEMORY_SOURCE_KINDS,
    DEFAULT_AGENT_MEMORY_MAX_BYTES,
    DEFAULT_AGENT_MEMORY_MAX_ENTRIES,
    MAX_AGENT_MEMORY_ENTRY_BYTES,
    MAX_AGENT_MEMORY_ID_CHARS,
    MAX_AGENT_MEMORY_PROFILE_BYTES,
    MAX_AGENT_MEMORY_PROFILE_ENTRIES,
    MAX_AGENT_MEMORY_STORE_BYTES,
    MAX_AGENT_MEMORY_STORE_ENTRIES,
    MAX_AGENT_MEMORY_TAG_CHARS,
    MAX_AGENT_MEMORY_TAGS,
)
from .agent_tool_policy import (
    MAX_COMMAND_ARGV,
    MAX_COMMAND_ARG_CHARS,
    MAX_COMMAND_RULES,
)
from .agent_team import (
    AGENT_TEAM_INTERRUPTION_SCHEMA,
    AGENT_TEAM_STATE_SCHEMA,
    AGENT_TEAM_STATE_SCHEMA_V5,
    AGENT_TEAM_STATE_SCHEMA_V4,
    AGENT_TEAM_STATE_SCHEMA_V3,
    AGENT_TEAM_STATE_SCHEMA_V2,
    AGENT_TEAM_STATE_SCHEMA_V1,
    AGENT_TEAM_TURN_SCHEMA,
    AGENT_TEAM_TURN_SCHEMA_V1,
    MAX_AGENT_TEAM_CLAIMS_PER_TURN,
    MAX_AGENT_TEAM_DYNAMIC_TASKS,
    MAX_AGENT_TEAM_GENERATION,
    MAX_AGENT_TEAM_INSTRUCTIONS_CHARS,
    MAX_AGENT_TEAM_MEMBERS,
    MAX_AGENT_TEAM_MESSAGES,
    MAX_AGENT_TEAM_MESSAGES_PER_TURN,
    MAX_AGENT_TEAM_MESSAGE_CHARS,
    MAX_AGENT_TEAM_MESSAGE_DEPTH,
    MAX_AGENT_TEAM_MESSAGE_TASKS,
    MAX_AGENT_TEAM_OPERATOR_TASKS,
    MAX_AGENT_TEAM_OPERATOR_QUESTIONS,
    MAX_AGENT_TEAM_OPERATOR_REPLY_TIMEOUT_SECONDS,
    MAX_AGENT_TEAM_PLAN_APPROVAL_TASKS,
    MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
    MAX_AGENT_TEAM_PLAN_REVISIONS,
    MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS,
    MAX_AGENT_TEAM_ROLE_CHARS,
    MAX_AGENT_TEAM_ROUNDS,
    MAX_AGENT_TEAM_SUMMARY_CHARS,
    MAX_AGENT_TEAM_TASKS,
    MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
    MAX_AGENT_TEAM_TASK_PROPOSALS_PER_TURN,
    MAX_AGENT_TEAM_TURNS,
    MIN_AGENT_TEAM_MEMBERS,
)
from .agent_team_operator import (
    AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
    AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
    MAX_AGENT_TEAM_OPERATOR_INBOX_ENTRIES,
)
from .agent_team_chat import (
    AGENT_TEAM_OPERATOR_CHAT_ENTRY_FIELDS,
    AGENT_TEAM_OPERATOR_CHAT_FIELDS,
    AGENT_TEAM_OPERATOR_CHAT_SCHEMA,
    AGENT_TEAM_OPERATOR_CHAT_STATUSES,
    MAX_AGENT_TEAM_OPERATOR_CHAT_ENTRIES,
)
from .agent_team_transcript import (
    AGENT_TEAM_TRANSCRIPT_ACTIVITY_TYPES,
    AGENT_TEAM_TRANSCRIPT_EVENT_FIELDS,
    AGENT_TEAM_TRANSCRIPT_EVENT_TYPES,
    AGENT_TEAM_TRANSCRIPT_FIELDS,
    AGENT_TEAM_TRANSCRIPT_SCHEMA,
    AGENT_TEAM_TRANSCRIPT_STATUSES,
    MAX_AGENT_TEAM_TRANSCRIPT_ATTEMPT,
    MAX_AGENT_TEAM_TRANSCRIPT_BYTES,
    MAX_AGENT_TEAM_TRANSCRIPT_EVENTS,
    MAX_AGENT_TEAM_TRANSCRIPT_MESSAGE_CHARS,
    MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES,
    MIN_AGENT_TEAM_TRANSCRIPT_BYTES,
    MIN_AGENT_TEAM_TRANSCRIPT_EVENTS,
)
from .agent_team_merge import (
    AGENT_TEAM_MERGE_INTENT_SCHEMA,
    AGENT_TEAM_MERGE_LEDGER_SCHEMA,
    AGENT_TEAM_MERGE_RECOVERY_SCHEMA,
    AGENT_TEAM_MERGE_TRANSACTION_SCHEMA,
    MAX_AGENT_TEAM_MERGE_EVENTS,
)
from .agent_team_hooks import (
    AGENT_TEAM_HOOK_EVENTS,
    AGENT_TEAM_HOOK_INPUT_FIELDS,
    AGENT_TEAM_HOOK_INPUT_SCHEMA,
    MAX_AGENT_TEAM_HOOK_CHANGED_FILES,
)
from .agent_lifecycle_hooks import (
    AGENT_LIFECYCLE_HOOK_EVENTS,
    AGENT_LIFECYCLE_HOOK_INPUT_FIELDS,
    AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA,
    AGENT_LIFECYCLE_SCOPES,
    AGENT_LIFECYCLE_STATUSES,
)
from .agent_native_tool_hooks import (
    AGENT_NATIVE_TOOL_GATE_SCHEMA,
    AGENT_NATIVE_TOOL_HOOK_EVENTS,
    AGENT_NATIVE_TOOL_INPUT_SCHEMA,
    AGENT_NATIVE_TOOL_RECEIPT_SCHEMA,
    GATE_FIELDS as AGENT_NATIVE_TOOL_GATE_FIELDS,
    INPUT_FIELDS as AGENT_NATIVE_TOOL_INPUT_FIELDS,
    MAX_AGENT_NATIVE_TOOL_HOOKS,
    MAX_AGENT_NATIVE_TOOL_NAMES,
    MAX_AGENT_NATIVE_TOOL_RECEIPTS,
    MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS,
    RECEIPT_FIELDS as AGENT_NATIVE_TOOL_RECEIPT_FIELDS,
    RECEIPT_RESULT_FIELDS as AGENT_NATIVE_TOOL_RECEIPT_RESULT_FIELDS,
)
from .agent_lifecycle_context import (
    AGENT_LIFECYCLE_CONTEXT_FIELDS,
    AGENT_LIFECYCLE_CONTEXT_FIELDS_V1,
    AGENT_LIFECYCLE_CONTEXT_SCHEMA,
    AGENT_LIFECYCLE_CONTEXT_SCHEMA_V1,
    AGENT_LIFECYCLE_CONTEXT_SCOPES,
    AGENT_LIFECYCLE_CONTEXT_SCOPES_V1,
    MAX_AGENT_LIFECYCLE_CONTEXT_BYTES,
)
from .agent_lifecycle_stop_gate import (
    AGENT_LIFECYCLE_STOP_GATE_FIELDS,
    AGENT_LIFECYCLE_STOP_GATE_RESULT_FIELDS,
    AGENT_LIFECYCLE_STOP_GATE_RESULT_STATUSES,
    AGENT_LIFECYCLE_STOP_GATE_SCHEMA,
    AGENT_LIFECYCLE_STOP_GATE_STATUSES,
    MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
)
from .direct_workspace_transaction import (
    DIRECT_WORKSPACE_TRANSACTION_FIELDS,
    DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
    DIRECT_WORKSPACE_TRANSACTION_STATUSES,
    MAX_DIRECT_WORKSPACE_ATTEMPT,
)
from .staged_apply import (
    MAX_STAGED_APPLY_NAME_CHARS,
    STAGED_APPLY_EVIDENCE_KINDS,
    STAGED_APPLY_FIELDS,
    STAGED_APPLY_POLICY_FIELDS,
    STAGED_APPLY_SCHEMA,
    STAGED_APPLY_STATUSES,
)
from .agent_team_quality_retry import (
    AGENT_TEAM_QUALITY_RETRY_FIELDS,
    AGENT_TEAM_QUALITY_RETRY_SCHEMA,
    AGENT_TEAM_QUALITY_RETRY_STATUSES,
    MAX_AGENT_TEAM_QUALITY_RETRIES,
)
from .agent_team_turn_completion import (
    AGENT_TEAM_TURN_COMPLETION_SCHEMA,
    AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1,
)
from .agent_team_turn_terminal import (
    AGENT_TEAM_TURN_TERMINAL_SCHEMA,
    AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1,
)
from .agent_map_packet_terminal import (
    AGENT_MAP_PACKET_TERMINAL_SCHEMA,
    MAX_AGENT_MAP_GENERATION,
    MAX_AGENT_MAP_PACKET_TERMINAL_EVENTS,
    TERMINAL_FIELDS as AGENT_MAP_PACKET_TERMINAL_FIELDS,
)
from .codex_checkpoint import MAX_CODEX_STEP_RESUMES
from .codex_step_terminal import (
    CODEX_STEP_TERMINAL_SCHEMA,
    CODEX_STEP_TERMINAL_SCHEMA_V1,
    MAX_CODEX_STEP_TERMINAL_EVENTS,
    TERMINAL_FIELDS as CODEX_STEP_TERMINAL_FIELDS,
    TERMINAL_FIELDS_V1 as CODEX_STEP_TERMINAL_FIELDS_V1,
)
from .codex_progress import (
    CODEX_PROGRESS_SCHEMA_V1,
    CODEX_PROGRESS_SCHEMA_V2,
    CODEX_PROGRESS_SCHEMA_V3,
    CODEX_PROGRESS_SCHEMA,
    COLLAB_TOOL_COUNT_FIELDS as CODEX_PROGRESS_COLLAB_TOOL_COUNT_FIELDS,
    ITEM_COUNT_FIELDS_V1 as CODEX_PROGRESS_ITEM_COUNT_FIELDS_V1,
    ITEM_COUNT_FIELDS as CODEX_PROGRESS_ITEM_COUNT_FIELDS,
    LAST_EVENTS_V1 as CODEX_PROGRESS_LAST_EVENTS_V1,
    LAST_EVENTS as CODEX_PROGRESS_LAST_EVENTS,
    MAX_CODEX_PROGRESS_ATTEMPTS,
    MAX_CODEX_PROGRESS_EVENTS,
    MAX_CODEX_PROGRESS_NATIVE_AGENTS,
    MAX_CODEX_PROGRESS_PACKET_INDEX,
    NATIVE_AGENT_STATUS_FIELDS as CODEX_PROGRESS_NATIVE_AGENT_STATUS_FIELDS,
    PROGRESS_FIELDS_V1 as CODEX_PROGRESS_FIELDS_V1,
    PROGRESS_FIELDS_V2 as CODEX_PROGRESS_FIELDS_V2,
    PROGRESS_FIELDS_V3 as CODEX_PROGRESS_FIELDS_V3,
    PROGRESS_FIELDS as CODEX_PROGRESS_FIELDS,
)
from .codex_native_usage import MAX_NATIVE_USAGE_SESSIONS
from .agent_team_plan_approval import (
    AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS,
    AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V2,
    AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V1,
    AGENT_TEAM_PLAN_APPROVAL_FIELDS,
    AGENT_TEAM_PLAN_APPROVAL_FIELDS_V2,
    AGENT_TEAM_PLAN_APPROVAL_FIELDS_V1,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1,
    AGENT_TEAM_PLAN_APPROVAL_STATUSES,
    AGENT_TEAM_PLAN_FIELDS,
    AGENT_TEAM_PLAN_REVIEW_FIELDS,
    AGENT_TEAM_PLAN_REVIEW_SCHEMA,
    AGENT_TEAM_PLAN_SCHEMA,
    MAX_AGENT_TEAM_PLAN_ITEM_CHARS,
    MAX_AGENT_TEAM_PLAN_ITEMS,
)
from .codex_config import (
    CODEX_NATIVE_AGENT_MAX_DEPTH,
    MAX_CODEX_NATIVE_AGENT_THREADS,
    CODEX_PROFILE_TOOL_POLICIES,
    MAX_CODEX_PROFILE_TOOL_OUTPUT_TOKEN_LIMIT,
    MIN_CODEX_NATIVE_AGENT_THREADS,
    MIN_CODEX_PROFILE_TOOL_OUTPUT_TOKEN_LIMIT,
)
from .agent_packets import MAX_AGENT_ITEMS_PER_PACKET, MAX_AGENT_PACKETS
from .provider_telemetry import MAX_PROVIDER_EVENTS
from .staged_workspace import (
    MAX_STAGED_CHANGES,
    MAX_STAGED_FILE_BYTES,
    MAX_STAGED_FILES,
    MAX_STAGED_TOTAL_BYTES,
    WORKSPACE_MERGE_STRATEGY,
    WORKSPACE_SNAPSHOT_MANIFEST_SCHEMA,
)
from .auto_orchestrator import (
    AUTO_ARTIFACT_CONTENT_STATES,
    AUTO_ARTIFACT_FIELDS,
    AUTO_ARTIFACT_KINDS,
    AUTO_DECISION_FIELDS,
    AUTO_EXECUTION_MODES,
    AUTO_EXECUTION_REASON_CODES,
    AUTO_LIMIT_FIELDS,
    AUTO_LIMIT_FIELDS_V1,
    AUTO_ORCHESTRATION_RECEIPT_SCHEMA,
    AUTO_ORCHESTRATION_RECEIPT_SCHEMA_V1,
    AUTO_POLICY_FIELDS,
    AUTO_POLICY_FIELDS_V1,
    AUTO_REASON_CODES,
    AUTO_REASON_CODES_V1,
    AUTO_RECEIPT_FIELDS,
    AUTO_RECEIPT_STATUSES,
    AUTO_ROUTES,
    AUTO_ROUTE_VERSIONS,
    AUTO_ROUTE_VERSION,
    AUTO_STRATEGIES,
    AUTO_STRATEGIES_V1,
    AUTO_TASK_FIELDS,
    AUTO_RESULT_FIELDS,
    AUTO_VERIFIER_KINDS,
    AUTO_WORKER_SOURCES,
    AUTO_WORKER_SOURCES_V1,
    MAX_AUTO_ARTIFACTS,
    MAX_AUTO_CHECK_TIMEOUT_SECONDS,
    MAX_AUTO_ITERATIONS,
)
from .auto_topology_campaign import (
    ARM_ARTIFACT_FIELDS as AUTO_TOPOLOGY_ARM_ARTIFACT_FIELDS,
    AUTO_TOPOLOGIES,
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1,
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2,
    AUTO_TOPOLOGY_EVIDENCE_STATUS,
    AUTO_TOPOLOGY_RANDOMIZATION,
    AUTO_TOPOLOGY_SPECS,
    BUDGET_FIELDS as AUTO_TOPOLOGY_BUDGET_FIELDS,
    CAMPAIGN_FIELDS as AUTO_TOPOLOGY_CAMPAIGN_FIELDS,
    CAMPAIGN_FIELDS_V1 as AUTO_TOPOLOGY_CAMPAIGN_FIELDS_V1,
    CAMPAIGN_FIELDS_V2 as AUTO_TOPOLOGY_CAMPAIGN_FIELDS_V2,
    CLAIM_GATE_FIELDS as AUTO_TOPOLOGY_CLAIM_GATE_FIELDS,
    COHORT_FIELDS as AUTO_TOPOLOGY_COHORT_FIELDS,
    COHORT_FIELDS_V2 as AUTO_TOPOLOGY_COHORT_FIELDS_V2,
    DESIGN_FIELDS as AUTO_TOPOLOGY_DESIGN_FIELDS,
    MAX_AUTO_TOPOLOGY_COHORTS,
    MAX_AUTO_TOPOLOGY_ITERATIONS,
    MAX_AUTO_TOPOLOGY_REPETITIONS,
    MODEL_FIELDS as AUTO_TOPOLOGY_MODEL_FIELDS,
    SOURCE_FIELDS as AUTO_TOPOLOGY_SOURCE_FIELDS,
    TOPOLOGY_FIELDS as AUTO_TOPOLOGY_FIELDS,
)
from .readonly_parity_campaign import (
    ARTIFACT_FIELDS as READONLY_PARITY_ARTIFACT_FIELDS,
    CAMPAIGN_FIELDS as READONLY_PARITY_CAMPAIGN_FIELDS,
    CLAIM_GATE_FIELDS as READONLY_PARITY_CLAIM_GATE_FIELDS,
    COHORT_FIELDS as READONLY_PARITY_COHORT_FIELDS,
    DESIGN_FIELDS as READONLY_PARITY_DESIGN_FIELDS,
    DESIGN_FIELDS_V1 as READONLY_PARITY_DESIGN_FIELDS_V1,
    MAX_COHORTS as MAX_READONLY_PARITY_COHORTS,
    MAX_REPETITIONS as MAX_READONLY_PARITY_REPETITIONS,
    READONLY_PARITY_CAMPAIGN_SCHEMA,
    READONLY_PARITY_CAMPAIGN_SCHEMA_V1,
    READONLY_PARITY_BUDGET_PROFILES,
    READONLY_PARITY_CLAUDE_MAX_TURNS,
    READONLY_PARITY_CODEX_TOKEN_CAP,
    READONLY_PARITY_EVIDENCE_STATUS,
    READONLY_PARITY_NATIVE_THREADS,
    READONLY_PARITY_RANDOMIZATION,
    READONLY_PARITY_PROFILE_TOKEN_CAPS,
    READONLY_PARITY_RESOURCE_POLICIES,
    READONLY_PARITY_SYSTEMS,
    SOURCE_FIELDS as READONLY_PARITY_SOURCE_FIELDS,
    SYSTEM_FIELDS as READONLY_PARITY_SYSTEM_FIELDS,
)
from .benchmark import (
    CLAIM_POLICY_FIELDS,
    COMPARISON_SCHEMA,
    BUDGET_CAP_ENFORCEMENT_LEVELS,
    BUDGET_FIELDS,
    BENCHMARK_REPORT_SCHEMA,
    LIVE_PARITY_RUNBOOK_CLAIM_GATE_FIELDS,
    LIVE_PARITY_RUNBOOK_EVIDENCE_STATUS,
    LIVE_PARITY_RUNBOOK_FIELDS,
    LIVE_PARITY_RUNBOOK_INVOCATION_FIELDS,
    LIVE_PARITY_RUNBOOK_SAFETY_TEMPLATE_FIELDS,
    LIVE_PARITY_RUNBOOK_SCHEMA,
    LIVE_PARITY_RUNBOOK_SCORECARD_FIELDS,
    LIVE_PARITY_RUNBOOK_SOURCE_FIELDS,
    LIVE_PARITY_RUNBOOK_SYSTEM_FIELDS,
    LIVE_PARITY_RUNBOOK_TASK_FIELDS,
    LIVE_TOOL_COST_LEVELS,
    MAX_OUTPUT_LIMIT_BYTES as MAX_BENCHMARK_OUTPUT_LIMIT_BYTES,
    MODEL_FIELDS,
    MODEL_CLAIM_MODES,
    MODEL_CLAIM_POLICY_FIELDS,
    PARITY_BUDGET_FIELDS,
    PARITY_TASKS_FIELDS,
    PARITY_TASKS_SCHEMA,
    PARITY_TASK_FIELDS,
    PERMISSION_FIELDS,
    REPOSITORY_FIELDS,
    RUN_MANIFEST_SCHEMA,
    SAFETY_SEVERITIES,
    SCHEMA as BENCHMARK_SCHEMA,
    SCORING_TASK_FIELDS,
)
from .benchmark_quality import (
    BENCHMARK_QUALITY_EVIDENCE_SCHEMA,
    BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2,
    BENCHMARK_SCORE_INPUT_SCHEMA,
    MAX_QUALITY_CRITERIA,
    MAX_QUALITY_NOTES_CHARS,
    MAX_QUALITY_SCORE,
    MAX_QUALITY_TASKS,
    QUALITY_CRITERION_FIELDS,
    QUALITY_EVIDENCE_FIELDS,
    QUALITY_EVIDENCE_FIELDS_V2,
    QUALITY_REVIEWER_FIELDS,
    QUALITY_REVIEWER_KINDS,
    SCORE_INPUT_FIELDS,
    SCORE_INPUT_TASK_FIELDS,
)
from .background_run import BACKGROUND_FIELDS, BACKGROUND_RUN_SCHEMA, BACKGROUND_STATUSES
from .desktop_notification import (
    DESKTOP_NOTIFICATION_BACKENDS,
    DESKTOP_NOTIFICATION_CONTENT_POLICY,
    DESKTOP_NOTIFICATION_ERROR_CLASSES,
    DESKTOP_NOTIFICATION_RECEIPT_FIELDS,
    DESKTOP_NOTIFICATION_SCHEMA,
    DESKTOP_NOTIFICATION_STATUSES,
    DESKTOP_NOTIFICATION_TIMESTAMP_PATTERN,
    MAX_DESKTOP_NOTIFICATION_RETURNCODE,
    MIN_DESKTOP_NOTIFICATION_RETURNCODE,
    RUN_NOTIFICATION_STATUSES,
)
from .background_goal import (
    BACKGROUND_MODEL_GOAL_FIELDS,
    BACKGROUND_MODEL_GOAL_SCHEMA,
    BACKGROUND_MODEL_GOAL_STATUSES,
)
from .background_supervisor import (
    BACKGROUND_ROUTINE_SUPERVISOR_FIELDS,
    BACKGROUND_ROUTINE_SUPERVISOR_RESULT_STATUSES,
    BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA,
    BACKGROUND_ROUTINE_SUPERVISOR_STATUSES,
)
from .claude_live import (
    CLAUDE_EFFORT,
    CLAUDE_INCIDENT_FIELDS,
    CLAUDE_LEGACY_PERMISSION_MODE,
    CLAUDE_MODEL,
    CLAUDE_OBSERVED_FIELDS,
    CLAUDE_OBSERVED_FIELDS_V1,
    CLAUDE_OUTPUT_SOURCES,
    CLAUDE_PERMISSION_MODE,
    CLAUDE_POLICY_FIELDS,
    CLAUDE_PROVIDER_EVIDENCE_FIELDS,
    CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
    CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
    CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
    CLAUDE_PROVIDER_SEVERITIES,
    CLAUDE_PROVIDER_STATUSES,
    CLAUDE_READ_ONLY_TOOLS,
    CLAUDE_STAGED_PERMISSION_MODE,
    CLAUDE_STAGED_WRITE_TOOLS,
    CLAUDE_REQUESTED_FIELDS,
    CLAUDE_TOKEN_ACCOUNTING,
    CLAUDE_TOKEN_USAGE_SOURCES,
    MAX_CLAUDE_INCIDENTS,
    MAX_CLAUDE_STREAM_EVENTS,
    MAX_CLAUDE_TOKENS,
    MAX_CLAUDE_TURNS,
)
from .claude_staged import (
    CLAUDE_STAGED_CHANGE_FIELDS,
    CLAUDE_STAGED_EVIDENCE_SCHEMA,
    CLAUDE_STAGED_FIELDS,
    CLAUDE_STAGED_INCIDENT_FIELDS,
    CLAUDE_STAGED_INCIDENT_SEVERITIES,
    CLAUDE_STAGED_POLICY_FIELDS,
    CLAUDE_STAGED_SOURCE_FIELDS,
    CLAUDE_STAGED_STAGE_FIELDS,
    CLAUDE_STAGED_STATUSES,
    CLAUDE_STAGED_VERIFICATION_FIELDS,
    CLAUDE_STAGED_VERIFICATION_STATUSES,
    CLAUDE_STAGED_VERIFIER_SANDBOXES,
    CLAUDE_STAGED_VERIFIER_WRITE_SCOPE,
    MAX_VERIFIER_EXCERPT_CHARS,
    MAX_VERIFIER_TIMEOUT_SECONDS,
)
from .codex_live import (
    CODEX_APPROVAL_POLICY,
    CODEX_EXECUTION_MODES,
    CODEX_INCIDENT_FIELDS,
    CODEX_MODEL_BINDING,
    CODEX_OBSERVED_FIELDS,
    CODEX_OBSERVED_FIELDS_V1,
    CODEX_OBSERVED_FIELDS_V2,
    CODEX_OBSERVED_FIELDS_V3,
    CODEX_OBSERVED_FIELDS_V4,
    CODEX_OUTPUT_SOURCES,
    CODEX_POLICY_FIELDS,
    CODEX_POLICY_FIELDS_V1,
    CODEX_POLICY_FIELDS_V2,
    CODEX_PROVIDER_EVIDENCE_FIELDS,
    CODEX_PROVIDER_EVIDENCE_SCHEMA,
    CODEX_PROVIDER_EVIDENCE_SCHEMA_V1,
    CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
    CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
    CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
    CODEX_PROVIDER_SEVERITIES,
    CODEX_PROVIDER_STATUSES,
    CODEX_READONLY_PERMISSION_PROFILE,
    CODEX_REASONING_EFFORTS,
    CODEX_REQUESTED_FIELDS,
    CODEX_REQUESTED_FIELDS_V1,
    CODEX_REQUESTED_FIELDS_V2,
    CODEX_SANDBOX,
    CODEX_SANDBOX_V1,
    CODEX_SERVICE_TIERS,
    CODEX_SERVICE_TIER_BINDINGS,
    CODEX_STAGED_PERMISSION_PROFILE,
    CODEX_TERMINAL_STATUSES,
    CODEX_TOKEN_USAGE_PRECISIONS,
    CODEX_TOKEN_USAGE_SOURCES,
    CODEX_WORKSPACE_FIELDS,
    CODEX_WORKSPACE_FIELDS_V1,
    MAX_CODEX_INCIDENTS,
    MAX_CODEX_STREAM_EVENTS,
    MAX_CODEX_TOKENS,
    MAX_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
    MIN_CODEX_RUNTIME_TOKEN_CAP,
    MIN_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
)
from .codex_staged import (
    CODEX_STAGED_EVIDENCE_SCHEMA,
    CODEX_STAGED_FIELDS,
    CODEX_STAGED_FILESYSTEM_SCOPE,
    CODEX_STAGED_INCIDENT_FIELDS,
    CODEX_STAGED_INCIDENT_SEVERITIES,
    CODEX_STAGED_POLICY_FIELDS,
    CODEX_STAGED_STATUSES,
)
from .codex_staged_repair import (
    CODEX_STAGED_REPAIR_ATTEMPT_FIELDS,
    CODEX_STAGED_REPAIR_FIELDS,
    CODEX_STAGED_REPAIR_FINAL_FIELDS,
    CODEX_STAGED_REPAIR_INCIDENT_FIELDS,
    CODEX_STAGED_REPAIR_LIMIT_FIELDS,
    CODEX_STAGED_REPAIR_POLICY_FIELDS,
    CODEX_STAGED_REPAIR_RETRY_STATUSES,
    CODEX_STAGED_REPAIR_SCHEMA,
    CODEX_STAGED_REPAIR_SOURCE_FIELDS,
    CODEX_STAGED_REPAIR_STATE_ATTEMPT_FIELDS,
    CODEX_STAGED_REPAIR_STATE_CONFIG_FIELDS,
    CODEX_STAGED_REPAIR_STATE_FIELDS,
    CODEX_STAGED_REPAIR_STATE_FINAL_FIELDS,
    CODEX_STAGED_REPAIR_STATE_POLICY_FIELDS,
    CODEX_STAGED_REPAIR_STATE_SCHEMA,
    CODEX_STAGED_REPAIR_STATE_SOURCE_FIELDS,
    CODEX_STAGED_REPAIR_STATE_STATUSES,
    CODEX_STAGED_REPAIR_STATUSES,
    CODEX_STAGED_REPAIR_USAGE_FIELDS,
    MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
    MAX_CODEX_STAGED_REPAIR_ATTEMPT_DURATION_MS,
    MAX_CODEX_STAGED_REPAIR_DURATION_MS,
    MAX_CODEX_STAGED_REPAIR_INCIDENTS,
)
from .staged_workspace import (
    MAX_STAGED_PATCH_BYTES,
    STAGED_COMPLETION_SUMMARY_FIELDS,
    STAGED_COMPLETION_SUMMARY_SCHEMA,
)
from .errors import ValidationError
from .evidence import (
    CLAIM_FIELDS,
    CLAIM_STATUSES,
    COUNT_FIELDS,
    EVIDENCE_BUNDLE_SCHEMA,
    GOAL_FIELDS,
    GOAL_LAST_ITERATION_FIELDS,
    LIMIT_FIELDS,
    MAX_EVIDENCE_ITEMS,
    REPORT_FIELDS,
    ROUTINE_FIELDS,
    ROUTINE_POLICY_FIELDS,
    RUN_FIELDS,
    SOURCE_FIELDS,
    TOP_LEVEL_FIELDS as EVIDENCE_TOP_LEVEL_FIELDS,
)
from .model_planner import (
    DIRECT_NATIVE_MODEL_WORKFLOW_PROMPT_CONTRACT,
    DIRECT_NATIVE_MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
    DIRECT_NATIVE_MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
    DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT,
    DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
    DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
    DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V3,
    MAX_MODEL_WORKFLOW_ITEMS,
    MAX_MODEL_WORKFLOW_OUTPUT_LIMIT_BYTES,
    MAX_MODEL_WORKFLOW_STEPS,
    MAX_MODEL_WORKFLOW_TASK_CHARS,
    MAX_MODEL_WORKFLOW_TIMEOUT_SECONDS,
    MODEL_WORKFLOW_EVIDENCE_STATUS,
    MODEL_WORKFLOW_PROMPT_CONTRACT,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V3,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V4,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V5,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V6,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V7,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V8,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V9,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V10,
    MODEL_WORKFLOW_PROMPT_CONTRACT_V11,
    MODEL_WORKFLOW_REASONING_EFFORTS,
    MODEL_WORKFLOW_RECEIPT_FIELDS,
    MODEL_WORKFLOW_RECEIPT_FIELDS_V3,
    MODEL_WORKFLOW_RECEIPT_SCHEMA,
    MODEL_WORKFLOW_RECEIPT_SCHEMA_V1,
    MODEL_WORKFLOW_RECEIPT_SCHEMA_V2,
    MODEL_WORKFLOW_RECEIPT_SCHEMA_V3,
    MODEL_WORKFLOW_RECEIPT_STATUS,
    MODEL_WORKFLOW_WORKSPACE_BINDING_POLICY,
    MODEL_WORKFLOW_WORKSPACE_FIELDS,
)
from .effort_policy import AUTO_REASONING_EFFORTS
from .parity_campaign import (
    ARTIFACT_FIELDS as PARITY_CAMPAIGN_ARTIFACT_FIELDS,
    CAMPAIGN_FIELDS as PARITY_CAMPAIGN_FIELDS,
    CLAIM_GATE_FIELDS as PARITY_CAMPAIGN_CLAIM_GATE_FIELDS,
    DESIGN_FIELDS as PARITY_CAMPAIGN_DESIGN_FIELDS,
    DESIGN_EFFORT_POLICY_FIELD,
    MAX_CAMPAIGN_REPETITIONS,
    MAX_CAMPAIGN_TRIALS,
    PARITY_CAMPAIGN_EVIDENCE_STATUS,
    PARITY_CAMPAIGN_EFFORT_POLICIES,
    PARITY_CAMPAIGN_RANDOMIZATION,
    PARITY_CAMPAIGN_SCHEMA,
    PARITY_CAMPAIGN_SCHEMA_V3,
    PARITY_CAMPAIGN_SCHEMA_V2,
    PARITY_CAMPAIGN_SCHEMA_V1,
    SOURCE_FIELDS as PARITY_CAMPAIGN_SOURCE_FIELDS,
    SYSTEM_FIELDS as PARITY_CAMPAIGN_SYSTEM_FIELDS,
    TRIAL_FIELDS as PARITY_CAMPAIGN_TRIAL_FIELDS,
)
from .model_orchestrator import (
    MODEL_WORKFLOW_EXECUTE_APPROVAL,
    MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA,
    MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V1,
    MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2,
    RUN_RECEIPT_FIELDS,
    RUN_STATUSES as MODEL_WORKFLOW_RUN_STATUSES,
)
from .packet_items import (
    MAX_JSON_POINTER_CHARS,
    MAX_JSON_PACKET_ITEM_DEPTH,
    MAX_JSON_PACKET_ITEM_NODES,
    MAX_OPAQUE_PACKET_ITEM_CHARS,
    MAX_PACKET_ITEM_CHARS,
)
from .routines import (
    BUDGET_KEYS as ROUTINE_BUDGET_KEYS,
    CRON_EXPRESSION_SHAPE_PATTERN,
    EXTERNAL_SUPERVISOR_KEYS,
    LAUNCH_KEYS,
    LAUNCH_REQUIRED_KEYS,
    MAX_CRON_EXPRESSION_CHARS,
    MAX_LIST_ROUTINES,
    MAX_AGENT_WORKERS as ROUTINE_MAX_AGENT_WORKERS,
    MAX_INTERVAL_MINUTES,
    POLICY_KEYS as ROUTINE_POLICY_KEYS,
    ROUTINE_MANIFEST_SCHEMA,
    TARGET_BASE_KEYS,
    TIME_PATTERN,
    TIMEZONE_PATTERN,
    TOP_LEVEL_KEYS as ROUTINE_TOP_LEVEL_KEYS,
    UTC_TIMESTAMP_PATTERN,
    WEEKDAY_ALIASES,
)
from .routine_supervisor import (
    MAX_ROUTINE_CONTROL_EVENTS,
    MAX_ROUTINE_HISTORY,
    MAX_ROUTINE_REASON_CHARS,
    MAX_SUPERVISOR_CYCLES,
    MAX_SUPERVISOR_POLL_SECONDS,
    ROUTINE_CONTROL_ACTIONS,
    ROUTINE_CONTROLS_SCHEMA,
    ROUTINE_EXECUTION_STATUSES,
    ROUTINE_HISTORY_FIELDS,
    ROUTINE_STATE_RECORD_FIELDS,  # noqa: F401 - compatibility re-export
    ROUTINE_SUPERVISOR_SCHEMA,
    ROUTINE_TRIGGERS,
)
from .routine_service import (
    AUTHORIZATION_KEYS as ROUTINE_SERVICE_AUTHORIZATION_KEYS,
    GRANT_KEYS as ROUTINE_SERVICE_GRANT_KEYS,
    LIMIT_KEYS as ROUTINE_SERVICE_LIMIT_KEYS,
    MAX_ROUTINE_SERVICE_BYTES,  # noqa: F401 - compatibility re-export
    MAX_SERVICE_ERROR_CHARS,
    ROUTINE_KEYS as ROUTINE_SERVICE_ROUTINE_KEYS,
    ROUTINE_SERVICE_GRANT_SCHEMA,
    ROUTINE_SERVICE_PLATFORMS,
    ROUTINE_SERVICE_STATE_SCHEMA,
    ROUTINE_SERVICE_STATUSES,
    RUNTIME_KEYS as ROUTINE_SERVICE_RUNTIME_KEYS,
    STATE_KEYS as ROUTINE_SERVICE_STATE_KEYS,
)
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, MAX_ITERATION_CONTEXT_CHARS
from .model_verdict import (
    COMPLETION_VERDICT_CONTRACT,
    COMPLETION_VERDICT_FEEDBACK_SCHEMA,
    MAX_MODEL_EVIDENCE_ITEMS,
    MAX_MODEL_FEEDBACK_CHARS,
    MAX_MODEL_VERDICT_BYTES,
)
from .security import RISK_LEVELS, SHELL_METACHARS
from .workflow import (
    HOOK_EVENTS,
    HOOK_FAILURE_MODES,
    MAX_AGENT_ITEMS,
    MAX_AGENT_WORKERS,
    MAX_CODEX_CONTEXT_SOURCES,
    MAX_CODEX_CONTEXT_SOURCE_ID_CHARS,
    MAX_CODEX_OUTPUT_SCHEMA_BYTES,
    MAX_OUTPUT_LIMIT_BYTES as MAX_WORKFLOW_OUTPUT_LIMIT_BYTES,
    MAX_TIMEOUT_SECONDS,
    SAFE_ID,
    SCHEMA as WORKFLOW_SCHEMA,
)


JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_COMMAND_METACHAR_CLASS = "".join("\\" + char if char in r"\]^-" else char for char in sorted(SHELL_METACHARS))
COMMAND_STRING_PATTERN = "^(?![\\s\\S]*[%s])(?=[\\s\\S]*\\S)[\\s\\S]+$" % _COMMAND_METACHAR_CLASS
PACKET_ITEM_PATTERN = "^(?!\\s*$)(?!\\s*/)(?!\\s*\\.\\.(?:/|$))(?!.*(?:/)\\.\\.(?:/|$))[^\\r\\n\\u0000]+$"
RELATIVE_PATH_PATTERN = r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+$"
SCHEMA_VERSION = "conductor.schema_export.v1"


def schema_types() -> List[Dict]:
    return [
        {
            "name": name,
            "artifact_schema": artifact_schema,
            "title": title,
        }
        for name, artifact_schema, title, _builder in _SCHEMA_REGISTRY
    ]


def get_schema(name: str) -> Dict:
    normalized = _normalize_schema_name(name)
    for schema_name, _artifact_schema, _title, builder in _SCHEMA_REGISTRY:
        if schema_name == normalized:
            return copy.deepcopy(builder())
    raise ValidationError("unknown schema type: %s" % name)


def schema_json(name: str) -> str:
    return json.dumps(get_schema(name), indent=2, sort_keys=True) + "\n"


def _normalize_schema_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValidationError("schema type must be non-empty")
    for schema_name, artifact_schema, _title, _builder in _SCHEMA_REGISTRY:
        if value == schema_name or value == artifact_schema:
            return schema_name
    raise ValidationError("unknown schema type: %s" % name)


def _base_schema(artifact_schema: str, title: str, description: str) -> Dict:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": "https://codex-conductor.local/schemas/%s.schema.json" % artifact_schema,
        "title": title,
        "description": (
            description
            + " This schema is an editor and CI aid; Conductor's Python validators remain authoritative for cross-field checks."
        ),
        "type": "object",
    }


def _workflow_schema() -> Dict:
    schema = _base_schema(
        WORKFLOW_SCHEMA,
        "Conductor Workflow",
        "JSON Schema for conductor.workflow.v1 workflow specs.",
    )
    schema.update(
        {
            "required": ["schema", "name", "steps"],
            "additionalProperties": True,
            "properties": {
                "schema": {"const": WORKFLOW_SCHEMA},
                "name": _non_empty_string(),
                "description": _unchecked_metadata("Workflow description is display metadata; prefer a string."),
                "mode": {"enum": ["read_only", "workspace_write", "review", "custom"]},
                "result_artifact": _relative_path(),
                "max_workers": _int_range(1, MAX_AGENT_WORKERS),
                "output_limit_bytes": _int_range(1, MAX_WORKFLOW_OUTPUT_LIMIT_BYTES),
                "max_items": _int_range(1, MAX_AGENT_ITEMS),
                "default_timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                "agent_timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                "agent_effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
                "agent_max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                "agent_map_max_total_tokens": _int_range(
                    MIN_CODEX_RUNTIME_TOKEN_CAP,
                    MAX_CODEX_TOKENS,
                ),
                "agent_profiles": {
                    "type": "array",
                    "maxItems": MAX_AGENT_PROFILES,
                    "items": _agent_profile_object_schema(),
                },
                "agent_profile_context_bindings": _agent_profile_context_bindings_schema(),
                "agent_profile_skill_bindings": _agent_profile_skill_bindings_schema(),
                "agent_profile_memory_bindings": _agent_profile_memory_bindings_schema(),
                "hooks": {
                    "type": "array",
                    "items": _hook_schema(),
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "oneOf": [
                            _write_artifact_step_schema(),
                            _collect_results_step_schema(),
                            _manual_gate_step_schema(),
                            _shell_step_schema(),
                            _codex_exec_step_schema(),
                            _agent_map_step_schema(),
                            _agent_team_step_schema(),
                            _agent_memory_step_schema(),
                        ]
                    },
                },
            },
        }
    )
    return schema


def _agent_profile_schema() -> Dict:
    schema = _base_schema(
        AGENT_PROFILE_SCHEMA,
        "Conductor Agent Profile",
        "JSON Schema for reusable, policy-bounded Codex agent profiles.",
    )
    schema.update(_agent_profile_object_schema())
    return schema


def _agent_profile_object_schema() -> Dict:
    return {
        "type": "object",
        "required": ["schema", "name", "instructions"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"required": ["tool_output_token_limit"]},
                "then": {
                    "required": ["tool_policy"],
                    "properties": {
                        "tool_policy": {"enum": ["isolated", "restricted"]}
                    },
                },
            },
            {
                "if": {
                    "anyOf": [
                        {"required": ["command_allowlist"]},
                        {"required": ["allow_apply_patch"]},
                        {"required": ["skill_scripts"]},
                        {"required": ["skill_mcp_dependencies"]},
                    ]
                },
                "then": {
                    "required": ["tool_policy"],
                    "properties": {"tool_policy": {"const": "restricted"}},
                },
            }
        ],
        "properties": {
            "schema": {"const": AGENT_PROFILE_SCHEMA},
            "name": _safe_id(),
            "description": {
                "type": "string",
                "maxLength": MAX_AGENT_PROFILE_DESCRIPTION_CHARS,
            },
            "instructions": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_PROFILE_INSTRUCTIONS_BYTES,
            },
            "model": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_PROFILE_MODEL_CHARS,
                "pattern": r"^[^\r\n\u0000]+$",
            },
            "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
            "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
            "sandbox": {"enum": ["read-only", "workspace-write"]},
            "tool_policy": {"enum": sorted(CODEX_PROFILE_TOOL_POLICIES)},
            "tool_output_token_limit": _int_range(
                MIN_CODEX_PROFILE_TOOL_OUTPUT_TOKEN_LIMIT,
                MAX_CODEX_PROFILE_TOOL_OUTPUT_TOKEN_LIMIT,
            ),
            "command_allowlist": {
                "type": "array",
                "maxItems": MAX_COMMAND_RULES,
                "uniqueItems": True,
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "required": ["argv"],
                            "additionalProperties": False,
                            "properties": {
                                "argv": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": MAX_COMMAND_ARGV,
                                    "items": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_COMMAND_ARG_CHARS,
                                        "pattern": r"^[^\r\n\u0000]+$",
                                    },
                                }
                            },
                        },
                        {
                            "type": "object",
                            "required": ["argv_prefix"],
                            "additionalProperties": False,
                            "properties": {
                                "argv_prefix": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": MAX_COMMAND_ARGV,
                                    "items": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_COMMAND_ARG_CHARS,
                                        "pattern": r"^[^\r\n\u0000]+$",
                                    },
                                }
                            },
                        },
                    ]
                },
            },
            "allow_apply_patch": {"type": "boolean"},
            "risk": {"enum": sorted(RISK_LEVELS)},
            "max_workers": _int_range(1, MAX_AGENT_PROFILE_WORKERS),
            "timeout_seconds": _int_range(1, MAX_AGENT_PROFILE_TIMEOUT_SECONDS),
            "context_files": {
                "type": "array",
                "maxItems": MAX_AGENT_PROFILE_CONTEXT_FILES,
                "uniqueItems": True,
                "items": _relative_path(),
            },
            "skills": {
                "type": "array",
                "maxItems": MAX_AGENT_PROFILE_SKILLS,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
                    "pattern": RELATIVE_PATH_PATTERN,
                },
                "description": (
                    "Workspace-relative Skill directories. Each selected hash-bound SKILL.md is "
                    "preloaded while optional regular resources remain on demand; no script, "
                    "integration, or permission is granted."
                ),
            },
            "skill_scripts": {
                "type": "array",
                "maxItems": MAX_AGENT_PROFILE_SKILL_SCRIPT_RULES,
                "uniqueItems": True,
                "items": {
                    "oneOf": [
                        _agent_profile_skill_script_rule_schema("args"),
                        _agent_profile_skill_script_rule_schema("args_prefix"),
                    ]
                },
                "description": (
                    "Operator-authorized executable resources from selected Skills. Exact args or an "
                    "explicit args prefix is merged into the existing restricted command policy; no "
                    "script runs automatically and no other Skill resource becomes executable."
                ),
            },
            "skill_mcp_dependencies": {
                "type": "array",
                "maxItems": MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES,
                "uniqueItems": True,
                "items": {
                    "oneOf": [
                        _agent_profile_skill_mcp_http_schema(),
                        _agent_profile_skill_mcp_stdio_schema(),
                    ]
                },
                "description": (
                    "Operator-pinned HTTPS or Skill-local stdio MCP dependencies declared by selected Skills. "
                    "Each route requires restricted policy, transport-specific runtime approval, and an exact "
                    "native tool allowlist. HTTPS auth is parent-resolved; stdio runs an immutable Skill snapshot "
                    "with a sanitized environment, denied network, and no repository access."
                ),
            },
            "memory": {
                "type": "object",
                "required": ["mode"],
                "additionalProperties": False,
                "properties": {
                    "mode": {"enum": sorted(AGENT_MEMORY_MODES)},
                    "selection": {"enum": sorted(AGENT_MEMORY_SELECTIONS)},
                    "max_entries": _int_range(1, MAX_AGENT_MEMORY_PROFILE_ENTRIES),
                    "max_bytes": _int_range(256, MAX_AGENT_MEMORY_PROFILE_BYTES),
                },
                "description": (
                    "External workspace-scoped memory snapshot limits. Selection defaults to recent; "
                    "relevant uses deterministic bounded lexical ranking. Defaults: %d entries and %d bytes."
                    % (DEFAULT_AGENT_MEMORY_MAX_ENTRIES, DEFAULT_AGENT_MEMORY_MAX_BYTES)
                ),
            },
        },
    }


def _agent_profile_skill_mcp_common_properties() -> Dict:
    return {
        "skill": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
            "pattern": RELATIVE_PATH_PATTERN,
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_NAME_CHARS,
            "pattern": SAFE_MCP_NAME.pattern,
        },
        "tools": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_AGENT_PROFILE_SKILL_MCP_TOOLS,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_TOOL_NAME_CHARS,
                "pattern": SAFE_MCP_TOOL_NAME.pattern,
            },
        },
    }


def _agent_profile_skill_mcp_http_schema() -> Dict:
    properties = _agent_profile_skill_mcp_common_properties()
    properties.update(
        {
            "url": {
                "type": "string",
                "minLength": len("https://x"),
                "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_URL_CHARS,
                "pattern": r"^https://[^\s?#]+$",
            },
            "auth": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": False,
                "properties": {
                    "bearer_token_env_var": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_ENV_VAR_CHARS,
                        "pattern": SAFE_MCP_ENV_VAR.pattern,
                    },
                    "env_http_headers": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS,
                        "propertyNames": {
                            "minLength": 1,
                            "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_HEADER_NAME_CHARS,
                            "pattern": SAFE_MCP_HEADER_NAME.pattern,
                        },
                        "additionalProperties": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_ENV_VAR_CHARS,
                            "pattern": SAFE_MCP_ENV_VAR.pattern,
                        },
                    },
                },
            },
        }
    )
    return {
        "type": "object",
        "required": ["skill", "name", "url", "tools"],
        "additionalProperties": False,
        "properties": properties,
    }


def _agent_profile_skill_mcp_stdio_schema() -> Dict:
    properties = _agent_profile_skill_mcp_common_properties()
    properties.update(
        {
            "transport": {"const": "stdio"},
            "script": {
                "type": "string",
                "minLength": len("scripts/x"),
                "maxLength": MAX_AGENT_PROFILE_SKILL_MCP_SCRIPT_PATH_CHARS,
                "pattern": r"^scripts/[^/].*$",
            },
            "args": {
                "type": "array",
                "maxItems": MAX_COMMAND_ARGV - 1,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_COMMAND_ARG_CHARS,
                    "pattern": r"^[^\r\n\u0000]+$",
                },
            },
        }
    )
    return {
        "type": "object",
        "required": ["skill", "name", "transport", "script", "args", "tools"],
        "additionalProperties": False,
        "properties": properties,
    }


def _agent_profile_skill_script_rule_schema(argument_field: str) -> Dict:
    return {
        "type": "object",
        "required": ["skill", "script", argument_field],
        "additionalProperties": False,
        "properties": {
            "skill": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
                "pattern": RELATIVE_PATH_PATTERN,
            },
            "script": {
                "type": "string",
                "minLength": len("scripts/x"),
                "maxLength": MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
                "pattern": r"^scripts/[^/].*$",
            },
            argument_field: {
                "type": "array",
                "maxItems": MAX_COMMAND_ARGV - 1,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_COMMAND_ARG_CHARS,
                    "pattern": r"^[^\r\n\u0000]+$",
                },
            },
        },
    }


def _agent_profile_context_bindings_schema() -> Dict:
    return {
        "type": "object",
        "required": ["schema", "profiles"],
        "additionalProperties": False,
        "properties": {
            "schema": {"const": AGENT_PROFILE_CONTEXT_BINDING_SCHEMA},
            "profiles": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_PROFILES,
                "items": {
                    "type": "object",
                    "required": ["name", "files"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _safe_id(),
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_AGENT_PROFILE_CONTEXT_FILES,
                            "items": {
                                "type": "object",
                                "required": ["path", "sha256", "bytes"],
                                "additionalProperties": False,
                                "properties": {
                                    "path": _relative_path(),
                                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                    "bytes": _int_range(0, MAX_AGENT_PROFILE_CONTEXT_FILE_BYTES),
                                },
                            },
                        },
                    },
                },
            },
        },
        "description": "Runtime-generated context hashes; total bytes are capped at %d." % MAX_AGENT_PROFILE_CONTEXT_BYTES,
    }


def _agent_profile_skill_bindings_schema() -> Dict:
    return {
        "type": "object",
        "required": ["schema", "profiles"],
        "additionalProperties": False,
        "properties": {
            "schema": {"const": AGENT_PROFILE_SKILL_BINDING_SCHEMA},
            "profiles": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_PROFILES,
                "items": {
                    "type": "object",
                    "required": ["name", "skills"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _safe_id(),
                        "skills": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_AGENT_PROFILE_SKILLS,
                            "items": {
                                "type": "object",
                                "required": [
                                    "path",
                                    "sha256",
                                    "bytes",
                                    "mode",
                                    "tree_sha256",
                                    "resource_count",
                                    "resource_bytes",
                                    "resources",
                                ],
                                "additionalProperties": False,
                                "properties": {
                                    "path": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
                                        "pattern": RELATIVE_PATH_PATTERN,
                                    },
                                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                    "bytes": _int_range(1, MAX_AGENT_PROFILE_SKILL_FILE_BYTES),
                                    "mode": _int_range(0, 0o777),
                                    "tree_sha256": {
                                        "type": "string",
                                        "pattern": "^[0-9a-f]{64}$",
                                    },
                                    "resource_count": _int_range(
                                        0,
                                        MAX_AGENT_PROFILE_SKILL_RESOURCE_FILES,
                                    ),
                                    "resource_bytes": _int_range(
                                        0,
                                        MAX_AGENT_PROFILE_SKILL_RESOURCE_BYTES,
                                    ),
                                    "resources": {
                                        "type": "array",
                                        "maxItems": MAX_AGENT_PROFILE_SKILL_RESOURCE_FILES,
                                        "items": {
                                            "type": "object",
                                            "required": ["path", "sha256", "bytes", "mode"],
                                            "additionalProperties": False,
                                            "properties": {
                                                "path": {
                                                    "type": "string",
                                                    "minLength": 1,
                                                    "maxLength": MAX_AGENT_PROFILE_SKILL_PATH_CHARS,
                                                    "pattern": RELATIVE_PATH_PATTERN,
                                                },
                                                "sha256": {
                                                    "type": "string",
                                                    "pattern": "^[0-9a-f]{64}$",
                                                },
                                                "bytes": _int_range(
                                                    0,
                                                    MAX_AGENT_PROFILE_SKILL_RESOURCE_FILE_BYTES,
                                                ),
                                                "mode": _int_range(0, 0o777),
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        "description": (
            "Runtime-generated Skill entrypoint and recursive resource hashes; entrypoint bytes "
            "are capped at %d and resource bytes at %d."
            % (MAX_AGENT_PROFILE_SKILL_BYTES, MAX_AGENT_PROFILE_SKILL_RESOURCE_BYTES)
        ),
    }


def _agent_profile_memory_bindings_schema() -> Dict:
    return {
        "type": "object",
        "required": ["schema", "profiles"],
        "additionalProperties": False,
        "properties": {
            "schema": {"const": AGENT_PROFILE_MEMORY_BINDING_SCHEMA},
            "profiles": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_PROFILES,
                "items": {
                    "type": "object",
                    "required": [
                        "name",
                        "mode",
                        "store_revision",
                        "store_sha256",
                        "snapshot_sha256",
                        "entry_count",
                        "omitted_entries",
                        "bytes",
                        "entries",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "name": _safe_id(),
                        "mode": {"enum": sorted(AGENT_MEMORY_MODES)},
                        "store_revision": _int_range(0),
                        "store_sha256": _sha256(),
                        "snapshot_sha256": _sha256(),
                        "entry_count": _int_range(0, MAX_AGENT_MEMORY_PROFILE_ENTRIES),
                        "omitted_entries": _int_range(0, MAX_AGENT_MEMORY_STORE_ENTRIES),
                        "bytes": _int_range(0, MAX_AGENT_MEMORY_PROFILE_BYTES),
                        "entries": {
                            "type": "array",
                            "maxItems": MAX_AGENT_MEMORY_PROFILE_ENTRIES,
                            "items": _agent_memory_entry_schema(),
                        },
                    },
                },
            },
        },
        "description": (
            "Runtime-generated immutable memory snapshots; aggregate limits are %d entries and %d bytes."
            % (MAX_AGENT_PROFILE_MEMORY_ENTRIES, MAX_AGENT_PROFILE_MEMORY_BYTES)
        ),
    }


def _agent_memory_schema() -> Dict:
    schema = _base_schema(
        AGENT_MEMORY_SCHEMA,
        "Conductor Agent Memory",
        "JSON Schema for revisioned workspace-scoped reusable agent memory stores.",
    )
    schema.update(
        {
            "required": [
                "schema",
                "workspace_sha256",
                "profile",
                "revision",
                "updated_at_utc",
                "entries",
            ],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_MEMORY_SCHEMA},
                "workspace_sha256": _sha256(),
                "profile": _safe_id(),
                "revision": _int_range(0),
                "updated_at_utc": {"oneOf": [_timestamp_string(), {"type": "null"}]},
                "entries": {
                    "type": "array",
                    "maxItems": MAX_AGENT_MEMORY_STORE_ENTRIES,
                    "items": _agent_memory_entry_schema(),
                },
            },
        }
    )
    return schema


def _agent_team_state_v1_schema() -> Dict:
    nullable_id = {"oneOf": [_safe_id(), {"type": "null"}]}
    nullable_sha = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_int = {"oneOf": [_int_range(0), {"type": "null"}]}
    schema = _base_schema(
        AGENT_TEAM_STATE_SCHEMA_V1,
        "Conductor Agent Team State v1",
        "JSON Schema for resumable parent-mediated agent team task, mailbox, session, and turn state.",
    )
    schema.update(
        {
            "required": [
                "schema",
                "step_id",
                "workflow_fingerprint",
                "generation",
                "status",
                "round",
                "max_rounds",
                "max_turns",
                "max_total_tokens",
                "authorized_tokens",
                "created_at_utc",
                "updated_at_utc",
                "members",
                "tasks",
                "messages",
                "turns",
            ],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_STATE_SCHEMA_V1},
                "step_id": _safe_id(),
                "workflow_fingerprint": _sha256(),
                "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
                "status": {"enum": ["running", "completed"]},
                "round": _int_range(0, MAX_AGENT_TEAM_ROUNDS),
                "max_rounds": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                "max_turns": _int_range(1, MAX_AGENT_TEAM_TURNS),
                "max_total_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                "authorized_tokens": _int_range(0, MAX_CODEX_TOKENS),
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "members": {
                    "type": "array",
                    "minItems": MIN_AGENT_TEAM_MEMBERS,
                    "maxItems": MAX_AGENT_TEAM_MEMBERS,
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "role",
                            "agent_profile",
                            "lead",
                            "status",
                            "session_id",
                            "turns",
                            "current_task_id",
                            "claim_task_ids",
                            "last_error",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "role": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_AGENT_TEAM_ROLE_CHARS,
                            },
                            "agent_profile": _safe_id(),
                            "lead": {"type": "boolean"},
                            "status": {"enum": ["idle", "working"]},
                            "session_id": {
                                "oneOf": [
                                    {
                                        "type": "string",
                                        "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
                                    },
                                    {"type": "null"},
                                ]
                            },
                            "turns": _int_range(0, MAX_AGENT_TEAM_TURNS),
                            "current_task_id": nullable_id,
                            "claim_task_ids": {
                                "type": "array",
                                "maxItems": MAX_AGENT_TEAM_CLAIMS_PER_TURN,
                                "uniqueItems": True,
                                "items": _safe_id(),
                            },
                            "last_error": {
                                "oneOf": [
                                    {"type": "string", "minLength": 1, "maxLength": 200},
                                    {"type": "null"},
                                ]
                            },
                        },
                    },
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_TEAM_TASKS,
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "status",
                            "assignee",
                            "claimed_by",
                            "depends_on",
                            "attempts",
                            "summary",
                            "output_sha256",
                            "completed_round",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "status": {"enum": ["pending", "claimed", "completed"]},
                            "assignee": nullable_id,
                            "claimed_by": nullable_id,
                            "depends_on": {
                                "type": "array",
                                "maxItems": MAX_AGENT_TEAM_TASKS,
                                "uniqueItems": True,
                                "items": _safe_id(),
                            },
                            "attempts": _int_range(0, MAX_AGENT_TEAM_ROUNDS),
                            "summary": {
                                "oneOf": [
                                    {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_AGENT_TEAM_SUMMARY_CHARS,
                                    },
                                    {"type": "null"},
                                ]
                            },
                            "output_sha256": nullable_sha,
                            "completed_round": nullable_int,
                        },
                    },
                },
                "messages": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_MESSAGES,
                    "items": {
                        "type": "object",
                        "required": ["id", "round", "from", "to", "body", "body_sha256"],
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                            "from": _safe_id(),
                            "to": {"type": "string", "minLength": 1},
                            "body": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_AGENT_TEAM_MESSAGE_CHARS,
                            },
                            "body_sha256": _sha256(),
                        },
                    },
                },
                "turns": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_TURNS,
                    "items": {
                        "type": "object",
                        "required": [
                            "round",
                            "member_id",
                            "task_id",
                            "status",
                            "output",
                            "output_sha256",
                            "session_id_sha256",
                            "started_at_utc",
                            "finished_at_utc",
                            "max_tokens",
                            "input_tokens",
                            "output_tokens",
                            "total_tokens",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                            "member_id": _safe_id(),
                            "task_id": _safe_id(),
                            "status": {"enum": ["completed", "continue"]},
                            "output": _relative_path(),
                            "output_sha256": _sha256(),
                            "session_id_sha256": _sha256(),
                            "started_at_utc": _timestamp_string(),
                            "finished_at_utc": _timestamp_string(),
                            "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                            "input_tokens": nullable_int,
                            "output_tokens": nullable_int,
                            "total_tokens": nullable_int,
                        },
                    },
                },
            },
        }
    )
    return schema


def _agent_team_state_v2_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_state_v1_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_STATE_SCHEMA_V2
    schema["title"] = "Conductor Agent Team State v2"
    schema["description"] = (
        "JSON Schema for resumable parent-mediated agent team state with bounded dynamic tasks. "
        "This schema is an editor and CI aid; Conductor's Python validators remain authoritative "
        "for cross-field checks."
    )
    schema["required"].extend(["max_dynamic_tasks", "dynamic_tasks_added"])
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_STATE_SCHEMA_V2}
    properties["max_dynamic_tasks"] = _int_range(0, MAX_AGENT_TEAM_DYNAMIC_TASKS)
    properties["dynamic_tasks_added"] = _int_range(0, MAX_AGENT_TEAM_DYNAMIC_TASKS)
    task_schema = properties["tasks"]["items"]
    task_schema["required"].extend(
        ["description", "origin", "proposed_by", "proposed_round"]
    )
    task_schema["properties"].update(
        {
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
            },
            "origin": {"enum": ["static", "proposed"]},
            "proposed_by": {"oneOf": [_safe_id(), {"type": "null"}]},
            "proposed_round": {
                "oneOf": [_int_range(1, MAX_AGENT_TEAM_ROUNDS), {"type": "null"}]
            },
        }
    )
    return schema


def _agent_team_state_v3_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_state_v2_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_STATE_SCHEMA_V3
    schema["title"] = "Conductor Agent Team State v3"
    schema["description"] = (
        "JSON Schema for resumable parent-mediated agent team state with bounded lead-dynamic "
        "and operator-authored next-turn tasks. This schema is an editor and CI aid; "
        "Conductor's Python validators remain authoritative for cross-field checks."
    )
    schema["required"].extend(["max_operator_tasks", "operator_tasks_added"])
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_STATE_SCHEMA_V3}
    properties["max_operator_tasks"] = _int_range(0, MAX_AGENT_TEAM_OPERATOR_TASKS)
    properties["operator_tasks_added"] = _int_range(0, MAX_AGENT_TEAM_OPERATOR_TASKS)
    task_schema = properties["tasks"]["items"]
    task_schema["required"].append("operator_entry_id")
    task_schema["properties"]["origin"] = {"enum": ["static", "proposed", "operator"]}
    task_schema["properties"]["operator_entry_id"] = {
        "oneOf": [_safe_id(), {"type": "null"}]
    }
    return schema


def _agent_team_state_v4_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_state_v3_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_STATE_SCHEMA_V4
    schema["title"] = "Conductor Agent Team State v4"
    schema["description"] = (
        "JSON Schema for resumable parent-mediated agent team state with explicit, "
        "hash-bound operator interruption turns. This schema is an editor and CI aid; "
        "Conductor's Python validators remain authoritative for cross-field checks."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_STATE_SCHEMA_V4}
    turn_schema = properties["turns"]["items"]
    turn_schema["required"].extend(
        ["interruption_entry_id", "interruption_instruction_sha256"]
    )
    turn_schema["properties"]["status"] = {
        "enum": ["completed", "continue", "interrupted"]
    }
    turn_schema["properties"]["interruption_entry_id"] = {
        "oneOf": [_safe_id(), {"type": "null"}]
    }
    turn_schema["properties"]["interruption_instruction_sha256"] = {
        "oneOf": [_sha256(), {"type": "null"}]
    }
    return schema


def _agent_team_state_v5_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_state_v4_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_STATE_SCHEMA_V5
    schema["title"] = "Conductor Agent Team State v5"
    schema["description"] = (
        "JSON Schema for resumable parent-mediated agent team state with bounded, "
        "restart-persistent active teammate message response tasks. This schema is an editor "
        "and CI aid; Conductor's Python validators remain authoritative for cross-field checks."
    )
    schema["required"].extend(["max_message_tasks", "message_tasks_added"])
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_STATE_SCHEMA_V5}
    properties["max_message_tasks"] = _int_range(0, MAX_AGENT_TEAM_MESSAGE_TASKS)
    properties["message_tasks_added"] = _int_range(0, MAX_AGENT_TEAM_MESSAGE_TASKS)
    task_schema = properties["tasks"]["items"]
    task_schema["required"].extend(["message_id", "message_depth"])
    task_schema["properties"]["origin"] = {
        "enum": ["static", "proposed", "operator", "message"]
    }
    task_schema["properties"]["message_id"] = {
        "oneOf": [_safe_id(), {"type": "null"}]
    }
    task_schema["properties"]["message_depth"] = {
        "oneOf": [_int_range(1, MAX_AGENT_TEAM_MESSAGE_DEPTH), {"type": "null"}]
    }
    return schema


def _agent_team_state_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_state_v5_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_STATE_SCHEMA
    schema["title"] = "Conductor Agent Team State"
    schema["description"] = (
        "JSON Schema for resumable parent-mediated agent team state with bounded active messaging "
        "and teammate-to-operator question/reply tasks. This schema is an editor and CI aid; "
        "Conductor's Python validators remain authoritative for cross-field checks."
    )
    schema["required"].extend(
        ["max_operator_questions", "operator_questions_added"]
    )
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_STATE_SCHEMA}
    properties["max_operator_questions"] = _int_range(
        0,
        MAX_AGENT_TEAM_OPERATOR_QUESTIONS,
    )
    properties["operator_questions_added"] = _int_range(
        0,
        MAX_AGENT_TEAM_OPERATOR_QUESTIONS,
    )
    task_schema = properties["tasks"]["items"]
    task_schema["required"].append("operator_reply_sha256")
    task_schema["properties"]["origin"] = {
        "enum": ["static", "proposed", "operator", "message", "operator-reply"]
    }
    task_schema["properties"]["status"] = {
        "enum": ["waiting", "pending", "claimed", "completed"]
    }
    task_schema["properties"]["operator_reply_sha256"] = {
        "oneOf": [_sha256(), {"type": "null"}]
    }
    return schema


def _agent_team_operator_inbox_v1_schema() -> Dict:
    nullable_round = {
        "oneOf": [_int_range(0, MAX_AGENT_TEAM_ROUNDS), {"type": "null"}]
    }
    nullable_timestamp = {"oneOf": [_timestamp_string(), {"type": "null"}]}
    schema = _base_schema(
        AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
        "Conductor Agent Team Operator Inbox v1",
        "JSON Schema for bounded operator-authored teammate follow-ups and their delivery status.",
    )
    schema.update(
        {
            "required": [
                "schema",
                "step_id",
                "workflow_fingerprint",
                "next_sequence",
                "entries",
            ],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1},
                "step_id": _safe_id(),
                "workflow_fingerprint": _sha256(),
                "next_sequence": _int_range(1, 10**9),
                "entries": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_OPERATOR_INBOX_ENTRIES,
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "generation",
                            "created_at_utc",
                            "member_id",
                            "instruction",
                            "instruction_sha256",
                            "status",
                            "task_id",
                            "accepted_round",
                            "superseded_at_utc",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
                            "created_at_utc": _timestamp_string(),
                            "member_id": _safe_id(),
                            "instruction": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_AGENT_TEAM_MESSAGE_CHARS,
                            },
                            "instruction_sha256": _sha256(),
                            "status": {"enum": ["pending", "accepted", "superseded"]},
                            "task_id": _safe_id(),
                            "accepted_round": nullable_round,
                            "superseded_at_utc": nullable_timestamp,
                        },
                    },
                },
            },
        }
    )
    return schema


def _agent_team_operator_inbox_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_operator_inbox_v1_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_OPERATOR_INBOX_SCHEMA
    schema["title"] = "Conductor Agent Team Operator Inbox"
    schema["description"] = (
        "JSON Schema for bounded operator-authored teammate follow-ups, optional "
        "current-turn interruption targets, and hash-only delivery status."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_OPERATOR_INBOX_SCHEMA}
    entry = properties["entries"]["items"]
    entry["required"].extend(
        [
            "delivery",
            "interrupt_round",
            "interrupt_task_id",
            "interrupted_at_utc",
        ]
    )
    entry["properties"].update(
        {
            "delivery": {"enum": ["next-turn", "interrupt-current"]},
            "interrupt_round": {
                "oneOf": [
                    _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                    {"type": "null"},
                ]
            },
            "interrupt_task_id": {
                "oneOf": [_safe_id(), {"type": "null"}]
            },
            "interrupted_at_utc": {
                "oneOf": [_timestamp_string(), {"type": "null"}]
            },
        }
    )
    return schema


def _agent_team_operator_chat_schema() -> Dict:
    nullable_text = {
        "oneOf": [
            {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_MESSAGE_CHARS,
            },
            {"type": "null"},
        ]
    }
    nullable_sha = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_timestamp = {"oneOf": [_timestamp_string(), {"type": "null"}]}
    nullable_round = {
        "oneOf": [_int_range(0, MAX_AGENT_TEAM_ROUNDS), {"type": "null"}]
    }
    schema = _base_schema(
        AGENT_TEAM_OPERATOR_CHAT_SCHEMA,
        "Conductor Agent Team Operator Chat",
        "JSON Schema for bounded teammate questions and operator replies with deterministic response tasks.",
    )
    schema.update(
        {
            "required": sorted(AGENT_TEAM_OPERATOR_CHAT_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_OPERATOR_CHAT_SCHEMA},
                "step_id": _safe_id(),
                "workflow_fingerprint": _sha256(),
                "entries": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_OPERATOR_CHAT_ENTRIES,
                    "items": {
                        "type": "object",
                        "required": sorted(AGENT_TEAM_OPERATOR_CHAT_ENTRY_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
                            "message_id": _safe_id(),
                            "member_id": _safe_id(),
                            "source_task_id": _safe_id(),
                            "asked_round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                            "asked_at_utc": _timestamp_string(),
                            "question": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_AGENT_TEAM_MESSAGE_CHARS,
                            },
                            "question_sha256": _sha256(),
                            "status": {
                                "enum": sorted(AGENT_TEAM_OPERATOR_CHAT_STATUSES)
                            },
                            "reply": nullable_text,
                            "reply_sha256": nullable_sha,
                            "answered_at_utc": nullable_timestamp,
                            "response_task_id": _safe_id(),
                            "delivered_round": nullable_round,
                            "superseded_at_utc": nullable_timestamp,
                        },
                    },
                },
            },
        }
    )
    return schema


def _agent_team_transcript_schema() -> Dict:
    nullable_sha = {"oneOf": [_sha256(), {"type": "null"}]}
    schema = _base_schema(
        AGENT_TEAM_TRANSCRIPT_SCHEMA,
        "Conductor Agent Team Transcript",
        "JSON Schema for a private bounded redacted operator view of one teammate provider turn.",
    )
    schema.update(
        {
            "required": sorted(AGENT_TEAM_TRANSCRIPT_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_TRANSCRIPT_SCHEMA},
                "transcript_id": {
                    "type": "string",
                    "pattern": "^transcript-[0-9a-f]{24}$",
                },
                "step_id": _safe_id(),
                "workflow_fingerprint": _sha256(),
                "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
                "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                "member_id": _safe_id(),
                "task_id": _safe_id(),
                "attempt": _int_range(0, MAX_AGENT_TEAM_TRANSCRIPT_ATTEMPT),
                "status": {"enum": sorted(AGENT_TEAM_TRANSCRIPT_STATUSES)},
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "max_events": _int_range(
                    MIN_AGENT_TEAM_TRANSCRIPT_EVENTS,
                    MAX_AGENT_TEAM_TRANSCRIPT_EVENTS,
                ),
                "max_bytes": _int_range(
                    MIN_AGENT_TEAM_TRANSCRIPT_BYTES,
                    MAX_AGENT_TEAM_TRANSCRIPT_BYTES,
                ),
                "provider_lines": _int_range(
                    0,
                    MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES,
                ),
                "ignored_lines": _int_range(
                    0,
                    MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES,
                ),
                "events_dropped": _int_range(
                    0,
                    MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES,
                ),
                "truncated": {"type": "boolean"},
                "session_id_sha256": nullable_sha,
                "output_sha256": nullable_sha,
                "error_class": {
                    "oneOf": [
                        {
                            "type": "string",
                            "pattern": "^[a-zA-Z_][a-zA-Z0-9_.]{0,127}$",
                        },
                        {"type": "null"},
                    ]
                },
                "events": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_TRANSCRIPT_EVENTS,
                    "items": {
                        "type": "object",
                        "required": sorted(AGENT_TEAM_TRANSCRIPT_EVENT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "sequence": _int_range(1, MAX_AGENT_TEAM_TRANSCRIPT_EVENTS),
                            "at_utc": _timestamp_string(),
                            "type": {"enum": sorted(AGENT_TEAM_TRANSCRIPT_EVENT_TYPES)},
                            "item_type": {
                                "oneOf": [
                                    {"enum": sorted(AGENT_TEAM_TRANSCRIPT_ACTIVITY_TYPES)},
                                    {"type": "null"},
                                ]
                            },
                            "status": {
                                "oneOf": [
                                    {"enum": ["started", "completed"]},
                                    {"type": "null"},
                                ]
                            },
                            "text": {
                                "oneOf": [
                                    {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_AGENT_TEAM_TRANSCRIPT_MESSAGE_CHARS,
                                    },
                                    {"type": "null"},
                                ]
                            },
                            "text_sha256": nullable_sha,
                            "truncated": {"type": "boolean"},
                        },
                    },
                },
            },
        }
    )
    return schema


def _agent_team_interruption_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_INTERRUPTION_SCHEMA,
        "Conductor Agent Team Interruption",
        "JSON Schema for one hash-only operator-interrupted teammate turn artifact.",
    )
    schema.update(
        {
            "required": [
                "schema",
                "task_id",
                "status",
                "operator_entry_id",
                "instruction_sha256",
                "session_id_sha256",
            ],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_INTERRUPTION_SCHEMA},
                "task_id": _safe_id(),
                "status": {"const": "interrupted"},
                "operator_entry_id": _safe_id(),
                "instruction_sha256": _sha256(),
                "session_id_sha256": _sha256(),
            },
        }
    )
    return schema


def _agent_team_turn_v1_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_TURN_SCHEMA_V1,
        "Conductor Agent Team Turn v1",
        "JSON Schema for one bounded teammate completion, continuation, message, and claim envelope.",
    )
    schema.update(
        {
            "required": ["schema", "task_id", "status", "summary", "messages", "claim_task_ids"],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_TURN_SCHEMA_V1},
                "task_id": _safe_id(),
                "status": {"enum": ["completed", "continue"]},
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_TEAM_SUMMARY_CHARS,
                },
                "messages": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_MESSAGES_PER_TURN,
                    "items": {
                        "type": "object",
                        "required": ["to", "body"],
                        "additionalProperties": False,
                        "properties": {
                            "to": {"type": "string", "minLength": 1},
                            "body": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_AGENT_TEAM_MESSAGE_CHARS,
                            },
                        },
                    },
                },
                "claim_task_ids": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_CLAIMS_PER_TURN,
                    "uniqueItems": True,
                    "items": _safe_id(),
                },
            },
        }
    )
    return schema


def _agent_team_turn_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_turn_v1_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % AGENT_TEAM_TURN_SCHEMA
    schema["title"] = "Conductor Agent Team Turn"
    schema["description"] = (
        "JSON Schema for one bounded teammate completion, continuation, message, claim, and "
        "lead-only task-proposal envelope. This schema is an editor and CI aid; Conductor's "
        "Python validators remain authoritative for cross-field checks."
    )
    schema["required"].append("task_proposals")
    schema["properties"]["schema"] = {"const": AGENT_TEAM_TURN_SCHEMA}
    schema["properties"]["task_proposals"] = {
        "type": "array",
        "maxItems": MAX_AGENT_TEAM_TASK_PROPOSALS_PER_TURN,
        "items": {
            "type": "object",
            "required": ["id", "description", "assignee", "depends_on"],
            "additionalProperties": False,
            "properties": {
                "id": _safe_id(),
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
                },
                "assignee": {"oneOf": [_safe_id(), {"type": "null"}]},
                "depends_on": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_TASKS,
                    "uniqueItems": True,
                    "items": _safe_id(),
                },
            },
        },
    }
    return schema


def _agent_team_plan_schema() -> Dict:
    return _strict_schema(
        AGENT_TEAM_PLAN_SCHEMA,
        "Conductor Agent Team Plan",
        "One bounded read-only teammate plan submitted for autonomous lead review.",
        {
            "schema": {"const": AGENT_TEAM_PLAN_SCHEMA},
            "task_id": _safe_id(),
            "revision": _int_range(1, MAX_AGENT_TEAM_PLAN_REVISIONS + 1),
            "plan": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_SUMMARY_CHARS,
            },
            "risks": {
                "type": "array",
                "maxItems": MAX_AGENT_TEAM_PLAN_ITEMS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_TEAM_PLAN_ITEM_CHARS,
                },
            },
            "verification": {
                "type": "array",
                "maxItems": MAX_AGENT_TEAM_PLAN_ITEMS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_TEAM_PLAN_ITEM_CHARS,
                },
            },
        },
        required=sorted(AGENT_TEAM_PLAN_FIELDS),
    )


def _agent_team_plan_review_schema() -> Dict:
    return _strict_schema(
        AGENT_TEAM_PLAN_REVIEW_SCHEMA,
        "Conductor Agent Team Plan Review",
        "One configured fixed-lead or human-operator approval or rejection of a teammate plan.",
        {
            "schema": {"const": AGENT_TEAM_PLAN_REVIEW_SCHEMA},
            "task_id": _safe_id(),
            "revision": _int_range(1, MAX_AGENT_TEAM_PLAN_REVISIONS + 1),
            "decision": {"enum": ["approve", "reject"]},
            "feedback": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
            },
        },
        required=sorted(AGENT_TEAM_PLAN_REVIEW_FIELDS),
    )


def _agent_team_plan_approval_schema() -> Dict:
    return _agent_team_plan_approval_schema_version(version=3)


def _agent_team_plan_approval_v2_schema() -> Dict:
    return _agent_team_plan_approval_schema_version(version=2)


def _agent_team_plan_approval_v1_schema() -> Dict:
    return _agent_team_plan_approval_schema_version(version=1)


def _agent_team_plan_approval_schema_version(*, version: int) -> Dict:
    current = version == 3
    modern = version >= 2
    schema_id = (
        AGENT_TEAM_PLAN_APPROVAL_SCHEMA
        if current
        else AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2
        if modern
        else AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1
    )
    state_fields = (
        AGENT_TEAM_PLAN_APPROVAL_FIELDS
        if current
        else AGENT_TEAM_PLAN_APPROVAL_FIELDS_V2
        if modern
        else AGENT_TEAM_PLAN_APPROVAL_FIELDS_V1
    )
    attempt_fields = (
        AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS
        if current
        else AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V2
        if modern
        else AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V1
    )
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_text = {"type": ["string", "null"]}
    nullable_timestamp = {"oneOf": [_timestamp_string(), {"type": "null"}]}
    nullable_tokens = _nullable_int(0, 10**12)
    attempt_properties = {
        "revision": _int_range(1, MAX_AGENT_TEAM_PLAN_REVISIONS + 1),
        "plan_output": _relative_path(),
        "plan_output_sha256": _sha256(),
        "review_output": {"oneOf": [_relative_path(), {"type": "null"}]},
        "review_output_sha256": nullable_sha256,
        "decision": {"enum": ["approve", "reject", None]},
        "feedback_sha256": nullable_sha256,
        "planner_session_id_sha256": _sha256(),
        "lead_session_id_sha256": nullable_sha256,
        "plan_max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
        "review_max_tokens": (
            _nullable_int(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS)
            if modern
            else _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS)
        ),
        "plan_started_at_utc": _timestamp_string(),
        "plan_finished_at_utc": _timestamp_string(),
        "review_started_at_utc": nullable_timestamp,
        "review_finished_at_utc": nullable_timestamp,
        "plan_input_tokens": nullable_tokens,
        "plan_output_tokens": nullable_tokens,
        "plan_total_tokens": nullable_tokens,
        "review_input_tokens": nullable_tokens,
        "review_output_tokens": nullable_tokens,
        "review_total_tokens": nullable_tokens,
    }
    if modern:
        attempt_properties.update(
            {
                "reviewer": {"enum": ["lead", "operator"]},
                "operator_decision_id": {"oneOf": [_safe_id(), {"type": "null"}]},
            }
        )
    if current:
        attempt_properties.update(
            {
                "plan_session_mode": {"enum": ["new", "resume"]},
                "plan_base_prompt_sha256": _sha256(),
                "plan_effective_prompt_sha256": _sha256(),
                "plan_lifecycle_context_receipt_sha256": copy.deepcopy(
                    nullable_sha256
                ),
                "review_session_mode": {
                    "enum": ["new", "resume", None]
                },
                "review_base_prompt_sha256": copy.deepcopy(nullable_sha256),
                "review_effective_prompt_sha256": copy.deepcopy(nullable_sha256),
                "review_lifecycle_context_receipt_sha256": copy.deepcopy(
                    nullable_sha256
                ),
            }
        )
    properties = {
        "schema": {"const": schema_id},
        "status": {"enum": sorted(AGENT_TEAM_PLAN_APPROVAL_STATUSES)},
        "step_id": _safe_id(),
        "workflow_fingerprint": _sha256(),
        "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
        "task_id": _safe_id(),
        "member_id": _safe_id(),
        "lead_member_id": _safe_id(),
        "criteria_sha256": _sha256(),
        "max_revisions": _int_range(0, MAX_AGENT_TEAM_PLAN_REVISIONS),
        "plan_max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
        "review_max_tokens": (
            _nullable_int(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS)
            if modern
            else _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS)
        ),
        "revision": _int_range(0, MAX_AGENT_TEAM_PLAN_REVISIONS + 1),
        "planner_session_id": nullable_text,
        "planner_session_id_sha256": nullable_sha256,
        "lead_session_id": nullable_text,
        "lead_session_id_sha256": nullable_sha256,
        "attempts": {
            "type": "array",
            "maxItems": MAX_AGENT_TEAM_PLAN_REVISIONS + 1,
            "items": {
                "type": "object",
                "required": sorted(attempt_fields),
                "additionalProperties": False,
                "properties": attempt_properties,
            },
        },
        "created_at_utc": _timestamp_string(),
        "updated_at_utc": _timestamp_string(),
        "error_class": nullable_text,
    }
    if modern:
        properties.update(
            {
                "reviewer": {"enum": ["lead", "operator"]},
                "operator_reply_timeout_seconds": _nullable_int(
                    1,
                    MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS,
                ),
            }
        )
    return _strict_schema(
        schema_id,
        "Conductor Agent Team Plan Approval",
        (
            "Private lifecycle state for bounded read-only planning and mutually exclusive "
            "fixed-lead or human-operator review."
            if modern
            else "Legacy private lifecycle state for bounded read-only planning and fixed-lead review."
        ),
        properties,
        required=sorted(state_fields),
    )


def _agent_team_hook_input_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_HOOK_INPUT_SCHEMA,
        "Conductor Agent Team Hook Input",
        (
            "JSON Schema for the bounded hash-only input supplied to deterministic agent-team "
            "quality hooks."
        ),
    )
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}
    schema.update(
        {
            "required": sorted(AGENT_TEAM_HOOK_INPUT_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_HOOK_INPUT_SCHEMA},
                "event": {"enum": sorted(AGENT_TEAM_HOOK_EVENTS)},
                "workflow_fingerprint": _sha256(),
                "step_id": _safe_id(),
                "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
                "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                "member_id": _safe_id(),
                "task_id": _safe_id(),
                "event_task_id": _safe_id(),
                "task_origin": {
                    "enum": ["static", "proposed", "operator", "message", "operator-reply"]
                },
                "task_assignee": {"oneOf": [_safe_id(), {"type": "null"}]},
                "task_description_sha256": _sha256(),
                "turn_output_sha256": _sha256(),
                "turn_summary_sha256": _sha256(),
                "turn_status": {"enum": ["completed", "continue"]},
                "workspace_mode": {"enum": ["read-only", "isolated-write"]},
                "workspace_base_sha256": nullable_sha256,
                "workspace_result_sha256": nullable_sha256,
                "changed_file_count": _int_range(0, MAX_STAGED_CHANGES),
                "changed_files": {
                    "type": "array",
                    "maxItems": MAX_AGENT_TEAM_HOOK_CHANGED_FILES,
                    "uniqueItems": True,
                    "items": _relative_path(),
                },
                "changed_files_truncated": {"type": "boolean"},
            },
        }
    )
    return schema


def _agent_lifecycle_hook_input_schema() -> Dict:
    schema = _base_schema(
        AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA,
        "Conductor Agent Lifecycle Hook Input",
        (
            "JSON Schema for content-free provider invocation metadata supplied to "
            "agent lifecycle hooks."
        ),
    )
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_id = {"oneOf": [_safe_id(), {"type": "null"}]}
    nullable_integer = {"type": ["integer", "null"]}
    nullable_non_negative = {
        "oneOf": [_int_range(0), {"type": "null"}],
    }
    nullable_positive = {
        "oneOf": [_int_range(1), {"type": "null"}],
    }
    schema.update(
        {
            "required": sorted(AGENT_LIFECYCLE_HOOK_INPUT_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA},
                "event": {"enum": sorted(AGENT_LIFECYCLE_HOOK_EVENTS)},
                "workflow_fingerprint": _sha256(),
                "invocation_sha256": _sha256(),
                "step_id": _safe_id(),
                "step_kind": {"enum": ["agent_map", "agent_team", "codex_exec"]},
                "scope": {"enum": sorted(AGENT_LIFECYCLE_SCOPES)},
                "agent_profile": nullable_id,
                "member_id": nullable_id,
                "task_id": nullable_id,
                "generation": nullable_non_negative,
                "round": nullable_non_negative,
                "attempt": _int_range(1),
                "packet_index": nullable_positive,
                "packet_generation": nullable_non_negative,
                "quality_retry_index": nullable_non_negative,
                "plan_revision": nullable_positive,
                "model_sha256": nullable_sha256,
                "effort": {"type": ["string", "null"], "maxLength": 32},
                "sandbox": {"enum": ["read-only", "workspace-write"]},
                "max_tokens": nullable_positive,
                "prompt_sha256": _sha256(),
                "session_mode": {"enum": ["new", "resume"]},
                "session_id_sha256": nullable_sha256,
                "status": {"enum": sorted(AGENT_LIFECYCLE_STATUSES)},
                "returncode": nullable_integer,
                "timed_out": {"type": "boolean"},
                "interrupted": {"type": "boolean"},
                "output_sha256": nullable_sha256,
                "provider_stdout_sha256": nullable_sha256,
                "provider_stderr_sha256": nullable_sha256,
                "error_class": {"type": ["string", "null"], "maxLength": 128},
            },
        }
    )
    return schema


def _agent_native_tool_input_schema() -> Dict:
    lifecycle = _agent_lifecycle_hook_input_schema()
    properties = {
        field: copy.deepcopy(lifecycle["properties"][field])
        for field in AGENT_NATIVE_TOOL_INPUT_FIELDS
        if field in lifecycle["properties"]
    }
    properties.update(
        {
            "schema": {"const": AGENT_NATIVE_TOOL_INPUT_SCHEMA},
            "event": {"enum": sorted(AGENT_NATIVE_TOOL_HOOK_EVENTS)},
            "tool_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
            },
            "tool_use_id_sha256": _sha256(),
            "tool_input": {},
            "tool_input_sha256": _sha256(),
        }
    )
    schema = _base_schema(
        AGENT_NATIVE_TOOL_INPUT_SCHEMA,
        "Conductor Agent Native Tool Hook Input",
        (
            "Strict bounded PreToolUse proposal supplied only to one explicitly "
            "configured local verifier; raw tool input is not retained in receipts."
        ),
    )
    schema.update(
        {
            "required": sorted(AGENT_NATIVE_TOOL_INPUT_FIELDS),
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _agent_native_tool_receipt_schema() -> Dict:
    result = {
        "type": "object",
        "required": sorted(AGENT_NATIVE_TOOL_RECEIPT_RESULT_FIELDS),
        "additionalProperties": False,
        "properties": {
            "hook_id": _safe_id(),
            "status": {"enum": ["completed", "failed", "timed_out"]},
            "returncode": {"type": ["integer", "null"]},
            "timed_out": {"type": "boolean"},
        },
    }
    schema = _base_schema(
        AGENT_NATIVE_TOOL_RECEIPT_SCHEMA,
        "Conductor Agent Native Tool Receipt",
        (
            "Private hash-only exactly-once receipt for one native Codex "
            "PreToolUse proposal and its ordered verifier outcomes."
        ),
    )
    schema.update(
        {
            "required": sorted(AGENT_NATIVE_TOOL_RECEIPT_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_NATIVE_TOOL_RECEIPT_SCHEMA},
                "status": {"enum": ["running", "passed", "denied"]},
                "workflow_fingerprint": _sha256(),
                "invocation_sha256": _sha256(),
                "step_id": _safe_id(),
                "scope": {"enum": sorted(AGENT_LIFECYCLE_SCOPES)},
                "tool_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "tool_use_id_sha256": _sha256(),
                "tool_input_sha256": _sha256(),
                "hook_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "uniqueItems": True,
                    "items": _safe_id(),
                },
                "hook_failure_modes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": {"enum": ["block", "warn"]},
                },
                "hook_program_sha256s": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": _sha256(),
                },
                "next_hook_index": _int_range(0, MAX_AGENT_NATIVE_TOOL_HOOKS),
                "active_hook_index": {
                    "oneOf": [
                        _int_range(0, MAX_AGENT_NATIVE_TOOL_HOOKS - 1),
                        {"type": "null"},
                    ]
                },
                "hook_results": {
                    "type": "array",
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": result,
                },
                "warning_count": _int_range(0, MAX_AGENT_NATIVE_TOOL_HOOKS),
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "error_class": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 128,
                },
                "receipt_sha256": _sha256(),
            },
        }
    )
    return schema


def _agent_native_tool_gate_schema() -> Dict:
    lifecycle = _agent_lifecycle_hook_input_schema()
    for key in ("$schema", "$id", "title", "description"):
        lifecycle.pop(key, None)
    schema = _base_schema(
        AGENT_NATIVE_TOOL_GATE_SCHEMA,
        "Conductor Agent Native Tool Gate",
        (
            "Crash-safe private invocation journal binding one trusted native "
            "PreToolUse bridge, immutable verifier snapshots, and terminal receipts."
        ),
    )
    schema.update(
        {
            "required": sorted(AGENT_NATIVE_TOOL_GATE_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_NATIVE_TOOL_GATE_SCHEMA},
                "status": {"enum": ["prepared", "settled", "failed"]},
                "start_input": lifecycle,
                "start_input_sha256": _sha256(),
                "workflow_fingerprint": _sha256(),
                "invocation_sha256": _sha256(),
                "step_id": _safe_id(),
                "scope": {"enum": sorted(AGENT_LIFECYCLE_SCOPES)},
                "hook_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "uniqueItems": True,
                    "items": _safe_id(),
                },
                "hook_failure_modes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": {"enum": ["block", "warn"]},
                },
                "hook_tools": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_AGENT_NATIVE_TOOL_NAMES,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                        },
                    },
                },
                "hook_program_sha256s": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": _sha256(),
                },
                "hook_timeouts": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_NATIVE_TOOL_HOOKS,
                    "items": _int_range(1, MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS),
                },
                "hook_set_sha256": _sha256(),
                "bridge_sha256": _sha256(),
                "config_sha256": _sha256(),
                "command_policy_sha256": {
                    "anyOf": [_sha256(), {"type": "null"}],
                },
                "receipt_count": _int_range(0, MAX_AGENT_NATIVE_TOOL_RECEIPTS),
                "receipt_sha256s": {
                    "type": "array",
                    "maxItems": MAX_AGENT_NATIVE_TOOL_RECEIPTS,
                    "uniqueItems": True,
                    "items": _sha256(),
                },
                "passed_count": _int_range(0, MAX_AGENT_NATIVE_TOOL_RECEIPTS),
                "denied_count": _int_range(0, MAX_AGENT_NATIVE_TOOL_RECEIPTS),
                "warning_count": _int_range(
                    0,
                    MAX_AGENT_NATIVE_TOOL_RECEIPTS * MAX_AGENT_NATIVE_TOOL_HOOKS,
                ),
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "error_class": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 128,
                },
                "gate_sha256": _sha256(),
            },
        }
    )
    return schema


def _agent_lifecycle_context_schema() -> Dict:
    return _agent_lifecycle_context_schema_version(current=True)


def _agent_lifecycle_context_v1_schema() -> Dict:
    return _agent_lifecycle_context_schema_version(current=False)


def _agent_lifecycle_context_schema_version(*, current: bool) -> Dict:
    schema_id = (
        AGENT_LIFECYCLE_CONTEXT_SCHEMA
        if current
        else AGENT_LIFECYCLE_CONTEXT_SCHEMA_V1
    )
    fields = (
        AGENT_LIFECYCLE_CONTEXT_FIELDS
        if current
        else AGENT_LIFECYCLE_CONTEXT_FIELDS_V1
    )
    scopes = (
        AGENT_LIFECYCLE_CONTEXT_SCOPES
        if current
        else AGENT_LIFECYCLE_CONTEXT_SCOPES_V1
    )
    schema = _base_schema(
        schema_id,
        "Conductor Agent Lifecycle Context Receipt",
        (
            "Hash-bound private receipt for bounded untrusted context injected by "
            "agent_start hooks before a provider launch."
            if current
            else "Hash-bound private receipt for bounded untrusted context injected by "
            "agent_start hooks before a fresh direct or map provider launch."
        ),
    )
    nullable_non_negative = {
        "oneOf": [_int_range(0), {"type": "null"}],
    }
    nullable_positive = {
        "oneOf": [_int_range(1), {"type": "null"}],
    }
    schema.update(
        {
            "required": sorted(fields),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": schema_id},
                "workflow_fingerprint": _sha256(),
                "step_id": _safe_id(),
                "scope": {"enum": sorted(scopes)},
                "invocation_sha256": _sha256(),
                "attempt": _int_range(1),
                "packet_index": nullable_positive,
                "packet_generation": nullable_non_negative,
                "base_prompt_sha256": _sha256(),
                "context_sha256": _sha256(),
                "context_bytes": _int_range(1, MAX_AGENT_LIFECYCLE_CONTEXT_BYTES),
                "effective_prompt_sha256": _sha256(),
                "hook_count": _int_range(1),
                "context_hook_count": _int_range(1),
                "hook_set_sha256": _sha256(),
                "hook_input_sha256": _sha256(),
                "context_log": _relative_path(),
                "created_at_utc": _timestamp_string(),
            },
        }
    )
    if current:
        schema["properties"].update(
            {
                "session_mode": {"enum": ["new", "resume"]},
                "session_id_sha256": {"oneOf": [_sha256(), {"type": "null"}]},
                "member_id": {"oneOf": [_safe_id(), {"type": "null"}]},
                "task_id": {"oneOf": [_safe_id(), {"type": "null"}]},
                "generation": nullable_non_negative,
                "round": copy.deepcopy(nullable_non_negative),
                "quality_retry_index": copy.deepcopy(nullable_non_negative),
                "plan_revision": nullable_positive,
            }
        )
    return schema


def _agent_lifecycle_stop_gate_schema() -> Dict:
    schema = _base_schema(
        AGENT_LIFECYCLE_STOP_GATE_SCHEMA,
        "Conductor Agent Lifecycle Stop Gate",
        (
            "Crash-safe private journal for ordered blocking agent_stop hook "
            "acceptance after a provider result."
        ),
    )
    lifecycle_input = _agent_lifecycle_hook_input_schema()
    for key in ("$schema", "$id", "title", "description"):
        lifecycle_input.pop(key, None)
    result = {
        "type": "object",
        "required": sorted(AGENT_LIFECYCLE_STOP_GATE_RESULT_FIELDS),
        "additionalProperties": False,
        "properties": {
            "hook_id": _safe_id(),
            "status": {
                "enum": sorted(AGENT_LIFECYCLE_STOP_GATE_RESULT_STATUSES)
            },
            "stdout_sha256": _sha256(),
            "stdout_truncated": {"type": "boolean"},
        },
    }
    schema.update(
        {
            "required": sorted(AGENT_LIFECYCLE_STOP_GATE_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_LIFECYCLE_STOP_GATE_SCHEMA},
                "status": {"enum": sorted(AGENT_LIFECYCLE_STOP_GATE_STATUSES)},
                "workflow_fingerprint": _sha256(),
                "step_id": _safe_id(),
                "scope": {"enum": sorted(AGENT_LIFECYCLE_SCOPES)},
                "invocation_sha256": _sha256(),
                "start_input": lifecycle_input,
                "start_input_sha256": _sha256(),
                "hook_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
                    "uniqueItems": True,
                    "items": _safe_id(),
                },
                "hook_failure_modes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
                    "items": {"enum": ["block", "warn"]},
                },
                "hook_set_sha256": _sha256(),
                "hook_count": _int_range(
                    1,
                    MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
                ),
                "stop_input": {
                    "oneOf": [copy.deepcopy(lifecycle_input), {"type": "null"}]
                },
                "stop_input_sha256": {
                    "oneOf": [_sha256(), {"type": "null"}]
                },
                "next_hook_index": _int_range(
                    0,
                    MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
                ),
                "active_hook_index": {
                    "oneOf": [
                        _int_range(0, MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS - 1),
                        {"type": "null"},
                    ]
                },
                "hook_results": {
                    "type": "array",
                    "maxItems": MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
                    "items": result,
                },
                "warning_count": _int_range(
                    0,
                    MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS,
                ),
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "error_class": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 128,
                },
                "gate_sha256": _sha256(),
            },
        }
    )
    return schema


def _agent_team_quality_retry_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_QUALITY_RETRY_SCHEMA,
        "Conductor Agent Team Quality Retry",
        "JSON Schema for one bounded same-session agent-team quality retry checkpoint.",
    )
    nullable_path = {"oneOf": [_relative_path(), {"type": "null"}]}
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_timestamp = {"oneOf": [_timestamp_string(), {"type": "null"}]}
    nullable_error = {
        "oneOf": [
            {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "pattern": r"^[a-zA-Z][a-zA-Z0-9_.-]*$",
            },
            {"type": "null"},
        ]
    }
    schema.update(
        {
            "required": sorted(AGENT_TEAM_QUALITY_RETRY_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_QUALITY_RETRY_SCHEMA},
                "status": {"enum": sorted(AGENT_TEAM_QUALITY_RETRY_STATUSES)},
                "step_id": _safe_id(),
                "workflow_fingerprint": _sha256(),
                "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
                "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
                "member_id": _safe_id(),
                "task_id": _safe_id(),
                "event_task_id": _safe_id(),
                "event": {"enum": sorted(AGENT_TEAM_HOOK_EVENTS)},
                "hook_id": _safe_id(),
                "hook_input_sha256": _sha256(),
                "retry_index": _int_range(1, MAX_AGENT_TEAM_QUALITY_RETRIES),
                "max_retries": _int_range(1, MAX_AGENT_TEAM_QUALITY_RETRIES),
                "hook_retry_index": _int_range(1, MAX_AGENT_TEAM_QUALITY_RETRIES),
                "hook_max_retries": _int_range(1, MAX_AGENT_TEAM_QUALITY_RETRIES),
                "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                "session_id_sha256": _sha256(),
                "feedback_sha256": _sha256(),
                "workspace_mode": {"enum": ["read-only", "isolated-write"]},
                "workspace_fingerprint_sha256": _sha256(),
                "rejected_output": _relative_path(),
                "rejected_output_sha256": _sha256(),
                "retry_output": nullable_path,
                "retry_output_sha256": nullable_sha256,
                "started_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "finished_at_utc": nullable_timestamp,
                "error_class": nullable_error,
            },
        }
    )
    return schema


def _agent_team_merge_ledger_schema() -> Dict:
    path_list = {
        "type": "array",
        "maxItems": MAX_STAGED_CHANGES,
        "uniqueItems": True,
        "items": _relative_path(),
    }
    event = {
        "type": "object",
        "required": [
            "sequence",
            "kind",
            "round",
            "member_id",
            "task_id",
            "turn_output_sha256",
            "status",
            "workspace_base_sha256",
            "workspace_result_sha256",
            "source_before_sha256",
            "source_after_sha256",
            "delta_sha256",
            "plan_sha256",
            "changed_files",
            "applied_files",
            "deduplicated_files",
            "conflicting_files",
        ],
        "additionalProperties": False,
        "properties": {
            "sequence": _int_range(1, MAX_AGENT_TEAM_MERGE_EVENTS),
            "kind": {"enum": ["merge", "conflict"]},
            "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
            "member_id": _safe_id(),
            "task_id": _safe_id(),
            "turn_output_sha256": _sha256(),
            "status": {"enum": ["applied", "deduplicated", "mixed", "no-change", "conflict"]},
            "workspace_base_sha256": _sha256(),
            "workspace_result_sha256": _sha256(),
            "source_before_sha256": _sha256(),
            "source_after_sha256": _sha256(),
            "delta_sha256": _sha256(),
            "plan_sha256": _sha256(),
            "changed_files": copy.deepcopy(path_list),
            "applied_files": copy.deepcopy(path_list),
            "deduplicated_files": copy.deepcopy(path_list),
            "conflicting_files": copy.deepcopy(path_list),
        },
    }
    schema = _base_schema(
        AGENT_TEAM_MERGE_LEDGER_SCHEMA,
        "Conductor Agent Team Merge Ledger",
        (
            "Hash-bound ordered merge and conflict evidence for an isolated write-capable agent team. "
            "Runtime validators enforce source chains and accepted-turn bindings beyond this editor schema."
        ),
    )
    schema.update(
        {
        "type": "object",
        "required": [
            "schema",
            "step_id",
            "workflow_fingerprint",
            "generation",
            "strategy",
            "source_initial_sha256",
            "source_current_sha256",
            "event_count",
            "conflict_count",
            "created_at_utc",
            "updated_at_utc",
            "events",
        ],
        "additionalProperties": False,
        "properties": {
            "schema": {"const": AGENT_TEAM_MERGE_LEDGER_SCHEMA},
            "step_id": _safe_id(),
            "workflow_fingerprint": _sha256(),
            "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
            "strategy": {"const": WORKSPACE_MERGE_STRATEGY},
            "source_initial_sha256": _sha256(),
            "source_current_sha256": _sha256(),
            "event_count": _int_range(0, MAX_AGENT_TEAM_MERGE_EVENTS),
            "conflict_count": _int_range(0, MAX_AGENT_TEAM_MERGE_EVENTS),
            "created_at_utc": _timestamp_string(),
            "updated_at_utc": _timestamp_string(),
            "events": {
                "type": "array",
                "maxItems": MAX_AGENT_TEAM_MERGE_EVENTS,
                "items": event,
            },
        },
        }
    )
    return schema


def _workspace_snapshot_manifest_object_schema() -> Dict:
    return {
        "type": "object",
        "required": [
            "schema",
            "tracked_fingerprint_sha256",
            "file_count",
            "total_bytes",
            "entries",
        ],
        "additionalProperties": False,
        "properties": {
            "schema": {"const": WORKSPACE_SNAPSHOT_MANIFEST_SCHEMA},
            "tracked_fingerprint_sha256": _sha256(),
            "file_count": _int_range(0, MAX_STAGED_FILES),
            "total_bytes": _int_range(0, MAX_STAGED_TOTAL_BYTES),
            "entries": {
                "type": "array",
                "maxItems": MAX_STAGED_FILES,
                "items": {
                    "type": "object",
                    "required": ["path", "sha256", "size", "executable"],
                    "additionalProperties": False,
                    "properties": {
                        "path": _relative_path(),
                        "sha256": _sha256(),
                        "size": _int_range(0, MAX_STAGED_FILE_BYTES),
                        "executable": {"type": "boolean"},
                    },
                },
            },
        },
    }


def _workspace_merge_plan_object_schema() -> Dict:
    nullable_record = {
        "oneOf": [
            {
                "type": "object",
                "required": ["sha256", "size", "executable"],
                "additionalProperties": False,
                "properties": {
                    "sha256": _sha256(),
                    "size": _int_range(0, MAX_STAGED_FILE_BYTES),
                    "executable": {"type": "boolean"},
                },
            },
            {"type": "null"},
        ]
    }
    path_list = {
        "type": "array",
        "maxItems": MAX_STAGED_CHANGES,
        "uniqueItems": True,
        "items": _relative_path(),
    }
    return {
        "type": "object",
        "required": [
            "strategy",
            "status",
            "workspace_base_sha256",
            "workspace_result_sha256",
            "source_before_sha256",
            "source_after_sha256",
            "delta_sha256",
            "changed_files",
            "apply_files",
            "deduplicated_files",
            "conflicting_files",
            "records",
            "plan_sha256",
        ],
        "additionalProperties": False,
        "properties": {
            "strategy": {"const": WORKSPACE_MERGE_STRATEGY},
            "status": {"enum": ["applied", "deduplicated", "mixed", "no-change", "conflict"]},
            "workspace_base_sha256": _sha256(),
            "workspace_result_sha256": _sha256(),
            "source_before_sha256": _sha256(),
            "source_after_sha256": _sha256(),
            "delta_sha256": _sha256(),
            "changed_files": copy.deepcopy(path_list),
            "apply_files": copy.deepcopy(path_list),
            "deduplicated_files": copy.deepcopy(path_list),
            "conflicting_files": copy.deepcopy(path_list),
            "records": {
                "type": "array",
                "maxItems": MAX_STAGED_CHANGES,
                "items": {
                    "type": "object",
                    "required": ["path", "operation", "base", "result"],
                    "additionalProperties": False,
                    "properties": {
                        "path": _relative_path(),
                        "operation": {"enum": ["add", "modify", "delete"]},
                        "base": copy.deepcopy(nullable_record),
                        "result": copy.deepcopy(nullable_record),
                    },
                },
            },
            "plan_sha256": _sha256(),
        },
    }


def _direct_workspace_transaction_schema() -> Dict:
    schema = _base_schema(
        DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
        "Conductor Direct Workspace Transaction",
        (
            "Crash-safe private journal for isolated direct workspace-write "
            "acceptance behind a blocking agent_stop gate."
        ),
    )
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_manifest = {
        "oneOf": [
            _workspace_snapshot_manifest_object_schema(),
            {"type": "null"},
        ]
    }
    nullable_plan = {
        "oneOf": [_workspace_merge_plan_object_schema(), {"type": "null"}]
    }
    nullable_error = {
        "oneOf": [
            {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": r"^[a-zA-Z_][a-zA-Z0-9_.]*$",
            },
            {"type": "null"},
        ]
    }
    schema.update(
        {
            "required": sorted(DIRECT_WORKSPACE_TRANSACTION_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": DIRECT_WORKSPACE_TRANSACTION_SCHEMA},
                "status": {
                    "enum": sorted(DIRECT_WORKSPACE_TRANSACTION_STATUSES)
                },
                "workflow_fingerprint": _sha256(),
                "step_id": _safe_id(),
                "attempt": _int_range(1, MAX_DIRECT_WORKSPACE_ATTEMPT),
                "source_workspace_sha256": _sha256(),
                "stage_relative": _relative_path(),
                "base_manifest": _workspace_snapshot_manifest_object_schema(),
                "result_manifest": copy.deepcopy(nullable_manifest),
                "source_before_manifest": copy.deepcopy(nullable_manifest),
                "merge_plan": nullable_plan,
                "invocation_sha256": copy.deepcopy(nullable_sha256),
                "terminal_sha256": copy.deepcopy(nullable_sha256),
                "stop_gate_sha256": copy.deepcopy(nullable_sha256),
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "error_class": nullable_error,
                "transaction_sha256": _sha256(),
            },
        }
    )
    return schema


def _staged_apply_schema() -> Dict:
    schema = _base_schema(
        STAGED_APPLY_SCHEMA,
        "Conductor Verified Stage Apply Receipt",
        (
            "Hash-bound, crash-recoverable transaction evidence for an explicit local apply "
            "of a retained verifier-passing stage. Approval values are never persisted."
        ),
    )
    change_path_list = {
        "type": "array",
        "maxItems": MAX_STAGED_CHANGES,
        "uniqueItems": True,
        "items": _relative_path(),
    }
    excluded_path_list = {
        "type": "array",
        "maxItems": MAX_STAGED_FILES,
        "uniqueItems": True,
        "items": _relative_path(),
    }
    artifact_name = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_STAGED_APPLY_NAME_CHARS,
        "pattern": r"^[^/\\\r\n]+$",
        "not": {"enum": [".", ".."]},
    }
    schema.update(
        {
            "required": sorted(STAGED_APPLY_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": STAGED_APPLY_SCHEMA},
                "status": {"enum": sorted(STAGED_APPLY_STATUSES)},
                "evidence_kind": {"enum": sorted(STAGED_APPLY_EVIDENCE_KINDS)},
                "evidence_schema": {
                    "enum": [BENCHMARK_REPORT_SCHEMA, CODEX_STAGED_REPAIR_SCHEMA]
                },
                "evidence_path_sha256": _sha256(),
                "evidence_sha256": _sha256(),
                "workspace_path_sha256": _sha256(),
                "stage_path_sha256": _sha256(),
                "stage_directory_name": copy.deepcopy(artifact_name),
                "patch_path_sha256": _sha256(),
                "patch_name": copy.deepcopy(artifact_name),
                "patch_sha256": _sha256(),
                "patch_bytes": _int_range(1, MAX_STAGED_PATCH_BYTES),
                "provider_status": {
                    "enum": ["success", "budget-exceeded", "token-budget-exceeded"]
                },
                "staged_status": {"const": "success"},
                "verification_status": {"const": "passed"},
                "change_count": _int_range(1, MAX_STAGED_CHANGES),
                "added": copy.deepcopy(change_path_list),
                "modified": copy.deepcopy(change_path_list),
                "deleted": copy.deepcopy(change_path_list),
                "destructive": {"type": "boolean"},
                "policy": {
                    "type": "object",
                    "required": sorted(STAGED_APPLY_POLICY_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "automatic_apply": {"const": False},
                        "explicit_apply": {"const": True},
                        "approval_values_persisted": {"const": False},
                        "provider_calls": {"const": 0},
                        "verifier_calls": {"const": 0},
                    },
                },
                "source_before_manifest": _workspace_snapshot_manifest_object_schema(),
                "source_before_fingerprint_sha256": _sha256(),
                "source_excluded_directories": copy.deepcopy(excluded_path_list),
                "stage_manifest": _workspace_snapshot_manifest_object_schema(),
                "stage_fingerprint_sha256": _sha256(),
                "stage_excluded_directories": copy.deepcopy(excluded_path_list),
                "merge_plan": _workspace_merge_plan_object_schema(),
                "source_after_fingerprint_sha256": {
                    "oneOf": [_sha256(), {"type": "null"}]
                },
                "receipt_path_sha256": _sha256(),
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "receipt_sha256": _sha256(),
            },
        }
    )
    return schema


def _embedded_schema(builder) -> Dict:
    schema = builder()
    for field in ("$schema", "$id", "title", "description"):
        schema.pop(field, None)
    return schema


def _agent_team_merge_transaction_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_MERGE_TRANSACTION_SCHEMA,
        "Conductor Agent Team Merge Transaction",
        "Prepared exactly-once source merge with candidate state and ledger checkpoints.",
    )
    sha_fields = {
        field: _sha256()
        for field in (
            "workflow_fingerprint",
            "workspace_result_sha256",
            "turn_output_sha256",
            "source_before_sha256",
            "source_after_sha256",
            "state_before_sha256",
            "ledger_before_sha256",
            "candidate_state_sha256",
            "candidate_ledger_sha256",
            "transaction_sha256",
        )
    }
    properties = {
        "schema": {"const": AGENT_TEAM_MERGE_TRANSACTION_SCHEMA},
        "step_id": _safe_id(),
        "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
        "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
        "member_id": _safe_id(),
        "task_id": _safe_id(),
        "created_at_utc": _timestamp_string(),
        "workspace_relative": _relative_path(),
        "source_before_manifest": _workspace_snapshot_manifest_object_schema(),
        "merge_plan": _workspace_merge_plan_object_schema(),
        "candidate_state": {
            "oneOf": [
                _embedded_schema(_agent_team_state_schema),
                _embedded_schema(_agent_team_state_v5_schema),
                _embedded_schema(_agent_team_state_v4_schema),
                _embedded_schema(_agent_team_state_v3_schema),
                _embedded_schema(_agent_team_state_v2_schema),
                _embedded_schema(_agent_team_state_v1_schema),
            ]
        },
        "candidate_ledger": _embedded_schema(_agent_team_merge_ledger_schema),
    }
    properties.update(sha_fields)
    required = [
        "schema",
        "step_id",
        "workflow_fingerprint",
        "generation",
        "round",
        "member_id",
        "task_id",
        "created_at_utc",
        "workspace_relative",
        "workspace_result_sha256",
        "turn_output_sha256",
        "source_before_sha256",
        "source_after_sha256",
        "state_before_sha256",
        "ledger_before_sha256",
        "candidate_state_sha256",
        "candidate_ledger_sha256",
        "source_before_manifest",
        "merge_plan",
        "candidate_state",
        "candidate_ledger",
        "transaction_sha256",
    ]
    schema.update(
        {
            "type": "object",
            "required": required,
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _agent_team_merge_intent_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_MERGE_INTENT_SCHEMA,
        "Conductor Agent Team Merge Intent",
        (
            "Private accepted-turn checkpoint written before merge planning. Runtime validation "
            "binds the retained output, isolated workspace, pre-merge state, and candidate state."
        ),
    )
    sha_fields = {
        field: _sha256()
        for field in (
            "workflow_fingerprint",
            "workspace_base_sha256",
            "workspace_result_sha256",
            "turn_output_sha256",
            "source_expected_sha256",
            "state_before_sha256",
            "ledger_before_sha256",
            "candidate_state_sha256",
            "intent_sha256",
        )
    }
    properties = {
        "schema": {"const": AGENT_TEAM_MERGE_INTENT_SCHEMA},
        "step_id": _safe_id(),
        "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
        "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
        "member_id": _safe_id(),
        "task_id": _safe_id(),
        "created_at_utc": _timestamp_string(),
        "workspace_relative": _relative_path(),
        "turn_output": _relative_path(),
        "workspace_base_manifest": _workspace_snapshot_manifest_object_schema(),
        "state_before": {
            "oneOf": [
                _embedded_schema(_agent_team_state_schema),
                _embedded_schema(_agent_team_state_v5_schema),
                _embedded_schema(_agent_team_state_v4_schema),
                _embedded_schema(_agent_team_state_v3_schema),
                _embedded_schema(_agent_team_state_v2_schema),
                _embedded_schema(_agent_team_state_v1_schema),
            ]
        },
        "ledger_before": _embedded_schema(_agent_team_merge_ledger_schema),
        "candidate_state": {
            "oneOf": [
                _embedded_schema(_agent_team_state_schema),
                _embedded_schema(_agent_team_state_v5_schema),
                _embedded_schema(_agent_team_state_v4_schema),
                _embedded_schema(_agent_team_state_v3_schema),
                _embedded_schema(_agent_team_state_v2_schema),
                _embedded_schema(_agent_team_state_v1_schema),
            ]
        },
    }
    properties.update(sha_fields)
    schema.update(
        {
            "type": "object",
            "required": [
                "schema",
                "step_id",
                "workflow_fingerprint",
                "generation",
                "round",
                "member_id",
                "task_id",
                "created_at_utc",
                "workspace_relative",
                "workspace_base_sha256",
                "workspace_result_sha256",
                "turn_output",
                "turn_output_sha256",
                "source_expected_sha256",
                "state_before_sha256",
                "ledger_before_sha256",
                "candidate_state_sha256",
                "workspace_base_manifest",
                "state_before",
                "ledger_before",
                "candidate_state",
                "intent_sha256",
            ],
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _agent_team_turn_completion_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_TURN_COMPLETION_SCHEMA,
        "Conductor Agent Team Turn Completion",
        (
            "Private provider-completed checkpoint written before parent quality and merge "
            "acceptance. Runtime validation binds the output, session, telemetry, launch state, "
            "and optional isolated workspace."
        ),
    )
    nullable_sha = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_path = {"oneOf": [_relative_path(), {"type": "null"}]}
    telemetry_event_properties = {
        "event": {"type": "string", "minLength": 1, "maxLength": 300},
        "provider": {"type": "string", "minLength": 1, "maxLength": 300},
        "status": {"type": "string", "minLength": 1, "maxLength": 300},
        "session_id": {"type": "string", "minLength": 1, "maxLength": 300},
        "model": {"type": "string", "minLength": 1, "maxLength": 300},
        "input_tokens": _int_range(0, MAX_CODEX_TOKENS),
        "output_tokens": _int_range(0, MAX_CODEX_TOKENS),
        "total_tokens": _int_range(0, MAX_CODEX_TOKENS),
        "cost_usd": {"type": "number", "minimum": 0},
    }
    telemetry_properties = {
        "events": {
            "type": "array",
            "maxItems": MAX_PROVIDER_EVENTS,
            "items": {
                "type": "object",
                "required": ["event", "provider"],
                "additionalProperties": False,
                "properties": telemetry_event_properties,
            },
        },
        "input_tokens": _nullable_int(0, MAX_CODEX_TOKENS),
        "output_tokens": _nullable_int(0, MAX_CODEX_TOKENS),
        "total_tokens": _nullable_int(0, MAX_CODEX_TOKENS),
        "cost_usd": {
            "oneOf": [
                {"type": "number", "minimum": 0},
                {"type": "null"},
            ]
        },
    }
    properties = {
        "schema": {"const": AGENT_TEAM_TURN_COMPLETION_SCHEMA},
        "step_id": _safe_id(),
        "workflow_fingerprint": _sha256(),
        "generation": _int_range(0, MAX_AGENT_TEAM_GENERATION),
        "round": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
        "member_id": _safe_id(),
        "task_id": _safe_id(),
        "quality_retry_index": _int_range(0, MAX_AGENT_TEAM_QUALITY_RETRIES),
        "created_at_utc": _timestamp_string(),
        "started_at_utc": _timestamp_string(),
        "finished_at_utc": _timestamp_string(),
        "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
        "output": _relative_path(),
        "output_sha256": _sha256(),
        "session_id": {"type": "string", "format": "uuid"},
        "session_id_sha256": _sha256(),
        "session_mode": {"enum": ["new", "resume"]},
        "base_prompt_sha256": _sha256(),
        "effective_prompt_sha256": _sha256(),
        "lifecycle_context_receipt_sha256": copy.deepcopy(nullable_sha),
        "launch_state_sha256": _sha256(),
        "launch_turn_count": _int_range(0, MAX_AGENT_TEAM_TURNS),
        "launch_task_count": _int_range(1, MAX_AGENT_TEAM_TASKS),
        "launch_message_count": _int_range(0, MAX_AGENT_TEAM_MESSAGES),
        "launch_authorized_tokens": _int_range(
            MIN_CODEX_RUNTIME_TOKEN_CAP,
            MAX_CODEX_TOKENS,
        ),
        "telemetry": {
            "type": "object",
            "required": sorted(telemetry_properties),
            "additionalProperties": False,
            "properties": telemetry_properties,
        },
        "workspace_mode": {"enum": ["read-only", "isolated-write"]},
        "workspace_relative": nullable_path,
        "workspace_base_manifest": {
            "oneOf": [
                _workspace_snapshot_manifest_object_schema(),
                {"type": "null"},
            ]
        },
        "workspace_base_sha256": nullable_sha,
        "workspace_result_sha256": nullable_sha,
        "completion_sha256": _sha256(),
    }
    schema.update(
        {
            "type": "object",
            "required": sorted(properties),
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _agent_team_turn_completion_v1_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_turn_completion_schema())
    schema["$id"] = (
        "https://codex-conductor.local/schemas/%s.schema.json"
        % AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1
    )
    schema["title"] = "Conductor Agent Team Turn Completion v1"
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1}
    for field in (
        "session_mode",
        "base_prompt_sha256",
        "effective_prompt_sha256",
        "lifecycle_context_receipt_sha256",
    ):
        properties.pop(field)
    schema["required"] = sorted(properties)
    return schema


def _agent_team_turn_terminal_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_turn_completion_schema())
    schema["$id"] = (
        "https://codex-conductor.local/schemas/%s.schema.json"
        % AGENT_TEAM_TURN_TERMINAL_SCHEMA
    )
    schema["title"] = "Conductor Agent Team Turn Terminal"
    schema["description"] = (
        "Private receipt written when a local Codex stream emits a validated terminal event "
        "and exact output/workspace checkpoints exist, before parent process return."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_TURN_TERMINAL_SCHEMA}
    properties.pop("finished_at_utc")
    properties.pop("output_sha256")
    properties.pop("completion_sha256")
    properties["terminal_at_utc"] = _timestamp_string()
    properties["output_raw_sha256"] = _sha256()
    properties["output_redacted_sha256"] = _sha256()
    properties["terminal_sha256"] = _sha256()
    schema["required"] = sorted(properties)
    return schema


def _agent_team_turn_terminal_v1_schema() -> Dict:
    schema = copy.deepcopy(_agent_team_turn_completion_v1_schema())
    schema["$id"] = (
        "https://codex-conductor.local/schemas/%s.schema.json"
        % AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1
    )
    schema["title"] = "Conductor Agent Team Turn Terminal v1"
    properties = schema["properties"]
    properties["schema"] = {"const": AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1}
    properties.pop("finished_at_utc")
    properties.pop("output_sha256")
    properties.pop("completion_sha256")
    properties["terminal_at_utc"] = _timestamp_string()
    properties["output_raw_sha256"] = _sha256()
    properties["output_redacted_sha256"] = _sha256()
    properties["terminal_sha256"] = _sha256()
    schema["required"] = sorted(properties)
    return schema


def _agent_map_packet_terminal_schema() -> Dict:
    telemetry = copy.deepcopy(
        _agent_team_turn_completion_schema()["properties"]["telemetry"]
    )
    telemetry["properties"]["events"]["maxItems"] = (
        MAX_AGENT_MAP_PACKET_TERMINAL_EVENTS
    )
    properties = {
        "schema": {"const": AGENT_MAP_PACKET_TERMINAL_SCHEMA},
        "step_id": _safe_id(),
        "workflow_fingerprint": _sha256(),
        "index": _int_range(1, MAX_AGENT_PACKETS),
        "cache_generation": _int_range(0, MAX_AGENT_MAP_GENERATION),
        "packet_generation": _int_range(0, MAX_AGENT_MAP_GENERATION),
        "launch_pending_count": _int_range(1, MAX_AGENT_PACKETS),
        "source_item_count": _int_range(1, MAX_AGENT_ITEMS_PER_PACKET),
        "created_at_utc": _timestamp_string(),
        "started_at_utc": _timestamp_string(),
        "terminal_at_utc": _timestamp_string(),
        "max_tokens": {
            "oneOf": [
                _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                {"type": "null"},
            ]
        },
        "cache_key": _sha256(),
        "item_sha256": _sha256(),
        "item_source_sha256": _sha256(),
        "prompt_sha256": _sha256(),
        "output": _relative_path(),
        "output_raw_sha256": _sha256(),
        "output_redacted_sha256": _sha256(),
        "session_id": {"type": "string", "format": "uuid"},
        "session_id_sha256": _sha256(),
        "telemetry": telemetry,
        "terminal_sha256": _sha256(),
    }
    schema = _base_schema(
        AGENT_MAP_PACKET_TERMINAL_SCHEMA,
        "Conductor Agent Map Packet Terminal",
        (
            "Private receipt written when a local Codex packet stream emits a validated "
            "terminal event and exact read-only source/output checkpoints exist."
        ),
    )
    schema.update(
        {
            "type": "object",
            "required": sorted(AGENT_MAP_PACKET_TERMINAL_FIELDS),
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _codex_step_terminal_v1_schema() -> Dict:
    schema = copy.deepcopy(_codex_step_terminal_schema())
    schema["$id"] = (
        "https://codex-conductor.local/schemas/%s.schema.json"
        % CODEX_STEP_TERMINAL_SCHEMA_V1
    )
    schema["title"] = "Conductor Codex Step Terminal v1"
    properties = schema["properties"]
    properties["schema"] = {"const": CODEX_STEP_TERMINAL_SCHEMA_V1}
    for field in (
        "invocation_base_prompt_sha256",
        "invocation_effective_prompt_sha256",
        "lifecycle_context_receipt_sha256",
    ):
        properties.pop(field)
    schema["required"] = sorted(CODEX_STEP_TERMINAL_FIELDS_V1)
    return schema


def _codex_step_terminal_schema() -> Dict:
    telemetry = copy.deepcopy(
        _agent_team_turn_completion_schema()["properties"]["telemetry"]
    )
    telemetry["properties"]["events"]["maxItems"] = MAX_CODEX_STEP_TERMINAL_EVENTS
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}

    def nullable_count(maximum):
        return {"oneOf": [_int_range(0, maximum), {"type": "null"}]}

    properties = {
        "schema": {"const": CODEX_STEP_TERMINAL_SCHEMA},
        "step_id": _safe_id(),
        "workflow_fingerprint": _sha256(),
        "created_at_utc": _timestamp_string(),
        "started_at_utc": _timestamp_string(),
        "terminal_at_utc": _timestamp_string(),
        "mode": {"enum": ["started", "resumed"]},
        "resume_count": _int_range(0, MAX_CODEX_STEP_RESUMES),
        "max_tokens": {
            "oneOf": [
                _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                {"type": "null"},
            ]
        },
        "sandbox": {"enum": ["read-only", "workspace-write"]},
        "model": {
            "oneOf": [
                {"type": "string", "minLength": 1, "maxLength": 200},
                {"type": "null"},
            ]
        },
        "effort": {
            "oneOf": [
                {"type": "string", "minLength": 1, "maxLength": 200},
                {"type": "null"},
            ]
        },
        "prompt_sha256": _sha256(),
        "invocation_base_prompt_sha256": _sha256(),
        "invocation_effective_prompt_sha256": _sha256(),
        "lifecycle_context_receipt_sha256": copy.deepcopy(nullable_sha256),
        "output": _relative_path(),
        "output_raw_sha256": _sha256(),
        "output_redacted_sha256": _sha256(),
        "session_id": {"type": "string", "format": "uuid"},
        "session_id_sha256": _sha256(),
        "checkpoint_sha256": _sha256(),
        "telemetry": telemetry,
        "workspace_path_sha256": _sha256(),
        "workspace_mode": {"enum": ["read-only", "direct-write"]},
        "workspace_result_sha256": copy.deepcopy(nullable_sha256),
        "workspace_excluded_sha256": copy.deepcopy(nullable_sha256),
        "workspace_file_count": nullable_count(MAX_STAGED_FILES),
        "workspace_total_bytes": nullable_count(MAX_STAGED_TOTAL_BYTES),
        "terminal_sha256": _sha256(),
    }
    schema = _base_schema(
        CODEX_STEP_TERMINAL_SCHEMA,
        "Conductor Codex Step Terminal",
        (
            "Private receipt written when an ordinary Codex step emits a validated "
            "terminal event and exact checkpoint, output, and optional direct-write "
            "workspace evidence exists."
        ),
    )
    schema.update(
        {
            "type": "object",
            "required": sorted(CODEX_STEP_TERMINAL_FIELDS),
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _codex_progress_schema() -> Dict:
    return _codex_progress_schema_for(
        schema_id=CODEX_PROGRESS_SCHEMA,
        title="Conductor Codex Progress",
        progress_fields=CODEX_PROGRESS_FIELDS,
        item_count_fields=CODEX_PROGRESS_ITEM_COUNT_FIELDS,
        last_events=CODEX_PROGRESS_LAST_EVENTS,
        include_native_agents=True,
        include_native_authority=True,
        include_native_reconciliation=True,
    )


def _codex_progress_v3_schema() -> Dict:
    return _codex_progress_schema_for(
        schema_id=CODEX_PROGRESS_SCHEMA_V3,
        title="Conductor Codex Progress v3",
        progress_fields=CODEX_PROGRESS_FIELDS_V3,
        item_count_fields=CODEX_PROGRESS_ITEM_COUNT_FIELDS,
        last_events=CODEX_PROGRESS_LAST_EVENTS,
        include_native_agents=True,
        include_native_authority=True,
        include_native_reconciliation=False,
    )


def _codex_progress_v2_schema() -> Dict:
    return _codex_progress_schema_for(
        schema_id=CODEX_PROGRESS_SCHEMA_V2,
        title="Conductor Codex Progress v2",
        progress_fields=CODEX_PROGRESS_FIELDS_V2,
        item_count_fields=CODEX_PROGRESS_ITEM_COUNT_FIELDS,
        last_events=CODEX_PROGRESS_LAST_EVENTS,
        include_native_agents=True,
        include_native_authority=False,
        include_native_reconciliation=False,
    )


def _codex_progress_v1_schema() -> Dict:
    return _codex_progress_schema_for(
        schema_id=CODEX_PROGRESS_SCHEMA_V1,
        title="Conductor Codex Progress v1",
        progress_fields=CODEX_PROGRESS_FIELDS_V1,
        item_count_fields=CODEX_PROGRESS_ITEM_COUNT_FIELDS_V1,
        last_events=CODEX_PROGRESS_LAST_EVENTS_V1,
        include_native_agents=False,
        include_native_authority=False,
        include_native_reconciliation=False,
    )


def _codex_progress_schema_for(
    *,
    schema_id: str,
    title: str,
    progress_fields,
    item_count_fields,
    last_events,
    include_native_agents: bool,
    include_native_authority: bool,
    include_native_reconciliation: bool,
) -> Dict:
    nullable_sha256 = {"oneOf": [_sha256(), {"type": "null"}]}
    nullable_tokens = {
        "oneOf": [_int_range(0, MAX_CODEX_TOKENS), {"type": "null"}]
    }
    nullable_text = {
        "oneOf": [
            {"type": "string", "minLength": 1, "maxLength": 200},
            {"type": "null"},
        ]
    }
    properties = {
        "schema": {"const": schema_id},
        "status": {"enum": ["active", "completed", "failed", "timed-out", "interrupted"]},
        "scope": {"enum": ["step", "packet"]},
        "step_id": _safe_id(),
        "packet_index": {
            "oneOf": [
                _int_range(1, MAX_CODEX_PROGRESS_PACKET_INDEX),
                {"type": "null"},
            ]
        },
        "workflow_fingerprint": _sha256(),
        "invocation_id": {"type": "string", "format": "uuid"},
        "attempt": _int_range(1, MAX_CODEX_PROGRESS_ATTEMPTS),
        "started_at_utc": _timestamp_string(),
        "updated_at_utc": _timestamp_string(),
        "finished_at_utc": {
            "oneOf": [_timestamp_string(), {"type": "null"}]
        },
        "sandbox": {"enum": ["read-only", "workspace-write"]},
        "model": copy.deepcopy(nullable_text),
        "effort": copy.deepcopy(nullable_text),
        "max_tokens": {
            "oneOf": [
                _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                {"type": "null"},
            ]
        },
        "session_id_sha256": copy.deepcopy(nullable_sha256),
        "event_count": _int_range(0, MAX_CODEX_PROGRESS_EVENTS),
        "events_truncated": {"type": "boolean"},
        "turn_started_count": _int_range(0, MAX_CODEX_PROGRESS_EVENTS),
        "turn_completed_count": _int_range(0, MAX_CODEX_PROGRESS_EVENTS),
        "item_started_count": _int_range(0, MAX_CODEX_PROGRESS_EVENTS),
        "item_completed_count": _int_range(0, MAX_CODEX_PROGRESS_EVENTS),
        "failed_item_count": _int_range(0, MAX_CODEX_PROGRESS_EVENTS),
        "item_counts": {
            "type": "object",
            "required": sorted(item_count_fields),
            "additionalProperties": False,
            "properties": {
                name: _int_range(0, MAX_CODEX_PROGRESS_EVENTS)
                for name in sorted(item_count_fields)
            },
        },
        "last_event": {"enum": sorted(last_events)},
        "input_tokens": copy.deepcopy(nullable_tokens),
        "output_tokens": copy.deepcopy(nullable_tokens),
        "total_tokens": copy.deepcopy(nullable_tokens),
        "progress_sha256": _sha256(),
    }
    if include_native_agents:
        properties.update(
            {
                "collab_tool_counts": {
                    "type": "object",
                    "required": sorted(CODEX_PROGRESS_COLLAB_TOOL_COUNT_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        name: _int_range(0, MAX_CODEX_PROGRESS_EVENTS)
                        for name in sorted(CODEX_PROGRESS_COLLAB_TOOL_COUNT_FIELDS)
                    },
                },
                "collab_tool_failed_count": _int_range(
                    0,
                    MAX_CODEX_PROGRESS_EVENTS,
                ),
                "native_agent_count": _int_range(
                    0,
                    MAX_CODEX_PROGRESS_NATIVE_AGENTS,
                ),
                "native_agent_status_counts": {
                    "type": "object",
                    "required": sorted(CODEX_PROGRESS_NATIVE_AGENT_STATUS_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        name: _int_range(0, MAX_CODEX_PROGRESS_NATIVE_AGENTS)
                        for name in sorted(CODEX_PROGRESS_NATIVE_AGENT_STATUS_FIELDS)
                    },
                },
                "native_agent_observation_truncated": {"type": "boolean"},
                "native_agent_usage_attributed": (
                    {"type": "boolean"}
                    if include_native_reconciliation
                    else {"const": False}
                ),
                "native_agents_enabled_by_observer": {"const": False},
            }
        )
    if include_native_authority:
        properties.update(
            {
                "native_agents_enabled_by_runner": {"type": "boolean"},
                "native_agent_max_threads": {
                    "oneOf": [
                        _int_range(
                            MIN_CODEX_NATIVE_AGENT_THREADS,
                            MAX_CODEX_NATIVE_AGENT_THREADS,
                        ),
                        {"type": "null"},
                    ]
                },
                "native_agent_max_depth": {
                    "oneOf": [
                        {"const": CODEX_NATIVE_AGENT_MAX_DEPTH},
                        {"type": "null"},
                    ]
                },
            }
        )
    if include_native_reconciliation:
        properties.update(
            {
                "native_agent_usage_status": {
                    "enum": ["complete", "not-requested", "pending", "unavailable"]
                },
                "native_agent_usage_session_count": _int_range(
                    0,
                    MAX_NATIVE_USAGE_SESSIONS,
                ),
                "native_agent_usage_child_count": _int_range(
                    0,
                    MAX_NATIVE_USAGE_SESSIONS - 1,
                ),
                "native_agent_usage_input_tokens": copy.deepcopy(nullable_tokens),
                "native_agent_usage_cached_input_tokens": copy.deepcopy(
                    nullable_tokens
                ),
                "native_agent_usage_output_tokens": copy.deepcopy(nullable_tokens),
                "native_agent_usage_total_tokens": copy.deepcopy(nullable_tokens),
                "native_agent_usage_rollout_tokens": copy.deepcopy(nullable_tokens),
            }
        )
    schema = _base_schema(
        schema_id,
        title,
        (
            "Hash-bound content-free progress counters for one ordinary Codex step "
            "or agent-map packet invocation, with passive native-agent lifecycle "
            "counters in v2."
        ),
    )
    schema.update(
        {
            "type": "object",
            "required": sorted(progress_fields),
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _agent_team_merge_recovery_schema() -> Dict:
    schema = _base_schema(
        AGENT_TEAM_MERGE_RECOVERY_SCHEMA,
        "Conductor Agent Team Merge Recovery",
        "Hash-only evidence that a prepared team merge was reconciled without provider replay.",
    )
    schema.update(
        {
            "type": "object",
            "required": [
                "schema",
                "transaction_sha256",
                "recovered_at_utc",
                "source_before_sha256",
                "source_after_sha256",
                "source_checkpoint",
                "state_checkpoint",
                "ledger_checkpoint",
                "candidate_state_sha256",
                "candidate_ledger_sha256",
                "provider_replayed",
            ],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": AGENT_TEAM_MERGE_RECOVERY_SCHEMA},
                "transaction_sha256": _sha256(),
                "recovered_at_utc": _timestamp_string(),
                "source_before_sha256": _sha256(),
                "source_after_sha256": _sha256(),
                "source_checkpoint": {"enum": ["before", "partial", "candidate"]},
                "state_checkpoint": {"enum": ["before", "candidate"]},
                "ledger_checkpoint": {"enum": ["before", "candidate"]},
                "candidate_state_sha256": _sha256(),
                "candidate_ledger_sha256": _sha256(),
                "provider_replayed": {"const": False},
            },
        }
    )
    return schema


def _agent_memory_entry_schema() -> Dict:
    return {
        "type": "object",
        "required": [
            "id",
            "created_at_utc",
            "content",
            "content_sha256",
            "content_bytes",
            "tags",
            "source",
        ],
        "additionalProperties": False,
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_MEMORY_ID_CHARS,
                "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
            },
            "created_at_utc": _timestamp_string(),
            "content": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_MEMORY_ENTRY_BYTES,
            },
            "content_sha256": _sha256(),
            "content_bytes": _int_range(1, MAX_AGENT_MEMORY_ENTRY_BYTES),
            "tags": {
                "type": "array",
                "maxItems": MAX_AGENT_MEMORY_TAGS,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_MEMORY_TAG_CHARS,
                    "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
                },
            },
            "source": {
                "type": "object",
                "required": ["kind", "artifact_sha256", "run_id", "step_id"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"enum": sorted(AGENT_MEMORY_SOURCE_KINDS)},
                    "artifact_sha256": _sha256(),
                    "run_id": {"oneOf": [_safe_id(), {"type": "null"}]},
                    "step_id": {"oneOf": [_safe_id(), {"type": "null"}]},
                },
            },
        },
        "description": "Content bytes are bounded per entry; Python validation enforces total store size %d."
        % MAX_AGENT_MEMORY_STORE_BYTES,
    }


def _benchmark_suite_schema() -> Dict:
    schema = _base_schema(
        BENCHMARK_SCHEMA,
        "Conductor Benchmark Suite",
        "JSON Schema for conductor.benchmark.v1 benchmark suite specs.",
    )
    schema.update(
        {
            "required": ["schema", "name", "tasks"],
            "additionalProperties": True,
            "properties": {
                "schema": {"const": BENCHMARK_SCHEMA},
                "name": _non_empty_string(),
                "description": _unchecked_metadata("Benchmark suite description is display metadata; prefer a string."),
                "default_timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                "output_limit_bytes": _int_range(1, MAX_BENCHMARK_OUTPUT_LIMIT_BYTES),
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": _benchmark_task_schema(),
                },
            },
        }
    )
    return schema


def _run_manifest_schema() -> Dict:
    schema = _base_schema(
        RUN_MANIFEST_SCHEMA,
        "Conductor Run Manifest",
        "JSON Schema for conductor.run_manifest.v1 benchmark provenance manifests.",
    )
    schema.update(
        {
            "required": [
                "schema",
                "run_id",
                "system",
                "suite",
                "generated_at_utc",
                "live_run",
                "repository",
                "model",
                "budget",
                "permissions",
                "scoring",
                "safety",
            ],
            "additionalProperties": False,
            "properties": {
                "schema": {"const": RUN_MANIFEST_SCHEMA},
                "run_id": _safe_id(),
                "system": _safe_id(),
                "suite": _non_empty_string(),
                "generated_at_utc": _non_empty_string(),
                "live_run": {"type": "boolean"},
                "repository": _string_object_schema(REPOSITORY_FIELDS, required=["state"]),
                "model": _string_object_schema(MODEL_FIELDS, required=["provider", "name", "effort"]),
                "budget": _run_budget_schema(),
                "permissions": _run_permissions_schema(),
                "scoring": _run_scoring_schema(),
                "safety": _run_safety_schema(),
            },
        }
    )
    return schema


def _benchmark_report_schema() -> Dict:
    schema = _base_schema(
        BENCHMARK_REPORT_SCHEMA,
        "Conductor Benchmark Report",
        "JSON Schema for conductor.benchmark_report.v1 benchmark result reports.",
    )
    schema.update(
        {
            "required": [
                "schema",
                "suite",
                "total_tasks",
                "passed_tasks",
                "failed_tasks",
                "duration_ms",
                "results",
            ],
            "additionalProperties": True,
            "properties": {
                "schema": {"const": BENCHMARK_REPORT_SCHEMA},
                "system": _safe_id(),
                "suite": _non_empty_string(),
                "total_tasks": _int_range(0),
                "passed_tasks": _int_range(0),
                "failed_tasks": _int_range(0),
                "duration_ms": _int_range(0),
                "results": {"type": "array", "minItems": 1, "items": _benchmark_result_schema()},
            },
        }
    )
    return schema


def _quality_reviewer_schema() -> Dict:
    return {
        "type": "object",
        "required": sorted(QUALITY_REVIEWER_FIELDS),
        "additionalProperties": False,
        "properties": {
            "kind": {"enum": sorted(QUALITY_REVIEWER_KINDS)},
            "identity": {"type": "string", "minLength": 1, "maxLength": 200},
            "independent": {"type": "boolean"},
        },
    }


def _quality_criteria_schema() -> Dict:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_QUALITY_CRITERIA,
        "items": {
            "type": "object",
            "required": sorted(QUALITY_CRITERION_FIELDS),
            "additionalProperties": False,
            "properties": {
                "id": _safe_id(),
                "score": _int_range(0, MAX_QUALITY_SCORE),
                "max_score": _int_range(1, MAX_QUALITY_SCORE),
                "notes": {"type": "string", "maxLength": MAX_QUALITY_NOTES_CHARS},
            },
        },
    }


def _benchmark_quality_evidence_schema() -> Dict:
    nullable_sha256 = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
    schema = _base_schema(
        BENCHMARK_QUALITY_EVIDENCE_SCHEMA,
        "Conductor Benchmark Quality Evidence",
        "Hash-bound rubric scoring evidence for one benchmark result.",
    )
    schema.update(
        {
            "required": sorted(QUALITY_EVIDENCE_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": BENCHMARK_QUALITY_EVIDENCE_SCHEMA},
                "suite": _non_empty_string(),
                "task_id": _safe_id(),
                "task_contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "output_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "provider_evidence_sha256": nullable_sha256,
                "staged_evidence_sha256": nullable_sha256,
                "reviewer": _quality_reviewer_schema(),
                "criteria": _quality_criteria_schema(),
                "score": _int_range(0, MAX_QUALITY_SCORE),
                "max_score": _int_range(1, MAX_QUALITY_SCORE),
                "pass_threshold": _int_range(1, MAX_QUALITY_SCORE),
                "passed": {"type": "boolean"},
                "generated_at_utc": _non_empty_string(),
            },
        }
    )
    return schema


def _benchmark_quality_evidence_v2_schema() -> Dict:
    schema = copy.deepcopy(_benchmark_quality_evidence_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2
    schema["title"] = "Conductor Benchmark Quality Evidence v2"
    schema["description"] = (
        "Hash-bound rubric scoring evidence with optional aggregate repair execution bindings."
    )
    schema["required"] = sorted(QUALITY_EVIDENCE_FIELDS_V2)
    schema["properties"]["schema"] = {"const": BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2}
    sha256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    schema["properties"]["repair_evidence_sha256"] = sha256
    schema["properties"]["repair_attempt_reports_sha256"] = sha256
    return schema


def _benchmark_score_input_schema() -> Dict:
    schema = _base_schema(
        BENCHMARK_SCORE_INPUT_SCHEMA,
        "Conductor Benchmark Score Input",
        "Strict operator input used to attach hash-bound rubric scoring to a benchmark report.",
    )
    schema.update(
        {
            "required": sorted(SCORE_INPUT_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": BENCHMARK_SCORE_INPUT_SCHEMA},
                "reviewer": _quality_reviewer_schema(),
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_QUALITY_TASKS,
                    "items": {
                        "type": "object",
                        "required": sorted(SCORE_INPUT_TASK_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "pass_threshold": _int_range(1, MAX_QUALITY_SCORE),
                            "criteria": _quality_criteria_schema(),
                        },
                    },
                },
            },
        }
    )
    return schema


def _claude_provider_evidence_schema_for(
    schema_id: str,
    observed_fields: set,
    *,
    token_usage: bool,
    readonly_permission_mode: str,
) -> Dict:
    observed_properties = {
        "cli_version": _non_empty_string(),
        "main_models": _string_list(min_items=0, non_empty=True),
        "helper_models": _string_list(min_items=0, non_empty=True),
        "sonnet_main_only": {"type": "boolean"},
        "opus_observed": {"type": "boolean"},
        "terminal_event_present": {"type": "boolean"},
        "terminal_subtype": {"type": ["string", "null"]},
        "is_error": {"type": ["boolean", "null"]},
        "cost_usd": {"type": ["number", "null"], "minimum": 0},
        "budget_overshoot_usd": {"type": "number", "minimum": 0},
        "turns": {"type": ["integer", "null"], "minimum": 0},
        "assistant_messages": _int_range(0, MAX_CLAUDE_STREAM_EVENTS),
        "stream_events": _int_range(0, MAX_CLAUDE_STREAM_EVENTS),
        "output_source": {"enum": sorted(CLAUDE_OUTPUT_SOURCES)},
        "partial_output_preserved": {"type": "boolean"},
        "returncode": {"type": "integer"},
        "timed_out": {"type": "boolean"},
        "stdout_truncated": {"type": "boolean"},
        "stderr_truncated": {"type": "boolean"},
        "parse_error": {"type": ["string", "null"]},
    }
    if token_usage:
        nullable_tokens = {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_CLAUDE_TOKENS}
        observed_properties.update(
            {
                "token_usage_source": {"enum": sorted(CLAUDE_TOKEN_USAGE_SOURCES)},
                "token_accounting": {"const": CLAUDE_TOKEN_ACCOUNTING},
                "input_tokens": nullable_tokens,
                "cache_creation_input_tokens": nullable_tokens,
                "cache_read_input_tokens": nullable_tokens,
                "output_tokens": nullable_tokens,
                "total_tokens": nullable_tokens,
            }
        )
    schema = _base_schema(
        schema_id,
        "Conductor Claude Provider Evidence",
        "JSON Schema for pinned Claude Sonnet Ultracode live-run model, cost, token, stream, and policy evidence.",
    )
    schema.update(
        {
            "required": sorted(CLAUDE_PROVIDER_EVIDENCE_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": schema_id},
                "status": {"enum": sorted(CLAUDE_PROVIDER_STATUSES)},
                "requested": {
                    "type": "object",
                    "required": sorted(CLAUDE_REQUESTED_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "model": {"const": CLAUDE_MODEL},
                        "effort": {"const": CLAUDE_EFFORT},
                        "permission_mode": {
                            "enum": [readonly_permission_mode, CLAUDE_STAGED_PERMISSION_MODE]
                        },
                        "tools": {
                            "oneOf": [
                                {"const": CLAUDE_READ_ONLY_TOOLS},
                                {"const": CLAUDE_STAGED_WRITE_TOOLS},
                            ]
                        },
                        "max_budget_usd": {"type": "number", "exclusiveMinimum": 0},
                        "max_turns": _int_range(1, MAX_CLAUDE_TURNS),
                        "timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                        "output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                    },
                },
                "observed": {
                    "type": "object",
                    "required": sorted(observed_fields),
                    "additionalProperties": False,
                    "properties": observed_properties,
                },
                "policy": {
                    "type": "object",
                    "required": sorted(CLAUDE_POLICY_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "paid_approval_present": {"const": True},
                        "approval_values_persisted": {"const": False},
                        "no_fallback_model": {"const": True},
                        "no_session_persistence": {"const": True},
                        "prompt_in_argv": {"const": False},
                        "read_only_tools": {"type": "boolean"},
                    },
                },
                "incidents": {
                    "type": "array",
                    "maxItems": MAX_CLAUDE_INCIDENTS,
                    "items": {
                        "type": "object",
                        "required": sorted(CLAUDE_INCIDENT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "severity": {"enum": sorted(CLAUDE_PROVIDER_SEVERITIES)},
                            "description": _non_empty_string(),
                        },
                    },
                },
            },
        }
    )
    return schema


def _claude_provider_evidence_schema() -> Dict:
    return _claude_provider_evidence_schema_for(
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
        CLAUDE_OBSERVED_FIELDS,
        token_usage=True,
        readonly_permission_mode=CLAUDE_PERMISSION_MODE,
    )


def _claude_provider_evidence_v2_schema() -> Dict:
    schema = _claude_provider_evidence_schema_for(
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
        CLAUDE_OBSERVED_FIELDS,
        token_usage=True,
        readonly_permission_mode=CLAUDE_LEGACY_PERMISSION_MODE,
    )
    schema["title"] = "Conductor Claude Provider Evidence v2"
    return schema


def _claude_provider_evidence_v1_schema() -> Dict:
    schema = _claude_provider_evidence_schema_for(
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
        CLAUDE_OBSERVED_FIELDS_V1,
        token_usage=False,
        readonly_permission_mode=CLAUDE_LEGACY_PERMISSION_MODE,
    )
    schema["title"] = "Conductor Claude Provider Evidence v1"
    return schema


def _codex_provider_evidence_schema() -> Dict:
    nullable_token = {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_CODEX_TOKENS}
    nullable_sha256 = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
    schema = _base_schema(
        CODEX_PROVIDER_EVIDENCE_SCHEMA,
        "Conductor Codex Provider Evidence",
        "JSON Schema for pinned, ephemeral, permission-profile-isolated Codex provider evidence with a runtime token cap.",
    )
    schema.update(
        {
            "required": sorted(CODEX_PROVIDER_EVIDENCE_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": CODEX_PROVIDER_EVIDENCE_SCHEMA},
                "status": {"enum": sorted(CODEX_PROVIDER_STATUSES)},
                "requested": {
                    "type": "object",
                    "required": sorted(CODEX_REQUESTED_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "model": _non_empty_string(),
                        "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
                        "service_tier": {"enum": sorted(CODEX_SERVICE_TIERS)},
                        "execution_mode": {"enum": sorted(CODEX_EXECUTION_MODES)},
                        "sandbox": {"const": CODEX_SANDBOX},
                        "permission_profile": {
                            "enum": [CODEX_READONLY_PERMISSION_PROFILE, CODEX_STAGED_PERMISSION_PROFILE]
                        },
                        "approval_policy": {"const": CODEX_APPROVAL_POLICY},
                        "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                        "timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                        "output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                        "tool_output_token_limit": _int_range(
                            MIN_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                            MAX_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                        ),
                        "rollout_budget_reminders": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "uniqueItems": True,
                            "items": _int_range(1, MAX_CODEX_TOKENS),
                        },
                    },
                },
                "observed": {
                    "type": "object",
                    "required": sorted(CODEX_OBSERVED_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "cli_version": _non_empty_string(),
                        "model_binding": {"const": CODEX_MODEL_BINDING},
                        "service_tier_binding": {"enum": sorted(CODEX_SERVICE_TIER_BINDINGS)},
                        "terminal_event_present": {"type": "boolean"},
                        "terminal_status": {"enum": sorted(CODEX_TERMINAL_STATUSES)},
                        "runtime_budget_exhausted": {"type": "boolean"},
                        "thread_started": _int_range(0, MAX_CODEX_STREAM_EVENTS),
                        "turns": _int_range(0, MAX_CODEX_STREAM_EVENTS),
                        "agent_messages": _int_range(0, MAX_CODEX_STREAM_EVENTS),
                        "stream_events": _int_range(0, MAX_CODEX_STREAM_EVENTS),
                        "input_tokens": nullable_token,
                        "cached_input_tokens": nullable_token,
                        "output_tokens": nullable_token,
                        "reasoning_output_tokens": nullable_token,
                        "total_tokens": nullable_token,
                        "rollout_budget_tokens": nullable_token,
                        "rollout_budget_overshoot": nullable_token,
                        "token_usage_precision": {"enum": sorted(CODEX_TOKEN_USAGE_PRECISIONS)},
                        "token_usage_source": {"enum": sorted(CODEX_TOKEN_USAGE_SOURCES)},
                        "gross_token_lower_bound": nullable_token,
                        "gross_token_upper_bound": nullable_token,
                        "rollout_budget_lower_bound": nullable_token,
                        "rollout_budget_upper_bound": nullable_token,
                        "output_source": {"enum": sorted(CODEX_OUTPUT_SOURCES)},
                        "partial_output_preserved": {"type": "boolean"},
                        "provider_error_events": _int_range(0, MAX_CODEX_STREAM_EVENTS),
                        "returncode": {"type": "integer"},
                        "timed_out": {"type": "boolean"},
                        "stdout_truncated": {"type": "boolean"},
                        "stderr_truncated": {"type": "boolean"},
                        "parse_error": {"type": ["string", "null"], "maxLength": 4096},
                    },
                },
                "workspace": {
                    "type": "object",
                    "required": sorted(CODEX_WORKSPACE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "source_before_fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "source_after_fingerprint_sha256": nullable_sha256,
                        "stage_before_fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "stage_after_fingerprint_sha256": nullable_sha256,
                        "source_unchanged": {"type": "boolean"},
                        "stage_unchanged": {"type": "boolean"},
                        "stage_mutation_allowed": {"type": "boolean"},
                        "file_count": _int_range(0, MAX_STAGED_FILES),
                        "total_bytes": _int_range(0, MAX_STAGED_TOTAL_BYTES),
                        "stage_directory_name": _non_empty_string(),
                        "stage_path_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "stage_outside_source": {"const": True},
                        "stage_persisted": {"const": True},
                        "scan_error": {"type": ["string", "null"], "maxLength": 4096},
                    },
                },
                "policy": {
                    "type": "object",
                    "required": sorted(CODEX_POLICY_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "live_approval_present": {"const": True},
                        "staged_write_approval_present": {"type": "boolean"},
                        "approval_values_persisted": {"const": False},
                        "explicit_model": {"const": True},
                        "no_fallback_model": {"const": True},
                        "no_session_persistence": {"const": True},
                        "prompt_in_argv": {"const": False},
                        "read_only_tools": {"type": "boolean"},
                        "permission_profile_enforced": {"const": True},
                        "minimal_filesystem_scope": {"const": True},
                        "shell_environment_sanitized": {"const": True},
                        "user_config_ignored": {"const": True},
                        "rules_ignored": {"const": True},
                        "project_instructions_ignored": {"const": True},
                        "network_tools_enabled": {"const": False},
                        "web_search_enabled": {"const": False},
                        "hooks_enabled": {"const": False},
                        "plugins_enabled": {"const": False},
                        "multi_agent_enabled": {"const": False},
                        "rollout_budget_enabled": {"const": True},
                        "rollout_budget_experimental": {"const": True},
                        "tool_output_token_limit_enforced": {"const": True},
                        "priority_service_tier_enabled": {"type": "boolean"},
                        "automatic_retry": {"const": False},
                        "token_cap_enforcement": {"const": "runtime-hard"},
                    },
                },
                "incidents": {
                    "type": "array",
                    "maxItems": MAX_CODEX_INCIDENTS,
                    "items": {
                        "type": "object",
                        "required": sorted(CODEX_INCIDENT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "severity": {"enum": sorted(CODEX_PROVIDER_SEVERITIES)},
                            "description": _non_empty_string(),
                        },
                    },
                },
            },
        }
    )
    return schema


def _codex_provider_evidence_v3_schema() -> Dict:
    schema = copy.deepcopy(_codex_provider_evidence_v4_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % CODEX_PROVIDER_EVIDENCE_SCHEMA_V3
    schema["title"] = "Conductor Codex Provider Evidence v3"
    schema["description"] = (
        "Legacy JSON Schema for weighted runtime-capped Codex evidence before explicit priority-tier binding. "
        "Conductor's Python validator remains authoritative for cross-field checks."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": CODEX_PROVIDER_EVIDENCE_SCHEMA_V3}
    requested = properties["requested"]
    requested["required"] = sorted(CODEX_REQUESTED_FIELDS_V2)
    for field in sorted(CODEX_REQUESTED_FIELDS - CODEX_REQUESTED_FIELDS_V2):
        requested["properties"].pop(field, None)
    observed = properties["observed"]
    observed["required"] = sorted(CODEX_OBSERVED_FIELDS_V3)
    for field in sorted(CODEX_OBSERVED_FIELDS_V4 - CODEX_OBSERVED_FIELDS_V3):
        observed["properties"].pop(field, None)
    policy = properties["policy"]
    policy["required"] = sorted(CODEX_POLICY_FIELDS_V2)
    for field in sorted(CODEX_POLICY_FIELDS - CODEX_POLICY_FIELDS_V2):
        policy["properties"].pop(field, None)
    return schema


def _codex_provider_evidence_v4_schema() -> Dict:
    schema = copy.deepcopy(_codex_provider_evidence_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % CODEX_PROVIDER_EVIDENCE_SCHEMA_V4
    schema["title"] = "Conductor Codex Provider Evidence v4"
    schema["description"] = (
        "Legacy JSON Schema for runtime-capped Codex evidence before explicit token-usage provenance and bounds. "
        "Conductor's Python validator remains authoritative for cross-field checks."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": CODEX_PROVIDER_EVIDENCE_SCHEMA_V4}
    observed = properties["observed"]
    observed["required"] = sorted(CODEX_OBSERVED_FIELDS_V4)
    for field in sorted(CODEX_OBSERVED_FIELDS - CODEX_OBSERVED_FIELDS_V4):
        observed["properties"].pop(field, None)
    return schema


def _codex_provider_evidence_v2_schema() -> Dict:
    schema = copy.deepcopy(_codex_provider_evidence_v3_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % CODEX_PROVIDER_EVIDENCE_SCHEMA_V2
    schema["title"] = "Conductor Codex Provider Evidence v2"
    schema["description"] = (
        "Legacy JSON Schema for runtime-capped Codex evidence that compared the cap against gross terminal usage. "
        "Conductor's Python validator remains authoritative for cross-field checks."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": CODEX_PROVIDER_EVIDENCE_SCHEMA_V2}
    observed = properties["observed"]
    observed["required"] = sorted(CODEX_OBSERVED_FIELDS_V2)
    for field in sorted(CODEX_OBSERVED_FIELDS_V3 - CODEX_OBSERVED_FIELDS_V2):
        observed["properties"].pop(field, None)
    observed["properties"]["token_overshoot"] = _int_range(0, MAX_CODEX_TOKENS)
    return schema


def _codex_provider_evidence_v1_schema() -> Dict:
    schema = copy.deepcopy(_codex_provider_evidence_v2_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % CODEX_PROVIDER_EVIDENCE_SCHEMA_V1
    schema["title"] = "Conductor Codex Provider Evidence v1"
    schema["description"] = (
        "Legacy JSON Schema for the post-run-capped isolated Codex read-only adapter. "
        "Conductor's Python validator remains authoritative for cross-field checks."
    )
    properties = schema["properties"]
    properties["schema"] = {"const": CODEX_PROVIDER_EVIDENCE_SCHEMA_V1}
    requested = properties["requested"]
    requested["required"] = sorted(CODEX_REQUESTED_FIELDS_V1)
    for field in sorted(CODEX_REQUESTED_FIELDS - CODEX_REQUESTED_FIELDS_V1):
        requested["properties"].pop(field, None)
    requested["properties"]["sandbox"] = {"const": CODEX_SANDBOX_V1}
    requested["properties"]["max_tokens"] = _int_range(1, MAX_CODEX_TOKENS)
    observed = properties["observed"]
    observed["required"] = sorted(CODEX_OBSERVED_FIELDS_V1)
    for field in sorted(CODEX_OBSERVED_FIELDS - CODEX_OBSERVED_FIELDS_V1):
        observed["properties"].pop(field, None)
    workspace = properties["workspace"]
    workspace["required"] = sorted(CODEX_WORKSPACE_FIELDS_V1)
    for field in sorted(CODEX_WORKSPACE_FIELDS - CODEX_WORKSPACE_FIELDS_V1):
        workspace["properties"].pop(field, None)
    policy = properties["policy"]
    policy["required"] = sorted(CODEX_POLICY_FIELDS_V1)
    for field in sorted(CODEX_POLICY_FIELDS - CODEX_POLICY_FIELDS_V1):
        policy["properties"].pop(field, None)
    policy["properties"]["read_only_tools"] = {"const": True}
    policy["properties"]["token_cap_enforcement"] = {"const": "post-run-fail-closed"}
    return schema


def _claude_staged_evidence_schema() -> Dict:
    nullable_sha256 = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
    path_list = {
        "type": "array",
        "maxItems": MAX_STAGED_CHANGES,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": RELATIVE_PATH_PATTERN},
    }
    optional_error = {"type": ["string", "null"], "minLength": 1, "maxLength": 4096}
    schema = _base_schema(
        CLAUDE_STAGED_EVIDENCE_SCHEMA,
        "Conductor Claude Staged-Write Evidence",
        "JSON Schema for source-isolated Claude Sonnet Ultracode edits, deterministic verification, and patch evidence.",
    )
    schema.update(
        {
            "required": sorted(CLAUDE_STAGED_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": CLAUDE_STAGED_EVIDENCE_SCHEMA},
                "status": {"enum": sorted(CLAUDE_STAGED_STATUSES)},
                "source": {
                    "type": "object",
                    "required": sorted(CLAUDE_STAGED_SOURCE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "before_fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "after_provider_fingerprint_sha256": nullable_sha256,
                        "final_fingerprint_sha256": nullable_sha256,
                        "file_count": _int_range(0, MAX_STAGED_FILES),
                        "total_bytes": _int_range(0, MAX_STAGED_TOTAL_BYTES),
                        "unchanged": {"type": "boolean"},
                        "scan_error": optional_error,
                    },
                },
                "stage": {
                    "type": "object",
                    "required": sorted(CLAUDE_STAGED_STAGE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "directory_name": {"type": "string", "minLength": 1, "maxLength": 255},
                        "path_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "file_count": {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_STAGED_FILES},
                        "total_bytes": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "maximum": MAX_STAGED_TOTAL_BYTES,
                        },
                        "before_verification_sha256": nullable_sha256,
                        "after_verification_sha256": nullable_sha256,
                        "verifier_mutated_files": {"type": "boolean"},
                        "persisted": {"const": True},
                        "scan_error": optional_error,
                    },
                },
                "changes": {
                    "type": "object",
                    "required": sorted(CLAUDE_STAGED_CHANGE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "change_count": _int_range(0, MAX_STAGED_CHANGES),
                        "added": path_list,
                        "modified": path_list,
                        "deleted": path_list,
                        "binary": path_list,
                        "mode_changed": path_list,
                        "unpatchable": path_list,
                        "patch_written": {"type": "boolean"},
                        "patch_name": {"type": ["string", "null"], "minLength": 1, "maxLength": 255},
                        "patch_sha256": nullable_sha256,
                        "patch_bytes": _int_range(0, MAX_STAGED_PATCH_BYTES),
                        "validation_error": optional_error,
                        "patch_error": optional_error,
                    },
                },
                "verification": {
                    "type": "object",
                    "required": sorted(CLAUDE_STAGED_VERIFICATION_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "configured": {"const": True},
                        "status": {"enum": sorted(CLAUDE_STAGED_VERIFICATION_STATUSES)},
                        "returncode": {"type": ["integer", "null"]},
                        "timed_out": {"type": "boolean"},
                        "duration_ms": _int_range(0),
                        "stdout_bytes": _int_range(0),
                        "stderr_bytes": _int_range(0),
                        "stdout_truncated": {"type": "boolean"},
                        "stderr_truncated": {"type": "boolean"},
                        "stdout_excerpt": {"type": "string", "maxLength": MAX_VERIFIER_EXCERPT_CHARS},
                        "stderr_excerpt": {"type": "string", "maxLength": MAX_VERIFIER_EXCERPT_CHARS},
                        "command_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "argv_count": _int_range(1, 1024),
                        "sandbox": {"enum": sorted(CLAUDE_STAGED_VERIFIER_SANDBOXES)},
                        "environment_sanitized": {"const": True},
                        "network_isolated": {"const": True},
                        "write_scope": {"const": CLAUDE_STAGED_VERIFIER_WRITE_SCOPE},
                    },
                },
                "policy": {
                    "type": "object",
                    "required": sorted(CLAUDE_STAGED_POLICY_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "source_mutation_allowed": {"const": False},
                        "source_mutated": {"type": "boolean"},
                        "claude_shell_enabled": {"const": False},
                        "tools": {"const": CLAUDE_STAGED_WRITE_TOOLS},
                        "stage_outside_source": {"const": True},
                        "approval_values_persisted": {"const": False},
                        "automatic_apply": {"const": False},
                    },
                },
                "incidents": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "required": sorted(CLAUDE_STAGED_INCIDENT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "severity": {"enum": sorted(CLAUDE_STAGED_INCIDENT_SEVERITIES)},
                            "description": {"type": "string", "minLength": 1, "maxLength": 8192},
                        },
                    },
                },
            },
        }
    )
    return schema


def _codex_staged_evidence_schema() -> Dict:
    schema = copy.deepcopy(_claude_staged_evidence_schema())
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % CODEX_STAGED_EVIDENCE_SCHEMA
    schema["title"] = "Conductor Codex Staged-Write Evidence"
    schema["description"] = (
        "JSON Schema for source-isolated Codex edits, deterministic verification, and no-auto-apply patch evidence. "
        "Conductor's Python validator remains authoritative for cross-field checks."
    )
    schema["required"] = sorted(CODEX_STAGED_FIELDS)
    properties = schema["properties"]
    properties["schema"] = {"const": CODEX_STAGED_EVIDENCE_SCHEMA}
    properties["status"] = {"enum": sorted(CODEX_STAGED_STATUSES)}
    properties["policy"] = {
        "type": "object",
        "required": sorted(CODEX_STAGED_POLICY_FIELDS),
        "additionalProperties": False,
        "properties": {
            "source_mutation_allowed": {"const": False},
            "source_mutated": {"type": "boolean"},
            "provider_shell_enabled": {"const": True},
            "permission_profile": {"const": CODEX_STAGED_PERMISSION_PROFILE},
            "filesystem_scope": {"const": CODEX_STAGED_FILESYSTEM_SCOPE},
            "network_access": {"const": False},
            "temporary_writes_allowed": {"const": False},
            "tool_output_token_limit": _int_range(
                MIN_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                MAX_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
            ),
            "stage_outside_source": {"const": True},
            "approval_values_persisted": {"const": False},
            "automatic_apply": {"const": False},
        },
    }
    properties["incidents"]["items"]["required"] = sorted(CODEX_STAGED_INCIDENT_FIELDS)
    properties["incidents"]["items"]["properties"]["severity"] = {
        "enum": sorted(CODEX_STAGED_INCIDENT_SEVERITIES)
    }
    return schema


def _codex_staged_repair_schema() -> Dict:
    nullable_token = {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": MAX_CODEX_TOKENS,
    }
    nullable_sha256 = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
    nullable_text = {"type": ["string", "null"], "minLength": 1, "maxLength": 4096}
    path_list = {
        "type": "array",
        "maxItems": MAX_STAGED_CHANGES,
        "uniqueItems": True,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "pattern": RELATIVE_PATH_PATTERN,
        },
    }
    schema = _base_schema(
        CODEX_STAGED_REPAIR_SCHEMA,
        "Conductor Codex Staged Repair Evidence",
        "JSON Schema for bounded verifier-feedback Codex repair attempts over source-isolated stages.",
    )
    schema.update(
        {
            "required": sorted(CODEX_STAGED_REPAIR_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": CODEX_STAGED_REPAIR_SCHEMA},
                "status": {"enum": sorted(CODEX_STAGED_REPAIR_STATUSES)},
                "suite": _non_empty_string(),
                "task_id": _safe_id(),
                "started_at_utc": _timestamp_string(),
                "duration_ms": _int_range(0, MAX_CODEX_STAGED_REPAIR_DURATION_MS),
                "limits": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_LIMIT_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "max_attempts": _int_range(1, MAX_CODEX_STAGED_REPAIR_ATTEMPTS),
                        "max_tokens_per_attempt": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                        "total_token_cap": _int_range(
                            MIN_CODEX_RUNTIME_TOKEN_CAP,
                            MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        ),
                        "provider_timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                        "check_timeout_seconds": _int_range(1, MAX_AUTO_CHECK_TIMEOUT_SECONDS),
                        "total_timeout_cap_seconds": _int_range(
                            1,
                            (MAX_TIMEOUT_SECONDS + MAX_VERIFIER_TIMEOUT_SECONDS)
                            * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        ),
                        "output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                        "check_output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                        "tool_output_token_limit": _int_range(
                            MIN_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                            MAX_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                        ),
                    },
                },
                "usage": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_USAGE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "attempt_count": _int_range(1, MAX_CODEX_STAGED_REPAIR_ATTEMPTS),
                        "gross_total_tokens": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "maximum": MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        },
                        "cached_input_tokens": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "maximum": MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        },
                        "rollout_budget_tokens": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "maximum": MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        },
                        "budget_charge_tokens": _int_range(
                            0,
                            MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        ),
                        "remaining_token_budget": _int_range(
                            0,
                            MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        ),
                        "usage_complete": {"type": "boolean"},
                    },
                },
                "source": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_SOURCE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "before_fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "final_fingerprint_sha256": nullable_sha256,
                        "file_count": _int_range(0, MAX_STAGED_FILES),
                        "total_bytes": _int_range(0, MAX_STAGED_TOTAL_BYTES),
                        "unchanged": {"type": "boolean"},
                        "scan_error": nullable_text,
                    },
                },
                "attempts": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                    "items": {
                        "type": "object",
                        "required": sorted(CODEX_STAGED_REPAIR_ATTEMPT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "index": _int_range(1, MAX_CODEX_STAGED_REPAIR_ATTEMPTS),
                            "report_name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 255,
                                "pattern": r"^[^/\\\r\n]+$",
                            },
                            "report_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "source_fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "stage_fingerprint_sha256": nullable_sha256,
                            "stage_tracked_fingerprint_sha256": nullable_sha256,
                            "stage_path_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "chain_scan_error": nullable_text,
                            "provider_schema": {
                                "enum": [
                                    CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
                                    CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
                                    CODEX_PROVIDER_EVIDENCE_SCHEMA,
                                ]
                            },
                            "provider_status": {"enum": sorted(CODEX_PROVIDER_STATUSES)},
                            "model": _non_empty_string(),
                            "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
                            "cli_version": _non_empty_string(),
                            "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                            "timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                            "output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                            "tool_output_token_limit": _int_range(
                                MIN_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                                MAX_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                            ),
                            "staged_schema": {"const": CODEX_STAGED_EVIDENCE_SCHEMA},
                            "staged_status": {"enum": sorted(CODEX_STAGED_STATUSES)},
                            "verification_status": {"enum": sorted(CLAUDE_STAGED_VERIFICATION_STATUSES)},
                            "gross_total_tokens": nullable_token,
                            "cached_input_tokens": nullable_token,
                            "rollout_budget_tokens": nullable_token,
                            "budget_charge_tokens": _int_range(0, MAX_CODEX_TOKENS),
                            "duration_ms": _int_range(0, MAX_CODEX_STAGED_REPAIR_ATTEMPT_DURATION_MS),
                            "feedback_sha256": nullable_sha256,
                            "feedback_chars": _int_range(0, MAX_ITERATION_CONTEXT_CHARS),
                        },
                    },
                },
                "final": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_FINAL_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "stage_directory_name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "pattern": r"^[^/\\\r\n]+$",
                        },
                        "stage_path_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "stage_fingerprint_sha256": nullable_sha256,
                        "change_count": _int_range(0, MAX_STAGED_CHANGES),
                        "added": path_list,
                        "modified": path_list,
                        "deleted": path_list,
                        "binary": path_list,
                        "mode_changed": path_list,
                        "unpatchable": path_list,
                        "verification_status": {"enum": sorted(CLAUDE_STAGED_VERIFICATION_STATUSES)},
                        "patch_written": {"type": "boolean"},
                        "patch_name": {"type": ["string", "null"], "minLength": 1, "maxLength": 255},
                        "patch_sha256": nullable_sha256,
                        "patch_bytes": _int_range(0, MAX_STAGED_PATCH_BYTES),
                        "patch_error": nullable_text,
                    },
                },
                "policy": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_POLICY_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "source_mutation_allowed": {"const": False},
                        "source_mutated": {"type": "boolean"},
                        "stage_chain_external": {"const": True},
                        "feedback_treated_as_untrusted": {"const": True},
                        "feedback_max_chars": {"const": MAX_ITERATION_CONTEXT_CHARS},
                        "network_access": {"const": False},
                        "approval_values_persisted": {"const": False},
                        "automatic_apply": {"const": False},
                        "retry_statuses": {"const": sorted(CODEX_STAGED_REPAIR_RETRY_STATUSES)},
                    },
                },
                "incidents": {
                    "type": "array",
                    "maxItems": MAX_CODEX_STAGED_REPAIR_INCIDENTS,
                    "items": {
                        "type": "object",
                        "required": sorted(CODEX_STAGED_REPAIR_INCIDENT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "id": _safe_id(),
                            "severity": {"enum": ["info", "low", "medium", "high", "critical"]},
                            "description": _non_empty_string(),
                        },
                    },
                },
            },
        }
    )
    return schema


def _codex_staged_repair_state_schema() -> Dict:
    nullable_sha256 = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
    artifact_name = {
        "type": "string",
        "minLength": 1,
        "maxLength": 255,
        "pattern": r"^[^/\\\r\n]+$",
    }
    nullable_name = {
        "type": ["string", "null"],
        "minLength": 1,
        "maxLength": 255,
        "pattern": r"^[^/\\\r\n]+$",
    }
    schema = _base_schema(
        CODEX_STAGED_REPAIR_STATE_SCHEMA,
        "Conductor Codex Staged Repair State",
        "JSON Schema for resumable, hash-bound Codex staged-repair checkpoints.",
    )
    schema.update(
        {
            "required": sorted(CODEX_STAGED_REPAIR_STATE_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": CODEX_STAGED_REPAIR_STATE_SCHEMA},
                "status": {"enum": sorted(CODEX_STAGED_REPAIR_STATE_STATUSES)},
                "suite": _non_empty_string(),
                "task_id": _safe_id(),
                "started_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "active_duration_ms": _int_range(0, MAX_CODEX_STAGED_REPAIR_DURATION_MS),
                "task_contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "workspace_path_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "config": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_STATE_CONFIG_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "max_attempts": _int_range(1, MAX_CODEX_STAGED_REPAIR_ATTEMPTS),
                        "max_tokens_per_attempt": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
                        "total_token_cap": _int_range(
                            MIN_CODEX_RUNTIME_TOKEN_CAP,
                            MAX_CODEX_TOKENS * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        ),
                        "provider_timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                        "check_timeout_seconds": _int_range(1, MAX_VERIFIER_TIMEOUT_SECONDS),
                        "total_timeout_cap_seconds": _int_range(
                            1,
                            (MAX_TIMEOUT_SECONDS + MAX_VERIFIER_TIMEOUT_SECONDS)
                            * MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                        ),
                        "output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                        "check_output_limit_bytes": _int_range(1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES),
                        "tool_output_token_limit": _int_range(
                            MIN_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                            MAX_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
                        ),
                        "model": _non_empty_string(),
                        "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
                        "check_command_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    },
                },
                "source": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_STATE_SOURCE_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "before_fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "file_count": _int_range(0, MAX_STAGED_FILES),
                        "total_bytes": _int_range(0, MAX_STAGED_TOTAL_BYTES),
                    },
                },
                "attempts": {
                    "type": "array",
                    "maxItems": MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                    "items": {
                        "type": "object",
                        "required": sorted(CODEX_STAGED_REPAIR_STATE_ATTEMPT_FIELDS),
                        "additionalProperties": False,
                        "properties": {
                            "index": _int_range(1, MAX_CODEX_STAGED_REPAIR_ATTEMPTS),
                            "report_name": artifact_name,
                            "report_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "stage_directory_name": artifact_name,
                            "stage_fingerprint_sha256": nullable_sha256,
                            "stage_tracked_fingerprint_sha256": nullable_sha256,
                            "stage_path_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "chain_scan_error": {"type": ["string", "null"], "minLength": 1, "maxLength": 4096},
                        },
                    },
                },
                "active_attempt": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": MAX_CODEX_STAGED_REPAIR_ATTEMPTS,
                },
                "final": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_STATE_FINAL_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "repair_name": nullable_name,
                        "repair_sha256": nullable_sha256,
                        "report_name": nullable_name,
                        "report_sha256": nullable_sha256,
                        "patch_name": nullable_name,
                        "patch_sha256": nullable_sha256,
                    },
                },
                "policy": {
                    "type": "object",
                    "required": sorted(CODEX_STAGED_REPAIR_STATE_POLICY_FIELDS),
                    "additionalProperties": False,
                    "properties": {
                        "approval_values_persisted": {"const": False},
                        "automatic_apply": {"const": False},
                        "active_attempt_replay_allowed": {"const": False},
                    },
                },
            },
        }
    )
    return schema


def _comparison_manifest_schema() -> Dict:
    schema = _base_schema(
        COMPARISON_SCHEMA,
        "Conductor Benchmark Comparison Manifest",
        "JSON Schema for conductor.comparison.v1 benchmark comparison manifests.",
    )
    schema.update(
        {
            "required": ["schema", "name", "systems"],
            "additionalProperties": True,
            "properties": {
                "schema": {"const": COMPARISON_SCHEMA},
                "name": _non_empty_string(),
                "systems": {
                    "type": "array",
                    "minItems": 2,
                    "items": _comparison_system_schema(),
                },
                "claim_policy": _claim_policy_schema(),
            },
        }
    )
    return schema


def _parity_tasks_schema() -> Dict:
    schema = _base_schema(
        PARITY_TASKS_SCHEMA,
        "Conductor Parity Task Fixture",
        "JSON Schema for conductor.parity_tasks.v1 parity task fixtures.",
    )
    schema.update(
        {
            "required": ["schema", "name", "tasks"],
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in {
                    "schema": {"const": PARITY_TASKS_SCHEMA},
                    "name": _non_empty_string(),
                    "description": _unchecked_metadata(
                        "Parity task suite description is display metadata; prefer a string."
                    ),
                    "required_dimensions": _string_list(min_items=1, non_empty=True),
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": _parity_task_schema(),
                    },
                }.items()
                if key in PARITY_TASKS_FIELDS
            },
        }
    )
    return schema


def _parity_campaign_schema() -> Dict:
    return _parity_campaign_schema_for(PARITY_CAMPAIGN_SCHEMA)


def _parity_campaign_v1_schema() -> Dict:
    return _parity_campaign_schema_for(PARITY_CAMPAIGN_SCHEMA_V1)


def _parity_campaign_v2_schema() -> Dict:
    return _parity_campaign_schema_for(PARITY_CAMPAIGN_SCHEMA_V2)


def _parity_campaign_v3_schema() -> Dict:
    return _parity_campaign_schema_for(PARITY_CAMPAIGN_SCHEMA_V3)


def _parity_campaign_schema_for(schema_id: str) -> Dict:
    schema = _base_schema(
        schema_id,
        "Conductor Parity Campaign",
        "JSON Schema for seed-randomized, balanced matched-provider campaign plans.",
    )
    source_schema = _strict_object(
        PARITY_CAMPAIGN_SOURCE_FIELDS,
        {
            "parity_tasks_schema": {"const": PARITY_TASKS_SCHEMA},
            "parity_tasks_source": {"type": ["string", "null"]},
            "parity_tasks_sha256": _sha256(),
            "suite": _non_empty_string(),
            "task_count": _int_range(1, MAX_CAMPAIGN_TRIALS),
            "required_dimensions": _string_list(min_items=0, non_empty=True),
        },
    )
    design_fields = set(PARITY_CAMPAIGN_DESIGN_FIELDS)
    design_properties = {
            "seed": _int_range(0, 2**63 - 1),
            "repetitions": _int_range(1, MAX_CAMPAIGN_REPETITIONS),
            "randomization_algorithm": {"const": PARITY_CAMPAIGN_RANDOMIZATION},
            "trial_count": _int_range(1, MAX_CAMPAIGN_TRIALS),
            "provider_first_counts": _strict_object(
                {"codex", "claude"},
                {"codex": _int_range(0, MAX_CAMPAIGN_TRIALS), "claude": _int_range(0, MAX_CAMPAIGN_TRIALS)},
            ),
    }
    if schema_id == PARITY_CAMPAIGN_SCHEMA:
        design_fields.add(DESIGN_EFFORT_POLICY_FIELD)
        design_properties[DESIGN_EFFORT_POLICY_FIELD] = {
            "enum": sorted(PARITY_CAMPAIGN_EFFORT_POLICIES)
        }
    design_schema = _strict_object(design_fields, design_properties)
    artifacts_schema = _strict_object(
        {"codex", "claude"},
        {
            system: _strict_object(
                PARITY_CAMPAIGN_ARTIFACT_FIELDS,
                {field: _non_empty_string() for field in PARITY_CAMPAIGN_ARTIFACT_FIELDS},
            )
            for system in ["codex", "claude"]
        },
    )
    trial_fields = set(PARITY_CAMPAIGN_TRIAL_FIELDS)
    trial_properties = {
            "id": _safe_id(),
            "sequence": _int_range(1, MAX_CAMPAIGN_TRIALS),
            "task_id": _safe_id(),
            "repetition": _int_range(1, MAX_CAMPAIGN_REPETITIONS),
            "task_contract_sha256": _sha256(),
            "budget": _parity_budget_schema(),
            "system_order": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "uniqueItems": True,
                "items": {"enum": ["codex", "claude"]},
            },
            "artifacts": artifacts_schema,
            "status": {"const": "planned"},
    }
    if schema_id in {PARITY_CAMPAIGN_SCHEMA, PARITY_CAMPAIGN_SCHEMA_V3}:
        trial_fields.add("codex_effort")
        trial_properties["codex_effort"] = {"enum": sorted(CODEX_REASONING_EFFORTS)}
    trial_schema = _strict_object(trial_fields, trial_properties)
    claim_gate_schema = _strict_object(
        PARITY_CAMPAIGN_CLAIM_GATE_FIELDS,
        {
            "eligible_for_parity_claims": {"const": False},
            "required_before_claim": _string_list(min_items=1, non_empty=True),
        },
    )
    schema.update(
        {
            "required": sorted(PARITY_CAMPAIGN_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": schema_id},
                "name": _safe_id(),
                "generated_at_utc": _non_empty_string(),
                "evidence_status": {"const": PARITY_CAMPAIGN_EVIDENCE_STATUS},
                "source": source_schema,
                "design": design_schema,
                "systems": {
                    "type": "array",
                    "prefixItems": [
                        _parity_campaign_system_schema("codex", schema_id),
                        _parity_campaign_system_schema("claude", schema_id),
                    ],
                    "items": False,
                    "minItems": 2,
                    "maxItems": 2,
                },
                "trials": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_CAMPAIGN_TRIALS,
                    "items": trial_schema,
                },
                "claim_gate": claim_gate_schema,
            },
        }
    )
    return schema


def _parity_campaign_system_schema(name: str, schema_id: str) -> Dict:
    if name == "codex" and schema_id in {PARITY_CAMPAIGN_SCHEMA, PARITY_CAMPAIGN_SCHEMA_V3}:
        effort_schema = {"enum": sorted(AUTO_REASONING_EFFORTS)}
    elif name == "codex" and schema_id == PARITY_CAMPAIGN_SCHEMA_V2:
        effort_schema = {"enum": sorted(CODEX_REASONING_EFFORTS)}
    else:
        effort_schema = {"const": "ultra" if name == "codex" else "ultracode"}
    properties = {
        "name": {"const": name},
        "provider": {"const": "openai" if name == "codex" else "anthropic"},
        "model": _non_empty_string() if name == "codex" else {"const": "sonnet"},
        "effort": effort_schema,
        "alternative_model_allowed": {"const": False},
        "requires_external_tool": {"const": True},
        "requires_paid_approval": {"const": name == "claude"},
    }
    return _strict_object(PARITY_CAMPAIGN_SYSTEM_FIELDS, properties)


def _auto_topology_campaign_schema_for(campaign_schema: str) -> Dict:
    legacy = campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1
    current = campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA
    version_suffix = (
        " v1"
        if legacy
        else " v2"
        if campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2
        else ""
    )
    schema = _base_schema(
        campaign_schema,
        "Conductor Auto Topology Campaign%s" % version_suffix,
        (
            "JSON Schema for frozen release-unbound direct, progressive, and plan-first automatic orchestration campaigns."
            if legacy
            else "JSON Schema for frozen exact-release-bound direct, progressive, and plan-first automatic orchestration campaigns."
            if not current
            else "JSON Schema for exact-release-bound direct, progressive, and plan-first automatic orchestration campaigns with hash-bound public execution contracts."
        ),
    )
    source_schema = _strict_object(
        AUTO_TOPOLOGY_SOURCE_FIELDS,
        {
            "parity_tasks_schema": {"const": PARITY_TASKS_SCHEMA},
            "parity_tasks_source": {"type": ["string", "null"]},
            "parity_tasks_sha256": _sha256(),
            "suite": _non_empty_string(),
            "task_count": _int_range(1, MAX_AUTO_TOPOLOGY_COHORTS),
            "required_dimensions": _string_list(min_items=0, non_empty=True),
        },
    )
    first_counts = _strict_object(
        set(AUTO_TOPOLOGIES),
        {name: _int_range(0, MAX_AUTO_TOPOLOGY_COHORTS) for name in AUTO_TOPOLOGIES},
    )
    design_schema = _strict_object(
        AUTO_TOPOLOGY_DESIGN_FIELDS,
        {
            "seed": _int_range(0, 2**63 - 1),
            "repetitions": _int_range(1, MAX_AUTO_TOPOLOGY_REPETITIONS),
            "randomization_algorithm": {"const": AUTO_TOPOLOGY_RANDOMIZATION},
            "cohort_count": _int_range(1, MAX_AUTO_TOPOLOGY_COHORTS),
            "arm_count": _int_range(3, MAX_AUTO_TOPOLOGY_COHORTS * 3),
            "max_iterations": _int_range(1, MAX_AUTO_TOPOLOGY_ITERATIONS),
            "parallel_workers": _int_range(2, 128),
            "topology_first_counts": first_counts,
            "resource_policy": {"const": "measured-observed; worker rollout caps remain per-call"},
        },
    )
    model_schema = _strict_object(
        AUTO_TOPOLOGY_MODEL_FIELDS,
        {
            "provider": {"const": "openai"},
            "name": _non_empty_string(),
            "effort": {"enum": ["low", "medium", "high", "xhigh", "ultra"]},
            "alternative_model_allowed": {"const": False},
        },
    )
    topology_schemas = []
    for name in AUTO_TOPOLOGIES:
        topology_schemas.append(
            _strict_object(
                AUTO_TOPOLOGY_FIELDS,
                {
                    "name": {"const": name},
                    "strategy": {"const": AUTO_TOPOLOGY_SPECS[name]["strategy"]},
                    "max_workers": _int_range(1, 128),
                    "planning_policy": {"const": AUTO_TOPOLOGY_SPECS[name]["planning_policy"]},
                },
            )
        )
    arm_artifact_schema = _strict_object(
        AUTO_TOPOLOGY_ARM_ARTIFACT_FIELDS,
        {field: _non_empty_string() for field in AUTO_TOPOLOGY_ARM_ARTIFACT_FIELDS},
    )
    artifacts_schema = _strict_object(
        set(AUTO_TOPOLOGIES),
        {name: arm_artifact_schema for name in AUTO_TOPOLOGIES},
    )
    budget_schema = _strict_object(
        AUTO_TOPOLOGY_BUDGET_FIELDS,
        {
            "max_minutes": _int_range(1, 24 * 60),
            "max_agents": _int_range(0, 128),
            "max_live_tool_cost": {"enum": ["none", "low", "medium", "high"]},
            "max_cost_usd": {"type": "number", "minimum": 0},
            "max_tokens": _int_range(0, 10**12),
        },
    )
    cohort_schema = _strict_object(
        (
            AUTO_TOPOLOGY_COHORT_FIELDS
            if current
            else AUTO_TOPOLOGY_COHORT_FIELDS_V2
        ),
        {
            "id": _safe_id(),
            "sequence": _int_range(1, MAX_AUTO_TOPOLOGY_COHORTS),
            "task_id": _safe_id(),
            "repetition": _int_range(1, MAX_AUTO_TOPOLOGY_REPETITIONS),
            "task_contract_sha256": _sha256(),
            "task_prompt_sha256": _sha256(),
            **({"task_execution_sha256": _sha256()} if current else {}),
            "source_fixture_sha256": _sha256(),
            "held_out_fixture_sha256": _sha256(),
            "budget": budget_schema,
            "arm_order": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "uniqueItems": True,
                "items": {"enum": list(AUTO_TOPOLOGIES)},
            },
            "artifacts": artifacts_schema,
            "status": {"const": "planned"},
        },
    )
    claim_schema = _strict_object(
        AUTO_TOPOLOGY_CLAIM_GATE_FIELDS,
        {
            "eligible_for_topology_claims": {"const": False},
            "required_before_claim": _string_list(min_items=1, non_empty=True),
        },
    )
    properties = {
        "schema": {"const": campaign_schema},
        "name": _safe_id(),
        "generated_at_utc": _non_empty_string(),
        "evidence_status": {"const": AUTO_TOPOLOGY_EVIDENCE_STATUS},
        "source": source_schema,
        "design": design_schema,
        "model": model_schema,
        "topologies": {
            "type": "array",
            "prefixItems": topology_schemas,
            "items": False,
            "minItems": 3,
            "maxItems": 3,
        },
        "cohorts": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_AUTO_TOPOLOGY_COHORTS,
            "items": cohort_schema,
        },
        "claim_gate": claim_schema,
    }
    campaign_fields = (
        AUTO_TOPOLOGY_CAMPAIGN_FIELDS_V1
        if legacy
        else AUTO_TOPOLOGY_CAMPAIGN_FIELDS
        if current
        else AUTO_TOPOLOGY_CAMPAIGN_FIELDS_V2
    )
    if not legacy:
        properties["runtime_version"] = {
            "type": "string",
            "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
        }
        properties["runtime_build_sha256"] = _sha256()
    schema.update(
        {
            "required": sorted(campaign_fields),
            "additionalProperties": False,
            "properties": properties,
        }
    )
    return schema


def _auto_topology_campaign_schema() -> Dict:
    return _auto_topology_campaign_schema_for(AUTO_TOPOLOGY_CAMPAIGN_SCHEMA)


def _auto_topology_campaign_v2_schema() -> Dict:
    return _auto_topology_campaign_schema_for(AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2)


def _auto_topology_campaign_v1_schema() -> Dict:
    return _auto_topology_campaign_schema_for(AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1)


def _readonly_parity_campaign_schema_for(campaign_schema: str) -> Dict:
    legacy = campaign_schema == READONLY_PARITY_CAMPAIGN_SCHEMA_V1
    schema = _base_schema(
        campaign_schema,
        "Conductor Read-Only Parity Campaign%s" % (" v1" if legacy else ""),
        (
            "JSON Schema for frozen matched serial Codex, native Codex, and Claude Sonnet Ultracode diagnostic campaigns."
            if legacy
            else "JSON Schema for matched or product-profile serial Codex, native Codex, and Claude Sonnet Ultracode diagnostic campaigns."
        ),
    )
    source_schema = _strict_object(
        READONLY_PARITY_SOURCE_FIELDS,
        {
            "parity_tasks_schema": {"const": PARITY_TASKS_SCHEMA},
            "parity_tasks_source": {"type": ["string", "null"]},
            "parity_tasks_sha256": _sha256(),
            "suite": {"const": "conductor-readonly-diagnostic-canary"},
            "task_count": {"const": 3},
            "required_dimensions": _string_list(min_items=1, non_empty=True),
            "fixture_profile": {"const": "implementation-canary-diagnostic-v1"},
        },
    )
    first_counts = _strict_object(
        set(READONLY_PARITY_SYSTEMS),
        {name: _int_range(0, MAX_READONLY_PARITY_COHORTS) for name in READONLY_PARITY_SYSTEMS},
    )
    design_fields = READONLY_PARITY_DESIGN_FIELDS_V1 if legacy else READONLY_PARITY_DESIGN_FIELDS
    design_properties = {
        "seed": _int_range(0, 2**63 - 1),
        "repetitions": _int_range(1, MAX_READONLY_PARITY_REPETITIONS),
        "randomization_algorithm": {"const": READONLY_PARITY_RANDOMIZATION},
        "cohort_count": _int_range(1, MAX_READONLY_PARITY_COHORTS),
        "arm_count": _int_range(3, MAX_READONLY_PARITY_COHORTS * 3),
        "system_first_counts": first_counts,
        "minimum_claim_tasks": {"const": 3},
        "minimum_claim_repetitions": {"const": 2},
        "resource_policy": (
            {"const": READONLY_PARITY_RESOURCE_POLICIES["matched-v1"]}
            if legacy
            else {"enum": sorted(READONLY_PARITY_RESOURCE_POLICIES.values())}
        ),
    }
    if not legacy:
        design_properties["budget_profile"] = {"enum": list(READONLY_PARITY_BUDGET_PROFILES)}
    design_schema = _strict_object(design_fields, design_properties)
    system_schemas = []
    for name in READONLY_PARITY_SYSTEMS:
        codex = name != "claude-sonnet"
        native = name == "native-codex"
        system_schemas.append(
            _strict_object(
                READONLY_PARITY_SYSTEM_FIELDS,
                {
                    "name": {"const": name},
                    "provider": {"const": "openai" if codex else "anthropic"},
                    "model": _non_empty_string() if codex else {"const": "sonnet"},
                    "effort": {"enum": ["low", "medium", "high", "xhigh", "ultra"]} if codex else {"const": "ultracode"},
                    "topology": {"const": "native-depth-1" if native else "serial" if codex else "provider-managed"},
                    "max_tokens": (
                        {"const": READONLY_PARITY_CODEX_TOKEN_CAP}
                        if legacy and codex
                        else {
                            "enum": sorted(
                                {
                                    profile_caps[name]
                                    for profile_caps in READONLY_PARITY_PROFILE_TOKEN_CAPS.values()
                                }
                            )
                        }
                        if codex
                        else {"const": None}
                    ),
                    "native_agent_threads": {"const": READONLY_PARITY_NATIVE_THREADS if native else 0 if codex else None},
                    "max_turns": {"const": None if codex else READONLY_PARITY_CLAUDE_MAX_TURNS},
                    "cap_enforcement": {
                        "const": (
                            "runtime-weighted-threshold-fail-closed"
                            if codex
                            else "provider-dollar-threshold-fail-closed"
                        )
                    },
                    "alternative_main_model_allowed": {"const": False},
                },
            )
        )
    artifact_schema = _strict_object(
        READONLY_PARITY_ARTIFACT_FIELDS,
        {field: _non_empty_string() for field in READONLY_PARITY_ARTIFACT_FIELDS},
    )
    artifacts_schema = _strict_object(
        set(READONLY_PARITY_SYSTEMS),
        {name: artifact_schema for name in READONLY_PARITY_SYSTEMS},
    )
    budget_schema = _strict_object(
        {"max_minutes", "max_agents", "max_live_tool_cost", "max_cost_usd", "max_tokens"},
        {
            "max_minutes": _int_range(1, 24 * 60),
            "max_agents": _int_range(2, 1000),
            "max_live_tool_cost": {"enum": ["low", "medium", "high"]},
            "max_cost_usd": {"type": "number", "exclusiveMinimum": 0},
            "max_tokens": (
                {"const": READONLY_PARITY_CODEX_TOKEN_CAP}
                if legacy
                else {
                    "enum": sorted(
                        {
                            max(profile_caps.values())
                            for profile_caps in READONLY_PARITY_PROFILE_TOKEN_CAPS.values()
                        }
                    )
                }
            ),
        },
    )
    cohort_schema = _strict_object(
        READONLY_PARITY_COHORT_FIELDS,
        {
            "id": _safe_id(),
            "sequence": _int_range(1, MAX_READONLY_PARITY_COHORTS),
            "task_id": _safe_id(),
            "repetition": _int_range(1, MAX_READONLY_PARITY_REPETITIONS),
            "task_contract_sha256": _sha256(),
            "rendered_task_sha256": _sha256(),
            "source_fixture_sha256": _sha256(),
            "budget": budget_schema,
            "arm_order": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "uniqueItems": True,
                "items": {"enum": list(READONLY_PARITY_SYSTEMS)},
            },
            "artifacts": artifacts_schema,
            "status": {"const": "planned"},
        },
    )
    claim_schema = _strict_object(
        READONLY_PARITY_CLAIM_GATE_FIELDS,
        {
            "eligible_for_claims": {"const": False},
            "required_before_claim": _string_list(min_items=5, non_empty=True),
        },
    )
    schema.update(
        {
            "required": sorted(READONLY_PARITY_CAMPAIGN_FIELDS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": campaign_schema},
                "name": _safe_id(),
                "generated_at_utc": _non_empty_string(),
                "evidence_status": {"const": READONLY_PARITY_EVIDENCE_STATUS},
                "runtime_version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
                "runtime_build_sha256": _sha256(),
                "source": source_schema,
                "design": design_schema,
                "systems": {
                    "type": "array",
                    "prefixItems": system_schemas,
                    "items": False,
                    "minItems": 3,
                    "maxItems": 3,
                },
                "cohorts": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_READONLY_PARITY_COHORTS,
                    "items": cohort_schema,
                },
                "claim_gate": claim_schema,
            },
        }
    )
    if not legacy:
        profile_branches = []
        for profile in READONLY_PARITY_BUDGET_PROFILES:
            caps = READONLY_PARITY_PROFILE_TOKEN_CAPS[profile]
            profile_branches.append(
                {
                    "properties": {
                        "design": {
                            "properties": {
                                "budget_profile": {"const": profile},
                                "resource_policy": {
                                    "const": READONLY_PARITY_RESOURCE_POLICIES[profile]
                                },
                            },
                            "required": ["budget_profile", "resource_policy"],
                        },
                        "systems": {
                            "prefixItems": [
                                {
                                    "properties": {
                                        "max_tokens": {"const": caps["serial-codex"]}
                                    }
                                },
                                {
                                    "properties": {
                                        "max_tokens": {"const": caps["native-codex"]}
                                    }
                                },
                                {},
                            ]
                        },
                        "cohorts": {
                            "items": {
                                "properties": {
                                    "budget": {
                                        "properties": {
                                            "max_tokens": {"const": max(caps.values())}
                                        }
                                    }
                                }
                            }
                        },
                    },
                    "required": ["design", "systems", "cohorts"],
                }
            )
        schema["oneOf"] = profile_branches
    return schema


def _readonly_parity_campaign_schema() -> Dict:
    return _readonly_parity_campaign_schema_for(READONLY_PARITY_CAMPAIGN_SCHEMA)


def _readonly_parity_campaign_v1_schema() -> Dict:
    return _readonly_parity_campaign_schema_for(READONLY_PARITY_CAMPAIGN_SCHEMA_V1)


def _live_parity_runbook_schema() -> Dict:
    schema = _base_schema(
        LIVE_PARITY_RUNBOOK_SCHEMA,
        "Conductor Live Parity Runbook",
        "JSON Schema for conductor.live_parity_runbook.v1 planned live parity runbooks.",
    )
    schema.update(
        {
            "required": sorted(LIVE_PARITY_RUNBOOK_FIELDS),
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in {
                    "schema": {"const": LIVE_PARITY_RUNBOOK_SCHEMA},
                    "name": _safe_id(),
                    "suite": _non_empty_string(),
                    "generated_at_utc": _non_empty_string(),
                    "evidence_status": {"const": LIVE_PARITY_RUNBOOK_EVIDENCE_STATUS},
                    "source": _live_runbook_source_schema(),
                    "aggregate_budget": _run_budget_schema(),
                    "systems": {"type": "array", "minItems": 2, "items": _live_runbook_system_schema()},
                    "tasks": {"type": "array", "minItems": 1, "items": _live_runbook_task_schema()},
                    "operator_checklist": _string_list(min_items=1, non_empty=True),
                    "claim_gate": _live_runbook_claim_gate_schema(),
                }.items()
                if key in LIVE_PARITY_RUNBOOK_FIELDS
            },
        }
    )
    return schema


def _evidence_bundle_schema() -> Dict:
    schema = _base_schema(
        EVIDENCE_BUNDLE_SCHEMA,
        "Conductor Evidence Bundle",
        "JSON Schema for conductor.evidence_bundle.v1 local parity evidence bundles.",
    )
    schema.update(
        {
            "required": sorted(EVIDENCE_TOP_LEVEL_FIELDS),
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in {
                    "schema": {"const": EVIDENCE_BUNDLE_SCHEMA},
                    "generated_at_utc": _non_empty_string(),
                    "source": _strict_string_fields(SOURCE_FIELDS),
                    "limits": _int_object_schema(LIMIT_FIELDS, minimum=1, maximum=MAX_EVIDENCE_ITEMS),
                    "counts": _int_object_schema(COUNT_FIELDS, minimum=0),
                    "claim": _evidence_claim_schema(),
                    "runs": _evidence_list(_evidence_run_schema()),
                    "goals": _evidence_list(_evidence_goal_schema()),
                    "routines": _evidence_list(_evidence_routine_schema()),
                    "reports": _evidence_list(_evidence_report_schema()),
                }.items()
                if key in EVIDENCE_TOP_LEVEL_FIELDS
            },
        }
    )
    return schema


def _routine_manifest_schema() -> Dict:
    schema = _base_schema(
        ROUTINE_MANIFEST_SCHEMA,
        "Conductor Routine Manifest",
        "JSON Schema for conductor.routine_manifest.v1 external-scheduler routine manifests.",
    )
    schema.update(
        {
            "required": sorted(ROUTINE_TOP_LEVEL_KEYS),
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in {
                    "schema": {"const": ROUTINE_MANIFEST_SCHEMA},
                    "name": _safe_id(),
                    "created_at_utc": _timestamp_string(),
                    "evidence_status": {"const": "planned-not-evidence"},
                    "schedule": _routine_schedule_schema(),
                    "target": _routine_target_schema(),
                    "launch": _routine_launch_schema(),
                    "budget": _routine_budget_schema(),
                    "external_supervisor_contract": _routine_external_supervisor_schema(),
                    "no_process_started": {"const": True},
                    "process_model": {"const": "operator-owned external scheduler launch"},
                }.items()
                if key in ROUTINE_TOP_LEVEL_KEYS
            },
        }
    )
    return schema


def _routine_controls_schema() -> Dict:
    nullable_schedule = {"oneOf": [_routine_schedule_schema(), {"type": "null"}]}
    event = _strict_object(
        {"action", "at_utc", "reason", "schedule_before", "schedule_after"},
        {
            "action": {"enum": sorted(ROUTINE_CONTROL_ACTIONS)},
            "at_utc": _timestamp_string(),
            "reason": {"type": "string", "maxLength": MAX_ROUTINE_REASON_CHARS},
            "schedule_before": nullable_schedule,
            "schedule_after": nullable_schedule,
        },
    )
    record = _strict_object(
        {"name", "paused", "updated_at_utc", "last_action", "reason", "events"},
        {
            "name": _safe_id(),
            "paused": {"type": "boolean"},
            "updated_at_utc": _timestamp_string(),
            "last_action": {"enum": sorted(ROUTINE_CONTROL_ACTIONS)},
            "reason": {"type": "string", "maxLength": MAX_ROUTINE_REASON_CHARS},
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_ROUTINE_CONTROL_EVENTS,
                "items": event,
            },
        },
    )
    schema = _base_schema(
        ROUTINE_CONTROLS_SCHEMA,
        "Conductor Routine Controls",
        "External pause, resume, and schedule-update control state for durable routine manifests.",
    )
    schema.update(
        _strict_object(
            {"schema", "updated_at_utc", "controls"},
            {
                "schema": {"const": ROUTINE_CONTROLS_SCHEMA},
                "updated_at_utc": {"oneOf": [_timestamp_string(), {"type": "null"}]},
                "controls": {
                    "type": "object",
                    "maxProperties": MAX_LIST_ROUTINES,
                    "propertyNames": {"pattern": SAFE_ID.pattern},
                    "additionalProperties": record,
                },
            },
        )
    )
    return schema


def _routine_service_grant_schema() -> Dict:
    runtime = _strict_object(
        ROUTINE_SERVICE_RUNTIME_KEYS,
        {
            "path": {"type": "string", "pattern": r"^/", "maxLength": 4096},
            "sha256": _sha256(),
            "python_path": {"type": "string", "pattern": r"^/", "maxLength": 4096},
        },
    )
    limits = _strict_object(
        ROUTINE_SERVICE_LIMIT_KEYS,
        {
            "poll_seconds": _int_range(1, MAX_SUPERVISOR_POLL_SECONDS),
            "max_routines": _int_range(1, MAX_LIST_ROUTINES),
            "output_limit_bytes": _int_range(1, DEFAULT_OUTPUT_LIMIT_BYTES),
        },
    )
    routine = _strict_object(
        ROUTINE_SERVICE_ROUTINE_KEYS,
        {
            "name": _safe_id(),
            "manifest_name": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$"},
            "manifest_sha256": _sha256(),
            "workflow_fingerprint": _sha256(),
            "approval_sha256": {
                "type": "array",
                "maxItems": 1000,
                "uniqueItems": True,
                "items": _sha256(),
            },
        },
    )
    authorization = _strict_object(
        ROUTINE_SERVICE_AUTHORIZATION_KEYS,
        {
            "raw_approval_values_persisted": {"const": False},
            "wildcard_approval_allowed": {"const": False},
            "exact_manifest_binding": {"const": True},
            "exact_runtime_binding": {"const": True},
            "bounded_expiration": {"const": True},
            "unlisted_routines_allowed": {"const": False},
        },
    )
    schema = _base_schema(
        ROUTINE_SERVICE_GRANT_SCHEMA,
        "Conductor Routine Service Grant",
        "JSON Schema for explicit, expiring, hash-bound durable routine-service grants.",
    )
    schema.update(
        {
            "required": sorted(ROUTINE_SERVICE_GRANT_KEYS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": ROUTINE_SERVICE_GRANT_SCHEMA},
                "service_id": {"type": "string", "pattern": r"^routine-[a-f0-9]{20}$"},
                "created_at_utc": _timestamp_string(),
                "expires_at_utc": _timestamp_string(),
                "platform": {"enum": sorted(ROUTINE_SERVICE_PLATFORMS)},
                "routines_dir": {"type": "string", "pattern": r"^/", "maxLength": 4096},
                "runtime": runtime,
                "limits": limits,
                "routines": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_LIST_ROUTINES,
                    "items": routine,
                },
                "authorization": authorization,
            },
        }
    )
    return schema


def _routine_service_state_schema() -> Dict:
    schema = _base_schema(
        ROUTINE_SERVICE_STATE_SCHEMA,
        "Conductor Routine Service State",
        "JSON Schema for visible durable routine-service lifecycle and heartbeat state.",
    )
    schema.update(
        {
            "required": sorted(ROUTINE_SERVICE_STATE_KEYS),
            "additionalProperties": False,
            "properties": {
                "schema": {"const": ROUTINE_SERVICE_STATE_SCHEMA},
                "service_id": {"type": "string", "pattern": r"^routine-[a-f0-9]{20}$"},
                "grant_sha256": _sha256(),
                "status": {"enum": sorted(ROUTINE_SERVICE_STATUSES)},
                "pid": _int_range(1),
                "started_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "heartbeat_at_utc": _timestamp_string(),
                "cycles_completed": _int_range(0),
                "results_completed": _int_range(0),
                "results_failed": _int_range(0),
                "last_error": {"type": "string", "maxLength": MAX_SERVICE_ERROR_CHARS},
                "raw_approval_values_persisted": {"const": False},
            },
        }
    )
    return schema


def _routine_state_schema() -> Dict:
    nullable_timestamp = {"oneOf": [_timestamp_string(), {"type": "null"}]}
    history = _strict_object(
        ROUTINE_HISTORY_FIELDS,
        {
            "started_at_utc": _timestamp_string(),
            "finished_at_utc": _timestamp_string(),
            "scheduled_for_utc": nullable_timestamp,
            "trigger": {"enum": sorted(ROUTINE_TRIGGERS)},
            "status": {"enum": sorted(ROUTINE_EXECUTION_STATUSES - {"running"})},
            "returncode": {"type": ["integer", "null"]},
            "duration_ms": _int_range(0),
            "stdout_log": {"type": "string"},
            "stderr_log": {"type": "string"},
            "detail": {"type": "string", "maxLength": MAX_ROUTINE_REASON_CHARS},
        },
    )
    record = {
        "type": "object",
        "additionalProperties": False,
        "required": ["last_status", "approval_values_persisted"],
        "properties": {
            "manifest": {"type": "string"},
            "last_started_at_utc": nullable_timestamp,
            "last_finished_at_utc": nullable_timestamp,
            "last_status": {"enum": sorted(ROUTINE_EXECUTION_STATUSES)},
            "last_trigger": {"enum": sorted(ROUTINE_TRIGGERS)},
            "last_scheduled_for_utc": nullable_timestamp,
            "last_returncode": {"type": ["integer", "null"]},
            "last_duration_ms": _int_range(0),
            "last_stdout_log": {"type": "string"},
            "last_stderr_log": {"type": "string"},
            "executions": _int_range(0),
            "history": {"type": "array", "maxItems": MAX_ROUTINE_HISTORY, "items": history},
            "approval_values_persisted": {"const": False},
            "owner_pid": _int_range(1),
        },
    }
    schema = _base_schema(
        ROUTINE_SUPERVISOR_SCHEMA,
        "Conductor Routine State",
        "Bounded external execution state and history for durable routines.",
    )
    schema.update(
        _strict_object(
            {"schema", "updated_at_utc", "routines"},
            {
                "schema": {"const": ROUTINE_SUPERVISOR_SCHEMA},
                "updated_at_utc": _timestamp_string(),
                "routines": {
                    "type": "object",
                    "maxProperties": MAX_LIST_ROUTINES,
                    "propertyNames": {"pattern": SAFE_ID.pattern},
                    "additionalProperties": record,
                },
            },
        )
    )
    return schema


def _model_workflow_receipt_schema() -> Dict:
    schema = _base_schema(
        MODEL_WORKFLOW_RECEIPT_SCHEMA,
        "Conductor Model Workflow Receipt",
        "Receipt for a validated model-authored or deterministic-direct workflow draft that has not been executed.",
    )
    usage = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "input_tokens": _int_range(0),
            "output_tokens": _int_range(0),
            "total_tokens": _int_range(0),
            "cost_usd": {"type": "number", "minimum": 0},
        },
    }
    schema.update(
        _strict_object(
            MODEL_WORKFLOW_RECEIPT_FIELDS,
            {
                "schema": {"const": MODEL_WORKFLOW_RECEIPT_SCHEMA},
                "generated_at_utc": _timestamp_string(),
                "status": {"const": MODEL_WORKFLOW_RECEIPT_STATUS},
                "evidence_status": {"const": MODEL_WORKFLOW_EVIDENCE_STATUS},
                "prompt_contract": {
                    "enum": sorted(
                        {
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V3,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V4,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V5,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V6,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V7,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V8,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V9,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V10,
                            MODEL_WORKFLOW_PROMPT_CONTRACT_V11,
                            MODEL_WORKFLOW_PROMPT_CONTRACT,
                            DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
                            DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
                            DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V3,
                            DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT,
                            DIRECT_NATIVE_MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
                            DIRECT_NATIVE_MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
                            DIRECT_NATIVE_MODEL_WORKFLOW_PROMPT_CONTRACT,
                        }
                    )
                },
                "task": _strict_object(
                    {"sha256", "chars", "raw_persisted"},
                    {
                        "sha256": _sha256(),
                        "chars": _int_range(1, MAX_MODEL_WORKFLOW_TASK_CHARS),
                        "raw_persisted": {"const": False},
                    },
                ),
                "workflow": _strict_object(
                    {"name", "filename", "sha256", "steps", "mode"},
                    {
                        "name": _non_empty_string(),
                        "filename": _non_empty_string(),
                        "sha256": _sha256(),
                        "steps": _int_range(1, MAX_MODEL_WORKFLOW_STEPS),
                        "mode": {"enum": ["read_only", "review", "workspace_write", "custom"]},
                    },
                ),
                "model": _strict_object(
                    {"requested", "reasoning_effort", "observed"},
                    {
                        "requested": {"type": ["string", "null"]},
                        "reasoning_effort": {"enum": sorted(MODEL_WORKFLOW_REASONING_EFFORTS)},
                        "observed": {"type": "array", "maxItems": 1000, "items": _non_empty_string()},
                    },
                ),
                "limits": _strict_object(
                    {
                        "max_workers",
                        "max_items",
                        "max_steps",
                        "timeout_seconds",
                        "output_limit_bytes",
                        "planner_max_tokens",
                    },
                    {
                        "max_workers": _int_range(1, MAX_AGENT_WORKERS),
                        "max_items": _int_range(1, MAX_MODEL_WORKFLOW_ITEMS),
                        "max_steps": _int_range(1, MAX_MODEL_WORKFLOW_STEPS),
                        "timeout_seconds": _int_range(1, MAX_MODEL_WORKFLOW_TIMEOUT_SECONDS),
                        "output_limit_bytes": _int_range(1, MAX_MODEL_WORKFLOW_OUTPUT_LIMIT_BYTES),
                        "planner_max_tokens": _int_range(0),
                    },
                ),
                "policy": _strict_object(
                    {
                        "planner_sandbox",
                        "write_capable_draft_allowed",
                        "hooks_allowed",
                        "mutating_shell_allowed",
                        "network_shell_allowed",
                        "parallel_write_agents_allowed",
                        "planner_token_cap_enforcement",
                        "planner_completion_checkpoint",
                        "planner_isolated_profile",
                    },
                    {
                        "planner_sandbox": {"const": "read-only"},
                        "write_capable_draft_allowed": {"type": "boolean"},
                        "hooks_allowed": {"const": False},
                        "mutating_shell_allowed": {"const": False},
                        "network_shell_allowed": {"const": False},
                        "parallel_write_agents_allowed": {"const": False},
                        "planner_token_cap_enforcement": {
                            "enum": ["runtime-hard", "not-applicable"]
                        },
                        "planner_completion_checkpoint": {"type": "boolean"},
                        "planner_isolated_profile": {"type": "boolean"},
                    },
                ),
                "provider": _strict_object(
                    {"event_count", "usage"},
                    {"event_count": _int_range(0, 1000), "usage": usage},
                ),
                "execution": _strict_object(
                    {"no_process_started", "review_required", "automatic_execution"},
                    {
                        "no_process_started": {"const": True},
                        "review_required": {"const": True},
                        "automatic_execution": {"const": False},
                    },
                ),
                "workspace": _strict_object(
                    MODEL_WORKFLOW_WORKSPACE_FIELDS,
                    {
                        "mode": {"enum": ["bound", "unbound"]},
                        "policy": {
                            "enum": [MODEL_WORKFLOW_WORKSPACE_BINDING_POLICY, "none"]
                        },
                        "path_sha256": {"anyOf": [_sha256(), {"type": "null"}]},
                        "tracked_fingerprint_sha256": {
                            "anyOf": [_sha256(), {"type": "null"}]
                        },
                        "file_count": {
                            "anyOf": [_int_range(0, MAX_STAGED_FILES), {"type": "null"}]
                        },
                        "total_bytes": {
                            "anyOf": [_int_range(0, MAX_STAGED_TOTAL_BYTES), {"type": "null"}]
                        },
                        "excluded_directory_count": {
                            "anyOf": [_int_range(0, MAX_STAGED_FILES), {"type": "null"}]
                        },
                        "excluded_directories_sha256": {
                            "anyOf": [_sha256(), {"type": "null"}]
                        },
                        "raw_paths_persisted": {"const": False},
                    },
                ),
            },
        )
    )
    return schema


def _completion_verdict_feedback_schema() -> Dict:
    schema = _base_schema(
        COMPLETION_VERDICT_FEEDBACK_SCHEMA,
        "Conductor Completion Verdict Feedback Receipt",
        "Content-free receipt for one private unsatisfied completion verdict used by a bounded goal iteration.",
    )
    schema.update(
        _strict_object(
            {
                "schema",
                "step_id",
                "contract",
                "confidence",
                "evidence_count",
                "feedback_present",
                "feedback_chars",
                "feedback_sha256",
                "output_bytes",
                "output_sha256",
            },
            {
                "schema": {"const": COMPLETION_VERDICT_FEEDBACK_SCHEMA},
                "step_id": {"type": "string", "pattern": SAFE_ID.pattern},
                "contract": {"const": COMPLETION_VERDICT_CONTRACT},
                "confidence": {"enum": ["low", "medium", "high"]},
                "evidence_count": _int_range(0, MAX_MODEL_EVIDENCE_ITEMS),
                "feedback_present": {"const": True},
                "feedback_chars": _int_range(1, MAX_MODEL_FEEDBACK_CHARS),
                "feedback_sha256": _sha256(),
                "output_bytes": _int_range(1, MAX_MODEL_VERDICT_BYTES),
                "output_sha256": _sha256(),
            },
        )
    )
    return schema


def _auto_orchestration_receipt_schema() -> Dict:
    schema = _base_schema(
        AUTO_ORCHESTRATION_RECEIPT_SCHEMA,
        "Conductor Auto Orchestration Receipt",
        "Strict routing and lifecycle evidence for the deterministic auto front door.",
    )
    artifact = _strict_object(
        AUTO_ARTIFACT_FIELDS,
        {
            "kind": {"enum": sorted(AUTO_ARTIFACT_KINDS)},
            "filename": {"type": "string", "minLength": 1, "maxLength": 255},
            "path_sha256": _sha256(),
            "content_sha256": _sha256(),
            "content_state": {"enum": sorted(AUTO_ARTIFACT_CONTENT_STATES)},
        },
    )
    schema.update(
        _strict_object(
            AUTO_RECEIPT_FIELDS,
            {
                "schema": {"const": AUTO_ORCHESTRATION_RECEIPT_SCHEMA},
                "generated_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "status": {"enum": sorted(AUTO_RECEIPT_STATUSES)},
                "task": _strict_object(
                    AUTO_TASK_FIELDS,
                    {
                        "sha256": _sha256(),
                        "chars": _int_range(1, MAX_MODEL_WORKFLOW_TASK_CHARS),
                        "raw_persisted": {"const": False},
                    },
                ),
                "decision": _strict_object(
                    AUTO_DECISION_FIELDS,
                    {
                        "route_version": {"enum": sorted(AUTO_ROUTE_VERSIONS)},
                        "strategy_requested": {"enum": sorted(AUTO_STRATEGIES)},
                        "route": {"enum": sorted(AUTO_ROUTES)},
                        "reason_code": {"enum": sorted(AUTO_REASON_CODES)},
                        "execution_mode": {"enum": sorted(AUTO_EXECUTION_MODES)},
                        "execution_reason_code": {"enum": sorted(AUTO_EXECUTION_REASON_CODES)},
                        "verifier_kind": {"enum": sorted(AUTO_VERIFIER_KINDS)},
                        "worker_source": {"enum": sorted(AUTO_WORKER_SOURCES)},
                        "write_capable": {"type": "boolean"},
                        "task_text_controls_route": {"const": False},
                        "task_text_controls_capabilities": {"const": False},
                    },
                ),
                "policy": _strict_object(
                    AUTO_POLICY_FIELDS,
                    {
                        "allow_writes": {"type": "boolean"},
                        "allow_destructive": {"type": "boolean"},
                        "allow_network": {"type": "boolean"},
                        "allow_agent": {"type": "boolean"},
                        "allow_parallel": {"type": "boolean"},
                        "execution_approval_present": {"type": "boolean"},
                        "write_approval_present": {"type": "boolean"},
                        "native_agent_approval_present": {"type": "boolean"},
                        "approval_count": _int_range(0, 10000),
                        "approval_values_persisted": {"const": False},
                    },
                ),
                "limits": _strict_object(
                    AUTO_LIMIT_FIELDS,
                    {
                        "max_workers": _int_range(1, MAX_AGENT_WORKERS),
                        "max_items": _int_range(1, MAX_MODEL_WORKFLOW_ITEMS),
                        "max_steps": _int_range(1, MAX_MODEL_WORKFLOW_STEPS),
                        "max_iterations": _int_range(1, MAX_AUTO_ITERATIONS),
                        "planning_timeout_seconds": _int_range(1, MAX_MODEL_WORKFLOW_TIMEOUT_SECONDS),
                        "planning_output_limit_bytes": _int_range(1, MAX_MODEL_WORKFLOW_OUTPUT_LIMIT_BYTES),
                        "check_timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
                        "check_output_limit_bytes": _int_range(1, MAX_WORKFLOW_OUTPUT_LIMIT_BYTES),
                        "planner_effort": {"enum": sorted(MODEL_WORKFLOW_REASONING_EFFORTS)},
                        "native_agent_max_threads": {
                            "anyOf": [
                                _int_range(
                                    MIN_CODEX_NATIVE_AGENT_THREADS,
                                    MAX_CODEX_NATIVE_AGENT_THREADS,
                                ),
                                {"type": "null"},
                            ]
                        },
                        "native_agent_max_total_tokens": {
                            "anyOf": [
                                _int_range(
                                    MIN_CODEX_RUNTIME_TOKEN_CAP,
                                    MAX_CODEX_TOKENS,
                                ),
                                {"type": "null"},
                            ]
                        },
                    },
                ),
                "artifacts": {
                    "type": "array",
                    "maxItems": MAX_AUTO_ARTIFACTS,
                    "items": artifact,
                },
                "result": _strict_object(
                    AUTO_RESULT_FIELDS,
                    {
                        "status": {"enum": sorted(AUTO_RECEIPT_STATUSES)},
                        "execution_started": {"type": "boolean"},
                        "background_handoff": {"type": "boolean"},
                        "iterations": _int_range(0, MAX_AUTO_ITERATIONS),
                        "run_count": _int_range(0, MAX_AUTO_ITERATIONS),
                        "error_class": {"type": "string", "maxLength": 200},
                    },
                ),
            },
        )
    )
    return schema


def _auto_orchestration_receipt_v1_schema() -> Dict:
    schema = _auto_orchestration_receipt_schema()
    schema["$id"] = (
        "https://codex-conductor.local/schemas/%s.schema.json"
        % AUTO_ORCHESTRATION_RECEIPT_SCHEMA_V1
    )
    schema["title"] = "Conductor Auto Orchestration Receipt v1"
    schema["properties"]["schema"] = {"const": AUTO_ORCHESTRATION_RECEIPT_SCHEMA_V1}
    decision = schema["properties"]["decision"]["properties"]
    decision["route_version"] = {
        "enum": sorted(AUTO_ROUTE_VERSIONS - {AUTO_ROUTE_VERSION})
    }
    decision["strategy_requested"] = {"enum": sorted(AUTO_STRATEGIES_V1)}
    decision["reason_code"] = {"enum": sorted(AUTO_REASON_CODES_V1)}
    decision["worker_source"] = {"enum": sorted(AUTO_WORKER_SOURCES_V1)}
    policy = schema["properties"]["policy"]
    policy["required"] = sorted(AUTO_POLICY_FIELDS_V1)
    del policy["properties"]["native_agent_approval_present"]
    limits = schema["properties"]["limits"]
    limits["required"] = sorted(AUTO_LIMIT_FIELDS_V1)
    del limits["properties"]["native_agent_max_threads"]
    del limits["properties"]["native_agent_max_total_tokens"]
    return schema


def _model_workflow_receipt_v3_schema() -> Dict:
    schema = _model_workflow_receipt_schema()
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % MODEL_WORKFLOW_RECEIPT_SCHEMA_V3
    schema["title"] = "Conductor Model Workflow Receipt v3"
    schema["properties"]["schema"] = {"const": MODEL_WORKFLOW_RECEIPT_SCHEMA_V3}
    schema["required"] = sorted(MODEL_WORKFLOW_RECEIPT_FIELDS_V3)
    del schema["properties"]["workspace"]
    schema["properties"]["prompt_contract"] = {
        "enum": sorted(
            {
                MODEL_WORKFLOW_PROMPT_CONTRACT_V2,
                MODEL_WORKFLOW_PROMPT_CONTRACT_V3,
                DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
            }
        )
    }
    return schema


def _model_workflow_receipt_v2_schema() -> Dict:
    schema = _model_workflow_receipt_v3_schema()
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % MODEL_WORKFLOW_RECEIPT_SCHEMA_V2
    schema["title"] = "Conductor Model Workflow Receipt v2"
    schema["properties"]["schema"] = {"const": MODEL_WORKFLOW_RECEIPT_SCHEMA_V2}
    schema["properties"]["prompt_contract"] = {
        "enum": [
            MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
            DIRECT_MODEL_WORKFLOW_PROMPT_CONTRACT_V1,
        ]
    }
    limits = schema["properties"]["limits"]
    limits["required"].remove("planner_max_tokens")
    del limits["properties"]["planner_max_tokens"]
    policy = schema["properties"]["policy"]
    for field in (
        "planner_token_cap_enforcement",
        "planner_completion_checkpoint",
        "planner_isolated_profile",
    ):
        policy["required"].remove(field)
        del policy["properties"][field]
    return schema


def _model_workflow_receipt_v1_schema() -> Dict:
    schema = _model_workflow_receipt_v2_schema()
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % MODEL_WORKFLOW_RECEIPT_SCHEMA_V1
    schema["title"] = "Conductor Model Workflow Receipt v1"
    schema["properties"]["schema"] = {"const": MODEL_WORKFLOW_RECEIPT_SCHEMA_V1}
    schema["properties"]["prompt_contract"] = {"const": MODEL_WORKFLOW_PROMPT_CONTRACT_V1}
    model = schema["properties"]["model"]
    model["required"].remove("reasoning_effort")
    del model["properties"]["reasoning_effort"]
    return schema


def _model_workflow_run_receipt_schema() -> Dict:
    schema = _base_schema(
        MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA,
        "Conductor Model Workflow Run Receipt",
        "Receipt for an explicitly approved foreground or detached handoff from a validated workflow draft to preflight and execution.",
    )
    schema.update(
        _strict_object(
            RUN_RECEIPT_FIELDS,
            {
                "schema": {"const": MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA},
                "generated_at_utc": _timestamp_string(),
                "status": {"enum": sorted(MODEL_WORKFLOW_RUN_STATUSES)},
                "task": _strict_object(
                    {"sha256", "chars", "raw_persisted"},
                    {
                        "sha256": _sha256(),
                        "chars": _int_range(1, MAX_MODEL_WORKFLOW_TASK_CHARS),
                        "raw_persisted": {"const": False},
                    },
                ),
                "workflow": _strict_object(
                    {"available", "name", "filename", "sha256", "steps", "mode"},
                    {
                        "available": {"type": "boolean"},
                        "name": {"type": "string"},
                        "filename": _non_empty_string(),
                        "sha256": {"anyOf": [_sha256(), {"type": "null"}]},
                        "steps": _int_range(0, MAX_MODEL_WORKFLOW_STEPS),
                        "mode": {"type": "string"},
                    },
                ),
                "planning": _strict_object(
                    {"status", "receipt_filename", "receipt_sha256"},
                    {
                        "status": {"enum": [MODEL_WORKFLOW_RECEIPT_STATUS, "failed"]},
                        "receipt_filename": {"type": "string"},
                        "receipt_sha256": {"anyOf": [_sha256(), {"type": "null"}]},
                    },
                ),
                "preflight": _strict_object(
                    {"completed", "status"},
                    {"completed": {"type": "boolean"}, "status": _non_empty_string()},
                ),
                "execution": _strict_object(
                    {
                        "mode",
                        "foreground",
                        "automatic_handoff",
                        "started",
                        "execute_approval_id",
                        "execute_approval_present",
                        "background_service",
                        "approval_values_persisted",
                    },
                    {
                        "mode": {"enum": ["run", "dry-run"]},
                        "foreground": {"type": "boolean"},
                        "automatic_handoff": {"type": "boolean"},
                        "started": {"type": "boolean"},
                        "execute_approval_id": {"const": MODEL_WORKFLOW_EXECUTE_APPROVAL},
                        "execute_approval_present": {"type": "boolean"},
                        "background_service": {"const": False},
                        "approval_values_persisted": {"const": False},
                    },
                ),
                "policy": _strict_object(
                    {
                        "allow_writes",
                        "allow_destructive",
                        "allow_network",
                        "allow_agent",
                        "allow_parallel",
                        "approval_count",
                        "approval_values_persisted",
                    },
                    {
                        "allow_writes": {"type": "boolean"},
                        "allow_destructive": {"type": "boolean"},
                        "allow_network": {"type": "boolean"},
                        "allow_agent": {"type": "boolean"},
                        "allow_parallel": {"type": "boolean"},
                        "approval_count": _int_range(0, 1000),
                        "approval_values_persisted": {"const": False},
                    },
                ),
                "limits": _strict_object(
                    {
                        "max_workers",
                        "max_items",
                        "max_steps",
                        "planning_timeout_seconds",
                        "planning_output_limit_bytes",
                        "planner_effort",
                    },
                    {
                        "max_workers": _int_range(1, MAX_AGENT_WORKERS),
                        "max_items": _int_range(1, MAX_MODEL_WORKFLOW_ITEMS),
                        "max_steps": _int_range(1, MAX_MODEL_WORKFLOW_STEPS),
                        "planning_timeout_seconds": _int_range(1, MAX_MODEL_WORKFLOW_TIMEOUT_SECONDS),
                        "planning_output_limit_bytes": _int_range(1, MAX_MODEL_WORKFLOW_OUTPUT_LIMIT_BYTES),
                        "planner_effort": {"enum": sorted(MODEL_WORKFLOW_REASONING_EFFORTS)},
                    },
                ),
                "run": _strict_object(
                    {"id", "status"},
                    {"id": {"type": "string"}, "status": {"enum": sorted(MODEL_WORKFLOW_RUN_STATUSES)}},
                ),
                "error": _strict_object({"class"}, {"class": {"type": "string", "maxLength": 200}}),
            },
        )
    )
    return schema


def _model_workflow_run_receipt_v2_schema() -> Dict:
    schema = _model_workflow_run_receipt_schema()
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2
    schema["title"] = "Conductor Model Workflow Run Receipt v2"
    schema["properties"]["schema"] = {"const": MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2}
    schema["properties"]["status"]["enum"].remove("running")
    schema["properties"]["run"]["properties"]["status"]["enum"].remove("running")
    schema["properties"]["execution"]["properties"]["foreground"] = {"const": True}
    return schema


def _model_workflow_run_receipt_v1_schema() -> Dict:
    schema = _model_workflow_run_receipt_v2_schema()
    schema["$id"] = "https://codex-conductor.local/schemas/%s.schema.json" % MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V1
    schema["title"] = "Conductor Model Workflow Run Receipt v1"
    schema["properties"]["schema"] = {"const": MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V1}
    workflow = schema["properties"]["workflow"]
    workflow["required"].remove("available")
    del workflow["properties"]["available"]
    workflow["properties"]["name"] = _non_empty_string()
    workflow["properties"]["sha256"] = _sha256()
    workflow["properties"]["steps"] = _int_range(1, MAX_MODEL_WORKFLOW_STEPS)
    workflow["properties"]["mode"] = _non_empty_string()
    planning = schema["properties"]["planning"]
    planning["properties"]["status"] = {"const": MODEL_WORKFLOW_RECEIPT_STATUS}
    planning["properties"]["receipt_filename"] = _non_empty_string()
    planning["properties"]["receipt_sha256"] = _sha256()
    limits = schema["properties"]["limits"]
    for key in ("planning_timeout_seconds", "planning_output_limit_bytes", "planner_effort"):
        limits["required"].remove(key)
        del limits["properties"][key]
    return schema


def _background_run_schema() -> Dict:
    nullable_positive_int = {"anyOf": [_int_range(1), {"type": "null"}]}
    schema = _base_schema(
        BACKGROUND_RUN_SCHEMA,
        "Conductor Background Run",
        "Metadata for one explicitly launched detached workflow worker.",
    )
    schema.update(
        _strict_object(
            BACKGROUND_FIELDS,
            {
                "schema": {"const": BACKGROUND_RUN_SCHEMA},
                "status": {"enum": sorted(BACKGROUND_STATUSES)},
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "finished_at_utc": {"anyOf": [_timestamp_string(), {"type": "null"}]},
                "run_status": _non_empty_string(),
                "workflow": _strict_object(
                    {"name", "run_id"},
                    {"name": _non_empty_string(), "run_id": _non_empty_string()},
                ),
                "worker": _strict_object(
                    {"pid", "process_group_id", "session_id", "detached_session", "argv"},
                    {
                        "pid": nullable_positive_int,
                        "process_group_id": nullable_positive_int,
                        "session_id": nullable_positive_int,
                        "detached_session": {"const": True},
                        "argv": {"type": "array", "minItems": 1, "items": _non_empty_string()},
                    },
                ),
                "policy": _strict_object(
                    {
                        "allow_writes",
                        "allow_destructive",
                        "allow_network",
                        "allow_agent",
                        "allow_parallel",
                        "approval_count",
                        "approval_values_persisted",
                    },
                    {
                        "allow_writes": {"type": "boolean"},
                        "allow_destructive": {"type": "boolean"},
                        "allow_network": {"type": "boolean"},
                        "allow_agent": {"type": "boolean"},
                        "allow_parallel": {"type": "boolean"},
                        "approval_count": _int_range(0, 1000),
                        "approval_values_persisted": {"const": False},
                    },
                ),
                "handoff": _strict_object(
                    {
                        "prepared_preflight",
                        "transport",
                        "payload_persisted",
                        "daemon_installed",
                        "restart_policy",
                        "max_workers",
                    },
                    {
                        "prepared_preflight": {"const": True},
                        "transport": {"const": "anonymous-stdin-pipe"},
                        "payload_persisted": {"const": False},
                        "daemon_installed": {"const": False},
                        "restart_policy": {"const": "never"},
                        "max_workers": _int_range(1, MAX_AGENT_WORKERS),
                    },
                ),
                "logs": _strict_object(
                    {"stdout", "stderr"},
                    {
                        "stdout": {"const": "background-worker.stdout.log"},
                        "stderr": {"const": "background-worker.stderr.log"},
                    },
                ),
                "exit_code": {"type": ["integer", "null"]},
                "error_class": {"type": "string", "maxLength": 200},
            },
        )
    )
    return schema


def _desktop_notification_receipt_schema() -> Dict:
    schema = _base_schema(
        DESKTOP_NOTIFICATION_SCHEMA,
        "Conductor Desktop Notification Receipt",
        "Content-free delivery evidence for one opt-in workflow desktop notification.",
    )
    schema.update(
        _strict_object(
            DESKTOP_NOTIFICATION_RECEIPT_FIELDS,
            {
                "schema": {"const": DESKTOP_NOTIFICATION_SCHEMA},
                "status": {"enum": sorted(DESKTOP_NOTIFICATION_STATUSES)},
                "run_status": {"enum": sorted(RUN_NOTIFICATION_STATUSES)},
                "backend": {"enum": sorted(DESKTOP_NOTIFICATION_BACKENDS)},
                "content_policy": {"const": DESKTOP_NOTIFICATION_CONTENT_POLICY},
                "requested_at_utc": {
                    "type": "string",
                    "pattern": DESKTOP_NOTIFICATION_TIMESTAMP_PATTERN,
                },
                "finished_at_utc": {
                    "anyOf": [
                        {"type": "string", "pattern": DESKTOP_NOTIFICATION_TIMESTAMP_PATTERN},
                        {"type": "null"},
                    ]
                },
                "returncode": {
                    "type": ["integer", "null"],
                    "minimum": MIN_DESKTOP_NOTIFICATION_RETURNCODE,
                    "maximum": MAX_DESKTOP_NOTIFICATION_RETURNCODE,
                },
                "error_class": {"enum": sorted(DESKTOP_NOTIFICATION_ERROR_CLASSES)},
            },
        )
    )
    return schema


def _background_model_goal_schema() -> Dict:
    nullable_positive_int = {"anyOf": [_int_range(1), {"type": "null"}]}
    schema = _base_schema(
        BACKGROUND_MODEL_GOAL_SCHEMA,
        "Conductor Background Model Goal",
        "Lifecycle metadata for one explicitly detached adaptive model-goal worker.",
    )
    schema.update(
        _strict_object(
            BACKGROUND_MODEL_GOAL_FIELDS,
            {
                "schema": {"const": BACKGROUND_MODEL_GOAL_SCHEMA},
                "status": {"enum": sorted(BACKGROUND_MODEL_GOAL_STATUSES)},
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "heartbeat_at_utc": _timestamp_string(),
                "finished_at_utc": {"anyOf": [_timestamp_string(), {"type": "null"}]},
                "goal": _strict_object(
                    {"id", "artifact", "resumed"},
                    {"id": _safe_id(), "artifact": _non_empty_string(), "resumed": {"type": "boolean"}},
                ),
                "worker": _strict_object(
                    {"pid", "process_group_id", "session_id", "hostname", "detached_session", "ready", "argv"},
                    {
                        "pid": nullable_positive_int,
                        "process_group_id": nullable_positive_int,
                        "session_id": nullable_positive_int,
                        "hostname": _non_empty_string(),
                        "detached_session": {"const": True},
                        "ready": {"type": "boolean"},
                        "argv": {"type": "array", "minItems": 1, "items": _non_empty_string()},
                    },
                ),
                "task": _strict_object(
                    {"sha256", "chars", "raw_persisted"},
                    {"sha256": _sha256(), "chars": _int_range(1, MAX_MODEL_WORKFLOW_TASK_CHARS), "raw_persisted": {"const": False}},
                ),
                "verifier": _strict_object(
                    {"kind", "fingerprint"},
                    {"kind": {"enum": ["command", "model"]}, "fingerprint": _sha256()},
                ),
                "policy": _strict_object(
                    {"allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel", "approval_count", "approval_values_persisted"},
                    {
                        "allow_writes": {"type": "boolean"},
                        "allow_destructive": {"type": "boolean"},
                        "allow_network": {"type": "boolean"},
                        "allow_agent": {"type": "boolean"},
                        "allow_parallel": {"type": "boolean"},
                        "approval_count": _int_range(0, 1000),
                        "approval_values_persisted": {"const": False},
                    },
                ),
                "handoff": _strict_object(
                    {"preflight_completed", "transport", "payload_persisted", "daemon_installed", "restart_policy"},
                    {
                        "preflight_completed": {"const": True},
                        "transport": {"const": "anonymous-stdin-pipe"},
                        "payload_persisted": {"const": False},
                        "daemon_installed": {"const": False},
                        "restart_policy": {"const": "never"},
                    },
                ),
                "logs": _strict_object(
                    {"stdout", "stderr"},
                    {
                        "stdout": {"type": "string", "pattern": r"^[^/\\]+\.stdout\.log$"},
                        "stderr": {"type": "string", "pattern": r"^[^/\\]+\.stderr\.log$"},
                    },
                ),
                "goal_status": _non_empty_string(),
                "iterations": _int_range(0),
                "exit_code": {"type": ["integer", "null"]},
                "error_class": {"type": "string", "maxLength": 200},
            },
        )
    )
    return schema


def _background_routine_supervisor_schema() -> Dict:
    nullable_positive_int = {"anyOf": [_int_range(1), {"type": "null"}]}
    schema = _base_schema(
        BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA,
        "Conductor Background Routine Supervisor",
        "Lifecycle metadata for one explicitly detached local routine scheduler.",
    )
    schema.update(
        _strict_object(
            BACKGROUND_ROUTINE_SUPERVISOR_FIELDS,
            {
                "schema": {"const": BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA},
                "status": {"enum": sorted(BACKGROUND_ROUTINE_SUPERVISOR_STATUSES)},
                "created_at_utc": _timestamp_string(),
                "updated_at_utc": _timestamp_string(),
                "heartbeat_at_utc": _timestamp_string(),
                "finished_at_utc": {"anyOf": [_timestamp_string(), {"type": "null"}]},
                "worker": _strict_object(
                    {"pid", "process_group_id", "session_id", "hostname", "detached_session", "ready", "argv"},
                    {
                        "pid": nullable_positive_int,
                        "process_group_id": nullable_positive_int,
                        "session_id": nullable_positive_int,
                        "hostname": _non_empty_string(),
                        "detached_session": {"const": True},
                        "ready": {"type": "boolean"},
                        "argv": {"type": "array", "minItems": 1, "items": _non_empty_string()},
                    },
                ),
                "configuration": _strict_object(
                    {"poll_seconds", "max_cycles", "max_routines", "output_limit_bytes"},
                    {
                        "poll_seconds": _int_range(1, MAX_SUPERVISOR_POLL_SECONDS),
                        "max_cycles": _int_range(0, MAX_SUPERVISOR_CYCLES),
                        "max_routines": _int_range(1, MAX_LIST_ROUTINES),
                        "output_limit_bytes": _int_range(1, DEFAULT_OUTPUT_LIMIT_BYTES),
                    },
                ),
                "policy": _strict_object(
                    {"approval_count", "approval_values_persisted"},
                    {
                        "approval_count": _int_range(1, 1000),
                        "approval_values_persisted": {"const": False},
                    },
                ),
                "handoff": _strict_object(
                    {"preflight_completed", "transport", "payload_persisted", "daemon_installed", "restart_policy"},
                    {
                        "preflight_completed": {"const": True},
                        "transport": {"const": "anonymous-stdin-pipe"},
                        "payload_persisted": {"const": False},
                        "daemon_installed": {"const": False},
                        "restart_policy": {"const": "never"},
                    },
                ),
                "logs": _strict_object(
                    {"stdout", "stderr"},
                    {
                        "stdout": {"type": "string", "pattern": r"^[^/\\]+\.stdout\.log$"},
                        "stderr": {"type": "string", "pattern": r"^[^/\\]+\.stderr\.log$"},
                    },
                ),
                "supervisor_status": {"enum": sorted(BACKGROUND_ROUTINE_SUPERVISOR_RESULT_STATUSES)},
                "cycles_completed": _int_range(0),
                "results_count": _int_range(0),
                "completed_count": _int_range(0),
                "blocked_count": _int_range(0),
                "failed_count": _int_range(0),
                "exit_code": {"type": ["integer", "null"]},
                "error_class": {"type": "string", "maxLength": 200},
            },
        )
    )
    return schema


def _common_step_properties(kind: str) -> Dict:
    return {
        "id": _safe_id(),
        "kind": {"const": kind},
        "description": _unchecked_metadata("Step description is display metadata; prefer a string."),
        "risk": {"enum": sorted(RISK_LEVELS)},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "phase": _safe_id(),
        "output_limit_bytes": _int_range(1, MAX_WORKFLOW_OUTPUT_LIMIT_BYTES),
        "max_items": _int_range(1, MAX_AGENT_ITEMS),
        "timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
    }


def _step_schema(kind: str, required: List[str], properties: Dict) -> Dict:
    step_properties = _common_step_properties(kind)
    step_properties.update(properties)
    return {
        "type": "object",
        "required": ["id", "kind"] + required,
        "additionalProperties": True,
        "properties": step_properties,
    }


def _write_artifact_step_schema() -> Dict:
    return _step_schema(
        "write_artifact",
        ["output", "content"],
        {
            "output": _relative_path(),
            "content": {"type": "string"},
        },
    )


def _collect_results_step_schema() -> Dict:
    return _step_schema(
        "collect_results",
        ["source_step", "output"],
        {
            "source_step": _safe_id(),
            "output": _relative_path(),
            "filter_falsey": {
                "type": "boolean",
                "description": (
                    "Drop JSON false, null, zero, and empty-string map results; "
                    "empty arrays and objects remain truthy, matching JavaScript Boolean semantics."
                ),
            },
            "intermediate": {
                "type": "boolean",
                "description": (
                    "Mark a bounded internal map-result handoff that must be consumed "
                    "directly by a later agent_map."
                ),
            },
        },
    )


def _manual_gate_step_schema() -> Dict:
    return _step_schema(
        "manual_gate",
        [],
        {
            "approval_id": _non_empty_string(),
        },
    )


def _shell_step_schema() -> Dict:
    return _step_schema(
        "shell",
        ["command"],
        {
            "command": _command_schema(),
            "cwd": _relative_path(),
            "capture": _relative_path(),
            "capture_mode": {"enum": ["combined", "stdout", "stderr"]},
            "writes": _policy_hint(),
            "destructive": _policy_hint(),
            "network": _policy_hint(),
            "external_path": _policy_hint(),
        },
    )


def _codex_exec_step_schema() -> Dict:
    schema = _step_schema(
        "codex_exec",
        [],
        {
            "prompt": {"type": "string"},
            "prompt_file": _relative_path(),
            "prompt_artifact": _relative_path(),
            "context_from": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_CODEX_CONTEXT_SOURCES,
                "uniqueItems": True,
                "items": {
                    **_safe_id(),
                    "maxLength": MAX_CODEX_CONTEXT_SOURCE_ID_CHARS,
                },
            },
            "completion_verdict": {"const": COMPLETION_VERDICT_CONTRACT},
            "output_schema": {
                "type": "object",
                "description": (
                    "Inline Codex final-output JSON Schema; Python validation enforces a "
                    "%d-byte canonical JSON limit." % MAX_CODEX_OUTPUT_SCHEMA_BYTES
                ),
            },
            "agent_profile": _safe_id(),
            "model": _non_empty_string(),
            "sandbox": {"enum": ["read-only", "workspace-write"]},
            "capture": _relative_path(),
            "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
            "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
            "native_agents": {
                "type": "object",
                "additionalProperties": False,
                "required": ["max_threads"],
                "properties": {
                    "max_threads": _int_range(
                        MIN_CODEX_NATIVE_AGENT_THREADS,
                        MAX_CODEX_NATIVE_AGENT_THREADS,
                    )
                },
            },
        },
    )
    schema["anyOf"] = [{"required": ["prompt"]}, {"required": ["prompt_file"]}, {"required": ["prompt_artifact"]}]
    return schema


def _agent_map_step_schema() -> Dict:
    schema = _step_schema(
        "agent_map",
        ["prompt_template"],
        {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_ITEMS,
                "items": {
                    "anyOf": [
                        _opaque_packet_item(),
                        _json_packet_item(),
                    ]
                },
            },
            "item_semantics": {
                "enum": ["workspace_path", "opaque", "json"],
                "description": (
                    "Treat items as workspace-relative paths (default), bounded opaque text, "
                    "or bounded canonical JSON objects. Opaque semantics require inline items; "
                    "JSON semantics support inline objects or a strict JSON artifact pointer."
                ),
            },
            "items_file": _relative_path(),
            "items_artifact": _relative_path(),
            "preserve_duplicate_items": {
                "type": "boolean",
                "description": "Preserve repeated item values instead of applying legacy map deduplication.",
            },
            "items_pointer": {
                "type": "string",
                "maxLength": MAX_JSON_POINTER_CHARS,
                "pattern": r"^(?:$|/)",
                "description": "RFC 6901 pointer into strict JSON from items_artifact.",
            },
            "prompt_template": {
                "type": "string",
                "pattern": r"\{item(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\}",
                "description": (
                    "Must contain {item} or, for JSON semantics, a dotted {item.property}; "
                    "Python validation rejects unsupported format fields."
                ),
            },
            "agent_profile": _safe_id(),
            "model": _non_empty_string(),
            "capture_dir": _relative_path(),
            "sandbox": {"enum": ["read-only", "workspace-write"]},
            "max_workers": _int_range(1, MAX_AGENT_WORKERS),
            "max_packets": _int_range(1, MAX_AGENT_PACKETS),
            "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
            "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
            "max_total_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
            "output_schema": {
                "type": "object",
                "description": (
                    "Inline Codex final-output JSON Schema; Python validation enforces a "
                    "%d-byte canonical JSON limit." % MAX_CODEX_OUTPUT_SCHEMA_BYTES
                ),
            },
        },
    )
    schema["oneOf"] = [{"required": ["items"]}, {"required": ["items_file"]}, {"required": ["items_artifact"]}]
    schema["allOf"] = [
        {
            "if": {"required": ["items_pointer"]},
            "then": {"required": ["items_artifact"]},
        },
        {
            "if": {
                "anyOf": [
                    {"not": {"required": ["item_semantics"]}},
                    {
                        "properties": {
                            "item_semantics": {"const": "workspace_path"}
                        },
                        "required": ["item_semantics"],
                    },
                ]
            },
            "then": {"properties": {"items": {"items": _packet_item()}}},
        },
        {
            "if": {
                "properties": {"item_semantics": {"const": "opaque"}},
                "required": ["item_semantics"],
            },
            "then": {
                "properties": {"items": {"items": _opaque_packet_item()}},
                "not": {"required": ["items_file"]},
                "allOf": [
                    {
                        "if": {"required": ["items_artifact"]},
                        "then": {"required": ["items_pointer"]},
                    }
                ],
            },
        },
        {
            "if": {
                "properties": {"item_semantics": {"const": "json"}},
                "required": ["item_semantics"],
            },
            "then": {
                "properties": {"items": {"items": _json_packet_item()}},
                "not": {
                    "anyOf": [
                        {"required": ["items_file"]},
                        {"required": ["max_packets"]},
                    ]
                },
                "allOf": [
                    {
                        "if": {"required": ["items_artifact"]},
                        "then": {"required": ["items_pointer"]},
                    }
                ],
            },
        },
    ]
    return schema


def _agent_team_plan_task_ids_schema() -> Dict:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_AGENT_TEAM_PLAN_APPROVAL_TASKS,
        "uniqueItems": True,
        "items": _safe_id(),
    }


def _agent_team_plan_criteria_schema() -> Dict:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
    }


def _agent_team_step_schema() -> Dict:
    return _step_schema(
        "agent_team",
        [
            "members",
            "tasks",
            "max_workers",
            "max_rounds",
            "max_tokens",
            "max_total_tokens",
            "capture_dir",
            "report",
        ],
        {
            "members": {
                "type": "array",
                "minItems": MIN_AGENT_TEAM_MEMBERS,
                "maxItems": MAX_AGENT_TEAM_MEMBERS,
                "items": {
                    "type": "object",
                    "required": ["id", "role", "instructions", "agent_profile", "lead"],
                    "additionalProperties": False,
                    "properties": {
                        "id": _safe_id(),
                        "role": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_AGENT_TEAM_ROLE_CHARS,
                        },
                        "instructions": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_AGENT_TEAM_INSTRUCTIONS_CHARS,
                        },
                        "agent_profile": _safe_id(),
                        "lead": {"type": "boolean"},
                    },
                },
            },
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_TEAM_TASKS,
                "items": {
                    "type": "object",
                    "required": ["id", "description", "assignee", "depends_on"],
                    "additionalProperties": False,
                    "properties": {
                        "id": _safe_id(),
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
                        },
                        "assignee": {"oneOf": [_safe_id(), {"type": "null"}]},
                        "depends_on": {
                            "type": "array",
                            "maxItems": MAX_AGENT_TEAM_TASKS,
                            "uniqueItems": True,
                            "items": _safe_id(),
                        },
                    },
                },
            },
            "model": _non_empty_string(),
            "sandbox": {"enum": ["read-only", "workspace-write"]},
            "effort": {"enum": sorted(CODEX_REASONING_EFFORTS)},
            "max_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
            "max_total_tokens": _int_range(MIN_CODEX_RUNTIME_TOKEN_CAP, MAX_CODEX_TOKENS),
            "max_workers": _int_range(1, MAX_AGENT_TEAM_MEMBERS),
            "max_rounds": _int_range(1, MAX_AGENT_TEAM_ROUNDS),
            "max_dynamic_tasks": _int_range(0, MAX_AGENT_TEAM_DYNAMIC_TASKS),
            "max_operator_tasks": _int_range(0, MAX_AGENT_TEAM_OPERATOR_TASKS),
            "active_messaging": {
                "type": "object",
                "required": ["max_tasks", "max_depth", "max_tokens", "allow_broadcast"],
                "additionalProperties": False,
                "properties": {
                    "max_tasks": _int_range(1, MAX_AGENT_TEAM_MESSAGE_TASKS),
                    "max_depth": _int_range(1, MAX_AGENT_TEAM_MESSAGE_DEPTH),
                    "max_tokens": _int_range(
                        MIN_CODEX_RUNTIME_TOKEN_CAP,
                        MAX_CODEX_TOKENS,
                    ),
                    "allow_broadcast": {"type": "boolean"},
                },
            },
            "operator_chat": {
                "type": "object",
                "required": ["max_questions", "max_tokens", "reply_timeout_seconds"],
                "additionalProperties": False,
                "properties": {
                    "max_questions": _int_range(1, MAX_AGENT_TEAM_OPERATOR_QUESTIONS),
                    "max_tokens": _int_range(
                        MIN_CODEX_RUNTIME_TOKEN_CAP,
                        MAX_CODEX_TOKENS,
                    ),
                    "reply_timeout_seconds": _int_range(
                        1,
                        MAX_AGENT_TEAM_OPERATOR_REPLY_TIMEOUT_SECONDS,
                    ),
                },
            },
            "operator_console": {
                "type": "object",
                "required": ["max_events", "max_bytes"],
                "additionalProperties": False,
                "properties": {
                    "max_events": _int_range(
                        MIN_AGENT_TEAM_TRANSCRIPT_EVENTS,
                        MAX_AGENT_TEAM_TRANSCRIPT_EVENTS,
                    ),
                    "max_bytes": _int_range(
                        MIN_AGENT_TEAM_TRANSCRIPT_BYTES,
                        MAX_AGENT_TEAM_TRANSCRIPT_BYTES,
                    ),
                },
            },
            "plan_approval": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": [
                            "task_ids",
                            "criteria",
                            "max_revisions",
                            "plan_max_tokens",
                            "review_max_tokens",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "task_ids": _agent_team_plan_task_ids_schema(),
                            "criteria": _agent_team_plan_criteria_schema(),
                            "max_revisions": _int_range(0, MAX_AGENT_TEAM_PLAN_REVISIONS),
                            "plan_max_tokens": _int_range(
                                MIN_CODEX_RUNTIME_TOKEN_CAP,
                                MAX_CODEX_TOKENS,
                            ),
                            "review_max_tokens": _int_range(
                                MIN_CODEX_RUNTIME_TOKEN_CAP,
                                MAX_CODEX_TOKENS,
                            ),
                            "reviewer": {"const": "lead"},
                        },
                    },
                    {
                        "type": "object",
                        "required": [
                            "task_ids",
                            "criteria",
                            "max_revisions",
                            "plan_max_tokens",
                            "reviewer",
                            "reply_timeout_seconds",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "task_ids": _agent_team_plan_task_ids_schema(),
                            "criteria": _agent_team_plan_criteria_schema(),
                            "max_revisions": _int_range(0, MAX_AGENT_TEAM_PLAN_REVISIONS),
                            "plan_max_tokens": _int_range(
                                MIN_CODEX_RUNTIME_TOKEN_CAP,
                                MAX_CODEX_TOKENS,
                            ),
                            "reviewer": {"const": "operator"},
                            "reply_timeout_seconds": _int_range(
                                1,
                                MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS,
                            ),
                        },
                    },
                ]
            },
            "capture_dir": _relative_path(),
            "report": _relative_path(),
        },
    )


def _agent_memory_step_schema() -> Dict:
    return _step_schema(
        "agent_memory",
        ["agent_profile", "source_artifact"],
        {
            "agent_profile": _safe_id(),
            "source_artifact": _relative_path(),
            "action": {"const": "append"},
            "entry_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_MEMORY_ID_CHARS,
                "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
            },
            "tags": {
                "type": "array",
                "maxItems": MAX_AGENT_MEMORY_TAGS,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_MEMORY_TAG_CHARS,
                    "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
                },
            },
        },
    )


def _hook_schema() -> Dict:
    return {
        "type": "object",
        "required": ["id", "event", "command"],
        "additionalProperties": True,
        "properties": {
            "id": _safe_id(),
            "event": {"enum": sorted(HOOK_EVENTS)},
            "command": _command_schema(),
            "risk": {"enum": sorted(RISK_LEVELS)},
            "cwd": _relative_path(),
            "capture": _relative_path(),
            "capture_mode": {"enum": ["combined", "stdout", "stderr"]},
            "on_failure": {"enum": sorted(HOOK_FAILURE_MODES)},
            "output_limit_bytes": _int_range(1, MAX_WORKFLOW_OUTPUT_LIMIT_BYTES),
            "timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
            "steps": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": _safe_id(),
            },
            "members": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": _safe_id(),
            },
            "tasks": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": _safe_id(),
            },
            "profiles": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": _safe_id(),
            },
            "scopes": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"enum": sorted(AGENT_LIFECYCLE_SCOPES)},
            },
            "tools": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_NATIVE_TOOL_NAMES,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": r"^(?:\*|[a-zA-Z0-9][a-zA-Z0-9_.:-]*)$",
                },
            },
            "inject_context": {"type": "boolean"},
            "context_limit_bytes": _int_range(
                1,
                MAX_AGENT_LIFECYCLE_CONTEXT_BYTES,
            ),
            "writes": {"type": "boolean"},
            "destructive": {"type": "boolean"},
            "network": {"type": "boolean"},
            "external_path": {"type": "boolean"},
            "max_retries": _int_range(1, MAX_AGENT_TEAM_QUALITY_RETRIES),
            "retry_exit_codes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "uniqueItems": True,
                "items": _int_range(1, 255),
            },
            "retry_max_tokens": _int_range(
                MIN_CODEX_RUNTIME_TOKEN_CAP,
                MAX_CODEX_TOKENS,
            ),
        },
    }


def _benchmark_task_schema() -> Dict:
    return {
        "type": "object",
        "required": ["id", "command"],
        "additionalProperties": True,
        "properties": {
            "id": _safe_id(),
            "description": _unchecked_metadata("Benchmark task description is display metadata; prefer a string."),
            "command": _command_schema(),
            "risk": {"enum": sorted(RISK_LEVELS)},
            "success_contains": {"type": "string"},
            "timeout_seconds": _int_range(1, MAX_TIMEOUT_SECONDS),
            "output_limit_bytes": _int_range(1, MAX_BENCHMARK_OUTPUT_LIMIT_BYTES),
            "writes": _policy_hint(),
            "destructive": _policy_hint(),
            "network": _policy_hint(),
            "external_path": _policy_hint(),
        },
    }


def _benchmark_result_schema() -> Dict:
    return {
        "type": "object",
        "required": ["id", "passed", "duration_ms", "timed_out", "stdout_truncated", "stderr_truncated"],
        "additionalProperties": True,
        "properties": {
            "id": _safe_id(),
            "passed": {"type": "boolean"},
            "duration_ms": _int_range(0),
            "timed_out": {"type": "boolean"},
            "stdout_truncated": {"type": "boolean"},
            "stderr_truncated": {"type": "boolean"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "returncode": {"type": "integer"},
            "description": _unchecked_metadata("Benchmark result description is display metadata; prefer a string."),
            "completion_summary": _staged_completion_summary_schema(),
        },
    }


def _staged_completion_summary_schema() -> Dict:
    return _strict_object(
        STAGED_COMPLETION_SUMMARY_FIELDS,
        {
            "schema": {"const": STAGED_COMPLETION_SUMMARY_SCHEMA},
            "changed_files": {
                "type": "array",
                "uniqueItems": True,
                "items": _non_empty_string(),
            },
            "change_count": _int_range(0, MAX_STAGED_CHANGES),
            "verification_configured": {"type": "boolean"},
            "verification_performed": {"type": "boolean"},
            "verification_status": _non_empty_string(),
            "verification_returncode": {"type": ["integer", "null"]},
            "verification_timed_out": {"type": "boolean"},
        },
    )


def _comparison_system_schema() -> Dict:
    return {
        "type": "object",
        "required": ["name", "report"],
        "additionalProperties": True,
        "properties": {
            "name": _safe_id(),
            "report": _relative_path(),
            "manifest": _relative_path(),
        },
    }


def _claim_policy_schema() -> Dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "model": {
                    "type": "object",
                    "required": ["mode"],
                    "additionalProperties": False,
                    "properties": {
                        key: value
                        for key, value in {
                            "mode": {"enum": sorted(MODEL_CLAIM_MODES)},
                            "rationale": _non_empty_string(),
                        }.items()
                        if key in MODEL_CLAIM_POLICY_FIELDS
                    },
                }
            }.items()
            if key in CLAIM_POLICY_FIELDS
        },
    }


def _parity_task_schema() -> Dict:
    return {
        "type": "object",
        "required": ["id", "dimension", "prompt", "success_criteria", "required_evidence", "scoring", "budget"],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "id": _safe_id(),
                "dimension": _non_empty_string(),
                "tags": _string_list(min_items=1, non_empty=True),
                "prompt": _non_empty_string(),
                "success_criteria": _string_list(min_items=1, non_empty=True),
                "required_evidence": _string_list(min_items=1, non_empty=True),
                "scoring": _non_empty_string(),
                "budget": _parity_budget_schema(),
            }.items()
            if key in PARITY_TASK_FIELDS
        },
    }


def _parity_budget_schema() -> Dict:
    return {
        "type": "object",
        "required": ["max_minutes", "max_agents", "max_live_tool_cost"],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "max_minutes": _int_range(1),
                "max_agents": _int_range(0),
                "max_live_tool_cost": {"enum": sorted(LIVE_TOOL_COST_LEVELS)},
                "max_cost_usd": {"type": "number", "minimum": 0},
                "max_tokens": _int_range(0),
                "cost_cap_enforcement": {"enum": sorted(BUDGET_CAP_ENFORCEMENT_LEVELS)},
                "token_cap_enforcement": {"enum": sorted(BUDGET_CAP_ENFORCEMENT_LEVELS)},
            }.items()
            if key in PARITY_BUDGET_FIELDS
        },
    }


def _live_runbook_source_schema() -> Dict:
    return {
        "type": "object",
        "required": ["parity_tasks_schema", "parity_tasks_sha256", "task_count", "required_dimensions"],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "parity_tasks_schema": {"const": PARITY_TASKS_SCHEMA},
                "parity_tasks_source": {"type": ["string", "null"]},
                "parity_tasks_sha256": _sha256(),
                "task_count": _int_range(1),
                "required_dimensions": _string_list(min_items=1, non_empty=True),
            }.items()
            if key in LIVE_PARITY_RUNBOOK_SOURCE_FIELDS
        },
    }


def _live_runbook_system_schema() -> Dict:
    return {
        "type": "object",
        "required": sorted(LIVE_PARITY_RUNBOOK_SYSTEM_FIELDS),
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "name": _safe_id(),
                "provider": _non_empty_string(),
                "tool": _non_empty_string(),
                "model_effort": _non_empty_string(),
                "requires_external_tool": {"type": "boolean"},
                "requires_paid_approval": {"type": "boolean"},
                "report_path": _relative_path(),
                "manifest_path": _relative_path(),
                "model_manifest_template": _string_object_schema(
                    MODEL_FIELDS, required=["provider", "name", "effort"]
                ),
                "permissions_template": _run_permissions_schema(),
                "invocation_templates": {
                    "type": "array",
                    "minItems": 1,
                    "items": _live_runbook_invocation_schema(),
                },
                "notes": _string_list(min_items=1, non_empty=True),
            }.items()
            if key in LIVE_PARITY_RUNBOOK_SYSTEM_FIELDS
        },
    }


def _live_runbook_invocation_schema() -> Dict:
    return {
        "type": "object",
        "required": sorted(LIVE_PARITY_RUNBOOK_INVOCATION_FIELDS),
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "description": _non_empty_string(),
                "status": _non_empty_string(),
                "launches_external_tools": {"type": "boolean"},
                "requires_paid_approval": {"type": "boolean"},
                "argv_template": _string_list(min_items=1, non_empty=True),
                "notes": _string_list(min_items=1, non_empty=True),
            }.items()
            if key in LIVE_PARITY_RUNBOOK_INVOCATION_FIELDS
        },
    }


def _live_runbook_task_schema() -> Dict:
    return {
        "type": "object",
        "required": sorted(LIVE_PARITY_RUNBOOK_TASK_FIELDS),
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "id": _safe_id(),
                "dimension": _non_empty_string(),
                "tags": _string_list(min_items=1, non_empty=True),
                "prompt": _non_empty_string(),
                "prompt_path": _relative_path(),
                "success_criteria": _string_list(min_items=1, non_empty=True),
                "required_evidence": _string_list(min_items=1, non_empty=True),
                "scoring": _non_empty_string(),
                "budget": _run_budget_schema(),
                "scorecard_template": _live_runbook_scorecard_schema(),
                "safety_incident_template": _live_runbook_safety_template_schema(),
                "system_artifacts": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": _relative_path(),
                    },
                },
            }.items()
            if key in LIVE_PARITY_RUNBOOK_TASK_FIELDS
        },
    }


def _live_runbook_scorecard_schema() -> Dict:
    return {
        "type": "object",
        "required": ["max_score", "notes"],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "score": {"type": ["number", "null"], "minimum": 0},
                "max_score": _int_range(1),
                "notes": _non_empty_string(),
            }.items()
            if key in LIVE_PARITY_RUNBOOK_SCORECARD_FIELDS
        },
    }


def _live_runbook_safety_template_schema() -> Dict:
    return {
        "type": "object",
        "required": ["id", "severity", "task_id", "description"],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "id": _safe_id(),
                "severity": {"enum": sorted(SAFETY_SEVERITIES)},
                "task_id": _safe_id(),
                "description": _non_empty_string(),
            }.items()
            if key in LIVE_PARITY_RUNBOOK_SAFETY_TEMPLATE_FIELDS
        },
    }


def _live_runbook_claim_gate_schema() -> Dict:
    return {
        "type": "object",
        "required": sorted(LIVE_PARITY_RUNBOOK_CLAIM_GATE_FIELDS),
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "eligible_for_parity_claims": {"const": False},
                "required_before_claim": _string_list(min_items=1, non_empty=True),
                "comparison_template": _relative_path(),
                "claim_check_command": _string_list(min_items=1, non_empty=True),
            }.items()
            if key in LIVE_PARITY_RUNBOOK_CLAIM_GATE_FIELDS
        },
    }


def _evidence_claim_schema() -> Dict:
    return {
        "type": "object",
        "required": sorted(CLAIM_FIELDS),
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "status": {"enum": sorted(CLAIM_STATUSES)},
                "parity_claim_eligible": {"type": "boolean"},
                "superiority_claim_eligible": {"const": False},
                "reason": _non_empty_string(),
                "eligible_reports": _string_list(min_items=0),
                "blockers": _string_list(min_items=0),
                "warnings": _string_list(min_items=0),
            }.items()
            if key in CLAIM_FIELDS
        },
    }


def _evidence_list(item_schema: Dict) -> Dict:
    return {"type": "array", "maxItems": MAX_EVIDENCE_ITEMS, "items": item_schema}


def _evidence_run_schema() -> Dict:
    return _strict_object(
        RUN_FIELDS,
        {
            "id": _non_empty_string(),
            "run_handle": {"type": "string"},
            "status": _non_empty_string(),
            "workflow": _non_empty_string(),
            "evidence_complete": {"type": "boolean"},
            "started_at_utc": {"type": "string"},
            "finished_at_utc": {"type": "string"},
            "duration_ms": _nullable_int(0),
            "mtime_utc": {"type": "string"},
        },
    )


def _evidence_goal_schema() -> Dict:
    return _strict_object(
        GOAL_FIELDS,
        {
            "goal_id": _non_empty_string(),
            "goal_handle": {"type": "string"},
            "status": _non_empty_string(),
            "workflow": _non_empty_string(),
            "iterations": _int_range(0),
            "max_iterations": _nullable_int(0),
            "updated_at_utc": {"type": "string"},
            "evidence_attention": {"type": "boolean"},
            "last_iteration": {
                "oneOf": [
                    {"type": "object", "maxProperties": 0},
                    _strict_object(
                        GOAL_LAST_ITERATION_FIELDS,
                        {
                            "iteration": _nullable_int(0),
                            "status": {"type": "string"},
                            "run_id": {"type": "string"},
                            "run_status": {"type": "string"},
                            "started_at_utc": {"type": "string"},
                            "finished_at_utc": {"type": "string"},
                        },
                    ),
                ]
            },
            "last_check_status": {"type": "string"},
            "last_check_returncode": {"type": ["integer", "null"], "minimum": 0},
            "error": {"type": "string"},
        },
    )


def _evidence_routine_schema() -> Dict:
    return _strict_object(
        ROUTINE_FIELDS,
        {
            "name": _non_empty_string(),
            "schedule": _non_empty_string(),
            "target_kind": _non_empty_string(),
            "workflow": _non_empty_string(),
            "evidence_status": _non_empty_string(),
            "no_process_started": {"type": "boolean"},
            "policy": _strict_object(
                ROUTINE_POLICY_FIELDS,
                {
                    "allow_writes": {"type": "boolean"},
                    "allow_destructive": {"type": "boolean"},
                    "allow_network": {"type": "boolean"},
                    "allow_agent": {"type": "boolean"},
                    "allow_parallel": {"type": "boolean"},
                    "approval_count": _int_range(0),
                },
            ),
            "created_at_utc": _non_empty_string(),
            "error": {"type": "string"},
        },
    )


def _evidence_report_schema() -> Dict:
    return _strict_object(
        REPORT_FIELDS,
        {
            "name": _non_empty_string(),
            "kind": _non_empty_string(),
            "schema": _non_empty_string(),
            "suite": _non_empty_string(),
            "system": _non_empty_string(),
            "result": _non_empty_string(),
            "failed_tasks": _nullable_int(0),
            "duration_ms": _nullable_int(0),
            "claim_eligible": {"type": ["boolean", "null"]},
            "provenance_complete": {"type": ["boolean", "null"]},
            "all_live": {"type": ["boolean", "null"]},
            "warnings": _string_list(min_items=0),
            "error": {"type": "string"},
            "evidence_status": _non_empty_string(),
            "evidence_label": _non_empty_string(),
            "evidence_attention": {"type": "boolean"},
            "mtime_utc": {"type": "string"},
            "source_sha256": _sha256(),
        },
    )


def _routine_schedule_schema() -> Dict:
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["kind"],
                "additionalProperties": False,
                "properties": {"kind": {"const": "manual"}},
            },
            {
                "type": "object",
                "required": ["kind", "every_minutes"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "interval"},
                    "every_minutes": _int_range(1, MAX_INTERVAL_MINUTES),
                },
            },
            {
                "type": "object",
                "required": ["kind", "time", "timezone"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "daily"},
                    "time": _time_string(),
                    "timezone": _timezone_string(),
                },
            },
            {
                "type": "object",
                "required": ["kind", "time", "timezone"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "weekdays"},
                    "time": _time_string(),
                    "timezone": _timezone_string(),
                },
            },
            {
                "type": "object",
                "required": ["kind", "day", "time", "timezone"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "weekly"},
                    "day": {"enum": sorted(set(WEEKDAY_ALIASES.values()))},
                    "time": _time_string(),
                    "timezone": _timezone_string(),
                },
            },
            {
                "type": "object",
                "required": ["kind", "day", "time", "timezone"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "monthly"},
                    "day": _int_range(1, 31),
                    "time": _time_string(),
                    "timezone": _timezone_string(),
                },
            },
            {
                "type": "object",
                "required": ["kind", "at_utc"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "once"},
                    "at_utc": _timestamp_string(),
                },
            },
            {
                "type": "object",
                "required": ["kind", "expression", "timezone"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "cron"},
                    "expression": {
                        "type": "string",
                        "minLength": 9,
                        "maxLength": MAX_CRON_EXPRESSION_CHARS,
                        "pattern": CRON_EXPRESSION_SHAPE_PATTERN,
                    },
                    "timezone": _timezone_string(),
                },
            },
        ]
    }


def _routine_target_schema() -> Dict:
    common = {
        "kind": {"enum": ["workflow", "saved_workflow"]},
        "workflow": _non_empty_string(),
        "workflow_fingerprint": _sha256(),
        "steps_planned": _int_range(0),
    }
    workflow_props = dict(common)
    workflow_props["kind"] = {"const": "workflow"}
    workflow_props["workflow_path"] = _non_empty_string()
    saved_props = dict(common)
    saved_props["kind"] = {"const": "saved_workflow"}
    saved_props.update(
        {
            "saved_command": _non_empty_string(),
            "saved_scope": _non_empty_string(),
            "saved_path": _non_empty_string(),
        }
    )
    return {
        "oneOf": [
            {
                "type": "object",
                "required": sorted(TARGET_BASE_KEYS | {"workflow_path"}),
                "additionalProperties": False,
                "properties": workflow_props,
            },
            {
                "type": "object",
                "required": sorted(TARGET_BASE_KEYS | {"saved_command", "saved_scope", "saved_path"}),
                "additionalProperties": False,
                "properties": saved_props,
            },
        ]
    }


def _routine_launch_schema() -> Dict:
    properties = {
        "command_argv": _string_list(min_items=1, non_empty=True),
        "command": _non_empty_string(),
        "workspace": _non_empty_string(),
        "runs_dir": _non_empty_string(),
        "max_workers": {"type": ["integer", "null"], "minimum": 1, "maximum": ROUTINE_MAX_AGENT_WORKERS},
        "runtime_sha256": _sha256(),
        "policy": _strict_object(
            ROUTINE_POLICY_KEYS,
            {
                "allow_writes": {"type": "boolean"},
                "allow_destructive": {"type": "boolean"},
                "allow_network": {"type": "boolean"},
                "allow_agent": {"type": "boolean"},
                "allow_parallel": {"type": "boolean"},
                "approval_count": _int_range(0),
                "approval_values_persisted": {"const": False},
            },
        ),
    }
    return {
        "type": "object",
        "required": sorted(LAUNCH_REQUIRED_KEYS),
        "additionalProperties": False,
        "properties": {field: properties[field] for field in sorted(LAUNCH_KEYS)},
    }


def _routine_budget_schema() -> Dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "max_minutes": _int_range(1, 60 * 24 * 7),
                "max_cost_usd": {"type": "number", "minimum": 0},
                "max_tokens": _int_range(1),
            }.items()
            if key in ROUTINE_BUDGET_KEYS
        },
    }


def _routine_external_supervisor_schema() -> Dict:
    return _strict_object(
        EXTERNAL_SUPERVISOR_KEYS,
        {
            "may_run_command_argv": _string_list(min_items=1, non_empty=True),
            "must_not_assume_conductor_daemon": {"const": True},
            "must_not_start_without_operator_or_scheduler_approval": {"const": True},
            "must_record_actual_run_manifest_for_parity_claims": {"const": True},
            "approval_tokens_must_be_supplied_at_launch_if_required": {"type": "boolean"},
            "no_process_started_by_manifest_write": {"const": True},
        },
    )


def _run_budget_schema() -> Dict:
    return {
        "type": "object",
        "required": ["max_minutes", "max_agents", "max_live_tool_cost"],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "max_minutes": _int_range(1),
                "max_agents": _int_range(0),
                "max_live_tool_cost": {"enum": sorted(LIVE_TOOL_COST_LEVELS)},
                "max_cost_usd": {"type": "number", "minimum": 0},
                "max_tokens": _int_range(0),
            }.items()
            if key in BUDGET_FIELDS
        },
    }


def _run_permissions_schema() -> Dict:
    return {
        "type": "object",
        "required": [
            "allow_writes",
            "allow_destructive",
            "allow_network",
            "allow_agent",
            "allow_parallel",
            "approved_tokens",
        ],
        "additionalProperties": False,
        "properties": {
            key: value
            for key, value in {
                "allow_writes": {"type": "boolean"},
                "allow_destructive": {"type": "boolean"},
                "allow_network": {"type": "boolean"},
                "allow_agent": {"type": "boolean"},
                "allow_parallel": {"type": "boolean"},
                "approved_tokens": {"type": "array", "items": _non_empty_string()},
            }.items()
            if key in PERMISSION_FIELDS
        },
    }


def _run_scoring_schema() -> Dict:
    return {
        "type": "object",
        "required": ["tasks"],
        "additionalProperties": False,
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["id", "score", "max_score"],
                    "additionalProperties": False,
                    "properties": {
                        key: value
                        for key, value in {
                            "id": _safe_id(),
                            "score": _int_range(0),
                            "max_score": _int_range(1),
                            "notes": {"type": "string"},
                            "quality_evidence_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "task_contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "pass_threshold": _int_range(1),
                            "reviewer_kind": {"enum": ["deterministic", "human", "model"]},
                            "reviewer_independent": {"type": "boolean"},
                        }.items()
                        if key in SCORING_TASK_FIELDS
                    },
                },
            }
        },
    }


def _run_safety_schema() -> Dict:
    return {
        "type": "object",
        "required": ["incidents"],
        "additionalProperties": False,
        "properties": {
            "incidents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "severity", "description"],
                    "additionalProperties": False,
                    "properties": {
                        "id": _safe_id(),
                        "severity": {"enum": sorted(SAFETY_SEVERITIES)},
                        "task_id": {
                            "anyOf": [_safe_id(), {"type": "null"}],
                        },
                        "description": _non_empty_string(),
                    },
                },
            }
        },
    }


def _string_object_schema(fields, required=None) -> Dict:
    required = required or []
    return {
        "type": "object",
        "required": list(required),
        "additionalProperties": False,
        "properties": {
            field: (_non_empty_string() if field in required else {"type": ["string", "null"]})
            for field in sorted(fields)
        },
    }


def _safe_id() -> Dict:
    return {"type": "string", "pattern": SAFE_ID.pattern}


def _non_empty_string() -> Dict:
    return {"type": "string", "minLength": 1}


def _string_list(min_items: int = 1, non_empty: bool = False) -> Dict:
    item_schema = _non_empty_string() if non_empty else {"type": "string"}
    schema = {
        "type": "array",
        "minItems": min_items,
        "items": item_schema,
    }
    return schema


def _strict_object(fields, properties: Dict) -> Dict:
    return {
        "type": "object",
        "required": sorted(fields),
        "additionalProperties": False,
        "properties": {field: properties.get(field, {}) for field in sorted(fields)},
    }


def _strict_schema(
    schema_name: str,
    title: str,
    description: str,
    properties: Dict,
    *,
    required,
) -> Dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://codex-conductor.local/schemas/%s.schema.json" % schema_name,
        "title": title,
        "description": description,
        "type": "object",
        "required": sorted(required),
        "additionalProperties": False,
        "properties": properties,
    }


def _strict_string_fields(fields) -> Dict:
    return _strict_object(
        fields,
        {field: _non_empty_string() for field in fields},
    )


def _int_object_schema(fields, minimum: int = 0, maximum: int = None) -> Dict:
    return _strict_object(
        fields,
        {field: _int_range(minimum, maximum) for field in fields},
    )


def _nullable_int(minimum: int = 0, maximum: int = None) -> Dict:
    schema = {"type": ["integer", "null"], "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _sha256() -> Dict:
    return {"type": "string", "pattern": r"^[a-f0-9]{64}$"}


def _timestamp_string() -> Dict:
    return {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN.pattern}


def _time_string() -> Dict:
    return {"type": "string", "pattern": TIME_PATTERN.pattern}


def _timezone_string() -> Dict:
    return {
        "type": "string",
        "pattern": TIMEZONE_PATTERN.pattern,
        "not": {"pattern": r"(?:^|/)\.\.(?:/|$)"},
    }


def _non_absolute_path() -> Dict:
    return {"type": "string", "minLength": 1, "pattern": r"^(?!/).+$"}


def _unchecked_metadata(description: str) -> Dict:
    return {
        "description": (
            description + " The Python validator currently preserves this field without type enforcement."
        )
    }


def _policy_hint() -> Dict:
    return {
        "description": (
            "Runtime policy hint. Prefer a JSON boolean; Python currently coerces this value with bool()."
        )
    }


def _relative_path() -> Dict:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": RELATIVE_PATH_PATTERN,
    }


def _packet_item() -> Dict:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_PACKET_ITEM_CHARS,
        "pattern": PACKET_ITEM_PATTERN,
        "description": (
            "Single-line packet item that is not blank, absolute, or parent-escaping. "
            "Python validation also strips surrounding whitespace, de-duplicates items, "
            "sanitizes task-boundary markers, and rejects secret-like values."
        ),
    }


def _opaque_packet_item() -> Dict:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_OPAQUE_PACKET_ITEM_CHARS,
        "description": (
            "Bounded opaque packet text. Python validation also rejects blank values, "
            "NUL characters, task-boundary markers, and secret-like values."
        ),
    }


def _json_packet_item() -> Dict:
    return {
        "type": "object",
        "minProperties": 1,
        "description": (
            "Bounded structured packet item. Python validation canonicalizes strict JSON, "
            "rejects secrets and task-boundary markers, and enforces at most %d levels and %d nodes."
            % (MAX_JSON_PACKET_ITEM_DEPTH, MAX_JSON_PACKET_ITEM_NODES)
        ),
    }


def _int_range(minimum: int, maximum: int = None) -> Dict:
    schema = {"type": "integer", "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _command_schema() -> Dict:
    return {
        "description": (
            "Command may be an argv string array or a simple shell-split string. "
            "String commands must contain non-whitespace text and no shell metacharacters; "
            "Python validation performs final shlex parsing and command policy classification."
        ),
        "oneOf": [
            {
                "type": "string",
                "pattern": COMMAND_STRING_PATTERN,
            },
            {
                "type": "array",
                "minItems": 1,
                "items": _non_empty_string(),
            },
        ],
    }


_SCHEMA_REGISTRY = [
    ("workflow", WORKFLOW_SCHEMA, "Conductor Workflow", _workflow_schema),
    (
        "completion-verdict-feedback",
        COMPLETION_VERDICT_FEEDBACK_SCHEMA,
        "Conductor Completion Verdict Feedback Receipt",
        _completion_verdict_feedback_schema,
    ),
    ("agent-profile", AGENT_PROFILE_SCHEMA, "Conductor Agent Profile", _agent_profile_schema),
    ("agent-memory", AGENT_MEMORY_SCHEMA, "Conductor Agent Memory", _agent_memory_schema),
    ("agent-team-state", AGENT_TEAM_STATE_SCHEMA, "Conductor Agent Team State", _agent_team_state_schema),
    (
        "agent-team-operator-inbox",
        AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
        "Conductor Agent Team Operator Inbox",
        _agent_team_operator_inbox_schema,
    ),
    (
        "agent-team-operator-chat",
        AGENT_TEAM_OPERATOR_CHAT_SCHEMA,
        "Conductor Agent Team Operator Chat",
        _agent_team_operator_chat_schema,
    ),
    (
        "agent-team-transcript",
        AGENT_TEAM_TRANSCRIPT_SCHEMA,
        "Conductor Agent Team Transcript",
        _agent_team_transcript_schema,
    ),
    (
        "agent-team-interruption",
        AGENT_TEAM_INTERRUPTION_SCHEMA,
        "Conductor Agent Team Interruption",
        _agent_team_interruption_schema,
    ),
    (
        "agent-team-plan",
        AGENT_TEAM_PLAN_SCHEMA,
        "Conductor Agent Team Plan",
        _agent_team_plan_schema,
    ),
    (
        "agent-team-plan-review",
        AGENT_TEAM_PLAN_REVIEW_SCHEMA,
        "Conductor Agent Team Plan Review",
        _agent_team_plan_review_schema,
    ),
    (
        "agent-team-plan-approval",
        AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
        "Conductor Agent Team Plan Approval",
        _agent_team_plan_approval_schema,
    ),
    (
        "agent-team-plan-approval-v2",
        AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2,
        "Conductor Agent Team Plan Approval v2",
        _agent_team_plan_approval_v2_schema,
    ),
    (
        "agent-team-plan-approval-v1",
        AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1,
        "Conductor Agent Team Plan Approval v1",
        _agent_team_plan_approval_v1_schema,
    ),
    (
        "agent-team-merge-ledger",
        AGENT_TEAM_MERGE_LEDGER_SCHEMA,
        "Conductor Agent Team Merge Ledger",
        _agent_team_merge_ledger_schema,
    ),
    (
        "agent-map-packet-terminal",
        AGENT_MAP_PACKET_TERMINAL_SCHEMA,
        "Conductor Agent Map Packet Terminal",
        _agent_map_packet_terminal_schema,
    ),
    (
        "codex-step-terminal",
        CODEX_STEP_TERMINAL_SCHEMA,
        "Conductor Codex Step Terminal",
        _codex_step_terminal_schema,
    ),
    (
        "codex-step-terminal-v1",
        CODEX_STEP_TERMINAL_SCHEMA_V1,
        "Conductor Codex Step Terminal v1",
        _codex_step_terminal_v1_schema,
    ),
    (
        "codex-progress",
        CODEX_PROGRESS_SCHEMA,
        "Conductor Codex Progress",
        _codex_progress_schema,
    ),
    (
        "codex-progress-v3",
        CODEX_PROGRESS_SCHEMA_V3,
        "Conductor Codex Progress v3",
        _codex_progress_v3_schema,
    ),
    (
        "codex-progress-v2",
        CODEX_PROGRESS_SCHEMA_V2,
        "Conductor Codex Progress v2",
        _codex_progress_v2_schema,
    ),
    (
        "codex-progress-v1",
        CODEX_PROGRESS_SCHEMA_V1,
        "Conductor Codex Progress v1",
        _codex_progress_v1_schema,
    ),
    (
        "agent-team-turn-terminal",
        AGENT_TEAM_TURN_TERMINAL_SCHEMA,
        "Conductor Agent Team Turn Terminal",
        _agent_team_turn_terminal_schema,
    ),
    (
        "agent-team-turn-terminal-v1",
        AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1,
        "Conductor Agent Team Turn Terminal v1",
        _agent_team_turn_terminal_v1_schema,
    ),
    (
        "agent-team-turn-completion",
        AGENT_TEAM_TURN_COMPLETION_SCHEMA,
        "Conductor Agent Team Turn Completion",
        _agent_team_turn_completion_schema,
    ),
    (
        "agent-team-turn-completion-v1",
        AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1,
        "Conductor Agent Team Turn Completion v1",
        _agent_team_turn_completion_v1_schema,
    ),
    (
        "agent-team-merge-intent",
        AGENT_TEAM_MERGE_INTENT_SCHEMA,
        "Conductor Agent Team Merge Intent",
        _agent_team_merge_intent_schema,
    ),
    (
        "agent-team-merge-transaction",
        AGENT_TEAM_MERGE_TRANSACTION_SCHEMA,
        "Conductor Agent Team Merge Transaction",
        _agent_team_merge_transaction_schema,
    ),
    (
        "agent-team-merge-recovery",
        AGENT_TEAM_MERGE_RECOVERY_SCHEMA,
        "Conductor Agent Team Merge Recovery",
        _agent_team_merge_recovery_schema,
    ),
    (
        "agent-team-state-v5",
        AGENT_TEAM_STATE_SCHEMA_V5,
        "Conductor Agent Team State v5",
        _agent_team_state_v5_schema,
    ),
    (
        "agent-team-state-v4",
        AGENT_TEAM_STATE_SCHEMA_V4,
        "Conductor Agent Team State v4",
        _agent_team_state_v4_schema,
    ),
    (
        "agent-team-state-v3",
        AGENT_TEAM_STATE_SCHEMA_V3,
        "Conductor Agent Team State v3",
        _agent_team_state_v3_schema,
    ),
    (
        "agent-team-state-v2",
        AGENT_TEAM_STATE_SCHEMA_V2,
        "Conductor Agent Team State v2",
        _agent_team_state_v2_schema,
    ),
    (
        "agent-team-operator-inbox-v1",
        AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
        "Conductor Agent Team Operator Inbox v1",
        _agent_team_operator_inbox_v1_schema,
    ),
    (
        "agent-team-state-v1",
        AGENT_TEAM_STATE_SCHEMA_V1,
        "Conductor Agent Team State v1",
        _agent_team_state_v1_schema,
    ),
    ("agent-team-turn", AGENT_TEAM_TURN_SCHEMA, "Conductor Agent Team Turn", _agent_team_turn_schema),
    (
        "agent-team-hook-input",
        AGENT_TEAM_HOOK_INPUT_SCHEMA,
        "Conductor Agent Team Hook Input",
        _agent_team_hook_input_schema,
    ),
    (
        "agent-lifecycle-hook-input",
        AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA,
        "Conductor Agent Lifecycle Hook Input",
        _agent_lifecycle_hook_input_schema,
    ),
    (
        "agent-native-tool-hook-input",
        AGENT_NATIVE_TOOL_INPUT_SCHEMA,
        "Conductor Agent Native Tool Hook Input",
        _agent_native_tool_input_schema,
    ),
    (
        "agent-native-tool-gate",
        AGENT_NATIVE_TOOL_GATE_SCHEMA,
        "Conductor Agent Native Tool Gate",
        _agent_native_tool_gate_schema,
    ),
    (
        "agent-native-tool-receipt",
        AGENT_NATIVE_TOOL_RECEIPT_SCHEMA,
        "Conductor Agent Native Tool Receipt",
        _agent_native_tool_receipt_schema,
    ),
    (
        "agent-lifecycle-stop-gate",
        AGENT_LIFECYCLE_STOP_GATE_SCHEMA,
        "Conductor Agent Lifecycle Stop Gate",
        _agent_lifecycle_stop_gate_schema,
    ),
    (
        "direct-workspace-transaction",
        DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
        "Conductor Direct Workspace Transaction",
        _direct_workspace_transaction_schema,
    ),
    (
        "staged-apply",
        STAGED_APPLY_SCHEMA,
        "Conductor Verified Stage Apply Receipt",
        _staged_apply_schema,
    ),
    (
        "agent-lifecycle-context",
        AGENT_LIFECYCLE_CONTEXT_SCHEMA,
        "Conductor Agent Lifecycle Context Receipt",
        _agent_lifecycle_context_schema,
    ),
    (
        "agent-lifecycle-context-v1",
        AGENT_LIFECYCLE_CONTEXT_SCHEMA_V1,
        "Conductor Agent Lifecycle Context Receipt v1",
        _agent_lifecycle_context_v1_schema,
    ),
    (
        "agent-team-quality-retry",
        AGENT_TEAM_QUALITY_RETRY_SCHEMA,
        "Conductor Agent Team Quality Retry",
        _agent_team_quality_retry_schema,
    ),
    (
        "agent-team-turn-v1",
        AGENT_TEAM_TURN_SCHEMA_V1,
        "Conductor Agent Team Turn v1",
        _agent_team_turn_v1_schema,
    ),
    ("benchmark", BENCHMARK_SCHEMA, "Conductor Benchmark Suite", _benchmark_suite_schema),
    ("run-manifest", RUN_MANIFEST_SCHEMA, "Conductor Run Manifest", _run_manifest_schema),
    ("benchmark-report", BENCHMARK_REPORT_SCHEMA, "Conductor Benchmark Report", _benchmark_report_schema),
    (
        "benchmark-quality-evidence",
        BENCHMARK_QUALITY_EVIDENCE_SCHEMA,
        "Conductor Benchmark Quality Evidence",
        _benchmark_quality_evidence_schema,
    ),
    (
        "benchmark-quality-evidence-v2",
        BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2,
        "Conductor Benchmark Quality Evidence v2",
        _benchmark_quality_evidence_v2_schema,
    ),
    (
        "benchmark-score-input",
        BENCHMARK_SCORE_INPUT_SCHEMA,
        "Conductor Benchmark Score Input",
        _benchmark_score_input_schema,
    ),
    (
        "claude-provider-evidence",
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
        "Conductor Claude Provider Evidence",
        _claude_provider_evidence_schema,
    ),
    (
        "claude-provider-evidence-v2",
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
        "Conductor Claude Provider Evidence v2",
        _claude_provider_evidence_v2_schema,
    ),
    (
        "claude-provider-evidence-v1",
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
        "Conductor Claude Provider Evidence v1",
        _claude_provider_evidence_v1_schema,
    ),
    (
        "codex-provider-evidence",
        CODEX_PROVIDER_EVIDENCE_SCHEMA,
        "Conductor Codex Provider Evidence",
        _codex_provider_evidence_schema,
    ),
    (
        "codex-provider-evidence-v4",
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
        "Conductor Codex Provider Evidence v4",
        _codex_provider_evidence_v4_schema,
    ),
    (
        "codex-provider-evidence-v3",
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
        "Conductor Codex Provider Evidence v3",
        _codex_provider_evidence_v3_schema,
    ),
    (
        "codex-provider-evidence-v2",
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
        "Conductor Codex Provider Evidence v2",
        _codex_provider_evidence_v2_schema,
    ),
    (
        "codex-provider-evidence-v1",
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V1,
        "Conductor Codex Provider Evidence v1",
        _codex_provider_evidence_v1_schema,
    ),
    (
        "claude-staged-evidence",
        CLAUDE_STAGED_EVIDENCE_SCHEMA,
        "Conductor Claude Staged-Write Evidence",
        _claude_staged_evidence_schema,
    ),
    (
        "codex-staged-evidence",
        CODEX_STAGED_EVIDENCE_SCHEMA,
        "Conductor Codex Staged-Write Evidence",
        _codex_staged_evidence_schema,
    ),
    (
        "codex-staged-repair",
        CODEX_STAGED_REPAIR_SCHEMA,
        "Conductor Codex Staged Repair Evidence",
        _codex_staged_repair_schema,
    ),
    (
        "codex-staged-repair-state",
        CODEX_STAGED_REPAIR_STATE_SCHEMA,
        "Conductor Codex Staged Repair State",
        _codex_staged_repair_state_schema,
    ),
    ("comparison", COMPARISON_SCHEMA, "Conductor Benchmark Comparison Manifest", _comparison_manifest_schema),
    ("parity-tasks", PARITY_TASKS_SCHEMA, "Conductor Parity Task Fixture", _parity_tasks_schema),
    ("parity-campaign", PARITY_CAMPAIGN_SCHEMA, "Conductor Parity Campaign", _parity_campaign_schema),
    (
        "parity-campaign-v3",
        PARITY_CAMPAIGN_SCHEMA_V3,
        "Conductor Parity Campaign v3",
        _parity_campaign_v3_schema,
    ),
    (
        "parity-campaign-v2",
        PARITY_CAMPAIGN_SCHEMA_V2,
        "Conductor Parity Campaign v2",
        _parity_campaign_v2_schema,
    ),
    (
        "parity-campaign-v1",
        PARITY_CAMPAIGN_SCHEMA_V1,
        "Conductor Parity Campaign v1",
        _parity_campaign_v1_schema,
    ),
    (
        "auto-topology-campaign",
        AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
        "Conductor Auto Topology Campaign",
        _auto_topology_campaign_schema,
    ),
    (
        "auto-topology-campaign-v2",
        AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2,
        "Conductor Auto Topology Campaign v2",
        _auto_topology_campaign_v2_schema,
    ),
    (
        "auto-topology-campaign-v1",
        AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1,
        "Conductor Auto Topology Campaign v1",
        _auto_topology_campaign_v1_schema,
    ),
    (
        "readonly-parity-campaign",
        READONLY_PARITY_CAMPAIGN_SCHEMA,
        "Conductor Read-Only Parity Campaign",
        _readonly_parity_campaign_schema,
    ),
    (
        "readonly-parity-campaign-v1",
        READONLY_PARITY_CAMPAIGN_SCHEMA_V1,
        "Conductor Read-Only Parity Campaign v1",
        _readonly_parity_campaign_v1_schema,
    ),
    ("live-parity-runbook", LIVE_PARITY_RUNBOOK_SCHEMA, "Conductor Live Parity Runbook", _live_parity_runbook_schema),
    ("evidence-bundle", EVIDENCE_BUNDLE_SCHEMA, "Conductor Evidence Bundle", _evidence_bundle_schema),
    ("routine-manifest", ROUTINE_MANIFEST_SCHEMA, "Conductor Routine Manifest", _routine_manifest_schema),
    ("routine-controls", ROUTINE_CONTROLS_SCHEMA, "Conductor Routine Controls", _routine_controls_schema),
    ("routine-state", ROUTINE_SUPERVISOR_SCHEMA, "Conductor Routine State", _routine_state_schema),
    (
        "routine-service-grant",
        ROUTINE_SERVICE_GRANT_SCHEMA,
        "Conductor Routine Service Grant",
        _routine_service_grant_schema,
    ),
    (
        "routine-service-state",
        ROUTINE_SERVICE_STATE_SCHEMA,
        "Conductor Routine Service State",
        _routine_service_state_schema,
    ),
    (
        "auto-orchestration-receipt",
        AUTO_ORCHESTRATION_RECEIPT_SCHEMA,
        "Conductor Auto Orchestration Receipt",
        _auto_orchestration_receipt_schema,
    ),
    (
        "auto-orchestration-receipt-v1",
        AUTO_ORCHESTRATION_RECEIPT_SCHEMA_V1,
        "Conductor Auto Orchestration Receipt v1",
        _auto_orchestration_receipt_v1_schema,
    ),
    (
        "model-workflow-receipt",
        MODEL_WORKFLOW_RECEIPT_SCHEMA,
        "Conductor Model Workflow Receipt",
        _model_workflow_receipt_schema,
    ),
    (
        "model-workflow-receipt-v3",
        MODEL_WORKFLOW_RECEIPT_SCHEMA_V3,
        "Conductor Model Workflow Receipt v3",
        _model_workflow_receipt_v3_schema,
    ),
    (
        "model-workflow-receipt-v2",
        MODEL_WORKFLOW_RECEIPT_SCHEMA_V2,
        "Conductor Model Workflow Receipt v2",
        _model_workflow_receipt_v2_schema,
    ),
    (
        "model-workflow-receipt-v1",
        MODEL_WORKFLOW_RECEIPT_SCHEMA_V1,
        "Conductor Model Workflow Receipt v1",
        _model_workflow_receipt_v1_schema,
    ),
    (
        "model-workflow-run-receipt",
        MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA,
        "Conductor Model Workflow Run Receipt",
        _model_workflow_run_receipt_schema,
    ),
    (
        "model-workflow-run-receipt-v2",
        MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2,
        "Conductor Model Workflow Run Receipt v2",
        _model_workflow_run_receipt_v2_schema,
    ),
    (
        "model-workflow-run-receipt-v1",
        MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V1,
        "Conductor Model Workflow Run Receipt v1",
        _model_workflow_run_receipt_v1_schema,
    ),
    (
        "background-run",
        BACKGROUND_RUN_SCHEMA,
        "Conductor Background Run",
        _background_run_schema,
    ),
    (
        "desktop-notification",
        DESKTOP_NOTIFICATION_SCHEMA,
        "Conductor Desktop Notification Receipt",
        _desktop_notification_receipt_schema,
    ),
    (
        "background-model-goal",
        BACKGROUND_MODEL_GOAL_SCHEMA,
        "Conductor Background Model Goal",
        _background_model_goal_schema,
    ),
    (
        "background-routine-supervisor",
        BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA,
        "Conductor Background Routine Supervisor",
        _background_routine_supervisor_schema,
    ),
]
