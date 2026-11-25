"""Genuine server-generated static source admission; no native execution claim."""

ORIGIN = "pagebuilder_candidate_v1"
TRIGGER = "pagebuilder_source_qa"


def owns(run, spec):
    return run.trigger == TRIGGER and spec.get("source_origin") == ORIGIN and spec.get("lifecycle_version") == 3
