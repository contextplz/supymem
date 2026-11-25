from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.skills.license import can_materialize, decision_for
from app.skills.parse import parse_skill_text


def test_parses_yaml_frontmatter_and_folded_description() -> None:
    skill = parse_skill_text(
        "---\nname: cold-email\ndescription: >-\n  Write an email that gets replies.\nmetadata:\n  frameworks: [AIDA]\n---\n# Steps\n"
    )
    assert skill.name == "cold-email"
    assert "gets replies" in skill.description
    assert skill.metadata["metadata"]["frameworks"] == ["AIDA"]


def test_anthropic_document_skills_are_not_redistributable() -> None:
    pack = {
        "id": "anthropic-skills",
        "license": "MIXED",
        "license_overrides": [
            {"skills": ["docx", "pdf", "pptx", "xlsx"], "license": "source-available", "redistribute": False}
        ],
    }
    assert decision_for(pack, "docx").redistribute is False
    assert can_materialize(pack, "docx", exportable=True) is False
    assert can_materialize(pack, "docx", exportable=False) is True
