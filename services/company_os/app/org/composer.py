"""Team Composer — deterministic triggers; LLM personalizes prompts only (07 §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.org.catalog import fitness_metric_exists, load_catalog
from app.gstack.prompts import overlay_system_prompt
from app.org.characters import character_for


@dataclass
class RoleSelection:
    role: str
    count: int
    evidence: str
    deliverable: str
    fitness_metric: str
    retire_when: str
    allowed_tools: list[str]
    model_tier: str
    priority: int
    name: str = ""
    charge: str = ""
    system_prompt: str = ""
    first_objective: str = ""


@dataclass
class OrgPlanResult:
    agent_count: int
    roles: list[RoleSelection]
    rationale: list[dict[str, Any]]
    rejected: list[dict[str, str]]
    deferred: list[dict[str, str]] = field(default_factory=list)


def _eval_trigger(trigger: str, charter: dict[str, Any], brief: dict[str, Any], selected_roles: set[str]) -> tuple[bool, str]:
    stage = str(charter.get("current_stage") or charter.get("stage") or "org_planning")
    gtm = str(charter.get("gtm_order") or "")
    archetype = str(charter.get("build_archetype") or brief.get("build_archetype") or "")
    complexity = float(brief.get("complexity_score") or charter.get("complexity_score") or 0)
    regulatory = str(charter.get("regulatory_surface") or brief.get("regulatory_surface") or "none")
    monetization = str(charter.get("monetization") or brief.get("monetization") or "subscription")
    integrations = charter.get("integrations") or []
    armed = int(charter.get("armed_channels") or 0)

    t = trigger.strip()
    if t == "always":
        return True, "always"
    if t == "has_role == builder":
        hit = "builder" in selected_roles
        return hit, "builder present" if hit else "no builder"
    # Simple expression evaluation for catalog triggers
    ctx = {
        "gtm_order": gtm,
        "stage": stage,
        "build_archetype": archetype,
        "complexity_score": complexity,
        "regulatory_surface": regulatory,
        "monetization": monetization,
        "integrations_count": len(integrations) if isinstance(integrations, list) else 0,
        "armed_channels": armed,
    }
    try:
        # Safe-ish eval of catalog expressions
        expr = t
        expr = expr.replace("==", " == ").replace("!=", " != ")
        # Translate `stage in [building]` style
        if " stage in " in f" {expr}" or expr.startswith("stage in"):
            # e.g. stage in [building, org_planning]
            pass
        # Manual parsers for known patterns
        if "gtm_order == product_first" in t:
            if gtm == "product_first":
                return True, "gtm product_first"
            if "stage in" in t and stage in ("building", "org_planning"):
                return True, f"stage {stage}"
            return False, "gtm not product_first and stage not building"
        if "gtm_order == market_first" in t:
            if gtm == "market_first" or stage == "live":
                return True, "market_first or live"
            return False, "not market_first / not live"
        if "complexity_score >=" in t:
            return complexity >= 0.4, f"complexity={complexity}"
        if "build_archetype != integration_bot" in t:
            return archetype != "integration_bot", f"archetype={archetype}"
        if "armed_channels > 0" in t:
            return armed > 0, f"armed={armed}"
        if "stage == live" in t:
            return stage == "live", f"stage={stage}"
        if "regulatory_surface != none" in t:
            return regulatory not in {"none", "", "None"}, f"regulatory={regulatory}"
        if "monetization != free" in t:
            hit = monetization not in {"free", "none", ""}
            if "stage == live" in t:
                hit = hit and stage == "live"
            return hit, f"monetization={monetization}"
        if "integrations_count > 2" in t:
            n = ctx["integrations_count"]
            return n > 2, f"integrations={n}"
        if "stage in [building]" in t or t == "stage in [building] or gtm_order == product_first":
            return stage == "building" or gtm == "product_first", f"stage={stage} gtm={gtm}"
        if "stage in [classifying, clarifying]" in t:
            return stage in {"classifying", "clarifying"}, f"stage={stage}"
        if "stage in [building, org_planning]" in t or "or stage in [building, org_planning]" in t:
            return stage in {"building", "org_planning"} or gtm == "product_first", f"stage={stage}"
    except Exception as exc:  # noqa: BLE001
        return False, f"trigger_error:{exc}"
    return False, f"unrecognized_trigger:{t}"


def _multiplicity(arch: dict[str, Any], charter: dict[str, Any]) -> int:
    m = arch.get("multiplicity", 1)
    if m == 1 or m == "1":
        return 1
    if m == "per_armed_channel":
        return max(1, int(charter.get("armed_channels") or 0)) or 0
    if m == "work_order_tracks":
        tracks = int(charter.get("work_order_tracks") or 1)
        return max(1, min(int(arch.get("max_count") or 3), tracks))
    if m == "extra_integrations":
        n = len(charter.get("integrations") or [])
        return max(0, n - 2)
    return 1


def _stage_allows(arch: dict[str, Any], stage: str) -> bool:
    stages = arch.get("stages") or []
    if not stages:
        return True
    # During org_planning we allow roles that will be needed soon for product_first
    if stage == "org_planning":
        return True  # composer still records stage preference; lifecycle activates later
    return stage in stages


def personalize(plan: list[RoleSelection], charter: dict[str, Any]) -> list[RoleSelection]:
    """Fill name/charge/prompt only — MUST NOT change headcount."""
    one = charter.get("one_liner") or charter.get("problem") or "the venture"
    out: list[RoleSelection] = []
    for i, r in enumerate(plan):
        person = character_for(r.role, f"{r.role}-{i}", r.name)
        out.append(
            RoleSelection(
                role=r.role,
                count=1,
                evidence=r.evidence,
                deliverable=r.deliverable,
                fitness_metric=r.fitness_metric,
                retire_when=r.retire_when,
                allowed_tools=list(r.allowed_tools),
                model_tier=r.model_tier,
                priority=r.priority,
                name=person.given_name,
                charge=r.charge or person.charge,
                system_prompt=overlay_system_prompt(
                    r.role,
                    r.system_prompt
                    or (
                        f"You are {person.given_name}, the {person.title} for: {one}. "
                        f"{person.voice} Deliverable: {r.deliverable}. "
                        f"Optimize {r.fitness_metric}. Retire when: {r.retire_when}."
                    ),
                ),
                first_objective=r.first_objective or f"Produce {r.deliverable}",
            )
        )
    assert len(out) == len(plan), "personalize must not change headcount"
    return out


def compose(
    charter: dict[str, Any],
    brief: dict[str, Any] | None = None,
    *,
    stage: str | None = None,
    max_agents: int | None = None,
) -> OrgPlanResult:
    brief = brief or {}
    catalog = load_catalog()
    cap = int(max_agents if max_agents is not None else catalog.get("max_active_agents") or 12)
    if cap < 1:
        raise ValueError("Your team needs at least one seat. Set the team cap to 1 or more and try again.")
    ch = dict(charter)
    if stage:
        ch["current_stage"] = stage
    ch.setdefault("current_stage", ch.get("stage") or "org_planning")

    selections: list[RoleSelection] = []
    rejected: list[dict[str, str]] = []
    selected_roles: set[str] = set()

    # Pass 1: non-dependent roles
    for arch in catalog.get("roles") or []:
        if arch.get("trigger") == "has_role == builder":
            continue
        hit, evidence = _eval_trigger(str(arch["trigger"]), ch, brief, selected_roles)
        if not hit:
            rejected.append({"role": arch["role"], "reason": evidence})
            continue
        if not _stage_allows(arch, ch["current_stage"]) and ch["current_stage"] not in {
            "org_planning",
            "fork_choice",
        }:
            rejected.append({"role": arch["role"], "reason": f"stage_gate:{ch['current_stage']}"})
            continue
        count = _multiplicity(arch, ch)
        if count <= 0:
            rejected.append({"role": arch["role"], "reason": "multiplicity_zero"})
            continue
        for _ in range(count):
            selections.append(
                RoleSelection(
                    role=arch["role"],
                    count=1,
                    evidence=evidence,
                    deliverable=arch["deliverable"],
                    fitness_metric=arch["fitness_metric"],
                    retire_when=arch["retire_when"],
                    allowed_tools=list(arch.get("allowed_tools") or []),
                    model_tier=arch.get("model_tier") or "flash",
                    priority=int(arch.get("priority") or 0),
                )
            )
        selected_roles.add(arch["role"])

    # Pass 2: builder-dependent (reviewer)
    for arch in catalog.get("roles") or []:
        if arch.get("trigger") != "has_role == builder":
            continue
        hit, evidence = _eval_trigger(str(arch["trigger"]), ch, brief, selected_roles)
        if hit:
            selections.append(
                RoleSelection(
                    role=arch["role"],
                    count=1,
                    evidence=evidence,
                    deliverable=arch["deliverable"],
                    fitness_metric=arch["fitness_metric"],
                    retire_when=arch["retire_when"],
                    allowed_tools=list(arch.get("allowed_tools") or []),
                    model_tier=arch.get("model_tier") or "flash",
                    priority=int(arch.get("priority") or 0),
                )
            )
            selected_roles.add(arch["role"])
        else:
            rejected.append({"role": arch["role"], "reason": evidence})

    # Invariants
    orch = [r for r in selections if r.role == "orchestrator"]
    if len(orch) == 0:
        # Force orchestrator
        arch = next(a for a in catalog["roles"] if a["role"] == "orchestrator")
        selections.insert(
            0,
            RoleSelection(
                role="orchestrator",
                count=1,
                evidence="forced",
                deliverable=arch["deliverable"],
                fitness_metric=arch["fitness_metric"],
                retire_when=arch["retire_when"],
                allowed_tools=list(arch["allowed_tools"]),
                model_tier="flash",
                priority=100,
            ),
        )
    elif len(orch) > 1:
        # Keep one
        keep = orch[0]
        selections = [r for r in selections if r.role != "orchestrator"]
        selections.insert(0, keep)

    has_builder = any(r.role == "builder" for r in selections)
    has_reviewer = any(r.role == "reviewer" for r in selections)
    if has_builder and not has_reviewer:
        arch = next(a for a in catalog["roles"] if a["role"] == "reviewer")
        selections.append(
            RoleSelection(
                role="reviewer",
                count=1,
                evidence="builder implies reviewer",
                deliverable=arch["deliverable"],
                fitness_metric=arch["fitness_metric"],
                retire_when=arch["retire_when"],
                allowed_tools=list(arch["allowed_tools"]),
                model_tier="pro",
                priority=84,
            )
        )

    selections.sort(key=lambda selection: -selection.priority)
    admitted = [next(selection for selection in selections if selection.role == "orchestrator")]
    reviewer = next((selection for selection in selections if selection.role == "reviewer"), None)
    for selection in selections:
        if selection.role in {"orchestrator", "reviewer"}:
            continue
        required = [selection]
        if selection.role == "builder" and not any(role.role == "reviewer" for role in admitted):
            required.append(reviewer)
        if len(admitted) + len(required) <= cap:
            admitted.extend(required)
    deferred = [{"role": selection.role, "reason": "deferred: agent cap reached"}
        for selection in selections if not any(selection is role for role in admitted)]
    rejected.extend(deferred)
    personalized = personalize(admitted, ch)

    # Validate
    for spec in personalized:
        if not spec.fitness_metric or not fitness_metric_exists(spec.fitness_metric):
            raise ValueError(f"fitness_metric required/registered: {spec.role}")
        if not spec.retire_when:
            raise ValueError(f"retire_when required: {spec.role}")
        if not spec.allowed_tools:
            raise ValueError(f"allowed_tools required: {spec.role}")

    rationale = [
        {
            "role": s.role,
            "why": s.evidence,
            "deliverable": s.deliverable,
            "retire_when": s.retire_when,
            "fitness_metric": s.fitness_metric,
        }
        for s in personalized
    ]
    return OrgPlanResult(
        agent_count=len(personalized),
        roles=personalized,
        rationale=rationale,
        rejected=rejected,
        deferred=deferred,
    )
