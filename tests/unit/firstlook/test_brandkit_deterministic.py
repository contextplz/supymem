"""Brand kit is a pure function of the idea sentence (45 §3.1)."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_brandkit_deterministic_over_three_runs() -> None:
    from app.firstlook.brandkit import build_brandkit, meets_aa

    idea = "A marketplace for restoring vintage synthesizers"
    kits = [build_brandkit(idea, venture_name="SynthRest") for _ in range(3)]
    assert kits[0].hash == kits[1].hash == kits[2].hash
    assert kits[0].to_dict() == kits[1].to_dict() == kits[2].to_dict()
    assert kits[0].wordmark == "SynthRest"
    assert meets_aa(kits[0].colors.ink, kits[0].colors.page)
    assert meets_aa(kits[0].colors.accent_ink, kits[0].colors.accent)


def test_brandkit_variant_changes_look_deterministically() -> None:
    from app.firstlook.brandkit import build_brandkit

    idea = "A tool that helps freelancers invoice clients"
    a = build_brandkit(idea, variant_index=0)
    b = build_brandkit(idea, variant_index=1)
    assert a.hash != b.hash
    assert build_brandkit(idea, variant_index=1).hash == b.hash


def test_brandkit_aa_over_many_seeds() -> None:
    from app.firstlook.brandkit import build_brandkit, meets_aa

    for i in range(50):
        kit = build_brandkit(f"Idea number {i} about local services for owners")
        assert meets_aa(kit.colors.ink, kit.colors.page), kit.colors
        assert meets_aa(kit.colors.accent_ink, kit.colors.accent), kit.colors
