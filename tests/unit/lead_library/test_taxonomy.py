"""Deterministic cleaning, flags, dedupe and taxonomy mapping on synthetic rows (no database)."""
from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.lead_library import clean, taxonomy as tx  # noqa: E402


def raw(lead_id, name, *, company="Loopforge AI", website="https://loopforge.test", email=None, role="VP Engineering", **extra):
    return {"lead_id": lead_id, "person_name": name, "company": company, "website": website, "email": email, "role_title": role,
            "linkedin_url": extra.pop("linkedin", None), "geography": extra.pop("geography", "San Francisco, CA, US"), "batch_id": "b1",
            "fit_score": extra.pop("fit", "80"), "source_url": "https://directory.example.test/x", "lead_type": extra.pop("lead_type", "customer_prospect"),
            "categories": extra.pop("categories", "devtools|ai_agents"), **extra}


def by_id(kept):
    return {lead["lead_id"]: lead for lead in kept}


@pytest.mark.parametrize("value,expected", [
    ("San Francisco, CA, US", {"country_code": "US", "region": "CA", "city": "San Francisco"}),
    ("Bengaluru, Karnataka, India", {"country_code": "IN", "region": "Karnataka", "city": "Bengaluru"}),
    ("London, United Kingdom", {"country_code": "GB", "region": None, "city": "London"}),
    ("Remote", {"country_code": None, "region": None, "city": None}),
])
def test_geography(value, expected):
    assert {key: tx.parse_geography(value)[key] for key in expected} == expected


def test_role_function_and_seniority_mappings():
    assert (tx.function_for("VP Engineering"), tx.seniority_for("VP Engineering")) == ("engineering", "vp")
    assert (tx.function_for("Co-founder & CEO"), tx.seniority_for("Co-founder & CEO")) == ("founder_exec", "founder_owner")
    assert tx.function_for("Chief Information Officer") == "it" and tx.seniority_for("Chief Information Officer") == "c_level"
    assert tx.seniority_for("Head of Growth Marketing") in {"director", "vp"} and tx.function_for("Head of Growth Marketing") == "marketing"


@pytest.mark.parametrize("text,expected", [
    ("Accounting invoices for wholesale freight", "finance_software"),
    ("Medical invoices for a family wellness clinic", "healthcare_it"),
])
def test_buyer_tags_do_not_read_audio_needs_inside_other_words(text, expected):
    tags = tx.buyer_tags_for(text)
    assert expected in tags
    assert "voice_ai" not in tags
    assert "voice_ai" in tx.buyer_tags_for("Voice recognition and call transcription")


def test_industry_mapping_fixes():
    assert tx.industries_for(tx.tokens("enterprise CXO|ai_governance")) and "government_public_sector" not in tx.industries_for(tx.tokens("ai_governance"))
    assert "biotech" not in tx.industries_for(tx.tokens("Bio: builder of tools"))
    assert tx.industries_for(tx.tokens("developer tools"))[0] == "devtools"
    assert "mobility" not in tx.industries_for(tx.tokens("b2b saas"), "score: 8 | auto: verifiable")


def test_ai_governance_cxo_is_not_government():
    lead_type, _ = tx.lead_type_for("customer_prospect", "customer_prospect", "Chief Information Officer", "enterprise CXO | ai_governance | industry_press")
    assert lead_type == "customer_prospect"
    lead_type, _ = tx.lead_type_for("ecosystem_partner", None, "Deputy Director", "government | district_office")
    assert lead_type == "government"


def test_stage_and_industry_fixes_from_qa():
    assert tx.stage_for("enterprise cxo | ai_governance | cio") == "enterprise"
    assert tx.stage_for("fintech | echai_public_sector_directory | Publicly listed YC W25 founder") == "startup"
    assert tx.stage_for("government_public_sector | msme_di") == "public_sector"
    assert tx.stage_for("listed company on NSE") == "enterprise"
    assert tx.stage_for("Late Stage | accel portfolio") == "scaleup"
    assert tx.stage_for("YC S19 startup, $573M in funding") == "scaleup"
    assert tx.function_for("Head of Biz Dev") == "sales" and tx.function_for("Founding CTO") == "founder_exec"
    noisy = "Developer of a podcast studio. Listed in public accelerator portfolio. Stanford University alum."
    assert tx.industries_for(tx.tokens("b2b saas"), noisy) == ["media_content"]
    assert "edtech" in tx.industries_for(tx.tokens("b2b saas"), "software for schools")
    assert "space_aerospace" not in tx.industries_for(tx.tokens("workspace|space-as-a-service"))
    assert tx.industries_for(tx.tokens("satellite")) == ["space_aerospace"]
    assert tx.stage_for("Techstars circuit-board printer | dashboard | Board Member") == "startup"
    assert tx.stage_for("Coir Board | export promotion") == "public_sector"
    assert set(tx.industries_for(tx.tokens("artificial intelligence"), "invoice financing for small businesses")) >= {"ai_ml", "fintech"}
    assert (tx.function_for("Vice President, Engineering"), tx.seniority_for("Vice President, Engineering")) == ("engineering", "vp")
    assert tx.function_for("President") == "founder_exec"
    assert tx.industries_for(tx.tokens("managing partner|experimental")) == []
    founder = {"lead_type_raw": "founder", "role_title": "Founder", "category": "incubator portfolio|insurtech", "company": "Covra"}
    assert tx.baseline(founder)["industry"] == ["insurtech"]


def test_quality_flags_and_usable_channels():
    kept, dropped = clean.prepare([
        raw("a1", "Avery Quill", email="avery@loopforge.test", linkedin="https://www.linkedin.com/in/avery-q"),
        raw("a2", "Bram Ostler", company="Stackwell", website="https://stackwell.test", email="info@stackwell.test"),
        raw("a3", "Cleo Marsh", company="Quayline", website="https://quayline.test", email="cleo@gmail.com"),
        raw("a4", "Dana Pike", company="Gridpine", website="https://gridpine.test", email="dana@othercorp.test"),
        raw("a5", "Eli Stone", company="Tidewell", website="https://tidewell.test", email="john.doe@tidewell.test"),
        raw("a6", "Faye Lund", company="Northbeam", website="https://northbeam.test", linkedin="https://www.linkedin.com/company/northbeam", geography="Remote"),
    ])
    leads = by_id(kept)
    assert not dropped
    assert leads["a1"]["usable_channels"] == ["email", "linkedin"] and leads["a1"]["quality_flags"] == []
    assert leads["a1"]["company_domain"] == "loopforge.test" and leads["a1"]["country_code"] == "US"
    assert "email_generic" in leads["a2"]["quality_flags"] and "email" not in leads["a2"]["usable_channels"]
    assert "email_free" in leads["a3"]["quality_flags"] and "email" in leads["a3"]["usable_channels"]
    assert "email_domain_mismatch" in leads["a4"]["quality_flags"] and "email" not in leads["a4"]["usable_channels"]
    assert "email_placeholder" in leads["a5"]["quality_flags"] and "email" not in leads["a5"]["usable_channels"]
    assert {"linkedin_is_company", "contact_company_level", "geography_unknown"} <= set(leads["a6"]["quality_flags"])
    assert leads["a6"]["usable_channels"] == []
    assert all(lead["license_source"] == tx.LICENSE_SOURCE for lead in kept)


def test_non_people_dropped_and_every_row_accounted_for():
    rows = [raw("n1", "+ 2 More"), raw("n2", "OpenCFO Desk"), raw("n3", "Team"), raw("n4", ""), raw("n5", "Loopforge AI", company="Loopforge AI")]
    kept, dropped = clean.prepare(rows)
    assert len(kept) + len(dropped) == len(rows)
    assert {row["lead_id"]: row["reason"] for row in dropped} == {"n1": "name_not_a_person", "n2": "name_not_a_person", "n3": "name_not_a_person",
                                                                  "n4": "missing_person_name"}
    assert kept[0]["name_is_company"] and "name" in tx.baseline(kept[0])["review"]


def test_dedupe_merges_on_email_and_person_company_keeping_the_richest():
    rows = [raw("d1", "Avery Quill", email="avery@loopforge.test"),
            raw("d2", "Avery Quill", email="avery@loopforge.test", linkedin="https://www.linkedin.com/in/avery-q"),
            raw("d3", "avery  quill", company="Loopforge AI"),
            raw("d4", "Bram Ostler", company="Stackwell", website="https://stackwell.test")]
    kept, dropped = clean.prepare(rows)
    assert sorted(lead["lead_id"] for lead in kept) == ["d2", "d4"]
    assert by_id(kept)["d2"]["merged_lead_ids"] == ["d1", "d3"]
    assert {row["lead_id"] for row in dropped if row["reason"] == "duplicate"} == {"d1", "d3"}


@pytest.mark.parametrize("company", ["Harbor 12 Logistics", "Meadow 12 Salon"])
def test_person_company_dedupe_preserves_numeric_company_identity(company):
    rows = [raw("numbered-1", "Avery Morgan", company=company),
            raw("numbered-2", "Avery Morgan", company=company.replace("12", "24")),
            raw("numbered-3", "Avery Morgan", company=company)]
    kept, dropped = clean.prepare(rows)
    assert {lead["company"] for lead in kept} == {company, company.replace("12", "24")}
    assert len(kept) == 2
    assert sum(row["reason"] == "duplicate" for row in dropped) == 1


def test_fit_score_scale_per_batch():
    kept, _ = clean.prepare([raw("f1", "Avery Quill", fit="7"), raw("f2", "Bram Ostler", company="Stackwell", website="https://stackwell.test", fit="9")])
    assert {lead["fit_score"] for lead in kept} == {70, 90}


def test_baseline_values_are_in_the_taxonomy():
    kept, _ = clean.prepare([raw("t1", "Avery Quill", categories="devtools|ai_agents|ci_cd")])
    base = tx.baseline(kept[0])
    assert base["lead_type"] in tx.LEAD_TYPES and base["function"] in tx.FUNCTIONS and base["seniority"] in tx.SENIORITIES
    assert set(base["industry"]) <= set(tx.INDUSTRIES) and set(base["buyer_tags"]) <= set(tx.BUYER_TAGS) and len(base["buyer_tags"]) >= 3
