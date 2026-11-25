import ast
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS = ("README.md", "docs/self-hosting/keys.md")


def importer_variables():
    """Read declarations without importing colliding service app packages."""
    importer = ROOT / "services/company_os/app/core/local_env_import.py"
    module = ast.parse(importer.read_text())
    declarations = [statement for statement in module.body
                    if isinstance(statement, (ast.Assign, ast.For))
                    and any(isinstance(node, ast.Name) and node.id == "_FIELDS"
                            for node in ast.walk(statement))]
    namespace = {}
    exec(compile(ast.Module(body=declarations, type_ignores=[]), str(importer), "exec"), namespace)
    providers = ast.parse((ROOT / "services/company_os/app/core/providers_local.py").read_text())
    provider_constants = {target.id: ast.literal_eval(statement.value) for statement in providers.body
                          if isinstance(statement, ast.Assign) for target in statement.targets
                          if isinstance(target, ast.Name) and target.id in {"PROVIDER_ENV", "ROLES"}}
    environment = next(statement for statement in module.body
                       if isinstance(statement, ast.FunctionDef) and statement.name == "_environment")
    declarations = [statement for statement in environment.body
                    if isinstance(statement, (ast.Assign, ast.Expr))
                    and any(isinstance(node, ast.Name) and node.id == "known"
                            for node in ast.walk(statement))]
    namespace.update(provider_constants)
    exec(compile(ast.Module(body=declarations, type_ignores=[]), str(importer), "exec"), namespace)
    return namespace["known"]


@pytest.fixture(scope="module")
def contract():
    module = ast.parse((ROOT / "services/company_os/app/api/local_settings.py").read_text())
    assignments = {target.id: statement.value for statement in module.body
                   if isinstance(statement, ast.Assign) for target in statement.targets
                   if isinstance(target, ast.Name)}
    catalog = ast.literal_eval(assignments["FIELD_INFO"])
    variables = importer_variables()
    assert all(name in variables for fields in catalog.values()
               for _, _, names in fields.values() for name in names)
    return catalog, variables


def check_references(text, catalog, variables, *, complete=False):
    mentioned_variables = set(re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b(?!\.md\b)", text))
    assert not mentioned_variables - variables, f"Variables not read by the importer: {sorted(mentioned_variables - variables)}"
    fields = {f"{section}.{name}" for section, names in catalog.items() for name in names}
    mentioned_fields = {value for value in re.findall(r"`([^`\n]+)`", text)
                        if re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", value)
                        and not (ROOT / value).is_file()}
    assert not mentioned_fields - fields, f"Fields not read by the catalog: {sorted(mentioned_fields - fields)}"
    if complete:
        assert not fields - mentioned_fields, f"Missing catalog fields: {sorted(fields - mentioned_fields)}"
        assert not variables - mentioned_variables, f"Missing importer variables: {sorted(variables - mentioned_variables)}"
        for section, entries in catalog.items():
            assert f"<!-- connection: {section} -->" in text
            for name, (label, _, names) in entries.items():
                row = next(line for line in text.splitlines() if f"`{section}.{name}`" in line)
                assert label in row
                assert all(f"`{variable}`" in row for variable in names)


@pytest.mark.parametrize("document", DOCUMENTS)
def test_docs_reference_only_imported_variables_and_catalog_fields(document, contract):
    catalog, variables = contract
    check_references((ROOT / document).read_text(), catalog, variables,
                     complete=document.endswith("/keys.md"))


@pytest.mark.parametrize("audience", ["warehouse inventory teams", "family meal planners"])
@pytest.mark.parametrize("reference", ["SUPYMEM_UNREAD_SETTING=example", "search.unread_setting", "providers.unread_setting"])
def test_unread_reference_is_rejected(contract, audience, reference):
    catalog, variables = contract
    with pytest.raises(AssertionError, match="not read by"):
        check_references(f"For {audience}, set `{reference}`.", catalog, variables)


@pytest.mark.parametrize("audience", ["warehouse inventory teams", "family meal planners"])
def test_missing_import_alias_is_rejected(contract, audience):
    catalog, variables = contract
    text = (ROOT / "docs/self-hosting/keys.md").read_text().replace("`SMTP_FROM`", "")
    with pytest.raises(AssertionError, match="Missing importer variables"):
        check_references(f"For {audience}.\n{text}", catalog, variables, complete=True)


def test_capability_names_titles_and_defaults_match_code():
    module = ast.parse((ROOT / "services/company_os/app/core/local_capabilities.py").read_text())
    registry = next(ast.literal_eval(statement.value) for statement in module.body
                    if isinstance(statement, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "REGISTRY" for target in statement.targets))
    text = (ROOT / "docs/self-hosting/configuration.md").read_text()
    for name, (title, default) in registry.items():
        row = next(line for line in text.splitlines() if f"`{name}`" in line)
        assert f"| {title} | {'On' if default else 'Off'} |" in row


def test_readme_preserves_live_failures_and_offers_connected_tool_page_builds():
    text = (ROOT / "README.md").read_text()
    issues = text.split("## Known issues", 1)[1].split("\n## ", 1)[0]
    assert "November 15, 2026" in issues
    assert "customer searches and requested scans did not complete in live checks" in issues
    assert "A failed page build cannot be published" in issues
    assert "Settings > Keys and connections > Coding tools" in issues
    assert "request a new page" in issues
    assert "leaves your previous saved page in place" in issues
    builds = text.split("## Build with your own coding tool", 1)[1].split("\n## ", 1)[0]
    assert "Codex, Claude Code, Cursor and OpenCode support builds" in builds
    assert "Cursor requires a CLI that supports its sandbox" in builds
    assert "Supymem does not fall back to its own builder" in builds


def test_local_docs_have_no_em_dashes_and_relative_links_resolve():
    documents = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md",
                 *sorted((ROOT / "docs/self-hosting").glob("*.md"))]
    for document in documents:
        text = document.read_text()
        assert "\u2014" not in text, document.relative_to(ROOT)
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^)\s]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            filename, _, anchor = target.partition("#")
            linked = document.parent / filename
            assert linked.is_file(), (document.relative_to(ROOT), target)
            if anchor and linked.suffix == ".md":
                headings = re.findall(r"^#+\s+(.+)$", linked.read_text(), re.MULTILINE)
                assert anchor in {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                                  for heading in headings}, (document.relative_to(ROOT), target)
