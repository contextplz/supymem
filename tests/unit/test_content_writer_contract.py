"""The metered writer must request only assets publication can accept."""
import json

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def writer_input(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.content import generation, generation_schema
    from app.research import metered

    snapshot = {"brief": {"product": {"name": "Booking concept"}, "claims": []},
                "topic": {"angle": "Review requests", "icp_segment": "Studio owner"},
                "platform": "linkedin"}
    return generation, generation_schema, metered, snapshot


@pytest.mark.parametrize("feedback", [[], ["Make the situation more specific."]])
def test_metered_writer_transmits_text_only_contract(writer_input, monkeypatch, feedback):
    generation, schema, metered, snapshot = writer_input
    calls = []
    monkeypatch.setattr(generation, "mutate", lambda *args: None)

    def capture(run_id, payload, **kwargs):
        calls.append(kwargs)
        data = json.loads(kwargs["messages"][1]["content"])
        assert kwargs["schema"] == {"type": "json_object"}
        assert data["schema"]["properties"]["visual"]["type"] == "null"
        assert "$defs" not in data["schema"]
        assert '"visual": null' in data["task"]
        assert "Only text publication is supported" in data["task"]
        assert "insight_card" not in json.dumps(data)
        assert data["fix_these"] == feedback
        return kwargs["parse"]('{"body":"Review booking requests before confirming.","title":"","visual":null}')

    monkeypatch.setattr(metered, "synthesize", capture)
    result = generation._call("run-fixture", {}, "writer", "writer.1", snapshot, feedback=feedback)
    assert len(calls) == 1
    assert result["visual"] is None
    assert result["body"] == "Review booking requests before confirming."


@pytest.mark.parametrize("headline", ["Invented visual claim", "Review booking requests"])
def test_visual_is_rejected_even_if_it_quotes_the_body(writer_input, headline):
    _, schema, _, snapshot = writer_input
    output = {"body": "Review booking requests before confirming.", "title": "",
              "visual": {"template": "insight_card", "headline": headline, "subheadline": "", "points": []}}
    with pytest.raises(ValueError, match="content_visual_unsupported"):
        schema.parse("writer", json.dumps(output), snapshot)
    assert output["visual"]["headline"] == headline


def test_omitted_visual_remains_text_only_and_unknown_fields_fail(writer_input):
    _, schema, _, snapshot = writer_input
    assert schema.parse("writer", '{"body":"Review requests."}', snapshot)["visual"] is None
    with pytest.raises(ValueError):
        schema.parse("writer", '{"body":"Review requests.","image_url":"https://example.test/image.png"}', snapshot)


def test_overlong_writer_title_is_clipped_not_rejected(writer_input):
    _, schema, _, snapshot = writer_input
    value = schema.parse("writer", json.dumps({"body": "Review requests.", "title": "T " * 300}), snapshot)
    assert len(value["title"]) <= 200
