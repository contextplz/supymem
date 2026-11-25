"""Gym churn-prevention runs (Pune/Hyderabad, 2026-10-22): real independent gyms were lost to quote_ungrounded and
chains/listings must still fail. Homepage text is condensed from the gyms' own sites as Firecrawl markdown; verdicts
are recorded structured model outputs, so these exercise the production gates, not model accuracy."""
import asyncio

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.gtm import journey, quality, reachable  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_modules():
    """Other gtm tests reload app modules; bind the current ones."""
    import importlib
    load_company_os_app()
    globals().update({name: importlib.import_module("app.gtm." + name) for name in ("journey", "quality", "reachable")})

CONTEXT = {"audience": "independent gyms in Hyderabad with 100-500 members", "mode": "B2B", "offer": "",
    "problem": "members quietly stop coming and cancel", "competitors": []}
BUYER = {"role_in_market": "buyer", "anti_icp": False, "icp_fit": 0.8}
# (home, homepage markdown, model's quote as the model wrote it, verdict, kept)
GYMS = [
    ("https://sample-onyx-gym.test/", "# ONYX The Professional Fitness Studio\n**Gym, Pilates & Personal Training** in S.R. Nagar, "
        "[Hyderabad](/contact?utm=x)\nCall studio@consumer.example to book a trial.",
        "Gym, Pilates &amp; Personal Training in S.R. Nagar, Hyderabad", BUYER, True),
    ("https://sample-neighborhood-gym.test/", "IGYM · Gachibowli\nNot just a gym.\nA standard.\n“True transformation is not merely physical.”",
        "\"True transformation is not merely physical.\"", BUYER, True),
    ("https://sample-fitness-studio.test/", "We are one of the best gym specializing in bodybuilding, workout and weight training.\n"
        "Write to admin@sample-fitness-studio.test for Zumba Classes.",
        "Write to [REDACTED] for Zumba Classes.", BUYER, True),
    ("https://sample-steel-gym.test/", "STEEL GYM\nStrength training and group classes in Jubilee Hills, Hyderabad.",
        "steel gym strength training and group classes", BUYER, True),
    # Chains and franchises: rejected before any paid model call.
    ("https://sample-chain-gym.test/", "MEMBER FIRST AT \"ABS\"\n30+ Clubs In Maharashtra\nThe ABS Passport Program", "", None, False),
    ("https://sample-franchise-gym.test/", "Anytime Fitness Banjara Hills\nOpen 24/7 for members", "", None, False),
    ("https://sample-club-gym.test/", "Cult.fit gym Kondapur – book a session", "", None, False),
    ("https://sample-fitzone.test/", "FitZone gyms\nFranchise opportunities available across India", "", None, False),
    # Vendors and competitors selling INTO gyms.
    ("https://sample-gym-software.test/", "Gym management software that keeps members coming back. Book a demo.",
        "Gym management software that keeps members coming back.", {"role_in_market": "vendor/competitor", "anti_icp": True, "icp_fit": 0.0}, False),
    # An invented quote never grounds.
    ("https://sample-iron-gym.test/", "Iron House Gym, Madhapur", "Iron House Gym has 300 members who churn", BUYER, False),
]


@pytest.mark.parametrize("home,text,quote,verdict,kept", GYMS, ids=[row[0].split("/")[2] for row in GYMS])
def test_gym_homepages(monkeypatch, home, text, quote, verdict, kept):
    calls, traces = [], []
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)
    monkeypatch.setattr(reachable, "trace", lambda payload, url, *args, **kwargs: traces.append(args))
    async def model(payload, stage, context):
        calls.append(stage)
        return verdict | {"homepage_quote": quote}
    monkeypatch.setattr(journey, "_discovery_model", model)
    result = asyncio.run(quality.classify_home(home, text, CONTEXT, {"workspace_id": "w"}))
    assert bool(result) == kept, traces
    if kept:
        assert result["homepage_quote"] in quality.main_text(text)
        assert quality.fit_passages([{"url": home, "text": quality.main_text(text)}], CONTEXT, result,
            [{"kind": "phone", "source_url": home}], identity=home)
    if verdict is None:
        assert calls == [] and traces == [("rejected", "chain_not_independent")]


def test_homepage_quote_stays_grounded():
    page = "**Gym, Pilates & Personal Training** in [Hyderabad](https://x.example/a?b=1)"
    assert quality.homepage_quote("gym, pilates &amp; personal training in hyderabad", page) == "Gym, Pilates & Personal Training** in [Hyderabad"
    assert quality.homepage_quote("Gym Pilates Yoga", page) is None
    assert quality.homepage_quote("Pilates Personal", page) is None
    # Exact quotes keep the strict path's span.
    assert quality.homepage_quote("Staff changes are hard.", "Staff\nchanges are hard.") == "Staff\nchanges are hard."


@pytest.mark.parametrize("hit,listing", [
    ({"url": "https://lifestyle.example/top-gyms-hyderabad", "title": "Top 10 Gyms in Hyderabad for 2026"}, True),
    ({"url": "https://sample-onyx-gym.test/", "title": "Best Gym in Hyderabad | ONYX"}, False),
    ({"url": "https://sample-onyx-gym.test/about", "title": "Best Gym in Hyderabad | ONYX"}, False),
    ({"url": "https://sample-gym-software.test/blog/retention", "title": "A guide to gym membership retention"}, False),
])
def test_gym_listings_are_mined(hit, listing):
    assert bool(reachable.directory_listing(hit | {"snippet": "independent gyms"}, CONTEXT)) == listing
