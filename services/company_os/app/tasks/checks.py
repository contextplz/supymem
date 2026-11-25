"""Supported code-owned completion predicates; unknown checks never pass."""


def validate(check: dict, *, whole_task=False):
    if not check:
        return
    if check.get("type") == "rubric":
        spec = check.get("spec")
        criteria = spec.get("criteria") if isinstance(spec, dict) else None
        if not isinstance(spec, dict) or spec.get("predicate") != "rubric" or not isinstance(criteria, list) or not 1 <= len(criteria) <= 6 or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 500 for item in criteria) or len({item.strip() for item in criteria}) != len(criteria):
            raise ValueError("Which review criteria should confirm this task is finished?")
        return
    if check.get("type", "deterministic") != "deterministic":
        raise ValueError("Which measurable result should confirm this task is finished?")
    spec = check.get("spec")
    if spec in ("All step checks pass", "workflow_receipt", None):
        return
    if not isinstance(spec, dict) or spec.get("predicate") not in {"workflow_receipt", "all_steps_verified", "at_least"}:
        raise ValueError("Which measurable result should confirm this task is finished?")
    if spec["predicate"] == "at_least":
        if spec.get("metric") not in {"source_count", "prospect_count", "draft_count", "variant_count"} or type(spec.get("count")) is not int or not 1 <= spec["count"] <= 1000:
            raise ValueError("Choose a supported result count for this task.")


def passes(check: dict, evidence: list[dict]) -> bool:
    validate(check)
    if check.get("type") == "rubric":
        return False  # A separate metered verifier owns judgment-based checks.
    spec = check.get("spec")
    if isinstance(spec, dict) and spec.get("predicate") == "at_least":
        if spec["metric"] == "source_count":
            sources = [item for item in evidence if "source_count" in item]
            if any("source_hashes" in item for item in sources):
                hashes = set()
                for item in sources:
                    values = item.get("source_hashes")
                    if (not isinstance(values, list) or type(item.get("source_count")) is not int
                            or any(not isinstance(value, str) or len(value) != 64
                                   or any(char not in "0123456789abcdef" for char in value) for value in values)
                            or len(set(values)) != item["source_count"]):
                        return False
                    hashes.update(values)
                return len(hashes) >= spec["count"]
            if len(sources) > 1:
                return False  # Multiple opaque counts cannot prove distinct sources.
        return sum(int(item.get(spec["metric"], 0)) for item in evidence) >= spec["count"]
    return True  # Caller must first verify every source-owned workflow receipt.
