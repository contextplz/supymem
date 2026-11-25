from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.genome.clone import clone_genome_to_venture
from app.genome.model import load_genome
from app.state import MEM


def test_seed_genomes_load_and_dept_automation_has_no_gtm_agents():
    saas = load_genome("gen_saas_b2b")
    dept = load_genome("gen_dept_automation")

    assert saas.archetype == "saas_b2b"
    assert dept.archetype == "dept_automation"
    assert not {agent.role for agent in dept.agents} & {"growth", "channel_operator", "copywriter"}


def test_clone_is_independent_and_persisted_per_venture():
    clone = clone_genome_to_venture("ven_seed", "gen_saas_b2b")
    clone.prompt_overrides["builder"] = "local change"

    stored = MEM.get("genome:ven_seed")
    fresh = load_genome("gen_saas_b2b")
    assert stored["prompt_overrides"] == {}
    assert fresh.prompt_overrides == {}
