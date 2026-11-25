"""A pre-retirement row for tests of existing template-page rewrites."""


def seed_legacy_page(workspace_id, venture_id=None):
    from app.db.models import SiteVersion
    from app.firstlook.versions import get_latest, next_version
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    if get_latest(workspace_id, venture_id=venture_id) is not None:
        return
    version = next_version(workspace_id) if get_latest(workspace_id) is not None else 0
    with db()() as session:
        session.add(SiteVersion(id=new_id("site"), workspace_id=workspace_id, venture_id=venture_id,
            version=version, stage="first_look", template="legacy_fixture", archetype="landing",
            brandkit_hash="legacy", copy_hash="legacy", path="legacy-fixture.html",
            sha256="0" * 64, created_at=utcnow()))
        session.commit()
