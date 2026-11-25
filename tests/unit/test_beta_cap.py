"""Closed-beta seat cap, code max_uses and admin mint/list."""
from core.assertion import sign_assertion
from tests.unit.test_beta_codes import client as client, SECRET
from tests.unit.test_referral_gateway_onboarding import hdr


def test_cap_refuses_new_keeps_existing_and_code_max_uses(client, monkeypatch):
    from app import state
    from app.db.supymem_models import BetaCode
    monkeypatch.setenv("SUPYMEM_BETA_MAX_FOUNDERS", "2")
    for person in ("usr_a", "usr_b"):
        assert client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr(person)).status_code == 200
    full = client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr("usr_c"))
    assert full.status_code == 403 and full.json()["detail"]["code"] == "beta_full"
    assert "waitlist" in full.json()["detail"]["message"]
    # Existing members keep access; a new person with no code is also told the beta is full.
    assert client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr("usr_a")).status_code == 200
    assert client.post("/beta/onboard", json={}, headers=hdr("usr_d")).json()["detail"]["code"] == "beta_full"
    # Per-code max_uses still applies below the cap.
    monkeypatch.setenv("SUPYMEM_BETA_MAX_FOUNDERS", "20")
    with state.db()() as s:
        s.get(BetaCode, "ABCD2345").max_uses = 2
        s.commit()
    assert client.post("/beta/onboard", json={"invite_code": "ABCD2345"}, headers=hdr("usr_e")).json()["detail"]["code"] == "invite_code_exhausted"


def test_admission_off_is_open_and_code_under_cap_admits(client):
    from app import state
    from app.consumer.referrals import admit
    from app.db.supymem_models import ReferralAdmission
    with state.db()() as s:
        admit(s, person_id="usr_open", user_id="usr_open", invite_code=None, required=False)
        admit(s, person_id="usr_coded", user_id="usr_coded", invite_code="ABCD2345", required=True)
        s.commit()
        assert s.get(ReferralAdmission, "usr_open") and s.get(ReferralAdmission, "usr_coded")


def test_admin_mint_and_list_beta_codes_authz(client):
    def admin(method, sub="usr_admin", email="admin@example.test"):
        return {"X-COS-Assertion": sign_assertion(SECRET, sub=sub, email=email, role="user", path="/admin/beta-codes", method=method)}
    body = {"max_uses": 20, "label": "Beta cohort 1"}
    assert client.post("/admin/beta-codes", json=body, headers=admin("POST", "usr_x", "x@example.test")).status_code in {401, 403}
    assert client.get("/admin/beta-codes", headers=admin("GET", "usr_x", "x@example.test")).status_code in {401, 403}
    minted = client.post("/admin/beta-codes", json=body, headers=admin("POST"))
    assert minted.status_code == 200 and minted.json()["code"]["max_uses"] == 20
    code = minted.json()["code"]["code"]
    listing = client.get("/admin/beta-codes", headers=admin("GET")).json()
    assert listing["beta_max_founders"] == 20 and listing["seats_taken"] == 0
    assert any(row["code"] == code and row["remaining"] == 20 for row in listing["codes"])
    assert client.post("/beta/onboard", json={"invite_code": code}, headers=hdr("usr_new1")).status_code == 200
