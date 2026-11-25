"""Waitlist migration and concurrent persistence on isolated local PostgreSQL."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import func, select, inspect
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def test_concurrent_signup_deduplicates_on_migrated_storage(journey_db):
    from app.conversion import waitlist
    from app.db.supymem_models import VentureWaitlistEntry
    d=journey_db
    columns={c['name']:c['type'] for c in inspect(d['engine']).get_columns('venture_waitlists')}
    assert all(columns[name].timezone for name in ('updated_at','verified_at','probe_expires_at'))
    waitlist.configure(d['vid'],d['wid'],enabled=True,actor=d['actor'])
    details=waitlist.public_details(d['vid'])
    payload={'email':'consenting@sample-business.example','consent':True,'consent_version':waitlist.VERSION,
             'consent_digest':hashlib.sha256(details['consent'].encode()).hexdigest()}
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:waitlist.capture(d['vid'],payload),range(10)))
    assert all(r=={'received':True,'email_verified':False} for r in results)
    with d['factory']() as session:
        assert session.scalar(select(func.count()).select_from(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id==d['vid']))==1
    assert waitlist.status(d['vid'],d['wid'])['total']==1
    with pytest.raises(waitlist.WaitlistError, match='waitlist_not_found'):
        waitlist.status(d['vid'],'foreign-workspace',include_leads=True)


def test_thirty_venture_conversion_burst_is_isolated_and_replay_safe(journey_db):
    """Storage admission load only: not thirty complete founder journeys."""
    from time import monotonic
    from app.conversion import waitlist
    from app.authz.workspace import ensure_subject_person
    from app.workspaces.service import create_workspace
    from app.db.models import Venture
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    scopes = []
    for index in range(30):
        actor = new_id("usr")
        ensure_subject_person(actor)
        workspace = create_workspace(kind="company", name=f"Burst fixture {index}", owner_id=actor)
        vid = new_id("ven")
        with d["factory"]() as session:
            session.add(Venture(id=vid, workspace_id=workspace.id, name=f"Burst fixture {index}",
                raw_idea="Isolated test fixture", stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        waitlist.configure(vid, workspace.id, enabled=True, actor=actor)
        details = waitlist.public_details(vid)
        payload = {"email": f"founder-{index}@fixture.business", "consent": True, "consent_version": waitlist.VERSION,
            "consent_digest": hashlib.sha256(details["consent"].encode()).hexdigest()}
        scopes.append((vid, workspace.id, payload))
    def submit(scope):
        vid, wid, payload = scope
        first = waitlist.capture(vid, payload)
        assert waitlist.capture(vid, payload) == first
        return waitlist.status(vid, wid, include_leads=True)
    started = monotonic()
    with ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(submit, scopes, timeout=60))
    for index, result in enumerate(results):
        assert result["total"] == 1 and result["submission_verified"] is False
        assert scopes[index][2]["email"] in str(result)
        other_wid = scopes[(index+1) % len(scopes)][1]
        with pytest.raises(waitlist.WaitlistError, match="waitlist_not_found"):
            waitlist.status(scopes[index][0], other_wid, include_leads=True)
    print(f"30 venture conversion burst: 60 submissions and 30 scoped reads in {monotonic()-started:.3f}s")
