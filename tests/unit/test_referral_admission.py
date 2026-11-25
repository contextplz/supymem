"""Invitation capacity is successful atomic activation, not installer traffic."""
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from tests.company_os_harness import load_company_os_app

@pytest.fixture
def engine():
    load_company_os_app()
    from app.db.models import Base
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    from app.db.supymem_models import BetaCode
    with Session(engine) as s:
        s.add(BetaCode(code="INVITE22", owner_person_id="owner", label="original", active=True,
            max_uses=2, created_at=datetime.now(timezone.utc)))
        s.commit()
    return engine

def test_two_distinct_activation_receipts_and_no_retry_charge(engine):
    from app.consumer.referrals import admit, used, ReferralDenied
    from app.db.supymem_models import ReferralAdmission
    for person in ("one", "two"):
        with Session(engine) as s:
            admit(s, person_id=person, user_id=person, invite_code="INVITE22", required=True)
            s.commit()
        with Session(engine) as s:
            admit(s, person_id=person, user_id=person, invite_code="INVITE22", required=True)
            s.commit()
    with Session(engine) as s:
        with pytest.raises(ReferralDenied, match="exhausted"):
            admit(s, person_id="three", user_id="three", invite_code="INVITE22", required=True)
        s.rollback()
        assert used(s, "INVITE22") == 2
        assert s.get(ReferralAdmission, "three") is None

def test_failed_activation_rolls_back_redemption_and_new_code(engine):
    from app.consumer.referrals import admit, used
    from app.db.supymem_models import PersonReferralCode, ReferralAdmission
    with Session(engine) as s:
        admit(s, person_id="one", user_id="one", invite_code="INVITE22", required=True)
        s.rollback()
        assert used(s, "INVITE22") == 0
        assert s.get(PersonReferralCode, "one") is None
        assert s.get(ReferralAdmission, "one") is None

def test_reject_self_disabled_missing_and_second_attribution(engine):
    from app.consumer.referrals import admit, redeem, ReferralDenied
    from app.db.supymem_models import BetaCode
    with Session(engine) as s:
        with pytest.raises(ReferralDenied, match="self"):
            admit(s, person_id="owner", user_id="owner", invite_code="INVITE22", required=True)
        s.rollback()
        with pytest.raises(ReferralDenied, match="required"):
            admit(s, person_id="one", user_id="one", invite_code=None, required=True)
        s.rollback()
        admit(s, person_id="one", user_id="one", invite_code="INVITE22", required=True)
        s.commit()
        with pytest.raises(ReferralDenied, match="already_onboarded"):
            redeem(s, code="OTHER222", person_id="one", user_id="one")
        s.rollback()
        s.get(BetaCode, "INVITE22").active = False
        s.commit()
        with pytest.raises(ReferralDenied, match="invalid"):
            admit(s, person_id="two", user_id="two", invite_code="INVITE22", required=True)

def test_allowance_preserves_history_and_original_code(engine):
    from app.consumer.referrals import admit, provision, change_allowance, payload
    from app.db.supymem_models import BetaCodeAllowanceChange
    with Session(engine) as s:
        original = provision(s, "owner")
        assert original.code == "INVITE22"
        assert original.label == "original"
        admit(s, person_id="one", user_id="one", invite_code="INVITE22", required=True)
        change_allowance(s, row=original, actor="admin", limit=0, active=True, reason="pause invitations")
        s.commit()
        assert payload(s, original)["successful_invitations"] == 1
        assert payload(s, original)["remaining"] == 0
        assert s.scalar(select(func.count()).select_from(BetaCodeAllowanceChange)) == 1
        change_allowance(s, row=original, actor="admin", limit=3, active=True, reason="expand invitations")
        s.commit()
        assert payload(s, original)["remaining"] == 2
        assert s.scalar(select(func.count()).select_from(BetaCodeAllowanceChange)) == 2


def test_generated_code_collision_retries_without_losing_transaction(engine, monkeypatch):
    from app.consumer import referrals
    sequence = iter("INVITE22NEWCODE2")
    monkeypatch.setattr(referrals.secrets, "choice", lambda _: next(sequence))
    with Session(engine) as s:
        code = referrals.provision(s, "new_owner")
        assert code.code == "NEWCODE2"
        s.commit()
        assert referrals.provision(s, "new_owner").code == "NEWCODE2"
