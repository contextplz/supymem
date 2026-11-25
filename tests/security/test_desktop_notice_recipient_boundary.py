"""Actual signed notices bind workspace membership and an optional exact person."""
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers, make_workspace


def test_signed_shared_workspace_recipient_cannot_read_or_ack_another_persons_notice(client):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from app.db.supymem_models import Notice
    from app.state import db
    from core.clock import utcnow

    wid = make_workspace(client, "usr_alice")
    ensure_subject_person("usr_bob")
    with db()() as session:
        session.add(WorkspaceMember(id="wsm_notice_bob", workspace_id=wid, person_id="usr_bob", role="viewer", created_at=utcnow()))
        for identifier, person in (("not_shared", None), ("not_alice_only", "usr_alice")):
            session.add(Notice(id=identifier, workspace_id=wid, person_id=person, kind="digest", title="Saved digest",
                body="Owned recorded event", target_path=None, created_at=utcnow()))
        session.commit()
    path = "/desktop/notices"
    assert client.get(path).status_code == 401
    alice = client.get(path, headers=headers(path))
    assert alice.status_code == 200 and {row["id"] for row in alice.json()["notices"]} == {"not_shared", "not_alice_only"}
    bob = client.get(path, headers=headers(path, subject="usr_bob"))
    assert bob.status_code == 200 and [row["id"] for row in bob.json()["notices"]] == ["not_shared"]
    ack_path = path + "/ack"
    refused = client.post(ack_path, headers=headers(ack_path, "POST", "usr_bob"), json={"ids": ["not_alice_only"]})
    assert refused.status_code == 200 and refused.json()["acked"] == 0
    with db()() as session:
        assert session.get(Notice, "not_alice_only").read_at is None
    allowed = client.post(ack_path, headers=headers(ack_path, "POST"), json={"ids": ["not_alice_only"]})
    assert allowed.status_code == 200 and allowed.json()["acked"] == 1


def test_signed_foreign_workspace_cannot_read_or_ack_even_a_person_targeted_notice(client):
    from app.db.supymem_models import Notice
    from app.state import db
    from core.clock import utcnow

    wid = make_workspace(client, "usr_alice")
    with db()() as session:
        session.add(Notice(id="not_foreign_membership", workspace_id=wid, person_id="usr_bob", kind="digest",
            title="Private scoped notice", body="Saved event", target_path=None, created_at=utcnow()))
        session.commit()
    path = "/desktop/notices"
    assert client.get(path, headers=headers(path, subject="usr_bob")).json()["notices"] == []
    ack_path = path + "/ack"
    result = client.post(ack_path, headers=headers(ack_path, "POST", "usr_bob"), json={"ids": ["not_foreign_membership"]})
    assert result.status_code == 200 and result.json()["acked"] == 0
    with db()() as session:
        assert session.get(Notice, "not_foreign_membership").read_at is None
