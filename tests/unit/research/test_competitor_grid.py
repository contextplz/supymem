import copy
import json

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True)
def research_app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()


@pytest.mark.parametrize("product,audience", [
    ("Warehouse incident logging software", "warehouse supervisors"),
    ("Weekend cycling route planning app", "adult weekend cyclists"),
])
def test_multiline_page_links_keep_verified_competitors_linked(caplog, product, audience):
    from app.research import selection
    from app.research.categorize import competitor_grid
    from app.research.presentation import clean_excerpt

    url = "https://vendor.example/product"
    quote = f"{product} helps {audience} avoid missing steps."
    text = f"See [\n{product} features\n](https://vendor.example/features).\n\n{quote}"
    pages = [{"text": text, "url": url, "verified_page": True}]
    source = {"id": "product", "url": url, "title": product, "summary": quote,
        "page_verified": True, "additional_excerpts": pages,
        "evidence": {"page_verified": True, "additional_excerpts": pages}}
    body = {"overview": "A review of saved public sources.", "observations": [], "assessments": [],
        "service_checks": [], "customer_reviews": [], "coverage_gaps": [], "proposed_tests": [],
        "competitor_checks": [{"source_id": "product", "product_quote_index": 0, "customer_quote_index": 0,
            "positioning_quote_index": None, "pricing_quote_index": None, "competition_type": "direct"}]}
    parsed = selection.parse_selection(json.dumps(body), [source]).model_dump()
    rows = competitor_grid({"product": source}, parsed, {})["rows"]
    assert len(rows) == 1 and rows[0]["url"] == url
    assert clean_excerpt(clean_excerpt(text)) == clean_excerpt(text)
    caplog.set_level("INFO", logger="app.research.categorize")
    assert not competitor_grid({"product": source | {"page_verified": False}}, parsed, {})["rows"]
    unsupported = copy.deepcopy(parsed)
    unsupported["competitor_checks"][0]["product_quote"] = "An unpublished claim"
    assert not competitor_grid({"product": source}, unsupported, {})["rows"]
    assert not competitor_grid({}, parsed, {})["rows"]
    assert [record.getMessage() for record in caplog.records if record.name == "app.research.categorize"] == [
        "research_competitor_dropped reason=unverified_page", "research_competitor_dropped reason=ungrounded_product_quote",
        "research_competitor_dropped reason=missing_source"]
    assert product not in caplog.text and audience not in caplog.text and url not in caplog.text


@pytest.mark.parametrize("name,audience,product,price,complaint", [
    ("ShiftLedger", "warehouse supervisors", "Incident logging software", "Plans cost $18 per site per month.", "Export stalls"),
    ("MendKit", "home sewing beginners", "A garment repair kit", "Kit price $24 includes fabric patches and a guide.", "Instructions skip a step"),
])
def test_grid_uses_fetched_quotes_counts_distinct_reviews_and_admits_no_gap(name, audience, product, price, complaint):
    from app.research import selection
    from app.research.categorize import competitor_grid_markdown
    from app.research.citations import project, restore
    from app.research.presentation import EMPTY_REPORT_MESSAGE, founder_report
    from app.research.review import parse_result
    from app.research.pricing import review_metrics
    from app.research.deep import queries_for

    queries = dict(queries_for({"problem": product, "customer": audience}, "all"))
    assert "1-star 2-star 3-star" in queries["community"] and "4-star" in queries["community"]
    assert "site:reddit.com OR site:news.ycombinator.com" in queries["customer_voice"]
    assert review_metrics(None, "1 234 reviews") == (None, None)
    assert review_metrics(None, "about 123 reviews") == (None, None)
    assert review_metrics("Overall rating 4,2 out of 5", None) == (None, None)
    assert review_metrics("Overall rating 4.2 out of 50", None) == (None, None)

    def source(source_id, url, text, verified=True):
        pages = [{"text": text, "url": url, "verified_page": True, "observed_at": "2026-10-26T10:00:00Z"}] if verified else []
        return {"id": source_id, "url": url, "title": name, "summary": text,
                "page_verified": verified, "additional_excerpts": pages,
                "evidence": {"page_verified": verified, "additional_excerpts": pages},
                "observed_at": "2026-10-26T10:00:00Z"}
    sources = [source("product", "https://seller.example/product", f"{name}: {product} for {audience}. {price} Overall rating 4.2 out of 5 from 123 reviews."),
               source("indirect", "https://alternative.example/product", f"A different offering for {audience} doing the same job."),
               source("substitute", "https://manual.example/blog/workaround", f"You can do this job yourself without buying {product}."),
               source("low1", "https://reviews.example/one", f"{name}, 1 star: {complaint}, the result never arrives."),
               source("low2", "https://reviews.example/two", f"{name}, 3 stars: {complaint}, I need a clearer next step."),
               source("qualified", "https://reviews.example/three", f"{name}, 4 stars: I love it but {complaint.lower()}."),
               source("praise", "https://reviews.example/four", f"{name}, 4 stars: I love everything about it."),
               source("five", "https://reviews.example/five", f"{name}, 5 stars: It is excellent."),
               source("snippet", "https://search.example/excerpt", f"{name}, 1 star: {complaint}, a search snippet only.", verified=False)]
    checks = [{"source_id": item["id"], "product_quote_index": 0, "customer_quote_index": 0,
               "positioning_quote_index": 0 if index == 0 else None, "pricing_quote_index": 0 if index == 0 else None,
               "competition_type": ["direct", "indirect", "substitute"][index],
               "comparable_item_quote_index": 0 if index == 0 else None,
               "rating_quote_index": 0 if index == 0 else None, "review_count_quote_index": 0 if index == 0 else None}
              for index, item in enumerate(sources[:3])]
    customer_reviews = [{"competitor_source_id": "product", "source_id": item["id"], "quote_index": 0, "theme": complaint}
                        for item in sources[3:]]
    body = {"overview": "A review of saved sources.", "observations": [], "assessments": [], "service_checks": [],
            "competitor_checks": checks, "customer_reviews": customer_reviews,
            "coverage_gaps": [], "proposed_tests": []}
    parsed = selection.parse_selection(json.dumps(body), sources).model_dump()
    legacy = copy.deepcopy(parsed)
    legacy.pop("customer_reviews")
    for check in legacy["competitor_checks"]:
        for field in ("competition_type", "comparable_item_quote", "rating_quote", "review_count_quote"):
            check.pop(field)
    assert parse_result(json.dumps(legacy), sources).model_dump() == legacy
    assert parsed["customer_reviews"][0]["quote"] == sources[3]["summary"]
    report = founder_report({"source": sources}, {}, parsed, context={"customer": audience, "problem": product})
    assert audience in report["opening"] and product in report["opening"]
    assert report["empty_message"] is None
    empty_report = founder_report({}, {}, context={"customer": audience, "problem": product})
    assert empty_report["empty_message"] == EMPTY_REPORT_MESSAGE
    assert audience in empty_report["opening"] and empty_report["competitor_grid"]["rows"] == []
    assert competitor_grid_markdown(empty_report["competitor_grid"]) == ""
    grid = report["competitor_grid"]
    assert [row["type"] for row in grid["rows"]] == ["direct", "indirect", "substitute"]
    direct = grid["rows"][0]
    assert direct["price_of_comparable_item"] == price
    assert direct["rating"] == 4.2 and direct["review_count"] == 123
    assert direct["source_date"] == sources[0]["observed_at"]
    assert direct["field_sources"]["rating"]["quote"] == sources[0]["summary"]
    model_sources = json.loads(selection.messages_for({"customer": audience}, sources)[1]["content"])["sources"]
    assert model_sources[0]["fetched_quote_indexes"] == [0]
    assert model_sources[-1]["fetched_quote_indexes"] == []
    assert grid["rows"][1]["price_of_comparable_item"] is None
    theme = grid["complaint_themes"][0]
    assert theme["review_count"] == 3 and not theme["thin"]
    assert [review["rating"] for review in theme["reviews"]] == [1, 3, 4]
    assert "Possible gaps to test" in grid["gap"]
    thin = {**parsed, "customer_reviews": parsed["customer_reviews"][:2] * 2}
    thin_grid = founder_report({"source": sources}, {}, thin)["competitor_grid"]
    assert thin_grid["complaint_themes"][0]["review_count"] == 2
    assert thin_grid["complaint_themes"][0]["thin"]
    assert thin_grid["gap"] == "no clear gap found"
    markdown = competitor_grid_markdown(thin_grid)
    assert "no clear gap found" in markdown and "(thin)" in markdown and "123" in markdown
    assert "Unknown" in markdown and "Price of comparable item" in markdown
    assert "\u2014" not in markdown and "the founder" not in markdown
    empty = founder_report({"source": sources}, {}, {**parsed, "customer_reviews": []})["competitor_grid"]
    assert empty["gap"] == "no clear gap found"
    forged = copy.deepcopy(parsed)
    forged["competitor_checks"][0]["rating_quote"] = "Overall rating 4.9 out of 5 from 999 reviews."
    with pytest.raises(ValueError, match="not present"):
        parse_result(json.dumps(forged), sources)
    assert founder_report({"source": sources}, {}, forged)["competitor_grid"]["rows"][0]["rating"] is None
    stale = copy.deepcopy(sources)
    stale[0]["summary"] = f"{name}: {product}. Suggested price $999. Overall rating 4.9 out of 5 from 999 reviews."
    stale_quote = copy.deepcopy(parsed)
    stale_quote["competitor_checks"][0]["rating_quote"] = stale[0]["summary"]
    stale_quote["competitor_checks"][0]["review_count_quote"] = stale[0]["summary"]
    stale_grid = founder_report({"source": stale}, {}, stale_quote)["competitor_grid"]
    assert stale_grid["rows"][0]["rating"] is None and stale_grid["rows"][0]["review_count"] is None
    ratings_source = source("ratings", "https://ratings.example/products/item", f"{name}: {product} for {audience}. Overall rating 4.2 out of 5 from 123 reviews.")
    associated = copy.deepcopy(parsed)
    associated["competitor_checks"][0].update(rating_quote=None, review_count_quote=None)
    associated["competitor_checks"].append({**parsed["competitor_checks"][0], "source_id": "ratings",
        "product_quote": ratings_source["summary"], "customer_quote": None, "positioning_quote": None,
        "pricing_quote": None, "comparable_item_quote": None,
        "rating_quote": ratings_source["summary"], "review_count_quote": ratings_source["summary"]})
    companies = {"companies": [{"key": "observed-company", "name": name, "finding_ids": ["product", "ratings"]}]}
    merged = founder_report({"source": [*sources, ratings_source]}, {}, associated, companies)["competitor_grid"]
    assert len(merged["rows"]) == 3 and merged["rows"][0]["rating"] == 4.2
    assert merged["rows"][0]["field_sources"]["rating"]["url"] == ratings_source["url"]
    projected, mapping = project(sources)
    reverse = {original: label for label, original in mapping.items()}
    projected_review = restore(parsed, reverse)
    assert restore(projected_review, mapping) == parsed
    assert parse_result(json.dumps(projected_review), projected).customer_reviews[0].competitor_source_id == reverse["product"]
