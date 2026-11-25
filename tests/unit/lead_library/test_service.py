"""Lead Library match, claim, cap, suppression and self-learning against the synthetic fixture."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as journey_db

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "lead_library" / "synthetic_leads.jsonl"
DEVTOOLS = {"who": "Engineering leaders at developer tools startups", "problem": "flaky CI pipelines slow down releases",
            "offer": "CI test analytics for DevOps teams", "location": "United States"}
ELDERCARE = {"who": "Founders of eldercare startups in India", "problem": "families miss medication schedules for elderly parents",
             "offer": "medication reminder service for elder care homes"}


@pytest.mark.parametrize("audience", ["Freight brokers", "Apartment gardeners"])
def test_local_library_refusal_offers_search_and_own_import(journey_db, monkeypatch, audience):
    from app.lead_library import service

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with pytest.raises(service.LeadLibraryError) as refused:
        service.claim(journey_db["wid"], journey_db["vid"], [audience], journey_db["actor"])
    message = str(refused.value)
    assert "You cannot use purchased contacts locally" in message
    assert "search public sources" in message and "import your own contacts" in message
    assert "Contacts > Import" in message


@pytest.mark.hosted_only
def test_nightly_sweep_bulk_lookup_batches_have_short_commits_across_domains(library, monkeypatch):
    from sqlalchemy import event
    from app.db.models import GtmContact
    from app.gtm.shared_models import SharedAddressValidity
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library import service
    from core.clock import utcnow
    monkeypatch.setenv("LEAD_SWEEP_BATCH_SIZE", "25")
    now = utcnow()
    with library["factory"]() as session:
        entries = [session.get(LeadLibraryEntry, entry_id(index)) for index in (1, 20)]
        for entry in entries:
            entry.email_status = "unverified_public"
            session.merge(SharedAddressValidity(address_hash=entry.email_hash, status="deliverable", last_seen=now))
        for index in range(200):
            entry = entries[index % 2]
            suffix = "2" if index % 2 else ""
            email = entry.email if index < 2 else f"batch-{index}@{entry.email.rsplit('@', 1)[1]}"
            session.add(GtmContact(id=f"batch-contact-{index:03}", workspace_id=library["wid" + suffix],
                email=email, source=f"venture:{library['vid' + suffix]}:lead_library:{entry.external_lead_id}",
                times_emailed=1, last_emailed_at=now - timedelta(days=4), created_at=now, stage_changed_at=now))
        session.commit()
    statements, commits = [], []
    engine = library["factory"].kw["bind"]
    def sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def commit(connection):
        commits.append(True)
    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        assert service.sweep(now=now)["promoted"] == 2
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    assert len(commits) == 8
    assert len([statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]) <= 40


def _rows():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def seed(factory, rows=None):
    from app.gtm.email_learning import opaque
    from app.lead_library.models import LeadLibraryEntry, LeadLibraryTag
    from core.clock import utcnow
    with factory() as session:
        for row in rows or _rows():
            entry_id = "ll_" + hashlib.sha256(row["external_lead_id"].encode()).hexdigest()[:24]
            fields = {k: row[k] for k in ("external_lead_id", "person_name", "role_title", "company", "company_domain", "website", "linkedin_url",
                "x_url", "email", "email_status", "phone", "phone_status", "country_code", "region", "city", "geography_raw", "lead_type", "function",
                "seniority", "company_stage", "confidence", "fit_score", "quality_score", "source_url", "source_host", "discovery_method",
                "license_source", "batch_id", "collected_at", "content_hash")}
            session.add(LeadLibraryEntry(id=entry_id, **fields, email_hash=opaque(row["email"]) if row["email"] else None,
                usable_channels=json.dumps(row["usable_channels"]), quality_flags=json.dumps(row["quality_flags"]),
                provenance_json=json.dumps(row["provenance"]), imported_at=utcnow(), status="active", search_text=""))
            session.add_all([LeadLibraryTag(entry_id=entry_id, tag_type=kind, tag=tag) for kind, tags in row["tags"].items() for tag in tags])
        session.commit()


def entry_id(n: int) -> str:
    return "ll_" + hashlib.sha256(f"synthetic_{n:03d}".encode()).hexdigest()[:24]


def _ext(items):
    return [item["name"] for item in items]


NAMES = {json.loads(line)["external_lead_id"][-3:]: json.loads(line)["person_name"] for line in FIXTURE.read_text().splitlines() if line.strip()}


def names(*numbers):
    return [NAMES[f"{n:03d}"] for n in numbers]


@pytest.fixture()
def library(journey_db, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    monkeypatch.setattr("app.intake.contacts._prepare_drafts", lambda wid, vid: "ready")
    seed(journey_db["factory"])
    actor = "usr_lead_library_india"
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Eldercare fixture", owner_id=actor)
    now = utcnow()
    with journey_db["factory"]() as session:
        session.add(Venture(id="ven_eldercare", workspace_id=workspace.id, name="Dawa reminders", raw_idea="Medication reminders for elder care",
                            stage="draft", created_at=now, updated_at=now))
        session.commit()
    return journey_db | {"wid2": workspace.id, "vid2": "ven_eldercare", "actor2": actor}


@pytest.fixture()
def published_library(library):
    from app.lead_library.models import LeadLibraryEntry
    with library["factory"]() as session:
        for entry in session.scalars(select(LeadLibraryEntry)):
            entry.provenance_json = json.dumps(json.loads(entry.provenance_json) | {"email_published": True})
        session.commit()
    return library


@pytest.mark.usefixtures("published_library")
@pytest.mark.hosted_only
def test_match_two_ventures_ranks_by_overlap_then_fit_and_filters_country_and_type(library):
    from app.lead_library.service import match
    collisions = []
    for number, person in ((4, "Morgan Bell"), (9, "Riley Park")):
        row = json.loads(json.dumps(_rows()[number - 1]))
        row.update(external_lead_id=f"title_collision_{number}", person_name=person, role_title="Managing Director",
                   seniority="c_level", fit_score=100, email_status="delivered")
        collisions.append(row)
    seed(library["factory"], collisions)
    devtools = match(library["vid"], icp=DEVTOOLS, limit=10)
    # US only, people at companies only; the company inbox follows the personal channels.
    assert _ext(devtools["items"]) == names(1, 2, 19, 5, 4)
    assert devtools["total"] == 5 and "developer tools" in devtools["items"][0]["why"]
    assert devtools["items"][0]["channels"] == ["email", "LinkedIn"]
    india = match(library["vid2"], icp=ELDERCARE, limit=10)
    assert _ext(india["items"]) == names(9, 11)
    assert {item["country"] for item in india["items"]} == {"IN"}
    page = match(library["vid"], icp=DEVTOOLS, limit=2, offset=2)
    assert _ext(page["items"]) == names(19, 5) and page["total"] == 5


@pytest.mark.parametrize("audience,industry,niche,function,seniority,stage,size,titles,wrong_role,wrong_level", [
    ("Office managers at software startups with 8-24 employees who arrange team lunches", "b2b_saas_general", "software",
     "operations", "manager", "startup", 12,
     ["Workplace Manager", "Administration Manager", "Office Manager", "Operations Manager"], "Supply Chain Manager", "COO"),
    ("Office managers at software startups with 8-24 employees", "devtools", "developer tools",
     "operations", "manager", "startup", 12,
     ["Workplace Manager", "Administration Manager", "Office Manager", "Operations Manager"], "Supply Chain Manager", "COO"),
    ("Office managers at tech startups with 8-24 employees", "b2b_saas_general", "software",
     "operations", "manager", "startup", 12,
     ["Workplace Manager", "Administration Manager", "Office Manager", "Operations Manager"], "Country Manager", "COO"),
    ("Office managers at tech startups with 8-24 employees", "hardware_iot", "connected sensors",
     "operations", "manager", "startup", 12,
     ["Workplace Manager", "Administration Manager", "Office Manager", "Operations Manager"], "Country Manager", "COO"),
    ("Software startup office managers with 8-24 employees", "b2b_saas_general", "software",
     "operations", "manager", "startup", 12,
     ["Workplace Manager", "Administration Manager", "Office Manager", "Operations Manager"], "Country Manager", "COO"),
    ("Clinic owners in small dental practices", "healthtech", "dental practice", "founder_exec", "founder_owner", "unknown", "small",
     ["Practice Owner", "Dentist and Proprietor"], "Practice Manager", "CEO"),
    ("Clinic owners in small dental clinics", "healthtech", "dental practice", "founder_exec", "founder_owner", "unknown", "small",
     ["Practice Owner", "Dentist and Proprietor"], "Practice Manager", "CEO"),
    ("Small dental practice owners", "healthtech", "dental practice", "founder_exec", "founder_owner", "unknown", "small",
     ["Practice Owner", "Dentist and Proprietor"], "Practice Manager", "CEO"),
    ("Clinic owners of small dental practices in United States", "healthtech", "dental practice", "founder_exec", "founder_owner", "unknown", "small",
     ["Practice Owner", "Dentist and Proprietor"], "Practice Manager", "CEO"),
    ("Heads of growth at B2B SaaS firms with 60-180 staff", "b2b_saas_general", "software", "marketing", "vp", "scaleup", 90,
     ["Head of Growth", "VP Growth"], "Head of Marketing", "Growth Specialist"),
])
@pytest.mark.hosted_only
def test_library_requires_role_industry_and_known_size_without_padding(
        library, audience, industry, niche, function, seniority, stage, size, titles, wrong_role, wrong_level):
    from app.chat import customers
    from app.lead_library.service import match

    icp = {"who": audience, "problem": "Medical teams lose time to clinic administration",
           "offer": "Scheduling and customer communication software", "business_model": "b2b"}
    candidates = [(title, industry, niche, size, seniority) for title in titles] + [
        (wrong_role, industry, niche, size, seniority),
        (wrong_level, industry, niche, size, "c_level" if seniority != "vp" else "ic"),
        (titles[0], "logistics_supply_chain", "freight transport", size, seniority),
        (titles[0], industry, niche, 900, seniority),
        (titles[0], industry, niche, None, seniority),
        (titles[0], industry, niche, "1-1000", seniority),
        (titles[0], industry, niche, "not recorded" if isinstance(size, str) else f"about {size} employees", seniority),
        (titles[0], industry, niche, False if isinstance(size, str) else f"-{size} employees", seniority),
    ]
    rows = []
    people = ["Alex Morgan", "Priya Shah", "Jamie Reed", "Rosa Chen", "Sam Taylor", "Amara Okafor", "Eli Brooks", "Nadia Ali", "Leo Costa", "Maya Singh", "Noah Kim", "Imani Cole"]
    for index, (title, sector, specialty, headcount, level) in enumerate(candidates):
        row = json.loads(json.dumps(_rows()[0]))
        domain = f"cedar-{specialty.replace(' ', '-')}-{index}.test"
        row.update(external_lead_id=f"relevance_{function}_{index}", person_name=people[index], role_title=title,
                   company=f"Cedar {specialty.title()} {index}", company_domain=domain, website=f"https://{domain}",
                   email=f"{people[index].split()[0].lower()}@{domain}",
                   linkedin_url=f"https://www.linkedin.com/in/{people[index].replace(' ', '-').lower()}-{index}",
                   function=function, seniority=level, company_stage=stage,
                   fit_score=index + 1, email_status="delivered" if index >= len(titles) else "public",
                   tags={"industry": [sector], "buyer_tag": ["healthcare_it", "erp_operations", "marketing_tools"],
                         "category_raw": [specialty], "niche_raw": []},
                   provenance={"company_size" if isinstance(size, str) or index % 2 else "employee_count": headcount} if headcount is not None else {})
        rows.append(row)
    if function == "founder_exec":
        unrelated = json.loads(json.dumps(rows[0]))
        unrelated.update(external_lead_id="unrelated_specialty", person_name="Quinn Osei", fit_score=100,
                         tags={"industry": [industry], "buyer_tag": ["healthcare_it"], "category_raw": ["veterinary clinic"], "niche_raw": []})
        rows.append(unrelated)
    conflicting = json.loads(json.dumps(rows[0]))
    conflicting.update(external_lead_id="conflicting_size", person_name="Jordan Blake", fit_score=100,
                       provenance={"company_size": size, "headcount": 900})
    rows.append(conflicting)
    customer_size = json.loads(json.dumps(rows[0]))
    customer_size.update(external_lead_id="customer_size", person_name="Casey Rivera", fit_score=100,
                         provenance={"qualification_notes": "Serves small practices" if isinstance(size, str) else f"Serves teams with {size} employees"})
    rows.append(customer_size)
    seed(library["factory"], rows)
    found = match(library["vid"], icp=icp, limit=5)
    expected = {row["person_name"] for row in rows[:len(titles)]}
    assert {item["name"] for item in found["items"]} == expected
    assert found["total"] == len(expected) < 5
    assert all("healthcare IT" not in item["why"] for item in found["items"])
    assert {item["name"] for item in match(library["vid"], icp=icp, limit=1, offset=1)["items"]} <= expected
    text, actions = customers._library_section(library["vid"], icp)
    assert f"Only {len(expected)}" in text and "Nothing is sent" in text
    assert "every email still waits for your approval" in text
    from app.chat.intent import find_customers_request
    assert any(action["label"] == "Search the web" and find_customers_request(action.get("text", "")) for action in actions)
    assert all(item["id"] in actions[0]["payload"]["entry_ids"] for item in found["items"])
    structured = icp | {"company_size": "small" if isinstance(size, str) else f"{size}-{size}"}
    assert {item["name"] for item in match(library["vid"], icp=structured)["items"]} == expected
    for malformed in ("size not recorded", {"size": "small"}, ["small"], True):
        assert match(library["vid"], icp=icp | {"company_size": malformed})["items"] == []


@pytest.mark.parametrize("venture,icp,template_number", [("", DEVTOOLS, 1), ("2", ELDERCARE, 9)])
@pytest.mark.hosted_only
def test_company_inbox_tier_ranking_chat_and_claim(library, venture, icp, template_number):
    from app.chat import customers
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, consent_block, source_line
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import channels, claim, match

    template = _rows()[template_number - 1]
    cases = [
        ("generic", ["email_generic"], True),
        ("shared", ["email_shared"], True),
        ("both", ["email_generic", "email_shared"], True),
        ("missing_role", ["email_generic", "missing_role"], True),
        ("mismatch_flag", ["email_generic", "email_domain_mismatch"], False),
        ("placeholder_flag", ["email_generic", "email_placeholder"], False),
        ("publisher", ["email_shared", "email_is_publisher"], False),
        ("not_personal", ["email_shared", "email_not_personal"], False),
        ("free_flag", ["email_generic", "email_free"], False),
        ("website_publisher", ["email_generic", "website_is_publisher"], False),
        ("mismatch_domain", ["email_generic"], False),
        ("missing_domain", ["email_generic"], False),
        ("invalid_email", ["email_generic"], False),
        ("missing_email", ["email_shared"], False),
        ("free_domain", ["email_generic"], False),
        ("placeholder_domain", ["email_generic"], False),
        ("placeholder_local", ["email_generic"], False),
        ("bounced", ["email_generic"], False),
        ("unusable", [], False),
        ("personal", [], True),
        ("linkedin", [], True),
        ("inbox_linkedin", ["email_shared"], True),
    ]
    rows = []
    for index, (label, flags, usable) in enumerate(cases):
        row = json.loads(json.dumps(template))
        domain = f"{label.replace('_', '-')}-{template['company_domain']}"
        row.update(external_lead_id=f"inbox_{template_number}_{label}", person_name=f"{template['person_name']} {label}",
            company_domain=domain, website=f"https://{domain}", email=f"contact@{domain}", email_status="public",
            usable_channels=[], quality_flags=flags, fit_score=100 - index, linkedin_url=None, x_url=None, phone=None)
        if label == "mismatch_domain":
            row["email"] = "contact@unrelated-business.test"
        elif label == "missing_domain":
            row["company_domain"] = None
        elif label == "invalid_email":
            row["email"] = f"not an email@{domain}"
        elif label == "missing_email":
            row["email"] = None
        elif label in {"free_domain", "placeholder_domain"}:
            row["company_domain"] = "gmail.com" if label == "free_domain" else "example.com"
            row["email"] = "contact@" + row["company_domain"]
        elif label == "placeholder_local":
            row["email"] = "john.doe@" + domain
        elif label == "bounced":
            row["email_status"] = "bounced"
        elif label == "personal":
            row["usable_channels"] = ["email"]
        elif label in {"linkedin", "inbox_linkedin"}:
            row["usable_channels"] = ["linkedin"]
            row["linkedin_url"] = f"https://www.linkedin.com/in/inbox-{template_number}-{label}"
            row["fit_score"] = 110 if label == "inbox_linkedin" else 1
        if label == "generic":
            row["email_status"] = "delivered"
            row["provenance"]["email_published"] = True
        rows.append(row)
    with library["factory"]() as session:
        for entry in session.scalars(select(LeadLibraryEntry)):
            entry.status = "retired"
        session.commit()
    seed(library["factory"], rows)
    with library["factory"]() as session:
        entries = {entry.external_lead_id: entry for entry in session.scalars(select(LeadLibraryEntry).where(LeadLibraryEntry.status == "active"))}
        for row, (label, flags, usable) in zip(rows, cases):
            expected = row["usable_channels"] + (["company inbox email"] if usable and flags else [])
            assert channels(entries[row["external_lead_id"]]) == expected, label
            assert json.loads(entries[row["external_lead_id"]].usable_channels) == row["usable_channels"]
        ids = {label: entries[row["external_lead_id"]].id for row, (label, _, _) in zip(rows, cases)}

    wid, vid, actor = (library[key + venture] for key in ("wid", "vid", "actor"))
    order = ["inbox_linkedin", "personal", "linkedin", "generic", "shared", "both", "missing_role"]
    matched = match(vid, icp=icp, limit=50)
    assert [item["id"] for item in matched["items"]] == [ids[label] for label in order]
    assert matched["total"] == len(order)
    assert [item["id"] for item in match(vid, icp=icp, limit=2, offset=2)["items"]] == [ids["linkedin"], ids["generic"]]
    text, actions = customers._library_section(vid, icp, offset=3)
    assert "Reach by company inbox email (lower priority)." in text
    assert actions[0]["payload"]["entry_ids"] == [ids[label] for label in order[3:]]
    result = claim(wid, vid, list(ids.values()), actor)
    assert (result["claimed"], result["unavailable"]) == (len(order), len(cases) - len(order))
    assert claim(wid, vid, [ids[label] for label in order], actor)["already"] == len(order)
    with library["factory"]() as session:
        contacts = {json.loads(contact.enrichment_json)["lead_library"]["entry_id"]: contact
            for contact in session.scalars(select(GtmContact).where(GtmContact.id.in_(result["contact_ids"])))}
        assert contacts[ids["personal"]].email_confidence == "risky"
        assert contacts[ids["linkedin"]].email is None
        for label in ("generic", "shared", "both", "missing_role", "inbox_linkedin"):
            contact = contacts[ids[label]]
            evidence = json.loads(contact.enrichment_json)
            assert contact.email == next(row["email"] for row, case in zip(rows, cases) if case[0] == label)
            assert contact.email_confidence == "guess"
            assert evidence["lead_library"]["email_tier"] == "company_inbox"
            assert evidence["email_evidence"] == "company_inbox"
            assert contact.contact_provenance == "lead_library" and contact.lawful_basis_at is None
            assert journey._block(session, wid, contact, icp=icp) == "email_evidence_required"
            contact.recipient_geo = "GB"
            assert consent_block(contact) == "gtm_recipient_consent_required"
            body = complete_body(session, wid, contact, "Hi, a quick question.", "You, the founder")
            assert source_line(contact) in body and OPT_OUT in body
            assert source_line(contact) == "Your email address is listed publicly on directory.example.test."


@pytest.mark.parametrize("audience,title,industry,country,region,city,wrong_region,wrong_city,decoy", [
    ("warehouse supervisors in texas", "Warehouse Supervisor", "logistics_supply_chain", "US", "Texas", "Austin", "CA", "Sacramento", "Chief Operations Officer"),
    ("yoga instructors in london", "Yoga Instructor", "healthtech", "GB", None, "London", None, "Manchester", "Chief Medical Officer"),
])
@pytest.mark.hosted_only
def test_library_requires_close_titles_and_stated_locality_without_executive_padding(
        library, audience, title, industry, country, region, city, wrong_region, wrong_city, decoy):
    from app.chat import customers
    from app.lead_library import taxonomy
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import match

    icp = {"who": audience, "location": country}
    rows = []
    for index, (role, saved_region, saved_city) in enumerate([
        (title, region, city), (f"Senior {title}", region, city), (decoy, region, city),
        (title.split()[0] + " CEO", region, city), ("Instructor" if country == "GB" else "Supervisor", region, city),
        (title, wrong_region, wrong_city), (title, None, None), (None, region, city),
    ]):
        row = json.loads(json.dumps(_rows()[0]))
        row.update(external_lead_id=f"local_role_{country}_{index}", person_name=f"Library person {index}", role_title=role,
                   email=None, usable_channels=["linkedin"], country_code=country, region=saved_region, city=saved_city,
                   linkedin_url=f"https://www.linkedin.com/in/local-role-{country}-{index}",
                   geography_raw=None, function=taxonomy.function_for(role or ""), seniority=taxonomy.seniority_for(role or ""),
                   fit_score=100 if index > 1 else 1,
                   tags={"industry": [industry], "buyer_tag": [], "category_raw": [], "niche_raw": []})
        rows.append(row)
    seed(library["factory"], rows)
    result = match(library["vid"], icp=icp, limit=1)
    assert result["total"] == 2
    assert _ext(result["items"]) == [rows[0]["person_name"]]
    assert _ext(match(library["vid"], icp=icp | {"industry": industry})["items"]) == [row["person_name"] for row in rows[:2]]
    assert _ext(match(library["vid"], icp=icp, limit=1, offset=1)["items"]) == [rows[1]["person_name"]]
    text, actions = customers._library_section(library["vid"], icp)
    assert "Top matches" in text and decoy not in text
    assert len(actions[0]["payload"]["entry_ids"]) == 2
    with library["factory"]() as session:
        for row in rows[:2]:
            entry = session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.external_lead_id == row["external_lead_id"]))
            entry.status = "retired"
        session.commit()
    assert match(library["vid"], icp=icp)["total"] == 0
    text, actions = customers._library_section(library["vid"], icp)
    assert f"No close matches in the library for {audience}" in text
    assert "You can search the web for matching people." in text
    assert "Top matches" not in text and "Nothing is sent" in text
    assert [action["action"] for action in actions] == ["chat.reply"]


@pytest.mark.hosted_only
def test_consumer_or_unknown_icp_matches_nothing(library):
    from app.lead_library.service import match
    assert match(library["vid"], icp={"who": "", "problem": "", "offer": ""})["items"] == []


def _audience_row(external, title, industry, **changes):
    from app.lead_library import taxonomy
    row = json.loads(json.dumps(_rows()[0]))
    domain = f"{external.replace('_', '-')}.example.test"
    row.update(external_lead_id=external, person_name=f"Avery Morgan {external}", role_title=title,
               company=f"Synthetic {external}", company_domain=domain, website=f"https://{domain}",
               email=f"avery@{domain}", linkedin_url=f"https://www.linkedin.com/in/{external}",
               function=taxonomy.function_for(title), seniority=taxonomy.seniority_for(title), company_stage="unknown",
               country_code="US", region="OH", city="Columbus", geography_raw=None, fit_score=75,
               tags={"industry": [industry], "buyer_tag": [], "category_raw": [], "niche_raw": []}, provenance={})
    row.update(changes)
    return row


@pytest.mark.parametrize("audience,title,abbreviation,industry,decoy,niche", [
    ("CFOs at payment companies", "Chief Financial Officer", "CFO", "fintech", "Chief Investment Officer", "payments"),
    ("CPOs at consumer app companies", "Chief Product Officer", "CPO", "consumer_apps", "Chief Technology Officer", "consumer apps"),
])
@pytest.mark.hosted_only
def test_library_plural_roles_match_full_titles_and_acronyms(library, audience, title, abbreviation, industry, decoy, niche):
    from app.lead_library import taxonomy
    from app.lead_library.service import match
    rows = [_audience_row(f"alias-{industry}-{index}", role, industry, fit_score=score,
                         tags={"industry": [industry], "buyer_tag": [], "category_raw": [niche], "niche_raw": []})
            for index, (role, score) in enumerate([(title, 80), (abbreviation, 70), (decoy, 100)])]
    seed(library["factory"], rows)
    assert taxonomy.function_for(abbreviation + "s") == taxonomy.function_for(title)
    assert taxonomy.seniority_for(abbreviation + "s") == taxonomy.seniority_for(title)
    for who in (audience, audience.replace(abbreviation + "s", title + "s")):
        result = match(library["vid"], icp={"who": who}, limit=10)
        assert _ext(result["items"]) == [row["person_name"] for row in rows[:2]]
        assert result["total"] == 2


@pytest.mark.parametrize("audience,title,industry,niche", [
    ("Operations directors at medium-sized freight companies", "Operations Director", "logistics_supply_chain", "freight"),
    ("Marketing directors at mid-sized apparel companies", "Marketing Director", "d2c_consumer_goods", "apparel"),
])
@pytest.mark.hosted_only
def test_library_size_qualifiers_do_not_become_industry_tags(library, audience, title, industry, niche):
    from app.lead_library.service import match
    rows = [_audience_row(f"size-{industry}-{index}", title, industry,
                         provenance={"company_size": size}, fit_score=70 + index,
                         tags={"industry": [industry], "buyer_tag": [], "category_raw": [niche], "niche_raw": []})
            for index, size in enumerate(["medium", "small", None])]
    seed(library["factory"], rows)
    result = match(library["vid"], icp={"who": audience})
    assert result["filters"]["company_size"] == "medium"
    assert result["filters"]["industry_terms"] == [niche]
    assert _ext(result["items"]) == [rows[0]["person_name"]]


@pytest.mark.parametrize("audience,title,industry,buyer_tag", [
    ("freight coordinators in Ohio", "Freight Coordinator", "logistics_supply_chain", "finance_software"),
    ("yoga instructors in Ohio", "Yoga Instructor", "healthtech", "finance_software"),
])
@pytest.mark.hosted_only
def test_library_buyer_tags_rank_but_do_not_exclude_the_audience(library, audience, title, industry, buyer_tag):
    from app.lead_library.service import match
    rows = [_audience_row(f"needs-{industry}-{index}", title, industry, fit_score=100 - index,
                         tags={"industry": [industry], "buyer_tag": tags, "category_raw": [], "niche_raw": []})
            for index, tags in enumerate([[], [buyer_tag]])]
    seed(library["factory"], rows)
    result = match(library["vid"], icp={"who": audience, "problem": "Accounting records are hard to reconcile"})
    assert result["filters"]["buyer_tag"] == [buyer_tag]
    assert _ext(result["items"]) == [rows[1]["person_name"], rows[0]["person_name"]]
    assert result["total"] == 2
    without_need = match(library["vid"], icp={"who": audience})
    assert _ext(without_need["items"]) == [rows[0]["person_name"], rows[1]["person_name"]]


@pytest.mark.parametrize("audience,title,industry", [
    ("freight coordinators in Ohio", "Freight Coordinator", "logistics_supply_chain"),
    ("yoga instructors in Ohio", "Yoga Instructor", "healthtech"),
])
@pytest.mark.hosted_only
def test_library_deduplicates_before_counting_and_paging_and_keeps_suppression(library, audience, title, industry):
    from app.gtm.email_learning import opaque
    from app.gtm.shared_models import SharedAddressValidity
    from app.lead_library.service import match
    from core.clock import utcnow
    rows = [_audience_row(f"identity-{industry}-{index}", title, industry, fit_score=100 - index)
            for index in range(4)]
    duplicate = rows[0] | {"external_lead_id": f"duplicate-{industry}", "fit_score": 60}
    social_alias = rows[1] | {"external_lead_id": f"social-alias-{industry}", "person_name": "Riley Morgan",
                              "email": None, "usable_channels": ["linkedin"]}
    seed(library["factory"], rows + [duplicate, social_alias])
    with library["factory"]() as session:
        session.add(SharedAddressValidity(address_hash=opaque(rows[3]["email"]), suppressed=True, last_seen=utcnow()))
        session.commit()
    first = match(library["vid"], icp={"who": audience}, limit=2)
    second = match(library["vid"], icp={"who": audience}, limit=2, offset=2)
    assert first["total"] == second["total"] == 3
    assert _ext(first["items"] + second["items"]) == [row["person_name"] for row in rows[:3]]
    assert all(item["channels"] for item in first["items"] + second["items"])


@pytest.mark.parametrize("audience,title,industry,niche,decoy", [
    ("Operations directors at hospitals", "Operations Director", "healthtech", "hospital", "medical clinic"),
    ("Marketing directors at apparel companies", "Marketing Director", "d2c_consumer_goods", "apparel", "beauty"),
])
@pytest.mark.hosted_only
def test_library_keeps_the_requested_specialty_inside_a_broad_industry(library, audience, title, industry, niche, decoy):
    from app.lead_library.service import match
    rows = [_audience_row(f"specialty-{industry}-{index}", title, industry, fit_score=70 + index,
                         tags={"industry": [industry], "buyer_tag": [], "category_raw": [category], "niche_raw": []})
            for index, category in enumerate([niche, decoy])]
    seed(library["factory"], rows)
    result = match(library["vid"], icp={"who": audience})
    assert result["total"] == 1
    assert _ext(result["items"]) == [rows[0]["person_name"]]


@pytest.mark.parametrize("audience,title,industry", [
    ("freight coordinators in Ohio", "Freight Coordinator", "logistics_supply_chain"),
    ("yoga instructors in Ohio", "Yoga Instructor", "healthtech"),
])
@pytest.mark.hosted_only
def test_library_missing_contact_routes_are_not_matched_or_claimed(library, audience, title, industry):
    from app.lead_library.service import claim, match
    rows = [_audience_row(f"route-{industry}-{index}", title, industry, **fields)
            for index, fields in enumerate([
                {}, {"email": None, "usable_channels": ["email"]},
                {"linkedin_url": None, "usable_channels": ["linkedin"]},
            ])]
    seed(library["factory"], rows)
    result = match(library["vid"], icp={"who": audience})
    assert result["total"] == 1
    assert _ext(result["items"]) == [rows[0]["person_name"]]
    ids = ["ll_" + hashlib.sha256(row["external_lead_id"].encode()).hexdigest()[:24] for row in rows[1:]]
    claimed = claim(library["wid"], library["vid"], ids, actor=library["actor"])
    assert claimed["claimed"] == 0 and claimed["unavailable"] == 2


@pytest.mark.parametrize("audience,title,industry", [
    ("freight coordinators in Ohio", "Freight Coordinator", "logistics_supply_chain"),
    ("yoga instructors in Ohio", "Yoga Instructor", "healthtech"),
])
@pytest.mark.hosted_only
def test_library_suppression_follows_shared_routes_across_aliases(library, audience, title, industry):
    from app.gtm.email_learning import opaque
    from app.gtm.shared_models import SharedAddressValidity
    from app.lead_library.service import claim, match
    from core.clock import utcnow
    stopped = _audience_row(f"stopped-{industry}", title, industry)
    alias = stopped | {"external_lead_id": f"stopped-alias-{industry}", "person_name": "Riley Morgan",
                       "email": None, "usable_channels": ["linkedin"]}
    available = _audience_row(f"available-{industry}", title, industry)
    seed(library["factory"], [stopped, alias, available])
    with library["factory"]() as session:
        session.add(SharedAddressValidity(address_hash=opaque(stopped["email"]), suppressed=True, last_seen=utcnow()))
        session.commit()
    result = match(library["vid"], icp={"who": audience})
    assert result["total"] == 1
    assert _ext(result["items"]) == [available["person_name"]]
    alias_id = "ll_" + hashlib.sha256(alias["external_lead_id"].encode()).hexdigest()[:24]
    claimed = claim(library["wid"], library["vid"], [alias_id], actor=library["actor"])
    assert claimed["claimed"] == 0 and claimed["unavailable"] == 1


@pytest.mark.parametrize("audience,title,industry", [
    ("freight coordinators in Ohio", "Freight Coordinator", "logistics_supply_chain"),
    ("yoga instructors in Ohio", "Yoga Instructor", "healthtech"),
])
@pytest.mark.hosted_only
def test_library_inbox_without_a_profile_stays_in_the_inbox_tier(library, audience, title, industry):
    from app.lead_library.service import match
    personal = _audience_row(f"personal-tier-{industry}", title, industry, fit_score=50)
    inbox = _audience_row(f"inbox-tier-{industry}", title, industry, fit_score=100,
                          email=f"contact@inbox-tier-{industry.replace('_', '-')}.example.test", quality_flags=["email_generic"],
                          usable_channels=["linkedin"], linkedin_url=None, provenance={"email_published": True})
    seed(library["factory"], [personal, inbox])
    result = match(library["vid"], icp={"who": audience})
    assert _ext(result["items"]) == [personal["person_name"], inbox["person_name"]]
    assert result["items"][1]["channels"] == ["company inbox email"]


@pytest.mark.hosted_only
def test_library_reasons_use_short_taxonomy_labels_in_unrelated_domains(library):
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import _item

    cases = [
        (1, "engineering", ["ai_ml", "ai_tools", "devtools", "devtools"], "engineering; AI/ML, AI tools, developer tools"),
        (9, "healthcare", ["healthtech", "eldercare_products", "healthcare_it"], "healthcare; eldercare products, healthcare IT, healthtech"),
    ]
    with library["factory"]() as session:
        for number, function, tags, expected in cases:
            entry = session.get(LeadLibraryEntry, entry_id(number))
            entry.function, entry.email_status = function, "public"
            item = _item(entry, tags, {"function": function, "country": None})
            assert item["why"] == expected
            assert len(item["why"]) < 100 and "_" not in item["why"]


@pytest.mark.hosted_only
def test_library_claim_reply_reports_real_drafts_and_social_channels(library, monkeypatch):
    import asyncio
    from app.autopilot import outreach
    from app.chat import customers
    from app.chat.execute import execute_action
    from app.db.models import GtmContact
    from app.lead_library.models import LeadLibraryEntry

    cases = [(library["wid"], library["vid"], library["actor"], DEVTOOLS, (1, 2, 3, 4)),
             (library["wid2"], library["vid2"], library["actor2"], ELDERCARE, (9, 10, 11, 12))]
    for wid, vid, actor, icp, numbers in cases:
        social_number, email_number, pending_number, x_number = numbers
        monkeypatch.setattr(customers, "_view", lambda _vid: {"icp": icp, "playbooks": [{"key": "problem_interview"}]})
        with library["factory"]() as session:
            social = session.get(LeadLibraryEntry, entry_id(social_number))
            social.usable_channels = '["linkedin"]'
            social.linkedin_url = f"https://www.linkedin.com/in/synthetic-{social_number}"
            x_entry = session.get(LeadLibraryEntry, entry_id(x_number))
            x_entry.usable_channels, x_entry.x_url = '["x"]', f"https://x.com/synthetic{x_number}"
            for number in (email_number, pending_number):
                entry = session.get(LeadLibraryEntry, entry_id(number))
                entry.usable_channels, entry.email = '["email"]', f"person{number}@example.test"
                entry.email_status = "public"
            session.commit()

        def forbidden(*_args, **_kwargs):
            pytest.fail("A social-only claim must not prepare any email drafts")
        monkeypatch.setattr("app.intake.contacts._prepare_drafts", forbidden)
        social_reply = asyncio.run(customers.execute(vid, "gtm.library_claim", {
            "entry_ids": [entry_id(social_number), entry_id(x_number)]}, actor))
        assert "No first emails were drafted." in social_reply["body_md"]
        assert "LinkedIn" in social_reply["body_md"] and "X" in social_reply["body_md"]
        note_action = next(action for action in social_reply["actions"] if action["label"] == "Write LinkedIn notes for them")
        social_ids = note_action["payload"]["contact_ids"]
        notes = asyncio.run(execute_action(vid, note_action["action"], payload=note_action["payload"], actor_subject=actor))
        assert "LinkedIn connection note:" in notes["body_md"] and "X message:" in notes["body_md"]
        assert "Nothing was sent." in notes["body_md"] and "```" in notes["body_md"]
        with library["factory"]() as session:
            session.get(LeadLibraryEntry, entry_id(x_number)).status = "suppressed"
            session.commit()
        globally_stopped = asyncio.run(customers.execute(vid, note_action["action"], note_action["payload"], actor))
        assert "LinkedIn connection note:" in globally_stopped["body_md"] and "X message:" not in globally_stopped["body_md"]
        with library["factory"]() as session:
            for cid in social_ids:
                contact = session.get(GtmContact, cid)
                assert contact.email is None
                contact.do_not_contact = True
            session.commit()
        stopped = asyncio.run(customers.execute(vid, note_action["action"], note_action["payload"], actor))
        assert "No selected people are available" in stopped["body_md"] and "```" not in stopped["body_md"]
        foreign = asyncio.run(customers.execute(vid, note_action["action"], {"contact_ids": ["foreign_contact"]}, actor))
        assert "```" not in foreign["body_md"]

        def prepare_one_draft(_wid, _vid):
            with library["factory"]() as session:
                contact = session.scalar(select(GtmContact).where(GtmContact.source == f"venture:{_vid}:lead_library:synthetic_{email_number:03d}"))
                pending = session.scalar(select(GtmContact).where(GtmContact.source == f"venture:{_vid}:lead_library:synthetic_{pending_number:03d}"))
                outreach._save_draft(_vid, {"contact_id": contact.id, "to": contact.email, "channel": "email", "day": 1,
                    "state": "draft", "subject": "A research question", "body": "How do you handle this today?"})
                outreach._save_draft(_vid, {"contact_id": pending.id, "to": pending.email, "channel": "linkedin", "day": 1,
                    "state": "draft", "body": "A social note, not an email"})
            return "ready"
        monkeypatch.setattr("app.intake.contacts._prepare_drafts", prepare_one_draft)
        email_reply = asyncio.run(customers.execute(vid, "gtm.library_claim", {
            "entry_ids": [entry_id(email_number), entry_id(pending_number)]}, actor))
        assert "1 first email drafted for your review." in email_reply["body_md"]
        assert "1 person has an email address but no first email draft yet." in email_reply["body_md"]
        assert not any(action["action"] == "gtm.library_notes" for action in email_reply["actions"])
        repeated = asyncio.run(customers.execute(vid, "gtm.library_claim", {
            "entry_ids": [entry_id(email_number), entry_id(pending_number)]}, actor))
        assert "Added 0 people" in repeated["body_md"] and "No first emails were drafted." in repeated["body_md"]


@pytest.mark.usefixtures("published_library")
@pytest.mark.hosted_only
def test_delivered_entries_rank_higher(library):
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import match
    with library["factory"]() as session:
        session.get(LeadLibraryEntry, entry_id(19)).email_status = "delivered"
        session.commit()
    assert _ext(match(library["vid"], icp=DEVTOOLS, limit=3)["items"]) == names(19, 1, 2)


@pytest.mark.usefixtures("published_library")
@pytest.mark.hosted_only
def test_claim_keeps_provenance_never_copies_unusable_channels_and_is_idempotent(library):
    from app.db.models import GtmContact
    from app.lead_library.models import LeadLibraryEntry, LeadLibraryUsage
    from app.lead_library.service import claim, match
    with library["factory"]() as session:
        session.get(LeadLibraryEntry, entry_id(2)).usable_channels = json.dumps(["linkedin"])
        session.commit()
    first = claim(library["wid"], library["vid"], [entry_id(1), entry_id(2), entry_id(7)], library["actor"])
    assert (first["claimed"], first["already"], first["unavailable"]) == (3, 0, 0)
    again = claim(library["wid"], library["vid"], [entry_id(1), entry_id(2)], library["actor"])
    assert (again["claimed"], again["already"]) == (0, 2)
    with library["factory"]() as session:
        contacts = {c.source: c for c in session.scalars(select(GtmContact).where(GtmContact.workspace_id == library["wid"],
            GtmContact.source.like(f"venture:{library['vid']}:lead_library:%")))}
        assert set(contacts) == {f"venture:{library['vid']}:lead_library:synthetic_{number:03d}" for number in (1, 2, 7)}
        avery = contacts[f"venture:{library['vid']}:lead_library:synthetic_001"]
        assert avery.email == "avery@loopforge.test" and avery.contact_provenance == "lead_library" and avery.recipient_geo == "US"
        assert avery.lawful_basis == "legitimate_interest" and avery.lawful_basis_at is None
        assert "SupyMem_GTM_Leads_Final.xlsx (purchased, founder-licensed)" in avery.lawful_basis_note
        evidence = json.loads(avery.enrichment_json)
        assert evidence["found_at"] == "https://directory.example.test/loopforge-ai"
        assert evidence["lead_library"]["external_lead_id"] == "synthetic_001" and evidence["lead_library"]["license_source"].startswith("SupyMem")
        bram = contacts[f"venture:{library['vid']}:lead_library:synthetic_002"]
        assert bram.email is None and bram.linkedin_url and not bram.x_handle
        assert session.scalar(select(LeadLibraryUsage.outcome).where(LeadLibraryUsage.entry_id == entry_id(1))) == "claimed"
    assert names(1, 2)[0] not in _ext(match(library["vid"], icp=DEVTOOLS, limit=10)["items"])


@pytest.mark.usefixtures("published_library")
@pytest.mark.hosted_only
def test_cross_workspace_cap_hides_from_match_and_blocks_send(library):
    from app.db.models import GtmContact
    from app.gtm.relationships.suppression import email_suppressed
    from app.lead_library.models import LeadLibraryUsage
    from app.lead_library.service import claim, match
    from core.clock import utcnow
    claim(library["wid"], library["vid"], [entry_id(1)], library["actor"])
    now = utcnow()
    with library["factory"]() as session:
        session.add(LeadLibraryUsage(id="llu_old", entry_id=entry_id(19), workspace_id="ws_old", venture_id=None, contacted_at=now - timedelta(days=31), outcome="sent"))
        for n in range(3):
            for entry in (1, 19):
                session.add(LeadLibraryUsage(id=f"llu_{entry}_{n}", entry_id=entry_id(entry), workspace_id=f"ws_other_{n}", venture_id=None,
                    contacted_at=now - timedelta(days=n + 1 if entry == 1 else n + 27), outcome="sent"))
        session.commit()
    assert names(19)[0] not in _ext(match(library["vid"], icp=DEVTOOLS, limit=10)["items"])
    assert claim(library["wid"], library["vid"], [entry_id(19)], library["actor"])["unavailable"] == 1
    with library["factory"]() as session:
        contact = session.scalar(select(GtmContact).where(GtmContact.source.like("%:lead_library:synthetic_001")))
        assert email_suppressed(session, library["wid"], contact)
        session.add(LeadLibraryUsage(id="llu_mine", entry_id=entry_id(1), workspace_id=library["wid"], venture_id=library["vid"], contacted_at=now, outcome="sent"))
        session.commit()
        assert not email_suppressed(session, library["wid"], contact)  # a follow-up in a thread this workspace started still goes


@pytest.mark.hosted_only
def test_opt_out_from_any_workspace_hides_at_once(library):
    from app.gtm.email_learning import suppress
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import match
    with library["factory"]() as session:
        suppress(session, "Avery@loopforge.test")
        session.commit()
        assert session.get(LeadLibraryEntry, entry_id(1)).status == "suppressed"
    assert names(1)[0] not in _ext(match(library["vid"], icp=DEVTOOLS, limit=10)["items"])


@pytest.mark.usefixtures("published_library")
@pytest.mark.hosted_only
def test_single_founder_bounce_blacklists_both_industries_globally(library):
    from app.db.models import GtmContact
    from app.gtm.email_learning import record
    from app.gtm.shared_models import SharedAddressValidity
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import claim, match, sweep
    claim(library["wid2"], library["vid2"], [entry_id(1), entry_id(20)], library["actor2"])
    with library["factory"]() as session:
        for source in ("%:lead_library:synthetic_001", "%:lead_library:synthetic_020"):
            record(session, session.scalar(select(GtmContact).where(GtmContact.source.like(source))), "hard_bounce", "bounce:" + source)
        session.commit()
    sweep()
    with library["factory"]() as session:
        for number in (1, 20):
            entry = session.get(LeadLibraryEntry, entry_id(number))
            assert (entry.status, entry.email_status, entry.status_reason) == ("retired", "bounced", "email_bounced")
            validity = session.get(SharedAddressValidity, entry.email_hash)
            assert validity.retired_at is not None and validity.status == "retired"
    items = {item["name"]: item for item in match(library["vid"], icp=DEVTOOLS, limit=10)["items"]}
    assert names(1)[0] not in items
    eldercare_us = {"who": "Operations managers at eldercare companies", "problem": "medication schedules", "offer": "eldercare reminders", "location": "US"}
    assert names(20)[0] not in _ext(match(library["vid"], icp=eldercare_us, limit=10)["items"])


@pytest.mark.hosted_only
def test_retired_address_is_hidden_and_swept(library):
    from app.gtm.shared_models import SharedAddressValidity
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import match, sweep
    from core.clock import utcnow
    with library["factory"]() as session:
        address = session.get(LeadLibraryEntry, entry_id(2)).email_hash
        session.add(SharedAddressValidity(address_hash=address, status="retired", suppressed=False, retired_at=utcnow(), last_seen=utcnow()))
        session.commit()
    assert names(2)[0] not in _ext(match(library["vid"], icp=DEVTOOLS, limit=10)["items"])
    assert sweep()["retired"] == 1
    with library["factory"]() as session:
        assert session.get(LeadLibraryEntry, entry_id(2)).status == "retired"


def _web_contact(session, d, *, cid, email, provenance=None, source="public", evidence=None):
    from app.db.models import GtmCompany, GtmContact
    from core.clock import utcnow
    now = utcnow()
    session.add(GtmCompany(id="co_" + cid, workspace_id=d["wid"], canonical_name="Acme Ledger", domain="acmeledger.test", created_at=now))
    session.flush()
    contact = GtmContact(id=cid, workspace_id=d["wid"], company_id="co_" + cid, email=email, first_name="Jane", last_name="Patel", role="Head of Finance",
        email_confidence="risky", recipient_geo="US", lawful_basis="legitimate_interest", source=f"venture:{d['vid']}:{source}",
        contact_provenance=provenance, linkedin_url="https://www.linkedin.com/in/jane-patel-acme",
        enrichment_json=json.dumps({"found_at": "https://acmeledger.test/team", "website": "https://acmeledger.test", "person_name": "Jane Patel",
            "business": "Acme Ledger", **(evidence or {})}), stage_changed_at=now, created_at=now)
    session.add(contact)
    session.flush()
    return contact


@pytest.mark.hosted_only
def test_delivered_guess_is_promoted_without_its_raw_address(library):
    from app.gtm.email_learning import opaque, record
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import sweep
    from core.clock import utcnow
    sent = utcnow() - timedelta(days=4)
    with library["factory"]() as session:
        contact = _web_contact(session, library, cid="ct_guess", email="jane.patel@acmeledger.test",
            evidence={"email_route": {"route_type": "guess", "pattern": "first.last", "email": "jane.patel@acmeledger.test"}})
        contact.times_emailed, contact.last_emailed_at = 1, sent
        record(session, contact, "send", "msg_guess", observed_at=sent)
        session.commit()
    assert sweep()["promoted"] == 1
    with library["factory"]() as session:
        entry = session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.email_hash == opaque("jane.patel@acmeledger.test")))
        assert entry.license_source == "supymem_verified" and entry.email_status == "delivered"
        assert entry.email is None and json.loads(entry.usable_channels) == ["linkedin"]
        assert (entry.person_name, entry.company_domain, entry.source_url) == ("Jane Patel", "acmeledger.test", "https://acmeledger.test/team")


@pytest.mark.hosted_only
def test_reply_on_published_address_is_promoted(library):
    from app.gtm.email_learning import record
    from app.lead_library.models import LeadLibraryEntry
    with library["factory"]() as session:
        contact = _web_contact(session, library, cid="ct_reply", email="jane@acmeledger.test", evidence={"email_evidence": "published"})
        record(session, contact, "reply", "touch_reply")
        session.commit()
        entry = session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.license_source == "supymem_verified"))
        assert entry.email == "jane@acmeledger.test" and entry.email_status == "replied" and "email" in json.loads(entry.usable_channels)


@pytest.mark.hosted_only
def test_private_import_is_never_promoted(library):
    from app.gtm.email_learning import record
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import sweep
    from core.clock import utcnow
    sent = utcnow() - timedelta(days=4)
    with library["factory"]() as session:
        contact = _web_contact(session, library, cid="ct_private", email="jane@acmeledger.test", provenance="founder_import", source="founder_import",
            evidence={"email_evidence": "published"})
        contact.times_emailed, contact.last_emailed_at = 1, sent
        record(session, contact, "send", "msg_private", observed_at=sent)
        record(session, contact, "reply", "touch_private")
        session.commit()
    sweep()
    with library["factory"]() as session:
        assert session.scalar(select(LeadLibraryEntry).where(LeadLibraryEntry.license_source == "supymem_verified")) is None


@pytest.mark.hosted_only
def test_library_contacts_state_source_honestly_and_eu_uk_need_permission(library):
    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import consent_block, source_line
    from app.lead_library.models import LeadLibraryEntry
    from app.lead_library.service import claim
    from core.clock import utcnow
    with library["factory"]() as session:
        session.get(LeadLibraryEntry, entry_id(2)).email_status = "public"
        session.commit()
    claim(library["wid"], library["vid"], [entry_id(1), entry_id(2), entry_id(6)], library["actor"])
    with library["factory"]() as session:
        get = lambda n: session.scalar(select(GtmContact).where(GtmContact.source.like(f"%:lead_library:synthetic_{n:03d}")))
        assert source_line(get(1)) == "Your work email is listed in a business contact directory."
        assert source_line(get(2)) == "Your email address is listed publicly on directory.example.test."
        assert consent_block(get(1)) is None
        fenna = get(6)
        assert consent_block(fenna) == "gtm_recipient_consent_required"
        fenna.lawful_basis_at, fenna.lawful_basis_note = utcnow(), "Fenna asked us to follow up at a meetup on 3 Oct."
        assert consent_block(fenna) is None


@pytest.mark.usefixtures("published_library")
@pytest.mark.hosted_only
def test_hook_failure_never_breaks_the_send_record(library, monkeypatch):
    from app.db.models import GtmContact
    from app.gtm.email_learning import record
    from app.gtm.shared_models import SharedEmailOutcome
    from app.lead_library import service
    from app.lead_library.service import claim
    claim(library["wid"], library["vid"], [entry_id(1)], library["actor"])
    monkeypatch.setattr(service, "_entry_for", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with library["factory"]() as session:
        contact = session.scalar(select(GtmContact).where(GtmContact.source.like("%:lead_library:synthetic_001")))
        record(session, contact, "send", "msg_hook")
        session.commit()
        assert session.scalar(select(SharedEmailOutcome.kind).where(SharedEmailOutcome.kind == "send")) == "send"
