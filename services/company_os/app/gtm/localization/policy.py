"""Deterministic policy gate for GTM profiling and thesis execution.

The LLM may recommend strategy, but it cannot decide whether protected or
high-risk data is acceptable. Geography is allowed for language, law, timing,
payments and aggregate market hypotheses; it is not a proxy for personal traits.
"""

from __future__ import annotations

from typing import Any

FORBIDDEN_TARGETING_KEYS = frozenset(
    {
        "race",
        "ethnicity",
        "caste",
        "religion",
        "religious_belief",
        "political_belief",
        "political_affiliation",
        "sexual_orientation",
        "gender_identity",
        "pregnancy",
        "health",
        "medical_condition",
        "disability",
        "biometric",
        "genetic",
        "vulnerability",
        "personality_type",
        "inferred_culture",
        "name_ethnicity",
        "zip_code_proxy",
    }
)

FORBIDDEN_TARGETING_TERMS = (
    "because they are indian",
    "because they are american",
    "because they are japanese",
    "people from this country are",
    "this ethnicity",
    "this religion",
    "infer ethnicity",
    "infer religion",
    "infer caste",
)

EVIDENCE_CLASSES = {
    "observed_outcome": 5,
    "segment_data": 4,
    "market_research": 3,
    "general_prior": 2,
    "llm_inference": 1,
}


def _walk(value: Any, path: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            found.append((child_path, child))
            found.extend(_walk(child, child_path))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            found.extend(_walk(child, f"{path}[{idx}]"))
    return found


def assess_profile(profile: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, str]] = []
    warnings: list[str] = []
    segment = profile.get("segment") or {}
    constraints = profile.get("constraints") or {}

    for path, value in _walk(segment):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
        if key in FORBIDDEN_TARGETING_KEYS:
            violations.append(
                {
                    "code": "protected_or_sensitive_targeting",
                    "path": f"segment.{path}",
                    "message": f"{key} cannot be used to target or score people.",
                }
            )
        if key in {"age", "age_bracket", "date_of_birth"}:
            violations.append(
                {
                    "code": "age_propensity_targeting",
                    "path": f"segment.{path}",
                    "message": (
                        "Age cannot be a buying-propensity feature. Use a legal "
                        "eligibility constraint where required."
                    ),
                }
            )
        if isinstance(value, str):
            low = value.lower()
            for term in FORBIDDEN_TARGETING_TERMS:
                if term in low:
                    violations.append(
                        {
                            "code": "stereotype_instruction",
                            "path": f"segment.{path}",
                            "message": "Targeting instructions cannot encode nationality stereotypes.",
                        }
                    )
                    break

    if constraints.get("minimum_legal_age") is not None:
        warnings.append(
            "minimum_legal_age is an eligibility gate only and must not affect propensity."
        )

    evidence = profile.get("evidence") or []
    if not evidence:
        warnings.append("No evidence supplied; generated claims remain low-confidence hypotheses.")
    for idx, item in enumerate(evidence):
        if not isinstance(item, dict):
            warnings.append(f"Evidence item {idx + 1} is unstructured and will rank as inference.")
            continue
        klass = str(item.get("evidence_class") or "").strip()
        if klass not in EVIDENCE_CLASSES:
            warnings.append(f"Evidence item {idx + 1} has no recognized evidence_class.")
        if not (item.get("source") or item.get("source_url")):
            warnings.append(f"Evidence item {idx + 1} has no source.")
        if item.get("confidence") is None:
            warnings.append(f"Evidence item {idx + 1} has no confidence.")

    return {
        "allowed": not violations,
        "violations": violations,
        "warnings": warnings,
        "policy_version": "gtm-localization-v1",
        "principles": [
            "observed_behavior_beats_prior",
            "geography_is_context_not_identity",
            "protected_traits_never_drive_propensity",
            "human_approval_before_execution",
            "global_ethics_floor",
        ],
    }


def assess_thesis(thesis: dict[str, Any], profile_policy: dict[str, Any]) -> dict[str, Any]:
    violations = list(profile_policy.get("violations") or [])
    warnings = list(profile_policy.get("warnings") or [])
    for path, value in _walk(thesis):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
        if key in FORBIDDEN_TARGETING_KEYS or key in {"age", "age_bracket", "date_of_birth"}:
            violations.append(
                {
                    "code": "protected_or_sensitive_output",
                    "path": path,
                    "message": f"{key} cannot be used in generated targeting strategy.",
                }
            )
        if not isinstance(value, str):
            continue
        low = value.lower()
        for term in FORBIDDEN_TARGETING_TERMS:
            if term in low:
                violations.append(
                    {
                        "code": "stereotype_output",
                        "path": path,
                        "message": "Generated strategy contains a nationality stereotype.",
                    }
                )
                break
    confidence = float(thesis.get("confidence") or 0)
    if confidence < 0.45:
        warnings.append("Thesis confidence is below 0.45; run only in shadow mode.")
    return {
        **profile_policy,
        "allowed": not violations,
        "violations": violations,
        "warnings": list(dict.fromkeys(warnings)),
        "execution_mode": "shadow" if confidence < 0.45 else "approval_required",
    }
