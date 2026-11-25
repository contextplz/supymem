"""Request-local citation labels, resolved only through a server-owned source map.

Long random database IDs can legitimately resemble secrets to the DLP boundary.
Keep the guard intact: the provider only needs short labels, not database IDs.
"""
from __future__ import annotations


def project(sources: list[dict], *, prefix: str = "source") -> tuple[list[dict], dict[str, str]]:
    if len({source["id"] for source in sources}) != len(sources):
        raise ValueError("duplicate citation source")
    mapping = {f"{prefix}_{index + 1}": source["id"] for index, source in enumerate(sources)}
    return [{**source, "id": label} for label, source in zip(mapping, sources, strict=True)], mapping


def restore(value, mapping: dict[str, str]):
    """Rewrite only declared citation fields after strict schema validation."""
    if isinstance(value, list):
        return [restore(item, mapping) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"source_id", "competitor_source_id"}:
            result[key] = mapping[item]
        elif key == "source_ids":
            result[key] = [mapping[source_id] for source_id in item]
        else:
            result[key] = restore(item, mapping)
    return result
