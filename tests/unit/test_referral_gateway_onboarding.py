"""The trusted verified gateway path activates accounts atomically."""
from datetime import datetime, timezone
from core.assertion import sign_assertion
from tests.unit.test_beta_codes import client as client, SECRET, HASH, _hdr


def hdr(subject, role="gateway_onboarding"):
    return {"X-COS-Assertion": sign_assertion(SECRET, sub=subject, email="member@example.test", role=role,
        path="/beta/onboard", method="POST")}


def test_trusted_admission_and_exhaustion_do_not_issue_third_account(client, monkeypatch):
    from app import state
    from app.db.models import Person
    from app.db.supymem_models import ReferralAdmission, BetaCodeRedemption, PersonReferralCode
    monkeypatch.setenv("SUPYMEM_REFERRAL_ADMISSION_REQUIRED", "1")
    assert client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr("usr_one", role="user")).status_code == 403
    # The existing founder-style fixture code is unbounded; bound it to two.
    from app.db.supymem_models import BetaCode
    with state.db()() as s:
        s.get(BetaCode, "ABCD2345").max_uses = 2
        s.commit()
    for person in ("usr_one", "usr_two"):
        assert client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr(person)).status_code == 200
        assert client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr(person)).status_code == 200
    refused = client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr("usr_three"))
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "invite_code_exhausted"
    # Capacity gates new membership, not an existing member's installer retry.
    assert client.get("/public/beta/download?code=ABCD2345", headers={"X-Client-Hash": HASH}).status_code == 302
    with state.db()() as s:
        assert s.get(Person, "usr_three") is None
        assert s.get(ReferralAdmission, "usr_three") is None
        assert s.get(PersonReferralCode, "usr_three") is None
        assert s.query(BetaCodeRedemption).count() == 2
    own = client.get("/beta/codes", headers=_hdr("/beta/codes", sub="usr_one")).json()["codes"]
    assert len(own) == 1 and own[0]["remaining"] == 2
    assert own[0]["invite_url"].startswith("https://app.supymem.com/?invite=")


def test_missing_invite_refuses_without_activation_and_historic_gateway_account_survives(client, monkeypatch):
    from app import state
    from app.db.supymem_models import ReferralProgramState, ReferralAdmission
    monkeypatch.setenv("SUPYMEM_REFERRAL_ADMISSION_REQUIRED", "1")
    with state.db()() as s:
        s.add(ReferralProgramState(id=1, grandfather_before=datetime(2026, 10, 2, tzinfo=timezone.utc)))
        s.commit()
    refused = client.post("/beta/onboard", json={}, headers=hdr("usr_new"))
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "invite_code_required"
    ok = client.post("/beta/onboard", json={"legacy_created_at": "2026-10-01T00:00:00Z"}, headers=hdr("usr_existing"))
    assert ok.status_code == 200
    with state.db()() as s:
        assert s.get(ReferralAdmission, "usr_existing").legacy
        assert s.get(ReferralAdmission, "usr_new") is None
    # A regular browser user cannot assert an old registration date.
    assert client.post("/beta/onboard", json={"legacy_created_at": "2020-01-01T00:00:00Z"}, headers=hdr("usr_spoof", "user")).status_code == 403


def test_admin_allowance_cannot_be_changed_by_normal_user(client):
    assert client.put("/admin/referrals/usr_one", json={"max_uses": 100, "active": True, "reason": "not authorized"},
        headers=_hdr("/admin/referrals/usr_one", "PUT", sub="usr_one")).status_code in {401, 403}


def test_admin_legacy_multiple_codes_reads_and_edits_the_canonical_owner_code(client):
    from app import state
    from app.db.models import Person
    from app.db.supymem_models import BetaCode, PersonReferralCode, BetaCodeAllowanceChange, BetaCodeDownload
    with state.db()() as session:
        session.add(Person(id="usr_multi", display_name="Member", email="multi@example.test", created_at=datetime.now(timezone.utc)))
        session.add_all([BetaCode(code="LEGACY22", owner_person_id="usr_multi", label="legacy standalone", active=True, max_uses=90, created_at=datetime.now(timezone.utc)),
            BetaCode(code="PRIMARY2", owner_person_id="usr_multi", label="canonical", active=True, max_uses=2, created_at=datetime.now(timezone.utc))])
        session.flush()
        session.add(PersonReferralCode(person_id="usr_multi", code="PRIMARY2"))
        session.add(BetaCodeDownload(code="LEGACY22", created_at=datetime.now(timezone.utc), ip_hash="history", user_agent="", platform="macos", version="latest"))
        session.commit()
    path = "/admin/referrals/usr_multi"
    def admin_headers(method):
        return {"X-COS-Assertion": sign_assertion(SECRET, sub="usr_admin", email="admin@example.test", role="user", path=path, method=method)}
    before = client.get(path, headers=admin_headers("GET"))
    assert before.status_code == 200
    assert before.json()["primary_code"] == "PRIMARY2"
    canonical = next(code for code in before.json()["codes"] if code["code"] == before.json()["primary_code"])
    assert canonical["max_uses"] == 2
    changed = client.put(path, headers=admin_headers("PUT"), json={"max_uses": 3, "active": False, "reason": "reviewed canonical change"})
    assert changed.status_code == 200 and changed.json()["referral"]["code"] == before.json()["primary_code"]
    after = client.get(path, headers=admin_headers("GET")).json()
    assert after["primary_code"] == "PRIMARY2"
    assert next(code for code in after["codes"] if code["code"] == "LEGACY22")["max_uses"] == 90
    assert after["history"][-1]["code"] == "PRIMARY2"
    assert after["history"][-1]["actor_admin_id"] == "bootstrap:usr_admin"
    assert after["history"][-1]["before_limit"] == 2 and after["history"][-1]["after_limit"] == 3
    with state.db()() as session:
        assert session.get(PersonReferralCode, "usr_multi").code == "PRIMARY2"
        assert session.query(BetaCodeDownload).filter_by(code="LEGACY22").count() == 1
        assert session.query(BetaCodeAllowanceChange).filter_by(code="LEGACY22").count() == 0
