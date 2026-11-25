#!/usr/bin/env python3
"""Add ONE contact the founder owns to a venture's outreach target list, so the real approve-and-send path can be tested.

    python scripts/outreach/seed_test_contact.py --venture ven_xxx --email operator@example.test --i-own-this-address

Runs against whatever COMPANY_OS_DB_URL points at (use a local Core, never a shared database you do not control).
Safe to run twice. Then ask chat "draft outreach emails": this contact is the only one without a draft, so the batch is exactly one email.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "company_os"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--venture", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--i-own-this-address", action="store_true", help="required: the address is the founder's own mailbox")
    a = ap.parse_args()
    if not a.i_own_this_address or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a.email):
        print("Refusing: pass a valid address and --i-own-this-address.", file=sys.stderr)
        return 2
    from app.db.models import GtmContact, Venture
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    from sqlalchemy import select

    email, now = a.email.strip().lower(), utcnow()
    with db()() as s:
        v = s.get(Venture, a.venture)
        if v is None:
            print("Unknown venture.", file=sys.stderr)
            return 1
        c = s.scalar(select(GtmContact).where(GtmContact.workspace_id == v.workspace_id, GtmContact.email == email))
        if c is None:
            c = GtmContact(id=new_id("gtmc"), workspace_id=v.workspace_id, email=email, email_confidence="verified",
                           lawful_basis="legitimate_interest", lawful_basis_note="Founder's own address, used to test delivery.",
                           lawful_basis_at=now, source=f"venture:{a.venture}:founder_test", dossier_hook="Founder's own test address",
                           enrichment_json=json.dumps({"business": "Supymem test"}), stage_changed_at=now, created_at=now)
            s.add(c)
            s.commit()
            print(f"Added {email} to {a.venture}.")
        else:
            print(f"{email} already exists (times_emailed={c.times_emailed or 0}); a contact is emailed only once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
