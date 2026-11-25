"""Recorded bakery evidence for offline journey discovery; no provider network."""
from urllib.parse import urlsplit

BAKERY_NEED = "We are independent bakery owners and lose phone orders in manual notes."
NAPNOTES_AUDIENCE = "First-time parents of babies under 12 months"
NAPNOTES_PROBLEM = "Parents are exhausted and don't know when the next nap should be, so the baby gets overtired and bedtime becomes a fight"
NAPNOTES_URL = "https://www.reddit.com/r/NewParents/comments/nap123/next_nap/"
NAPNOTES_NEED = "I am a first-time parent of a 4-month-old baby and need help with naps because I am exhausted and don't know when my baby's next nap should be, so my baby gets overtired and bedtime becomes a fight."
REDDIT_BLOCK_PAGE = "whoa there, pardner! Your request has been blocked due to a network policy. Try logging in or creating an account. If you're running a script or application, please register or sign in with your developer credentials."
NAPNOTES_HITS = [
    {"url": NAPNOTES_URL, "title": "When should the next nap be?", "snippet": "Baby naps and bedtime."},
    {"url": "https://sleep-consultant.example/", "title": "Baby sleep consultant", "snippet": "We help exhausted parents with baby naps. Book a consultation."},
    {"url": "https://www.amazon.com/dp/NAPBOOK", "title": "Baby sleep book", "snippet": "I need help with my baby's naps and bedtime. A reader review."},
    {"url": "https://profiles.test/in/fixture-nap-parent", "title": "Sprig Quiltcloud | Parent", "snippet": "First-time parent. Product manager."},
    {"url": "https://short-posts.test/fixture_naps", "title": "Sprig Quiltcloud / X", "snippet": "First-time parent. Product manager."},
]


async def napnotes_model(payload, stage, context):
    if stage == "plan":
        return {"queries": [
            {"query": 'site:reddit.com baby naps overtired bedtime "I need help"', "intent": "person"},
            {"query": 'site:linkedin.com/in parents baby naps', "intent": "person"},
            {"query": "baby sleep books expert articles", "intent": "customer_voice"}]}
    if stage == "passages":
        return {"items": [{"id": row["id"], "relevance": 0.95 if row["text"] in NAPNOTES_NEED else 0.0}
            for row in context["candidates"]]}
    if stage == "judge":
        return {"items": [{"id": row["id"], "kind": "customer_voice", "quote": NAPNOTES_NEED,
            "affiliation": "", "name": "", "url": row["url"]} for row in context["candidates"]]}
    if stage == "reachjudge":
        return {"items": [{"id": row["id"], "kind": "person", "quote": NAPNOTES_NEED, "affiliation": "",
            "name": "", "url": row["url"], "fit": 95, "why": "Published first-person baby nap problem.",
            "value_exchange": "Compare experiences timing baby naps in an opt-in research conversation."}
            for row in context["candidates"]]}
    return {"items": []}


async def quality_model(payload, stage, context):
    if stage == "marketrole":
        return {"role_in_market": "buyer", "icp_fit": 0.95, "anti_icp": False,
            "homepage_quote": context["candidates"][0]["text"]}
    if stage == "passages":
        return {"items": [{"id": row["id"], "relevance": 0.95 if row["text"] == BAKERY_NEED else 0.0}
            for row in context["candidates"]]}
    return {"items": []}


async def qualified_bakery(seed, who, *, payload, problem="", competitors=None, offer="", **unused):
    from app.gtm import enrich
    parts = urlsplit(seed.get("website") or seed["source_url"])
    home = f"{parts.scheme}://{parts.netloc}/"
    contact = home + "contact"
    pages = {home: f'{seed["organization"]}\n{BAKERY_NEED}\n[Contact]({contact})',
        contact: seed.get("contact_quote") or "Contact the bakery to discuss your order."}
    async def scrape(url):
        return pages.get(url, "")
    facts = await enrich.enrich_site(None, home, payload["workspace_id"], payload["venture_id"],
        scrape=scrape, main_scrape=scrape, published_mailboxes=True, payload=payload,
        context={"audience": who, "problem": problem, "competitors": competitors or [], "offer": offer, "mode": "B2B"})
    return enrich.merge(seed | {"website": home}, facts)
