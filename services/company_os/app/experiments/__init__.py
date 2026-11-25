from app.experiments.service import (
    conclude,
    create_experiment,
    list_experiments,
    run_evaluate,
)

MAX_ACTION_LENGTH = 2000


def normalize_action(text: str) -> str:
    from core.copy import founder_copy

    action = founder_copy(" ".join(str(text or "").split()))
    if len(action) > MAX_ACTION_LENGTH:
        raise ValueError("Your test plan is too long. Ask me to prepare a shorter plan with complete instructions.")
    return action


__all__ = ["MAX_ACTION_LENGTH", "conclude", "create_experiment", "list_experiments", "normalize_action", "run_evaluate"]
