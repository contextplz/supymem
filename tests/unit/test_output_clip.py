import logging

import pytest
from pydantic import BaseModel, Field, ValidationError

from tests.company_os_harness import load_company_os_app


class Item(BaseModel):
    name: str = Field(max_length=20)
    n: int


@pytest.fixture(autouse=True)
def output_clip_app():
    load_company_os_app()


@pytest.mark.parametrize("text", ["warehouse stock reports ", "family meal plans "])
def test_clips_only_when_every_error_is_string_too_long(caplog, text):
    from app.llm.output_clip import validate_clipping

    with caplog.at_level(logging.WARNING):
        item = validate_clipping(Item, '{"name": "' + text * 20 + '", "n": 1}')
    assert len(item.name) <= 20 and item.name.endswith("…")
    assert any(r.message == "model_output_clipped" and r.clipped_strings == 1 for r in caplog.records)
    with pytest.raises(ValidationError):
        validate_clipping(Item, '{"name": "' + text * 20 + '", "n": "x"}')
    assert validate_clipping(list[Item], [{"name": "ok", "n": 1}])[0].name == "ok"


def test_lists_of_strings_truncate_but_lists_of_objects_reject():
    from app.llm.output_clip import validate_clipping

    class Box(BaseModel):
        tags: list[str] = Field(max_length=2)
        items: list[Item] = Field(max_length=1)

    assert validate_clipping(Box, {"tags": ["a", "b", "c"], "items": []}).tags == ["a", "b"]
    with pytest.raises(ValidationError):
        validate_clipping(Box, {"tags": [], "items": [{"name": "a", "n": 1}] * 2})


def test_protected_paths_reject_nested_action_and_question_list_without_mutation():
    from copy import deepcopy
    from app.llm.output_clip import validate_clipping

    class Box(BaseModel):
        instructions: list[Item]
        questions: list[str] = Field(max_length=1)
        summary: str = Field(max_length=20)

    raw = {'instructions': [{'name': 'complete instruction ' * 3, 'n': 1}],
           'questions': ['one'], 'summary': 'word ' * 20}
    original = deepcopy(raw)
    with pytest.raises(ValidationError):
        validate_clipping(Box, raw, preserve_paths=(("instructions", "*", "name"),))
    assert raw == original
    with pytest.raises(ValidationError):
        validate_clipping(Box, {'instructions': [], 'questions': ['one', 'two'], 'summary': 'ok'},
                          preserve_paths=(("questions",),))
    assert validate_clipping(Box, {'instructions': [], 'questions': ['one'], 'summary': 'word ' * 20},
                             preserve_paths=(("questions",),)).summary.endswith('…')
