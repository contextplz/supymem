"""Replayable, bounded inputs and outputs for the landing-page workflow."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from decimal import Decimal
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_serializer, model_validator
from app.experiments.wizard_tests import RequestForm


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeadlineVariant(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    label: str = Field(min_length=1, max_length=80)
    headline: str = Field(min_length=3, max_length=140)


class PricePoint(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    label: str = Field(min_length=1, max_length=80)
    price: str = Field(min_length=1, max_length=60)

    @field_validator("price")
    @classmethod
    def positive_decimal(cls, value):
        amounts = re.findall(r"[0-9]+(?:\.[0-9]+)?", value)
        if len(amounts) != 1 or Decimal(amounts[0]) <= 0 or re.search(r"-\s*[0-9]", value):
            raise ValueError("page_test_positive_price_required")
        return value


class PageTest(Strict):
    experiment_id: str = Field(pattern=r"^exp_[a-f0-9]{24}$")
    headlines: list[HeadlineVariant] = Field(default_factory=list, max_length=3)
    prices: list[PricePoint] = Field(default_factory=list, max_length=3)
    request: RequestForm | None = None

    @model_serializer(mode="wrap")
    def legacy_wire(self, handler):
        data = handler(self)
        if self.request is None:
            data.pop("request", None)
        return data

    @model_validator(mode="after")
    def distinct_variants(self):
        if self.request:
            if self.headlines or self.prices:
                raise ValueError("page_test_variants_invalid")
            return self
        variants = self.headlines or self.prices
        if (bool(self.headlines) == bool(self.prices) or self.headlines and len(self.headlines) < 2
                or len({item.key for item in variants}) != len(variants)
                or len({item.label.casefold() for item in variants}) != len(variants)
                or self.headlines and len({item.headline.casefold() for item in self.headlines}) != len(self.headlines)):
            raise ValueError("page_test_variants_invalid")
        for item in variants:
            if any(character in item.model_dump_json() for character in ("\u2014", "\u2013")):
                raise ValueError("page_test_copy_dashes")
        if self.prices:
            formats = {re.sub(r"[0-9]+(?:\.[0-9]+)?", "#", item.price).strip().casefold() for item in self.prices}
            amounts = {Decimal(re.search(r"[0-9]+(?:\.[0-9]+)?", item.price)[0]) for item in self.prices}
            if len(formats) != 1 or len(amounts) != len(self.prices):
                raise ValueError("page_test_price_currency_and_period_must_match")
            self.prices.sort(key=lambda item: Decimal(re.search(r"[0-9]+(?:\.[0-9]+)?", item.price)[0]))
        return self


class PageInput(Strict):
    venture_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    name: str = Field(min_length=1, max_length=160)
    idea: str = Field(min_length=10, max_length=16000)
    clarified_answers: dict[str, Any] = Field(default_factory=dict)
    research: dict[str, Any] = Field(default_factory=dict)
    waitlist_url: str
    waitlist_consent_text: str = Field(default="", max_length=1000)
    waitlist_consent_version: str = Field(default="", max_length=80)
    logo_url: str | None = None
    page_test: PageTest | None = None

    @field_validator("waitlist_url")
    @classmethod
    def hosted_consent_url(cls, value):
        from app.core.local_mode import is_local

        url = urlsplit(value)
        local_url = is_local() and url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
        if ((url.scheme != "https" and not local_url) or not url.hostname or url.username or url.password
                or url.query or url.fragment
                or (not url.path.startswith("/public/waitlists/")
                    and not (is_local() and "/public/waitlists/" in url.path))):
            raise ValueError("pagebuilder_waitlist_url_invalid")
        return value

    @model_validator(mode="after")
    def same_venture(self):
        from app.core.local_mode import is_local

        path = urlsplit(self.waitlist_url).path
        route = "/public/waitlists/" + self.venture_id
        if path != route and not (is_local() and path.endswith(route)):
            raise ValueError("pagebuilder_waitlist_scope_invalid")
        if self.page_test and self.page_test.request and not (self.waitlist_consent_text.strip() and self.waitlist_consent_version.strip()):
            raise ValueError("request_consent_metadata_required")
        return self


class PageBrief(Strict):
    audience: str = Field(min_length=3, max_length=300)
    problem: str = Field(min_length=10, max_length=1000)
    promise: str = Field(min_length=10, max_length=500)
    tone: str = Field(min_length=3, max_length=200)
    proof_points: list[Annotated[str, Field(min_length=3, max_length=500)]] = Field(min_length=3, max_length=5)
    cta: str = Field(default="Join the waitlist", min_length=5, max_length=40)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=30)
    concept_disclosure: str = Field(default="Join the waitlist for early-access updates.", max_length=180)


class DesignDirection(Strict):
    concept: str = Field(min_length=5, max_length=500)
    palette: dict[str, str] = Field(description="Role -> #RRGGBB. Required roles: background, text, primary (button fill), accent. Optional: on_primary (button text), surface, muted_text. At most 12 roles.")
    palette_family: str = Field(min_length=3, max_length=200)
    display_font: str = Field(min_length=3, max_length=180)
    body_font: str = Field(min_length=3, max_length=180)
    layout_archetype: str = Field(min_length=3, max_length=500)
    density: Literal["airy", "balanced", "dense"] = "balanced"
    motion: Literal["none", "subtle"] = "none"
    illustration_style: str = Field(min_length=10, max_length=1000)
    section_rhythm: str = Field(min_length=10, max_length=1000)

    @field_validator("palette")
    @classmethod
    def palette_roles(cls, value):
        import re
        if (not {"background", "text", "primary", "accent"} <= value.keys()
                or len(value) > 12 or any(not re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in value.values())):
            missing = sorted({"background", "text", "primary", "accent"} - value.keys())
            raise ValueError("pagebuilder_palette_invalid: palette needs roles background, text, primary, accent as #RRGGBB"
                + (f"; missing {', '.join(missing)}; rename the body foreground role to text and the button fill role to primary"
                    if missing else "") + (f"; {len(value)} roles, max 12" if len(value) > 12 else ""))
        return value

    @property
    def font_pair(self):
        return [self.display_font, self.body_font]

    @property
    def fingerprint(self):
        return {"palette": self.palette, "palette_family": self.palette_family.casefold(),
                "font_pair": self.font_pair, "layout_archetype": self.layout_archetype.casefold()}


class Section(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    purpose: str = Field(min_length=10, max_length=300)
    headline: str = Field(min_length=3, max_length=140)
    body: str = Field(min_length=10, max_length=1600)
    image_id: str | None = None
    nav_label: str | None = Field(default=None, min_length=3, max_length=60)


class SceneObject(Strict):
    kind: Literal["note_board", "filmstrip", "leaf", "barrel", "orbit_rings", "wave_bands", "dot_field", "sunburst",
                  "stacked_blocks", "arch_window", "envelope", "calendar_page", "chat_bubble", "map_pin"]
    x: FiniteFloat
    y: FiniteFloat
    scale: FiniteFloat = Field(default=1, ge=.25, le=1.8)
    rotation: FiniteFloat = Field(default=0, ge=-180, le=180)


class IllustrationScene(Strict):
    objects: list[SceneObject] = Field(min_length=1, max_length=3)


class DraftImageAsset(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    role: Literal["hero", "section"]
    section_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    subject: str = Field(min_length=12, max_length=500)
    prompt: str = Field(min_length=20, max_length=1600)
    scene: IllustrationScene


class ImageAsset(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    role: Literal["hero", "section", "og", "favicon"]
    section_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    subject: str = Field(min_length=12, max_length=500)
    prompt: str = Field(min_length=20, max_length=1600)
    svg: str = Field(min_length=100, max_length=100000)
    alt: str = Field(max_length=160)
    path: str = ""
    width: int = Field(default=1200, ge=16, le=2400)
    height: int = Field(default=900, ge=16, le=2400)
    provenance: Literal["original_model_svg", "model_scene_native_svg"] = "original_model_svg"
    scene: IllustrationScene | None = None
    compiler_version: str | None = None


class PagePlan(Strict):
    brief: PageBrief
    direction: DesignDirection
    sections: list[Section] = Field(min_length=3, max_length=8)
    assets: list[ImageAsset] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def meaningful_placements(self):
        ids = {section.id for section in self.sections}
        asset_ids = {asset.id for asset in self.assets}
        if len(ids) != len(self.sections) or len(asset_ids) != len(self.assets):
            raise ValueError("pagebuilder_duplicate_id")
        if not {"hero", "waitlist"} <= ids:
            raise ValueError("pagebuilder_hero_waitlist_required")
        if not any(asset.role == "hero" and asset.section_id == "hero" for asset in self.assets):
            raise ValueError("pagebuilder_hero_image_required")
        if not any(asset.role == "section" and asset.section_id not in {"hero", "waitlist"} for asset in self.assets):
            raise ValueError("pagebuilder_contextual_section_image_required")
        for asset in self.assets:
            if asset.section_id not in ids:
                raise ValueError("pagebuilder_image_section_unknown")
            section = next(item for item in self.sections if item.id == asset.section_id)
            if asset.role in {"hero", "section"} and section.image_id != asset.id:
                raise ValueError("pagebuilder_image_placement_mismatch")
        if any(section.image_id and section.image_id not in asset_ids for section in self.sections):
            raise ValueError("pagebuilder_image_unknown")
        return self


class GenerationDraft(Strict):
    brief: PageBrief
    direction: DesignDirection
    sections: list[Section] = Field(min_length=3, max_length=8)
    assets: list[DraftImageAsset] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def meaningful_placements(self):
        return PagePlan.meaningful_placements(self)


class UnsupportedClaim(Strict):
    field_id: str = Field(min_length=3, max_length=160)
    claim: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=10, max_length=600)


class FieldReview(Strict):
    field_id: str = Field(min_length=3, max_length=160)
    classification: Literal["supported", "editorial", "unsupported"]
    reason: str = Field(min_length=10, max_length=300)
    source_quotes: list[Annotated[str, Field(min_length=3, max_length=1000)]] = Field(max_length=32)

    @model_validator(mode="after")
    def sourced_classification(self):
        if (self.classification == "supported") != bool(self.source_quotes):
            raise ValueError("pagebuilder_grounding_field_sources_inconsistent")
        return self


class CapabilityReview(Strict):
    approved: bool = Field(strict=True)
    field_reviews: list[FieldReview] = Field(min_length=1, max_length=48)
    unsupported_claims: list[UnsupportedClaim] = Field(max_length=12)
    suggestions: list[UnsupportedClaim] = Field(default_factory=list, max_length=12, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def coherent_verdict(self):
        if self.approved == bool(self.unsupported_claims):
            raise ValueError("pagebuilder_grounding_review_inconsistent")
        if ({item.field_id for item in self.field_reviews if item.classification == "unsupported"}
                != {item.field_id for item in self.unsupported_claims}):
            raise ValueError("pagebuilder_grounding_unsupported_fields_inconsistent")
        return self


class RasterImage(Strict):
    """Controller-placed illustrative artwork; never part of the reviewed plan copy."""
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    replaces: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    section_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    path: str = Field(pattern=r"^assets/[a-z][a-z0-9_]{0,39}\.webp$")
    alt: str = Field(min_length=12, max_length=160)
    width: int = Field(ge=256, le=2400)
    height: int = Field(ge=256, le=2400)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: str = Field(pattern=r"^image_[1-9]$")
    model: str = Field(min_length=3, max_length=80)
    provenance: Literal["openai_image_illustrative"] = "openai_image_illustrative"


class BuiltPage(Strict):
    files: dict[str, str | bytes]
    manifest: dict[str, Any]
    brief: PageBrief
    direction: DesignDirection
    sections: list[Section]
    assets: list[ImageAsset]
    imagery: list[RasterImage] = Field(default_factory=list, max_length=3)
    qa: dict[str, Any]
    screenshots: dict[str, bytes] = Field(default_factory=dict)
    cost_receipt: dict[str, Any] = Field(default_factory=dict)

    @property
    def plan(self):
        return PagePlan(brief=self.brief, direction=self.direction, sections=self.sections, assets=self.assets)
