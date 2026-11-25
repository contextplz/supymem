from tests.company_os_harness import load_company_os_app


def test_scratch_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    load_company_os_app()
    from app.entry.agent_packs import pack_for_stage

    pack = pack_for_stage("scratch", {})
    names = {a["name"] for a in pack["agents"]}
    assert "Maya" in names
    assert "Samira" in names
    assert "Lina" in names
    assert "Cole" in names


def test_product_github_landing(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    load_company_os_app()
    from app.entry.agent_packs import pack_for_stage

    pack = pack_for_stage(
        "product",
        {"want_first": "landing", "where": "github"},
    )
    names = {a["name"] for a in pack["agents"]}
    assert "Cole" in names
    assert "Bo" in names
    assert "Jonah" in names


def test_existing_product_gtm_pack_does_not_default_to_builder(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    load_company_os_app()
    from app.entry.agent_packs import pack_for_stage

    pack = pack_for_stage("product", {"want_first": "gtm"})
    names = {a["name"] for a in pack["agents"]}
    assert {"Samira", "Maya", "Nia", "Bo"} <= names
    assert "Cole" not in names


def test_entire_company_caps_at_l2(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    load_company_os_app()
    from app.entry.agent_packs import pack_for_stage

    pack = pack_for_stage(
        "company",
        {
            "company_type": "ai_native",
            "automate": ["entire_company"],
            "how_far": "full_autopilot",
        },
    )
    assert pack["autonomy_level"] == "L2"
