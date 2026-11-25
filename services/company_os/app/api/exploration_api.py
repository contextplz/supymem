"""Read-only exploration view. Experiment services own storage and tenancy."""
import logging

from fastapi import APIRouter, Header, HTTPException

from app.authz.workspace import assert_venture_access
from app.experiments.exploration import list_rounds

router = APIRouter(tags=["experiments"])
log = logging.getLogger(__name__)


@router.get("/ventures/{venture_id}/experiments/rounds")
def exploration_rounds(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    try:
        view = list_rounds(venture_id)
    except KeyError:
        raise HTTPException(404, "not_found") from None
    from app.chat.next_moves import compose

    try:
        view["next_moves"] = compose(venture_id)
    except Exception:
        log.warning("exploration_priorities_unavailable", extra={"venture_id": venture_id}, exc_info=True)
        view["next_moves"] = None
    from app.experiments.ledger import autopilot_enabled

    if autopilot_enabled():
        from app.api.exploration_read import autopilot_view

        try:
            view["experiments_autopilot_v1"] = autopilot_view(venture_id, workspace_id, x_subject)
        except Exception:
            log.warning("exploration_autopilot_unavailable", extra={"venture_id": venture_id}, exc_info=True)
            view["experiments_autopilot_v1"] = None
    return view
