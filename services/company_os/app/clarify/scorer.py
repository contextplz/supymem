"""Score clarify options."""
def score_option(option: dict) -> float:
    return 1.0 if option.get("recommended") else 0.5
