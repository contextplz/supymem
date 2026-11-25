import ast
import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def catalog():
    module = ast.parse((ROOT / "services/company_os/app/api/local_settings.py").read_text())
    assignments = {target.id: statement.value for statement in module.body
                   if isinstance(statement, ast.Assign) for target in statement.targets
                   if isinstance(target, ast.Name)}
    fields = ast.literal_eval(assignments["FIELD_INFO"])
    # shortcut: schemas declare fields directly, resolve inherited fields if that changes.
    schemas = {statement.name: {field.target.id for field in statement.body
                               if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)}
               for statement in module.body if isinstance(statement, ast.ClassDef)}
    for section, schema in zip(assignments["SCHEMAS"].keys, assignments["SCHEMAS"].values):
        assert set(fields[ast.literal_eval(section)]) == schemas[schema.id]
    return fields


def _check_references(text, catalog, *, complete=False):
    from tests.unit.infra.test_docs_match_code import importer_variables
    variables = importer_variables()
    mentioned_variables = set(re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b(?!\.md\b)", text))
    assert not mentioned_variables - variables, f"Variables not imported by the local catalog: {sorted(mentioned_variables - variables)}"
    fields = {f"{section}.{name}" for section, names in catalog.items() for name in names}
    mentioned_fields = {value for value in re.findall(r"`([^`\n]+)`", text)
                        if re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", value)}
    assert not mentioned_fields - fields, f"Unknown local settings fields: {sorted(mentioned_fields - fields)}"
    if complete:
        assert not fields - mentioned_fields, f"Missing catalog fields: {sorted(fields - mentioned_fields)}"
        for section, entries in catalog.items():
            assert f"<!-- connection: {section} -->" in text, f"Missing connection: {section}"
            for name, (label, _, names) in entries.items():
                row = next(line for line in text.splitlines() if f"`{section}.{name}`" in line)
                assert label in row, f"Missing in-app label: {section}.{name}"
                assert all(f"`{variable}`" in row for variable in names), f"Missing import names: {section}.{name}"


@pytest.mark.parametrize("document", ["README.md", "docs/self-hosting/keys.md"])
def test_local_docs_only_reference_read_settings(document, catalog):
    _check_references((ROOT / document).read_text(), catalog,
                      complete=document.endswith("/keys.md"))


@pytest.mark.parametrize("actions", [pytest.param(("export your data", "create a backup"), id="warehouse.example"),
                                     pytest.param(("restore a backup",), id="hiking.example")])
def test_readme_directs_recovery_actions_to_the_recovery_section(actions):
    settings = (ROOT / "apps/console/src/app/settings/local/DataSettings.tsx").read_text()
    heading = re.search(r'<h2 id="your-data-title">([^<]+)</h2>', settings)[1]
    readme = (ROOT / "README.md").read_text()
    data_section = readme.split("## Your data\n", 1)[1].split("\n## ", 1)[0]
    navigation = next(line for line in data_section.splitlines() if "Settings > Keys and connections >" in line)
    assert f"Settings > Keys and connections > {heading}" in navigation
    assert all(action in navigation.lower() for action in actions)


@pytest.mark.parametrize("platform, command", [("macOS or Linux", "./start --web"), ("Windows", r".\start.ps1 --web")])
def test_recovery_guide_lists_the_browser_restart_for_each_platform(platform, command):
    guide = (ROOT / "docs/self-hosting/backups-security.md").read_text()
    instructions = guide.split("## Restore and verify\n", 1)[1].split("\n## ", 1)[0]
    assert f"`{command}`" in instructions, platform


@pytest.mark.parametrize(("shell", "python", "local", "launcher", "temporary"), [
    pytest.param("sh", ".venv/bin/python", "SUPYMEM_MODE=local", "./start --web", "mktemp -d", id="warehouse.example"),
    pytest.param("powershell", r".\.venv\Scripts\python.exe", "$env:SUPYMEM_MODE = 'local'",
                 r".\start.ps1 --web", "GetTempPath()", id="hiking.example"),
])
def test_stopped_app_recovery_commands_select_local_sqlite(shell, python, local, launcher, temporary):
    text = (ROOT / "docs/self-hosting/LOCAL_WIRING.md").read_text().split("## Stopped-app recovery\n", 1)[1]
    snippets = re.findall(rf"```{shell}\n(.*?)```", text, re.S)
    assert len(snippets) == 2, f"Document setup and recovery for {shell}"
    setup, snippet = snippets
    for setting in (launcher, temporary, "COMPANY_OS_DATA_DIR", "COMPANY_OS_DB_URL"):
        assert setting in setup
    for setting in (local, "COMPANY_OS_DATA_DIR", "COMPANY_OS_DB_URL", "REPLACE_WITH_SQLITE_FILENAME"):
        assert setting in snippet
    for action in ("backup", "restore"):
        assert f"{python} -m app.cli_local {action} " in snippet


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_recovery_docs_match_each_launchers_sqlite_default(tmp_path, domain, isolate_app_imports):
    from sqlalchemy.engine import make_url

    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.db.local_sqlite import default_database_url
    from app.db.session import resolve_source_database_url

    directory = tmp_path / domain
    compose = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    _, docker_url = compose["x-local-env"]["COMPANY_OS_DB_URL"].removesuffix("}").split(":-", 1)
    modes = (
        ("Desktop", ("macOS desktop", "Linux desktop", "Windows desktop"),
         default_database_url({"COMPANY_OS_DATA_DIR": str(directory)})),
        ("Browser from source", ("Source browser launcher",), resolve_source_database_url({}, directory)),
        ("Docker", ("Docker",), docker_url),
    )
    recovery = (ROOT / "docs/self-hosting/LOCAL_WIRING.md").read_text()
    install = (ROOT / "docs/self-hosting/install.md").read_text()
    for mode, install_modes, url in modes:
        filename = Path(make_url(url).database).name
        for guide, labels in ((recovery, (mode,)), (install, install_modes)):
            for label in labels:
                row = next(line for line in guide.splitlines() if line.startswith(f"| {label} |"))
                assert f"`{filename}`" in row


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.test"])
def test_windows_workspace_move_uses_a_full_backup_not_explorer(domain):
    guide = (ROOT / "docs/self-hosting/backups-security.md").read_text()
    instructions = next(line for line in guide.splitlines() if line.startswith("On Windows, to move your data"))
    for step in ("do not copy", "Full backup folder path", "Your data and recovery", "recovery key", "original"):
        assert step in instructions, domain


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.test"])
def test_windows_docker_entrypoint_keeps_its_linux_shebang(domain):
    # shortcut: check the literal rule; evaluate pattern precedence if attributes add overlapping rules.
    rules = [sorted(fields[1:]) for line in (ROOT / ".gitattributes").read_text().splitlines()
             if (fields := line.split()) and fields[0] == "docker/entrypoint.sh"]
    assert rules == [["eol=lf", "text"]], domain
    assert (ROOT / "docker/entrypoint.sh").read_bytes().startswith(b"#!/bin/sh\n")
    assert "git restore --worktree -- docker/entrypoint.sh" in (ROOT / "docs/self-hosting/install.md").read_text()


@pytest.mark.parametrize("guide, heading", [
    pytest.param("troubleshooting.md", "## You see 403:", id="warehouse.example"),
    pytest.param("QUICKSTART.md", "## Known issues", id="hiking.test"),
])
def test_stalled_desktop_and_browser_403_list_both_restarts(guide, heading):
    text = (ROOT / "docs/self-hosting" / guide).read_text().split(heading, 1)[1].split("\n## ", 1)[0]
    assert "`./start --web`" in text and r"`.\start.ps1 --web`" in text
    assert "printed loopback URL" in text or "exact URL it prints" in text


@pytest.mark.parametrize("guide", ["RELEASING.md", "DESKTOP_RELEASE.md"])
def test_release_guides_reach_the_public_procedure_without_hiding_available_work(guide):
    directory = ROOT / "docs/self-hosting"
    path = directory / guide
    text = path.read_text()
    if guide == "DESKTOP_RELEASE.md":
        assert "November 15, 2026" in text and "live checks" in text
        assert "not in this version" not in text
        path = directory / "RELEASING.md"
        text = path.read_text()
    procedure = path.parent / re.search(r"\[maintainer release procedure\]\(([^)#]+)", text)[1]
    procedure = procedure.read_text()
    for step in ("make_public_tree.sh", "check_public.sh", "public-ci.yml", "git init", "desktop_version.py check", "SHA-256"):
        assert step in procedure


@pytest.mark.parametrize("audience", ["warehouse inventory teams", "family meal planners"])
def test_unread_environment_variable_is_rejected(catalog, audience):
    text = f"For {audience}, set `SUPYMEM_UNREAD_SETTING=example`."
    with pytest.raises(AssertionError, match="Variables not imported by the local catalog"):
        _check_references(text, catalog)


@pytest.mark.parametrize("audience", ["warehouse inventory teams", "family meal planners"])
def test_unread_settings_field_is_rejected(catalog, audience):
    text = f"For {audience}, save `search.unread_setting` in Keys and connections."
    with pytest.raises(AssertionError, match="Unknown local settings fields"):
        _check_references(text, catalog)


@pytest.mark.parametrize("audience", ["warehouse inventory teams", "family meal planners"])
def test_missing_catalog_field_is_rejected(catalog, audience):
    fields = {f"{section}.{name}" for section, names in catalog.items() for name in names}
    text = f"For {audience}.\n" + "\n".join(f"`{field}`" for field in sorted(fields)[1:])
    with pytest.raises(AssertionError, match="Missing catalog fields"):
        _check_references(text, catalog, complete=True)
