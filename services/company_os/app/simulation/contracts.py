"""Bounded, tool-free simulated-reader outputs; no conversion predictions."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt

BADGE = "Simulated · AI personas"
DISCLAIMER = "Not customer evidence. Use it to decide what to test with real people."
VERSION = "rehearse_v1"
MAX_CALLS, MAX_PERSONAS, MAX_VARIANTS, MAX_SECONDS = 4, 8, 3, 150
MAX_OUTPUT_TOKENS, MAX_PROMPT_TOKENS = 4096, 12000
Text = Annotated[str, Field(min_length=1, max_length=250)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DraftRef(StrictModel):
    doc_id: str = Field(min_length=1, max_length=100)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class Persona(StrictModel):
    id: str = Field(pattern=r"^p[1-8]$")
    role: Text
    situation: Text


class Panel(StrictModel):
    personas: list[Persona] = Field(min_length=8, max_length=8)


class Reaction(StrictModel):
    persona_id: str = Field(pattern=r"^p[1-8]$")
    variant_id: str = Field(pattern=r"^v[1-3]$")
    clarity: Annotated[StrictInt, Field(ge=1, le=5)]
    confusing_phrase: str = Field(max_length=250)
    objection: Text


class Batch(StrictModel):
    reactions: list[Reaction] = Field(min_length=4, max_length=12)


class Summary(StrictModel):
    clarity_order: list[str] = Field(min_length=1, max_length=3)
    explanation: Text
    top_objections: list[Text] = Field(min_length=1, max_length=3)
    confusing_phrases: list[Text] = Field(max_length=3)
    suggested_edit: str = Field(min_length=1, max_length=2000)
    interview_questions: list[Text] = Field(min_length=2, max_length=3)


def parse_json(text: str, model):
    if len(text.encode()) > 40000:
        raise ValueError("simulation_output_too_large")
    return model.model_validate_json(text).model_dump()
