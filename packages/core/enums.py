"""Enums — single source of truth from docs/company-os/04_DATA_MODEL.md §1."""

from __future__ import annotations

from enum import StrEnum


class VentureStage(StrEnum):
    DRAFT = "draft"
    CLASSIFYING = "classifying"
    CLARIFYING = "clarifying"
    CHARTER_REVIEW = "charter_review"
    ORG_PLANNING = "org_planning"
    FORK_CHOICE = "fork_choice"
    BUILDING = "building"
    DEMAND_TESTING = "demand_testing"
    READINESS_REVIEW = "readiness_review"
    HOSTING = "hosting"
    LIVE = "live"
    PIVOT = "pivot"
    PAUSED = "paused"
    ARCHIVED = "archived"


class GoToMarketOrder(StrEnum):
    PRODUCT_FIRST = "product_first"
    MARKET_FIRST = "market_first"


class AutonomyLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    QUARANTINED = "quarantined"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    FAILED = "failed"


class SlotCriticality(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    NICE = "nice"


class QuestionKind(StrEnum):
    GOAL = "goal"
    INPUT = "input"
    CONSTRAINT = "constraint"


class WorkOrderStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    GATING = "gating"
    NEEDS_REWORK = "needs_rework"
    DONE = "done"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class GateName(StrEnum):
    TYPECHECK = "typecheck"
    LINT = "lint"
    UNIT = "unit"
    INTEGRATION = "integration"
    SECURITY = "security"
    AUTH_ENFORCEMENT = "auth_enforcement"
    SCAFFOLD_PRESENT = "scaffold_present"
    LLM_SAFETY = "llm_safety"
    COMPONENT_BUDGET = "component_budget"
    BOOT = "boot"
    PEER_REVIEW = "peer_review"


class HumanTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    SKIPPED = "skipped"
    VERIFY_FAILED = "verify_failed"


class ProposalTier(StrEnum):
    CONFIG = "config"
    ORG = "org"
    CODE = "code"
    STRATEGY = "strategy"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    SIMULATING = "simulating"
    SIMULATED = "simulated"
    VALIDATING = "validating"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    REGRESSED = "regressed"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ChannelKind(StrEnum):
    LANDING = "landing"
    EMAIL = "email"
    REDDIT = "reddit"
    X = "x"
    INSTAGRAM = "instagram"
    LINKEDIN = "linkedin"


# --- Additional enums needed by 04 tables / P0.4 ---


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    POISON = "poison"
    SCHEDULED = "scheduled"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED_DEAD = "failed_dead"
    CANCELLED = "cancelled"


class ClarificationRoundStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    SUPERSEDED = "superseded"


class ChannelStatus(StrEnum):
    UNCONFIGURED = "unconfigured"
    PENDING_PREREQS = "pending_prereqs"
    READY = "ready"
    ARMED = "armed"
    SUSPENDED = "suspended"


class PostStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    BLOCKED = "blocked"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class FeedbackTrust(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    FOUNDER = "founder"


class SimulationVerdict(StrEnum):
    FAVOURABLE = "favourable"
    NEUTRAL = "neutral"
    UNFAVOURABLE = "unfavourable"
    INSUFFICIENT_DATA = "insufficient_data"


class PromotionOutcome(StrEnum):
    HELD = "held"
    REGRESSED = "regressed"
    INCONCLUSIVE = "inconclusive"


class SlotSource(StrEnum):
    INFERRED = "inferred"
    ANSWERED = "answered"
    DEFAULT = "default"
    FOUNDER_OTHER = "founder_other"
