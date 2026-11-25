"""Resource pinning parses syntax and never rewrites JavaScript or arbitrary text."""
import pytest

from tests.company_os_harness import load_company_os_app


def test_parser_preserves_script_strings_external_urls_and_css_content():
    load_company_os_app()
    from app.firstlook.asset_versions import AssetVersions
    pin = AssetVersions("wsp_test", 3, {"app.js":"x", "hero.svg":"x", "theme.css":"x"})
    script = 'const markup = "<img src=hero.svg>"; const value="url(hero.svg)";'
    body = '<script>'+script+'</script><img src=hero.svg><img src="https://external.test/hero.svg">'
    result = pin.html(body)
    assert '<script>'+script+'</script>' in result
    assert 'src="/w/wsp_test/site/hero.svg?v=3"' in result
    assert 'src="https://external.test/hero.svg"' in result
    css = pin.css('/* url(hero.svg) */ a{content:"url(hero.svg)";background:url("hero.svg?x=1&v=2#mark")}')
    assert '/* url(hero.svg) */' in css and 'content:"url(hero.svg)"' in css
    assert 'hero.svg?x=1&v=3#mark' in css
    assert pin.url("/w/other/site/hero.svg") == "/w/other/site/hero.svg"
    assert pin.url("unrecorded.js") == "/w/wsp_test/site/unrecorded.js?v=3"


def test_css_import_escape_case_and_srcset_data_url_are_parsed():
    load_company_os_app()
    from app.firstlook.asset_versions import AssetVersions
    pin = AssetVersions("wsp_test", 1, {"hero.svg":"x", "theme.css":"x"})
    css = pin.css('@IMPORT /* gap */ "theme.css" screen; a{background:URL(he\\72o.svg)}')
    assert 'theme.css?v=1' in css and 'hero.svg?v=1' in css
    value = pin.srcset('data:image/svg+xml,%3Csvg%3E,%3C/svg%3E 1x, hero.svg 2x')
    assert value == 'data:image/svg+xml,%3Csvg%3E,%3C/svg%3E 1x, /w/wsp_test/site/hero.svg?v=1 2x'


def test_oversized_or_excessively_nested_resources_fail_closed():
    import pytest
    load_company_os_app()
    from app.firstlook.asset_versions import AssetVersions
    pin = AssetVersions("wsp_test", 1, {})
    with pytest.raises(ValueError, match="firstlook_css_too_large"):
        pin.css("x" * 2_000_001)
    with pytest.raises(ValueError, match="firstlook_css_too_deep"):
        pin.css("f(" * 70 + "x" + ")" * 70)
    assert pin.url("https://[invalid/") == "https://[invalid/"


@pytest.mark.parametrize("workspace", ["maple-ledger", "winter-picnic"])
def test_local_published_site_serves_only_approved_regular_assets(tmp_path, monkeypatch, workspace):
    load_company_os_app()
    from app.api import firstlook_api
    from app.firstlook import serve

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(serve, "_data_root", lambda: tmp_path)
    directory = serve.site_dir(workspace_id=workspace, version=4)
    directory.mkdir(parents=True)
    css = f'body {{ color: navy; }} /* {workspace} */'
    (directory / "theme.css").write_text(css)
    (directory / "unlisted.css").write_text("Copyright notice")
    (directory / "linked.css").symlink_to(directory / "theme.css")
    site = {"version": 4, "copy": {"page_files": {"theme.css": css, "linked.css": css}}}
    monkeypatch.setattr(firstlook_api.fl_service, "public_site", lambda *args, **kwargs: site)
    response = firstlook_api.serve_site_asset(workspace, "theme.css", v=4)
    assert response.status_code == 200 and response.body == css.encode()
    assert firstlook_api.serve_site_asset(workspace, "unlisted.css", v=4).status_code == 404
    assert firstlook_api.serve_site_asset(workspace, "linked.css", v=4).status_code == 404
