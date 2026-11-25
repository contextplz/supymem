"""Choose the same live venture app for hosted beacons and managed signups."""

from sqlalchemy import select

from app.db.supymem_models import FounderApp


def select_venture_app(session, venture_id: str, workspace_id: str | None = None) -> FounderApp | None:
    statement = select(FounderApp).where(
        FounderApp.venture_id == venture_id,
        FounderApp.deleted_at.is_(None),
    )
    if workspace_id is not None:
        statement = statement.where(FounderApp.workspace_id == workspace_id)
    return session.scalar(statement.order_by(FounderApp.created_at.asc(), FounderApp.id.asc()).limit(1))
