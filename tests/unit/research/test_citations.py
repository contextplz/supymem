import pytest

from tests.company_os_harness import load_company_os_app


def test_short_labels_map_back_to_exact_persisted_ids_without_changing_prose():
    load_company_os_app()
    from app.research.citations import project, restore
    sources, mapping = project([{"id": "rsf_very_long_sensitive_looking_id", "summary": "A source"}])
    assert sources == [{"id": "source_1", "summary": "A source"}]
    result = restore({"source_id": "source_1", "assessments": [{"source_ids": ["source_1"], "assessment": "source_1 is a label"}]}, mapping)
    assert result["source_id"] == "rsf_very_long_sensitive_looking_id"
    assert result["assessments"][0]["assessment"] == "source_1 is a label"
    with pytest.raises(KeyError):
        restore({"source_id": "source_2"}, mapping)
    with pytest.raises(ValueError):
        project([{"id": "same"}, {"id": "same"}])
