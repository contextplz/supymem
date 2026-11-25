"""Explicit synthetic content data belongs in tests, not the product template."""
from copy import deepcopy


def filled_brief():
    from app.content.brief import TEMPLATE
    result = deepcopy(TEMPLATE)
    result["product"].update(name="Acceptance product", one_liner="Synthetic content workflow fixture")
    result["icp"]["segments"][0].update(name="Acceptance founders", pains=["Manual workflow friction"])
    result["cadence"].update(linkedin={"per_week": 4}, x={"per_day": 2}, reddit={"per_week": 1})
    return result
