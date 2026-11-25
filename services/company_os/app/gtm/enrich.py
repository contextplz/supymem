"""Website enrichment for public GTM candidates.

Keeps only what a company publishes on its own site: name, country (with the
evidence it came from), named people with a role, and a business email on its own
domain. Guessed routes are labelled separately. Pages are read through Firecrawl
and charged through the metered credit path.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from app.gtm.relationships.geo_policy import EEA_GEOS, policy_for

log = logging.getLogger(__name__)
MAX_PAGES = 4
# Parallel lanes per search job. Load-tested 2026-10-26 on the self-hosted Firecrawl VM: 3/6/9 parallel reads gave
# median 1.2/1.5/2.3 s with the same per-site failures; latency climbs from 12. Searches are model-bound, so 6.
CONCURRENCY = 6
COMPANY_TIMEOUT = 45
TOTAL_TIMEOUT = 120
_FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com", "yandex.com", "zoho.com", "qq.com"}
_SKIP_LOCALS = {"noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "abuse", "example", "email", "sentry", "wix", "privacy", "dpo",
    "careers", "jobs", "job", "recruitment", "recruit", "vacancies", "hr", "feedback", "complaints", "gdpr", "data", "accounts", "billing", "invoices", "finance", "payroll", "webmaster", "press", "media", "marketing-unsubscribe"}
_DIRECTORIES = {"clutch.co", "g2.com", "capterra.com", "crunchbase.com", "medium.com", "substack.com", "forbes.com", "techcrunch.com",
    "producthunt.com", "glassdoor.com", "indeed.com", "trustpilot.com", "zoominfo.com", "owler.com", "quora.com", "github.com",
    "ycombinator.com", "businessinsider.com", "inc.com", "entrepreneur.com", "fastcompany.com", "wired.com", "nytimes.com",
    "theguardian.com", "bloomberg.com", "reuters.com", "cbinsights.com", "pitchbook.com", "angel.co", "wellfound.com", "sortlist.com",
    "goodfirms.co", "softwareadvice.com", "getapp.com", "sourceforge.net", "alternativeto.net", "slashdot.org", "hackernoon.com", "dev.to"}
_DIRECTORIES |= {"yelp.com", "yellowpages.com", "yell.com", "google.com", "maps.google.com", "nhs.uk", "zocdoc.com", "healthgrades.com", "threebestrated.com", "justdial.com", "sulekha.com", "bark.com", "checkatrade.com"}
_DIRECTORIES |= {"behance.net", "creativepool.com", "designjobsboard.com", "contra.com", "twine.net", "glassdoor.co.uk"}
_DIRECTORIES |= {"opencare.com", "nextdoor.com", "upwork.com", "fiverr.com", "dribbble.com", "facebook.com"}
_DIRECTORIES |= {"opentable.com"}
_NON_PEOPLE_DOMAINS = {"indeed.com", "glassdoor.com", "glassdoor.co.uk", "designjobsboard.com", "jooble.org", "rigzone.com",
    "findaphd.com", "mdpi.com", "arxiv.org", "sciencedirect.com", "doi.org", "springer.com", "jstor.org",
    "semanticscholar.org", "pubmed.ncbi.nlm.nih.gov",
    "scholar.google.com", "einnews.com", "news.google.com"}
_DIRECTORY_INFRASTRUCTURE = {"googleapis.com", "gstatic.com", "google.com", "googletagmanager.com", "doubleclick.net",
    "cloudflare.com", "jsdelivr.net", "unpkg.com", "w3.org", "schema.org", "mozilla.org", "microsoft.com", "apple.com",
    "wordpress.org", "gravatar.com", "youtube.com", "vimeo.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "linkedin.com", "tiktok.com", "pinterest.com", "whatsapp.com"}
_DIRECTORY_BOILERPLATE = re.compile(r"\b(?:privacy|cookies?|terms|accessibility|sitemap|log ?in|sign in|register|download|"
    r"upgrade your browser|app store|google play|share|print)\b", re.I)
PREFERRED_EMAIL_LOCALS = {"info", "hello", "contact", "enquiries", "reception", "office", "practice", "admin"}
ROLE_EMAIL_LOCALS = PREFERRED_EMAIL_LOCALS | {"sales", "support", "team", "enquiry", "appointments", "booking", "bookings"}
_ARTICLE_PATHS = {"blog", "blogs", "news", "article", "articles", "post", "posts", "press", "insights", "magazine", "story", "stories",
    "directory", "listing", "listings", "category", "categories", "tag", "tags", "topics", "lists", "best", "top", "compare", "review",
    "reviews", "wiki", "resources", "guides", "guide", "learn", "podcast", "events"}
_LISTICLE = re.compile(r"^\s*(?:the\s+)?(?:\d+\s|best\b|top\b|ultimate\b|complete\b)|\b(?:alternatives?|vs\.?|versus|reviews?|how to|guide (?:to|for)|(?:'s|’s) guide|list of|directory|rankings?|roundup)\b", re.I)

_COUNTRY_NAMES = {"united states": "US", "united states of america": "US", "usa": "US", "u.s.a.": "US", "united kingdom": "GB", "uk": "GB",
    "england": "GB", "scotland": "GB", "canada": "CA", "germany": "DE", "deutschland": "DE", "india": "IN", "france": "FR", "italy": "IT", "italia": "IT",
    "spain": "ES", "españa": "ES", "netherlands": "NL", "ireland": "IE", "sweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI",
    "belgium": "BE", "austria": "AT", "poland": "PL", "portugal": "PT"}
_NAME_RE = re.compile(r"(?<![\w.])(" + "|".join(sorted((re.escape(n) for n in _COUNTRY_NAMES), key=len, reverse=True)) + r")(?![\w])", re.I)
_PHONE_PREFIX = {"44": "GB", "49": "DE", "91": "IN", "33": "FR", "39": "IT", "34": "ES", "31": "NL", "353": "IE", "46": "SE", "45": "DK",
    "47": "NO", "358": "FI", "32": "BE", "43": "AT", "48": "PL", "351": "PT"}
_US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
_ZIP = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
_CA_POSTAL = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[A-Z][ -]?\d[A-Z]\d\b")
_PHONE = re.compile(r"(?<![\w+])\+(\d{1,3})[\s\-.(]*\d")
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9_%+-]|\.[A-Za-z0-9])")
_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|＠)\s*"
_DOT = r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\.)\s*"
_OBFUSCATED_EMAIL = re.compile(rf"\b[A-Za-z0-9._%+-]+{_AT}[A-Za-z0-9-]+(?:{_DOT}[A-Za-z0-9-]+)+\b", re.I)
_NAME_PART = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:[-'’][A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+)?"
_NAME = rf"{_NAME_PART}(?:\s+[A-Z]\.?)?\s+{_NAME_PART}"
_CREDENTIALS = r"(?:,\s*[A-Z][a-z]*[A-Z][A-Za-z.]*)*"
_ROLE = (r"(?:Co-?Founder(?:\s*(?:&|and)\s*(?:CEO|CTO))?|Founder|CEO|CTO|COO|CFO|CMO|Chief(?:\s+[A-Za-z&-]+)+\s+Officer|Owner|President|Managing Director|Partner|Principal|"
    r"(?:Practice|Clinic|Office|Studio|Business) Manager|Manager|Dentist|Swim Coach|Coach|"
    r"(?:Senior |Lead |Structural |Civil |Mechanical |Electrical |Software |Project )?Engineer|Architect|Practitioner|Operator|Author|Researcher|Consultant|"
    r"(?:Director|Head|VP|Vice President)(?:\s+of\s+[A-Z][A-Za-z&]+(?:\s+[A-Z][A-Za-z&]+)*)?|"
    r"(?:[A-Z][A-Za-z&-]* ){1,3}(?:Director|Manager|Coach|Coordinator))")
_PERSON_INLINE = re.compile(rf"^(?:Dr\.?\s+)?({_NAME}){_CREDENTIALS}\s*[,\-–—|:·]\s*({_ROLE})\b")
_PERSON_NAME_LINE = re.compile(rf"^(?:Dr\.?\s+)?({_NAME}){_CREDENTIALS}$")
_ROLE_LINE = re.compile(rf"^({_ROLE})$")
_INLINE_TAGS = re.compile(r"</?(?:span|strong|em|b|i|small|u)\b[^>]*>", re.I)
_COPYRIGHT = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?([A-Z][\w&'.-]*(?:\s+[A-Z&][\w&'.-]*){0,4}?)\s*(?:[.,|\n]|all rights|$)", re.I)
_CONTACT_LINK = re.compile(r"/(?:contact|about|team|people|company|imprint|impressum|leadership)(?:[-_a-z0-9]*)?/?$", re.I)
_CONTACT_WORDS = re.compile(r"contact|kontakt|contacto|contato|contatti|impressum|imprint|nous-contacter|お問い合わせ|联系|연락|контакты|связаться|iletişim|kontak|liên hệ|επικοινωνία|יצירת קשר|اتصل", re.I)
_OTHER_CONTACT_WORDS = re.compile(r"get.in.touch|reach.us|enquir|inquir|\b(?:about|team|people|staff|directory|departments?)\b|leadership|appoint|(?:^|/)book(?:/|$)|book (?:a |an )?(?:call|appointment)", re.I)
# logic adapted from ethanplusai/harvey (MIT)
_NON_NAMES = set("request service services commercial history contact quote careers jobs privacy policy terms conditions menu navigation company solutions testimonials repair installation roofing emergency gallery sitemap".split())
_JUNK_DOMAIN = re.compile(r"(?:^|\.)(?:directory|listings?|find-a-[\w-]+|top\d+|best-of)\.|(?:news|magazine|journal|gazette|tribune|herald)\.(?:com|org|net)$", re.I)


def booking_url(url: str) -> bool:
    from app.gtm.scrapy_leads import _seed_url
    if not _seed_url(url) or urlsplit(url).scheme != "https":
        return False
    host, path = _host(url), urlsplit(url).path
    return bool(path.strip("/") and (host in {"calendly.com", "cal.com", "calendar.app.google"}
        or host == "calendar.google.com" and path.startswith("/calendar/appointments/")
        or host == "meetings.hubspot.com"))


def hiring_signals(url: str, raw: str) -> list[dict]:
    # logic adapted from ethanplusai/harvey (MIT)
    visible = re.sub(r"<(script|style)[\s\S]*?</\1>", "", raw, flags=re.I)
    headings = re.findall(r"<(?:h[2-5]|a|li)\b[^>]*>([^<]{6,100})</(?:h[2-5]|a|li)>", visible, re.I)
    headings += re.findall(r"^(?:#{2,5}\s+|\[)([^\]\n]{6,100})", visible, re.M)
    return [{"kind": "need_hiring", "quote": text, "source_url": url, "confidence": "inferred", "recency": "unknown", "published_at": None}
        for text in list(dict.fromkeys(headings)) if re.search(r"\b(?:hiring|open position|opening|wanted|engineer|technician|manager|estimator|developer)\b", text, re.I)
        and not re.search(r"\b(?:no (?:open|current)|filled|closed|privacy|cookie|equal opportunity)\b", text, re.I)][:3]


def _published_contacts(raw: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    emails, phones = [], []
    for match in re.finditer(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', raw, re.I):
        try:
            data = json.loads(html.unescape(match[1]))
        except (ValueError, TypeError):
            continue
        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"email", "telephone"}:
                        for item in child if isinstance(child, list) else [child]:
                            if isinstance(item, str):
                                token = next((token for token in (item, json.dumps(item, ensure_ascii=False)[1:-1], json.dumps(item)[1:-1]) if token in match[0]), "")
                                if token:
                                    (emails if key == "email" else phones).append((item.removeprefix("mailto:").removeprefix("tel:"), token))
                    elif isinstance(child, (dict, list)):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(data)
    visible = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", raw, flags=re.I)
    for match in re.finditer(r'data-cfemail=["\']([0-9a-f]+)["\']|/cdn-cgi/l/email-protection#([0-9a-f]+)', visible, re.I):
        try:
            encoded = bytes.fromhex(match[1] or match[2])
            decoded = bytes(value ^ encoded[0] for value in encoded[1:]).decode("utf-8")
        except (ValueError, UnicodeError, IndexError):
            continue
        emails.append((decoded, match[0]))
    for match in re.finditer(r'mailto:([^\s"\'<>?)\]]+)', visible, re.I):
        for mail in unquote(html.unescape(match[1])).split(","):
            emails.append((mail, match[0]))
    for match in _EMAIL.finditer(html.unescape(visible)):
        emails.append((match[1], match[1]))
    for match in _OBFUSCATED_EMAIL.finditer(html.unescape(visible)):
        decoded = re.sub(_AT, "@", match[0], flags=re.I)
        decoded = re.sub(_DOT, ".", decoded, flags=re.I)
        emails.append((decoded, match[0]))
    return emails, phones


def enabled() -> bool:
    from app.signals.research import _self_hosted_firecrawl
    from app.core.local_mode import is_local
    if is_local():
        from app.gtm.discovery import local_provider
        return local_provider() is not None
    return os.environ.get("GTM_ENRICH_ENABLED", "1").lower() not in {"0", "false", "off", "no"} and bool(os.environ.get("FIRECRAWL_API_KEY") or _self_hosted_firecrawl())


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")
    except ValueError:
        return ""


def classify(url: str, title: str = "") -> str:
    """'company', or the reason a public hit is not a company's own site."""
    host = _host(url)
    if host == "news.ycombinator.com" and urlsplit(url).path == "/item" and any(
            re.fullmatch(r"[0-9]+", item) for item in parse_qs(urlsplit(url).query).get("id", [])):
        return "thread"
    if _JUNK_DOMAIN.search(host) or any(host == d or host.endswith("." + d) for d in _DIRECTORIES):
        return "directory"
    try:
        parts = [p for p in urlsplit(url).path.lower().split("/") if p]
    except ValueError:
        return "invalid"
    if parts and (parts[0] in _ARTICLE_PATHS or any(re.fullmatch(r"(?:19|20)\d\d", p) for p in parts[:3])):
        return "article"
    if _LISTICLE.search(title or ""):
        return "article"
    return "company"


def directory_organisations(raw: str, base: str, audience: str = "") -> list[tuple[str, str]]:
    # shortcut: follow published links only; resolve unlinked names when official domains can be verified.
    from tldextract import TLDExtract
    from app.gtm.journey import _keywords, _signal_url
    from app.autopilot.outreach import _AGGREGATORS
    from app.research.source_quality import _stems
    domains = TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
    owner = domains(_host(base)).top_domain_under_public_suffix or _host(base)
    banned_hosts = _DIRECTORY_INFRASTRUCTURE | _AGGREGATORS
    words = _stems(_keywords(audience, 10)) if audience else set()
    raw = re.sub(r"<(script|style|iframe|svg)\b[^>]*>[\s\S]*?</\1>|<!--[\s\S]*?-->", " ", raw, flags=re.I)
    links = []
    for match in re.finditer(r'<a\b[^>]*\shref\s*=\s*(?:["\']([^"\']*)["\']|([^\s"\'=<>`]+))[^>]*>([\s\S]*?)</a\s*>'
            r'|<[^>]+>|(?<!!)\[([^\]\n]*)\]\(([^)\s]+)\)', raw, re.I):
        href, label = (match[1] or match[2], match[3]) if match[3] is not None else (match[5], match[4])
        if not href:
            continue
        label = " ".join(_clean(label)[0].split())
        if not label or _DIRECTORY_BOILERPLATE.search(label):
            continue
        line_end = raw.find("\n", match.end())
        line = raw[raw.rfind("\n", 0, match.start()) + 1:line_end if line_end != -1 else len(raw)]
        links.append((href, label, bool(words & _stems(label + " " + _clean(line)[0]))))
    found, seen = [], {owner}
    # ponytail: lexical link/line ranking; use grounded listing relevance if synonyms affect recall.
    for href, label, _ in sorted(links, key=lambda link: not link[2]):
        try:
            link = urljoin(base, html.unescape(href))
            host = _host(link)
            domain = domains(host).top_domain_under_public_suffix or host
            if (domain in seen or not _signal_url(link) or classify(link) != "company"
                    or any(host == banned or host.endswith("." + banned) for banned in banned_hosts)):
                continue
            home = urlsplit(link)._replace(path="/", query="", fragment="").geturl()
        except ValueError:
            continue
        seen.add(domain)
        name = host if re.fullmatch(r"(?:visit (?:the )?)?(?:official )?(?:site|website)|here|learn more|read more", label, re.I) or len(label) > 120 else label
        found.append((home, name))
        if len(found) == 10:
            break
    return found


def directory_links(raw: str, base: str, audience: str = "") -> list[str]:
    return [url for url, _ in directory_organisations(raw, base, audience)]


def _clean(raw: str) -> tuple[str, list[str]]:
    raw = re.sub(r"<(script|style|svg)\b[^>]*>[\s\S]*?</\1>", " ", raw, flags=re.I)
    mails = re.findall(r"mailto:([^\s\"'<>?)\]]+)", raw, re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    return text, [html.unescape(m) for m in mails]


def contact_excerpt(raw: str, value: str = "") -> str:
    raw = html.unescape(raw or "")
    raw = re.sub(r'^[^<>]*["\']>\s*(?=<)', "", raw)
    text = _clean(re.sub(r"</?(?:a|span|strong|em|b|i|small|u)\b[^>]*>", "", raw, flags=re.I))[0]
    text = re.sub(r'\b(?:href|class|style|aria-[\w-]+|d)\s*=\s*["\'][^"\']*["\']', "", text, flags=re.I)
    text = re.sub(r"\b(?:all rights reserved\.?|terms\s*(?:&|and)\s*conditions|privacy policy|refund policy|partner with us)\b", "\0", text, flags=re.I)
    lines = [" ".join(line.strip(" ·|© ").split()) for line in re.split(r"[\n\0]", text) if line.strip(" ·|© ")]
    if not value:
        return " ".join(lines)[:1000]
    line = next((line for line in lines if value.casefold() in line.casefold()), "")
    if not line:
        return value
    if re.search(r"skip to (?:main )?content", line, re.I) or re.fullmatch(r"\[[^\]]+\]\((?:mailto:|tel:)[^)]+\)", line, re.I):
        return value
    sentence = next((part for part in re.split(r"(?<=[.!?])\s+", line) if value.casefold() in part.casefold()), line)
    if re.search(r"\b(?:skip to|main content|menu|facebook page|instagram page|sitemap|navigation)\b", sentence, re.I):
        return value
    position = sentence.casefold().find(value.casefold())
    return sentence[max(0, position - 80):position + len(value) + 80].strip(" ·|© ")


def fit_excerpt(raw: str) -> str:
    # ponytail: explicit English role/workflow cues; use sourced audience-fit spans for other niches.
    text = contact_excerpt(raw)
    return next((sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+|[\n;]", text)
        if re.search(r"\b(?:staff(?:ing)?|schedul\w*|shifts?|rota|team|employees?|owner|manager|founder|director|consultant|engineer|dentist|clinic|practice)\b", sentence, re.I)
        and not re.search(r"\b(?:reservations?|book (?:your |a )?table|skip to|main content|privacy|cookies)\b", sentence, re.I)), "")[:1000]


def fit_reason(quote: str, source_url: str) -> str:
    from core.copy import no_dashes

    host = _host(source_url)
    excerpt = no_dashes(contact_excerpt(quote))
    if host and excerpt:
        excerpt = excerpt if len(excerpt) <= 125 else excerpt[:124].rsplit(" ", 1)[0] + "…"
        return f'"{excerpt}" (from {host})'
    if host:
        return f"The public source on {host} does not show whether they have this problem."
    return "The saved details do not show whether they have this problem."


def _business_email(host: str, candidates: list[str], *, published_mailboxes: bool = False) -> str | None:
    for raw in candidates:
        mail = raw.strip().lower().rstrip(".")
        if not _EMAIL.fullmatch(mail):
            continue
        local, domain = mail.rsplit("@", 1)
        if local in _SKIP_LOCALS or re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", domain):
            continue
        # ponytail: mailbox-name ownership matching; require published ownership evidence before accepting domain aliases.
        business = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
        mailbox = published_mailboxes and domain in _FREE_MAIL and len(business) >= 3 and business in re.sub(r"[^a-z0-9]", "", local)
        if domain == host or domain.endswith("." + host) or mailbox:
            return mail
    return None


def _country(text: str) -> tuple[str, str] | None:
    """(ISO code, evidence kind) from a published address, phone prefix or footer."""
    for match in _ZIP.finditer(text):
        if match.group(1) in _US_STATES:
            return "US", "postal_address"
    if _CA_POSTAL.search(text):
        return "CA", "postal_address"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    footer_from = int(len(lines) * 0.75) if len(lines) >= 8 else len(lines)
    found = None
    for index, line in enumerate(lines):
        for match in _NAME_RE.finditer(line):
            word = match.group(1)
            if word.lower() in {"uk", "usa"} and not word.isupper():
                continue
            code = _COUNTRY_NAMES[word.lower()]
            # An address line ends with the country (optionally a postcode); a sentence that mentions one does not.
            if len(line) <= 150 and any(ch.isdigit() for ch in line) and re.fullmatch(r"[\s.,;|\-\d]*[A-Za-z0-9 ]{0,10}", line[match.end():]):
                return code, "postal_address"
            if index >= footer_from and len(line) <= 80 and found is None:
                found = (code, "footer")
    for match in _PHONE.finditer(text):
        digits = match.group(1)
        for size in (3, 2):
            if digits[:size] in _PHONE_PREFIX:
                return _PHONE_PREFIX[digits[:size]], "phone_prefix"
    return found


def _people(text: str) -> list[dict]:
    lines = [re.sub(r"[*#_`>\[\]]|\(https?://[^)]*\)", "", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    found, seen = [], set()
    for index, line in enumerate(lines):
        match = _PERSON_INLINE.match(line)
        if match:
            name, role = match.group(1), match.group(2)
        elif (header := _PERSON_NAME_LINE.match(line)) and index + 1 < len(lines) and _ROLE_LINE.match(lines[index + 1]):
            name, role = header[1], lines[index + 1]
        else:
            continue
        if not set(re.findall(r"[a-z]+", name.casefold())) & _NON_NAMES and name.lower() not in seen:
            seen.add(name.lower())
            found.append({"name": name, "role": role, "quote": line if match else line + "\n" + lines[index + 1]})
    return found[:5]


def _evidence(text: str, *needles: str) -> str:
    positions = [text.lower().find(needle.lower()) for needle in needles]
    if not positions or min(positions) < 0:
        return ""
    start, end = min(positions), max(position + len(needle) for position, needle in zip(positions, needles))
    if end - start <= 900:
        return text[max(0, start - 50):max(end, start + 900)][:1000].strip()
    return "\n…\n".join(text[max(0, position - 40):position + len(needle) + 80].strip()
        for position, needle in zip(positions, needles))[:1000]


def extract(host: str, pages: list[tuple[str, str]], *, published_mailboxes: bool = False) -> dict:
    """Facts a company publishes across its pages [(url, html-or-markdown)]. No inference, no network."""
    result: dict = {"name": None, "email": None, "email_page": None, "email_quote": None, "country": None, "country_source": None, "country_page": None, "people": [], "contacts": [], "contact_form": None, "phone": None, "booking": None}
    for url, raw in pages:
        if _host(url) != host:
            continue
        published, ld_phones = _published_contacts(raw)
        readable = _INLINE_TAGS.sub("", raw)
        text, _ = _clean(readable)
        if re.search(r"no solicitation|do not (?:use|contact)[^\n<]{0,60}(?:marketing|solicitation)", text, re.I):
            return result | {"contacts": [], "email": None, "contact_form": None, "phone": None, "booking": None, "no_solicitation": True}
        visible = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", raw, flags=re.I)
        # logic adapted from ethanplusai/harvey (MIT)
        for match in re.finditer(r'href=["\']([^"\']+)["\']|\]\((https?://[^)\s]+)\)', visible, re.I):
            link = html.unescape(match[1] or match[2])
            if not result["booking"] and booking_url(link):
                result["booking"] = {"url": link, "source_url": url, "quote": match[0]}
        form = re.search(r"<form\b[\s\S]*?</form>|\b(?:contact|enquiry) form\b|\bsend (?:us |a |me )?(?:message|enquiry)\b|\b(?:fill (?:out|in)|complete) (?:the |my )?form below\b", visible, re.I)
        if not form:
            form = re.search(r"\b(?:your |first |last )?name\b[\s\S]{0,180}\b(?:your )?email\b[\s\S]{0,180}\b(?:your )?message\b", visible, re.I)
        if not result["contact_form"] and form and (re.search(r"\b(?:contact|enquiry|message)\b", form[0] + " " + url, re.I)
                or re.search(r'<textarea\b', form[0], re.I) and re.search(r'<input\b[^>]*(?:type|name)=["\']email', form[0], re.I)):
            result["contact_form"] = {"url": url, "source_url": url, "quote": _evidence(raw, form[0][:100])}
        if not result["phone"]:
            for phone, proof in ld_phones:
                if 8 <= len(re.sub(r"\D", "", phone)) <= 15:
                    result["phone"] = {"value": phone, "source_url": url, "quote": _evidence(proof, phone)}
                    break
        for match in re.finditer(r'tel:([^"\'<>\s]+(?:[ ()\d.-]*\d)?)', visible, re.I):
            phone = unquote(html.unescape(match[1])).strip()
            if not result["phone"] and re.fullmatch(r"\+?[\d ().-]+", phone) and 8 <= len(re.sub(r"\D", "", phone)) <= 15:
                result["phone"] = {"value": phone, "source_url": url, "quote": match[0]}
        for line in text.splitlines():
            if result["phone"] or not re.search(r"\b(?:phone|telephone|call|tel)\b", line, re.I):
                continue
            match = re.search(r"\b(?:phone|telephone|call|tel)\b\s*[:.]?\s*(\+?\d[\d ()\-.]{6,}\d)", line, re.I)
            if match and match[1] in raw and 8 <= len(re.sub(r"\D", "", match[1])) <= 15:
                result["phone"] = {"value": match[1].strip(), "source_url": url, "quote": match[0]}
        if result["name"] is None:
            name = _COPYRIGHT.search(text)
            found = name.group(1).strip().rstrip(".,") if name else ""
            if 2 <= len(found) <= 50 and found.lower() not in {"all", "the", "copyright"}:
                result["name"] = found
        for mail, published_as in published:
            mail = _business_email(host, [mail], published_mailboxes=published_mailboxes)
            if not mail:
                continue
            snippet = next((line.strip() for line in raw.splitlines() if published_as.lower() in line.lower()), "")
            if not snippet:
                snippet = next((line.strip() for line in raw.splitlines() if mail in html.unescape(line).lower()), "")
            if not snippet:
                continue
            if published_as.lower() not in raw.lower():
                published_as = next((token for token in re.findall(r"[^\s<>\"']+", snippet) if mail in html.unescape(token).lower()), "")
            if not published_as or published_as.lower() not in snippet.lower():
                continue
            position = snippet.lower().find(published_as.lower())
            snippet = snippet[max(0, position - 80):position + len(published_as) + 80]
            display_quote = contact_excerpt(raw, mail)
            if display_quote and display_quote in raw and mail in display_quote.lower():
                snippet = display_quote
            if not any(contact["email"] == mail for contact in result["contacts"]):
                result["contacts"].append({"email": mail, "source_url": url, "quote": snippet, "display_quote": display_quote,
                    **({"published_as": published_as} if mail not in snippet.lower() else {}),
                    "kind": "business_role" if mail.split("@")[0] in ROLE_EMAIL_LOCALS else "published_business"})
            if result["email"] is None:
                result["email"], result["email_page"], result["email_quote"] = mail, url, snippet
        if result["country"] is None:
            hit = _country(text)
            if hit:
                result["country"], result["country_source"] = hit
                result["country_page"] = url
        people = _people(text)
        blocks = [match.group(2) for match in re.finditer(r"<(address|p|li|tr)\b[^>]*>([\s\S]*?)</\1>", readable, re.I)]
        blocks += re.split(r"\n\s*\n", raw) if "<" not in raw else []
        for person in people:
            person["staff_verified"] = bool(re.search(r"/(?:team|people|leadership|staff)(?:/|$)", urlsplit(url).path, re.I)
                and not re.search(r"\b(?:testimonials?|reviews?|clients?|customers?)\b", text[:text.find(person["name"])], re.I))
            for block in blocks:
                contact_text = re.sub(r'<a\b[^>]*href=["\']mailto:([^"\'>?]+)[^>]*>[\s\S]*?</a>', r'\1', block, flags=re.I)
                block_text, block_mails = _clean(contact_text)
                if person["name"] not in block_text or person["role"] not in block_text:
                    continue
                if "<" not in raw:
                    lines = [line.strip() for line in block_text.splitlines() if line.strip()]
                    if len(lines) > 4 or any(not (person["name"] in line or person["role"] == line or _EMAIL.search(line)) for line in lines):
                        continue
                block_people = _people(block_text)
                emails = list(dict.fromkeys(m for candidate in block_mails + _EMAIL.findall(block_text)
                    if (m := _business_email(host, [candidate], published_mailboxes=published_mailboxes))))
                if len(block_people) != 1 or len(emails) != 1:
                    continue
                email = emails[0]
                compact = re.search(re.escape(person["name"]) + _CREDENTIALS + r"\s*[,–—|:\-]?\s*" + re.escape(person["role"])
                    + r"[\s,;:–—|.\-]*(?:(?:email|contact)(?:\s+me)?\s*(?:at|:)?\s*)?" + re.escape(email), block_text, re.I)
                direct = re.search(r"\b(?:contact|email|reach)\s+" + re.escape(person["name"])
                    + r"\s+(?:at\s+)?" + re.escape(email), block_text, re.I)
                if (compact or direct) and email.split("@")[0] not in ROLE_EMAIL_LOCALS:
                    person.update(email=email, contact_quote=_evidence(block_text, person["name"], person["role"], email))
                    break
            if person["name"].lower() not in {p["name"].lower() for p in result["people"]}:
                result["people"].append(person | {"page": url, "source_url": url})
            elif person.get("email") or person.get("staff_verified") and not next(p for p in result["people"] if p["name"].lower() == person["name"].lower()).get("email"):
                result["people"] = [person | {"page": url, "source_url": url} if p["name"].lower() == person["name"].lower() else p for p in result["people"]]
    result["people"] = result["people"][:5]
    for contact in result["contacts"]:
        person = next((person for person in result["people"] if person.get("email") == contact["email"]), None)
        if person:
            contact.update(kind="named_person", name=person["name"])
    result["contacts"].sort(key=lambda contact: (contact["kind"] != "named_person", contact["email"].split("@")[0] not in PREFERRED_EMAIL_LOCALS))
    return result


def supported_country(code: str | None) -> bool:
    return bool(code) and policy_for(code, "email").get("default") != "blocked"


def email_patterns(name: str) -> dict[str, str]:
    import unicodedata
    parts = re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).split()
    if len(parts) < 2 or set(parts) & _NON_NAMES:
        return {}
    first, last = parts[0], parts[-1]
    return {"first.last": first + "." + last, "first": first, "flast": first[0] + last,
        "firstl": first + last[0], "first_last": first + "_" + last, "f.last": first[0] + "." + last, "last": last}


def mx_status(domain: str) -> str:
    try:
        import dns.resolver
    except ImportError:
        return "unchecked"
    try:
        records = list(dns.resolver.resolve(domain, "MX", lifetime=3, search=False))
        return "mx" if any(str(record.exchange) != "." for record in records) else "no_mx"
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return "no_mx"
    except Exception:
        return "unchecked"


def guessed_routes(facts: dict, host: str, wid: str, *, b2b: bool = True) -> list[dict]:
    from app.core.local_mode import is_local
    if is_local():
        return []
    from app.config import settings
    from app.gtm import email_learning
    if settings.guessed_emails == "off" or not b2b or facts.get("no_solicitation") or facts.get("country") in {"DE", "AT"} or host.endswith((".de", ".at")):
        return []
    learned = email_learning.learned_patterns(host)
    retired = email_learning.retired_patterns(host)
    patterns = {}
    for person in facts.get("people", []):
        email = person.get("email", "")
        if not email or email.rsplit("@", 1)[-1].casefold() != host or email.split("@")[0] in ROLE_EMAIL_LOCALS:
            continue
        for pattern, local in email_patterns(person["name"]).items():
            if local == email.split("@")[0].casefold():
                patterns.setdefault(pattern, person)
    if patterns:
        from types import SimpleNamespace
        from app.state import db
        from app.gtm.email_learning import record
        with db()() as session:
            for pattern, person in patterns.items():
                contact = SimpleNamespace(email=person["email"], workspace_id=wid, enrichment_json="{}")
                record(session, contact, "pattern", person["email"] + ":" + pattern, pattern=pattern)
            session.commit()
        learned = email_learning.learned_patterns(host)
    targets = [person for person in facts.get("people", []) if not person.get("email") and email_patterns(person["name"])
        and person.get("staff_verified")]
    if not targets:
        return []
    mx = mx_status(host)
    if mx == "no_mx":
        return []
    routes = []
    for person in targets:
        options = email_patterns(person["name"])
        choices = list(dict.fromkeys([*patterns, *(item[0] for item in learned), "first.last"]))
        for pattern in choices:
            if pattern not in options or pattern in retired:
                continue
            email = options[pattern] + "@" + host
            validity = email_learning.candidate(email, wid, pattern)
            if validity["blocked"]:
                continue
            known = pattern in patterns or any(item[0] == pattern and item[2] for item in learned)
            confidence = {item[0]: item[1] for item in learned}.get(pattern, 80 if known else 40)
            confidence = min(confidence, 30) if validity["expired"] else 90 if validity["confirmed"] else confidence
            confidence = max(0, confidence - (10 if mx == "unchecked" and not validity["confirmed"] else 0))
            proof = patterns.get(pattern)
            routes.append({"kind": "email", "email": email, "email_kind": "named_person", "route_type": "pattern" if known else "guess",
                "confidence": confidence, "confidence_label": "Confirmed working by other founders" if validity["confirmed"] else "Derived from a published company pattern" if known else "Guessed business address",
                "pattern": pattern, "mx_status": mx, "name": person["name"], "source_url": person["source_url"],
                "quote": person["quote"], "pattern_source_url": proof["source_url"] if proof else None,
                "pattern_quote": proof.get("contact_quote") if proof else None})
            break
    return routes


def generated_route_allowed(data: dict, email: str, country=None, *, for_send: bool = False) -> bool:
    from app.core.local_mode import is_local
    if is_local():
        return False
    from app.config import settings
    route = data.get("email_route") or {}
    host = _host(data.get("website") or "")
    if settings.guessed_emails != "on" or country in {"DE", "AT"} or not host or host.endswith((".de", ".at")):
        return False
    if for_send and (route.get("route_type") != "pattern" or not isinstance(route.get("confidence"), (int, float))
            or not 90 <= route["confidence"] <= 100):
        return False
    return bool(route.get("route_type") in {"guess", "pattern"} and route.get("email") == email
        and route.get("mx_status") in {"mx", "unchecked"} and _host(route.get("source_url") or "") == host
        and email_patterns(data.get("person_name") or "").get(route.get("pattern")) == email.split("@")[0]
        and email.rsplit("@", 1)[-1] == host and data.get("person_quote") and data.get("person_source_url") == route.get("source_url"))


def geo_options() -> list[dict]:
    options = []
    for code in sorted({"US", "CA", "IN", "EU"} | (EEA_GEOS - {"UK"})):
        needs = bool(policy_for(code, "email").get("consent_required"))
        options.append({"code": code, "note_required": needs, "default_basis": "consent" if needs else "legitimate_interest"})
    return options


def _links(markdown: str, base: str) -> list[str]:
    from app.gtm.scrapy_leads import _seed_url
    host, found = _host(base), []
    links = [(href, label) for label, href in re.findall(r"(?<!!)\[([^\]\n]*)\]\(([^)\s]+)\)", markdown)]
    links += [(href, "") for href in re.findall(r"href=[\"']([^\"']+)[\"']", markdown)]
    links += [(href, _clean(label)[0]) for href, label in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', markdown, re.I)]
    scores = {}
    for href, label in links:
        try:
            link = urljoin(base, html.unescape(href)).split("#")[0].split("?")[0]
        except ValueError:
            continue
        if not _seed_url(link):
            continue
        words = unquote(urlsplit(link).path) + " " + label
        score = 3 * bool(_CONTACT_WORDS.search(words)) + bool(_OTHER_CONTACT_WORDS.search(words))
        if _host(link) == host and urlsplit(link).scheme in {"http", "https"} and score and link != base:
            scores[link] = max(scores.get(link, 0), score)
            if link not in found:
                found.append(link)
    return sorted(found, key=lambda url: (-scores[url], 0 if "contact" in url.lower() else 1))


async def _scrape(provider, url: str, wid: str, vid: str) -> str:
    from app.core.local_mode import is_local
    if is_local():
        from app.research.local_providers import local_read
        return await local_read(url, main_only=False)
    from app.credits.metered import call, price_micro
    from app.gtm import email_learning
    key = email_learning.cache_key("firecrawl", "firecrawl_scrape", [url, 0, {"main_only": False}])
    saved = email_learning.cached(key)
    if saved is not None:
        return saved
    from app.core.local_mode import is_local
    result = await call(lambda: provider.scrape(url, main_only=False), purpose="gtm.enrich.scrape", provider="firecrawl",
        workspace_id=wid, venture_id=vid, estimate_micro=price_micro("firecrawl_scrape"),
        **({"payer": "founder"} if is_local() else {}))
    email_learning.cache_save(key, result)
    return result


_READER_FAILURES = {
    "robots_disallowed": "site permissions (automated reads blocked)",
    "robots_unavailable": "site permissions could not be checked",
    "url_not_allowed": "site permissions",
    "page_too_large": "page too large to read",
    "page_too_complex": "page too large to read",
    "invalid_encoding": "unreadable compression",
    "unsupported_encoding": "unreadable compression",
    "javascript_required": "page needs JavaScript",
    "unsupported_content_type": "unsupported page format",
    "text_extraction_failed": "unreadable text or format",
    "pdf_requires_rendering": "unreadable text or format",
    "empty_page": "empty page",
    "page_empty": "empty page",
    "reader_timeout": "timed out",
    "reader_deadline": "timed out",
    "reader_busy": "reader busy",
}


def _source_status_reason(status) -> str | None:
    if type(status) is not int:
        return None
    if status in {401, 403}:
        return "blocked access"
    if status in {404, 410}:
        return "page not found"
    if status == 429:
        return "temporarily busy"
    if status >= 500:
        return "server error"
    if status >= 400:
        return "page request rejected"
    if 300 <= status < 400:
        return "page redirect failed"
    return None


SOURCE_SEARCH_LIMIT = "search limit reached"


def _source_error(error: Exception):
    import socket
    import ssl

    import httpx
    from core.ssrf import UnsafeRemoteUrl
    from app.tools.reader import ReaderError

    cause = error
    seen, fallback = set(), None
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        message = str(cause).casefold()
        if message == "gtm_call_budget_exhausted":
            return SOURCE_SEARCH_LIMIT, cause, message
        if isinstance(cause, ReaderError) and message in {"robots_disallowed", "robots_unavailable", "url_not_allowed"}:
            if message == "robots_disallowed":
                return _READER_FAILURES[message], cause, message
            fallback = (_READER_FAILURES[message], cause, message)
            cause = cause.__cause__
            continue
        status = getattr(getattr(cause, "response", None), "status_code", getattr(cause, "status_code", None))
        if reason := _source_status_reason(status):
            return reason, cause, f"HTTP {status}"
        if isinstance(cause, ReaderError) and message in _READER_FAILURES:
            return _READER_FAILURES[message], cause, message
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)):
            return "timed out", cause, "request timed out"
        if isinstance(cause, httpx.TooManyRedirects) or isinstance(cause, RuntimeError) and message.startswith("too many redirects"):
            return "too many redirects", cause, "redirect limit reached"
        if (isinstance(cause, socket.gaierror) or isinstance(cause, UnsafeRemoteUrl) and message == "unresolvable host"
                or isinstance(cause, PermissionError) and message.startswith("could not resolve ")
                or isinstance(cause, httpx.ConnectError) and any(part in message for part in (
                    "name or service not known", "nodename nor servname provided", "name resolution", "getaddrinfo failed"))):
            return "site address not found", cause, "DNS lookup failed"
        if isinstance(cause, (UnsafeRemoteUrl, PermissionError)):
            return "site permissions", cause, "public URL check failed"
        if isinstance(cause, (UnicodeError, httpx.DecodingError)):
            return "unreadable text or format", cause, "response decoding failed"
        if isinstance(cause, ssl.SSLError) or isinstance(cause, httpx.ConnectError) and any(
                part in message for part in ("certificate verify failed", "certificate_verify_failed", "[ssl:", "tls")):
            return "secure connection failed", cause, "TLS or certificate check failed"
        if isinstance(cause, ConnectionRefusedError) or isinstance(cause, httpx.NetworkError) and "connection refused" in message:
            return "connection refused", cause, "connection refused"
        if isinstance(cause, ConnectionResetError) or isinstance(cause, httpx.NetworkError) and "connection reset" in message:
            return "connection reset", cause, "connection reset"
        if isinstance(cause, httpx.ConnectError):
            fallback = ("connection failed", cause, "connection failed")
        if fallback is None or fallback[0] == "page read failed":
            fallback = ("page read failed", cause, "[redacted]")
        cause = cause.__cause__
    return fallback


def source_failure_reason(error: Exception) -> str:
    return _source_error(error)[0]


def source_content_reason(raw) -> str | None:
    from app.gtm.quality import blocked_page

    status = getattr(raw, "metadata", {}).get("statusCode")
    if reason := _source_status_reason(status):
        return reason
    if not raw or not raw.strip():
        return "empty page"
    return "blocked access" if blocked_page(raw) else None


def source_read_failure(url: str, *, error: Exception | None = None, raw=None, reason: str | None = None) -> dict:
    import httpx
    from core.logging import _local_secret_values
    from core.redact import redact_string
    from app.core.local_mode import is_local

    reason, cause, message = _source_error(error) if error is not None else (reason, None, reason)
    failure = {"url": url, "reason": reason}
    if not is_local():
        return failure
    metadata = getattr(raw, "metadata", {})
    response = getattr(cause, "response", None)
    status = getattr(response, "status_code", getattr(cause, "status_code", metadata.get("statusCode")))
    content_type = response.headers.get("content-type") if response is not None else metadata.get("contentType")
    byte_length = getattr(cause, "byte_length", metadata.get("byteLength"))
    if byte_length is None and response is not None:
        try:
            byte_length = len(response.content)
        except httpx.ResponseNotRead:
            pass
    if byte_length is None and isinstance(raw, str):
        byte_length = len(raw.encode("utf-8", errors="replace"))
    if isinstance(content_type, str):
        content_type = content_type.partition(";")[0].strip().lower()
    exact_values = [value.lower() for value in _local_secret_values() or ()]
    diagnostics = {"error_class": type(cause).__name__ if cause is not None else None, "message": message,
        "http_status": status if type(status) is int else None,
        "content_type": redact_string(content_type, exact_values=exact_values) if isinstance(content_type, str)
            and re.fullmatch(r"[\w.+-]{1,64}/[\w.+-]{1,64}", content_type, flags=re.ASCII) else None,
        "byte_length": byte_length if type(byte_length) is int and byte_length >= 0 else None}
    failure["diagnostics"] = diagnostics
    log.warning("gtm_source_read_failed", extra={"source_host": redact_string(_host(url), exact_values=exact_values),
        "source_reason": reason, "diagnostics": diagnostics})
    return failure


def source_failure_summary(failures: list[dict], *, searches_skipped: int = 0) -> str:
    from collections import Counter
    from app.research.local_providers import SEARCH_BUDGET_EXHAUSTED

    reasons = Counter(failure["reason"] for failure in failures)
    skipped = searches_skipped + reasons.pop(SOURCE_SEARCH_LIMIT, 0)
    count = sum(reasons.values())
    parts = []
    if count:
        parts.append(f"You could not read {count} {'source' if count == 1 else 'sources'}: "
            + ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
            + ". Retry your search, or choose a broader audience to find other public sources. Nothing was sent.")
    if skipped:
        parts.append(f"{skipped} {'check was' if skipped == 1 else 'checks were'} skipped after your search reached its limit, so "
            + SEARCH_BUDGET_EXHAUSTED[0].lower() + SEARCH_BUDGET_EXHAUSTED[1:])
    return " ".join(parts)


async def read_source(callback):
    from app.core.jobs import RetryableError
    from app.core.local_mode import is_local

    for attempt in range(2):
        try:
            async with asyncio.timeout(COMPANY_TIMEOUT if is_local() else None):
                return await callback()
        except RetryableError as error:
            if not is_local() or attempt or any(word in str(error) for word in (
                    "settlement", "receipt", "uncertain", "lease", "in_progress")):
                raise
            await asyncio.sleep(min(2, max(0, error.retry_after_s or 0.5)))


async def enrich_site(provider, website: str, wid: str, vid: str, *, pages: list | None = None, checkpoint=None, scrape=None, published_mailboxes: bool = False, context: dict | None = None, payload: dict | None = None, main_scrape=None, on_source=None, on_read=None) -> dict:
    """Read at most MAX_PAGES of one company's own site: home, then contact/about/team."""
    from app.gtm.scrapy_leads import _seed_url
    parts = urlsplit(website)
    home, host = f"{parts.scheme}://{parts.netloc}/", _host(website)
    if not _seed_url(home):
        return {"quality_rejected": True}
    from app.core.local_mode import is_local
    from app.gtm.discovery import LocalDiscoveryProvider
    expert_source = is_local() and context is not None and bool(context.get("expert_role"))
    failures = []

    async def read(url, callback):
        from app.core.jobs import FatalError
        from app.credits.errors import CreditError
        from app.signals.research import FirecrawlLimiterUnavailable
        if on_source:
            on_source(url, "reading", None)
        try:
            raw = await read_source(callback)
        except Exception as error:
            if not is_local() or isinstance(error, (CreditError, FirecrawlLimiterUnavailable)) or (
                    isinstance(error, FatalError) and str(error) not in {"gtm_call_budget_exhausted", "research_provider_rejected"}):
                raise
            if any(word in str(error) for word in ("settlement", "receipt", "uncertain", "lease", "in_progress")):
                raise
            failure = source_read_failure(url, error=error)
        else:
            reason = source_content_reason(raw)
            if reason is None:
                if on_source:
                    on_source(url, "read", None)
                if on_read:
                    on_read(url, raw)
                return raw
            failure = source_read_failure(url, raw=raw, reason=reason)
        failures.append(failure)
        if on_source:
            on_source(url, "failed", failure["reason"], failure.get("diagnostics"))
        return ""

    qualification = None
    if context is not None and payload is not None and not expert_source:
        from app.gtm import quality
        if is_local() and isinstance(provider, LocalDiscoveryProvider):
            homepage = await read(home, lambda: scrape(home) if scrape else _scrape(provider, home, wid, vid))
        else:
            homepage = await read(home, lambda: main_scrape(home) if main_scrape else _main_scrape(provider, home, wid, vid))
        if is_local() and failures:
            return {"quality_rejected": True, "read_failures": failures, "readable_sources": []}
        if getattr(homepage, "metadata", {}).get("statusCode", 200) >= 400:
            return {"quality_rejected": True}
        qualification = await quality.classify_home(home, homepage or "", context, payload)
        if qualification is None:
            return {"quality_rejected": True}
    seed_contact = website if (parts.path not in {"", "/"} or parts.query) and (is_local() or _CONTACT_WORDS.search(unquote(parts.path))) else None
    pages = pages if pages is not None else []
    queue = [website if expert_source else home] if not pages else _links(pages[0][1], pages[0][0])[:MAX_PAGES - 1]
    if is_local() and pages:
        queue = list(dict.fromkeys(link for url, text in pages for link in _links(text, url)))
    queue = [url for url in queue if url not in {page[0] for page in pages}]
    while queue and len(pages) < MAX_PAGES:
        url = queue.pop(0)
        if not _seed_url(url):
            continue
        try:
            if url == home and is_local() and not expert_source and context is not None and payload is not None and (main_scrape is None and scrape is None or isinstance(provider, LocalDiscoveryProvider)):
                markdown = homepage
            else:
                markdown = await read(url, lambda: scrape(url) if scrape else _scrape(provider, url, wid, vid))
        except Exception as exc:
            from app.core.jobs import FatalError
            from app.credits.errors import CreditError
            from app.signals.research import FirecrawlLimiterUnavailable
            if isinstance(exc, FirecrawlLimiterUnavailable):
                raise
            if scrape is None:
                from app.research.metered import _failure
                if _failure(exc)[0] == "uncertain":
                    raise
            if isinstance(exc, CreditError):
                raise
            if isinstance(exc, FatalError) and str(exc) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                raise
            log.info("gtm_enrich_page_failed", extra={"error_class": type(exc).__name__})
            pages.append((url, ""))
            continue
        if getattr(markdown, "metadata", {}).get("statusCode", 200) in {401, 403, 404, 429, 500, 502, 503, 504}:
            markdown = ""
        pages.append((url, markdown))
        if checkpoint:
            checkpoint()
        if published_mailboxes and not is_local():
            # ponytail: stop at the first published email; a later approved run can look for a named decision maker.
            published = extract(host, pages, published_mailboxes=True)
            if published["contacts"] and not (is_local() and seed_contact and seed_contact not in {page[0] for page in pages}):
                break
        if len(pages) == 1:
            queue = list(dict.fromkeys(([seed_contact] if seed_contact and seed_contact != url else []) + _links(markdown, url)))[:MAX_PAGES - 1]
        elif is_local():
            read_urls = {page[0] for page in pages}
            queue = [link for link in dict.fromkeys(queue + _links(markdown, url)) if link not in read_urls][:MAX_PAGES - len(pages)]
    if is_local() and failures and not any(text for _, text in pages):
        return {"quality_rejected": True, "read_failures": failures, "readable_sources": []}
    facts = extract(host, pages, published_mailboxes=published_mailboxes)
    if is_local():
        facts["read_failures"] = failures
        facts["readable_sources"] = [url for url, text in pages if text]
        from app.gtm.reachable import leadership_routes
        facts["leadership_routes"] = [route for url, text in pages for route in leadership_routes(url, text,
            [person for person in facts["people"] if person["source_url"] == url])]
    facts["pages"] = [u for u, _ in pages]
    from app.gtm.quality import main_text
    facts["evidence_pages"] = [{"url": url, "text": main_text(text)[:10000]} for url, text in pages if text]
    if qualification:
        facts["market_role"] = qualification
        facts["evidence_pages"] = [{"url": home, "text": main_text(homepage)[:16000]}] + [page for page in facts["evidence_pages"] if page["url"] != home]
    facts["careers_url"] = next((urljoin(url, html.unescape(match[1] or match[2]))
        for url, raw in pages for match in re.finditer(r'href=["\']([^"\']+)["\']|\]\(([^)\s]+)\)', raw, re.I)
        if _host(urljoin(url, html.unescape(match[1] or match[2]))) == host
        and re.search(r"/(?:careers|jobs|join-us|employment|hiring)(?:/|$)", urlsplit(urljoin(url, match[1] or match[2])).path, re.I)), None)
    facts["profile_urls"] = list(dict.fromkeys(link for url, raw in pages for link in re.findall(
        r"https?://(?:[\w-]+\.)?(?:linkedin\.com/in|x\.com|twitter\.com)/[\w-]+/?", raw)))
    facts["home"] = home
    facts["contact_page"] = next((url for url, text in pages if text and url != home and _CONTACT_WORDS.search(unquote(urlsplit(url).path))), None)
    if is_local():
        facts["contact_page_quote"] = next((fit_excerpt(page["text"]) or contact_excerpt(page["text"])
            for page in facts["evidence_pages"] if page["url"] == facts["contact_page"]), "")
    facts["company_text"] = "\n".join(_clean(text)[0] for _, text in pages)[:30000]
    if qualification:
        facts["company_text"] = "\n".join(page["text"] for page in facts["evidence_pages"])
        selected = await quality.select_passages(facts["evidence_pages"], context, payload, identity=home)
        facts["evidence_label"] = selected[0].get("evidence_label", "need_passage") if selected else "fit_only"
        if not selected:
            from app.gtm.reachable import contact_routes
            selected = quality.fit_passages(facts["evidence_pages"], context, qualification, contact_routes(facts, host=host), identity=home)
        facts["evidence_pages"] = selected
        if not facts["evidence_pages"]:
            return {"quality_rejected": True}
    return facts


async def _main_scrape(provider, url: str, wid: str, vid: str) -> str:
    from app.core.local_mode import is_local
    if is_local():
        from app.research.local_providers import local_read
        return await local_read(url)
    from app.credits.metered import call, price_micro
    from app.gtm import email_learning
    from app.gtm.quality import EXCLUDE_TAGS
    key = email_learning.cache_key("firecrawl", "firecrawl_scrape", [url, 0, {"main_only": True, "excludeTags": EXCLUDE_TAGS}])
    saved = email_learning.cached(key)
    if saved is not None:
        return saved
    from app.core.local_mode import is_local
    result = await call(lambda: provider.scrape(url), purpose="gtm.enrich.scrape", provider="firecrawl",
        workspace_id=wid, venture_id=vid, estimate_micro=price_micro("firecrawl_scrape"),
        **({"payer": "founder"} if is_local() else {}))
    email_learning.cache_save(key, result)
    return result


async def enrich_seeds(seeds: list[dict], wid: str, vid: str, *, progress: dict | None = None, checkpoint=None, context=None, payload=None) -> dict[str, dict]:
    """Read businesses in bounded lanes; checkpoint paid pages and defer unspent slots."""
    if not enabled() or not seeds:
        return {}
    from app.core.jobs import FatalError, RetryableError
    from app.core.local_mode import is_local
    from app.credits.errors import CreditError
    from app.signals.research import (
        FirecrawlLimiterUnavailable,
        HttpResearchProvider,
        _self_hosted_firecrawl,
    )
    if is_local():
        from app.gtm.discovery import local_provider
        provider = local_provider()
    else:
        provider = HttpResearchProvider("firecrawl", os.environ.get("FIRECRAWL_API_KEY", ""))
    progress = progress if progress is not None else {}
    pages = progress.setdefault("pages", {})
    gate, out = asyncio.Semaphore(CONCURRENCY), progress.setdefault("done", {})
    failed = progress.setdefault("failed", [])
    if is_local():
        progress.setdefault("read_failures", [])
    stop = False
    queued = {}
    for seed in seeds:
        queued.setdefault(_host(seed["website"]), seed)
    order = {host: index for index, host in enumerate(queued)}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_TIMEOUT * max(1, (len(queued) + CONCURRENCY - 1) // CONCURRENCY)

    def save_progress():
        ordered = sorted(out.items(), key=lambda item: order.get(item[0], len(order)))
        out.clear()
        out.update(ordered)
        if checkpoint:
            checkpoint()

    async def one(seed):
        nonlocal stop
        async with gate:
            host = _host(seed["website"])
            if host in out or host in failed:
                return
            if stop or loop.time() >= deadline:
                stop = True
                return
            try:
                async with asyncio.timeout(None if is_local() else 60 if _self_hosted_firecrawl() else COMPANY_TIMEOUT):
                    out[host] = await enrich_site(provider, seed["website"], wid, vid,
                        pages=pages.setdefault(host, []), checkpoint=save_progress, context=context, payload=payload)
                if is_local():
                    progress["read_failures"].extend(out[host].get("read_failures", []))
                    if out[host].get("read_failures") and not out[host].get("readable_sources"):
                        out.pop(host)
                        failed.append(host)
                        save_progress()
                        return
                if out[host].get("quality_rejected"):
                    save_progress()
                    return
                from app.gtm.journey import _consumer, _icp
                out[host]["guessed_routes"] = await asyncio.to_thread(guessed_routes, out[host], host, wid, b2b=not _consumer(_icp(vid)))
                save_progress()
            except FirecrawlLimiterUnavailable:
                stop = True
                raise RetryableError("gtm_enrichment_slot_unavailable", retry_after_s=30) from None
            except FatalError as error:
                if is_local() and str(error) in {"gtm_call_budget_exhausted", "discovery_invalid_output"}:
                    from app.gtm.journey import _log_search_failure
                    failed.append(host)
                    save_progress()
                    _log_search_failure(error, vid, "gtm.enrichment")
                    return
                stop = True
                raise
            except RetryableError:
                stop = True
                save_progress()
                raise
            except CreditError:
                stop = True
                raise
            except asyncio.CancelledError:
                if not is_local():
                    failed.append(host)
                save_progress()
                raise
            except Exception as exc:
                failed.append(host)
                save_progress()
                log.info("gtm_enrich_failed", extra={"error_class": type(exc).__name__})

    tasks = [asyncio.create_task(one(seed)) for seed in queued.values()]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                from app.gtm.journey import _log_search_failure
                _log_search_failure(result, vid, "gtm.enrichment")
                raise result
        if not is_local() and any(host not in out and host not in failed for host in queued):
            from app.gtm.journey import _log_search_failure
            error = RetryableError("gtm_enrichment_deferred", retry_after_s=30)
            _log_search_failure(error, vid, "gtm.enrichment")
            raise error
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return out


def merge(seed: dict, facts: dict) -> dict:
    """Overlay published facts on a seed row. Geo is set only from page evidence."""
    row = dict(seed)
    row["company_text"] = facts.get("company_text") or ""
    row["enriched"] = True
    row["people"] = facts["people"]
    row["contacts"] = facts.get("contacts", [])
    if facts.get("market_role"):
        row["market_role"] = facts["market_role"]
        row["evidence_label"] = facts.get("evidence_label")
        if row["evidence_label"] == "fit_only":
            passage = next(iter(facts.get("evidence_pages") or []), {})
            reason = fit_reason(passage.get("text", ""), passage.get("url") or seed.get("source_url") or seed.get("website") or "")
            row.update(why=reason, why_now=reason)
    if facts["name"]:
        row["organization"] = facts["name"]
    if facts["email"]:
        person = next((p for p in facts["people"] if p.get("email")), None)
        generic = next((c for c in facts.get("contacts", []) if c["email"].split("@")[0] in PREFERRED_EMAIL_LOCALS), None)
        mail = person["email"] if person else generic["email"] if generic else facts["email"]
        page = person["page"] if person else generic["source_url"] if generic else facts["email_page"]
        quote = person["contact_quote"] if person else generic["quote"] if generic else facts.get("email_quote")
        row.update(email=mail, contact_source_url=page, contact_quote=quote, email_confidence="published")
        if not seed.get("signals"):
            row["source_url"] = page
        if person:
            row.update(first_name=person["name"].split()[0], last_name=" ".join(person["name"].split()[1:]), title=person["role"],
                person_name=person["name"], person_role=person["role"], person_source_url=person["page"], person_quote=person["quote"])
        else:
            row.update(first_name=None, last_name=None, title=None)
    elif facts.get("guessed_routes"):
        route = facts["guessed_routes"][0]
        person = next(p for p in facts["people"] if p["name"] == route["name"])
        row.update(email=route["email"], email_confidence=route["route_type"], email_route=route,
            first_name=person["name"].split()[0], last_name=" ".join(person["name"].split()[1:]), title=person["role"],
            person_name=person["name"], person_role=person["role"], person_source_url=route["source_url"], person_quote=person["quote"],
            contact_source_url=route["source_url"], contact_quote=route["quote"])
    if not row.get("person_source_url") and facts["people"]:
        person = facts["people"][0]
        row.update(person_name=person["name"], person_role=person["role"], person_source_url=person["page"], person_quote=person["quote"])
    if supported_country(facts["country"]):
        row.update(country=facts["country"], country_source=facts["country_source"], country_page=facts["country_page"])
    return row


def merge_rows(rows: list[dict], facts_by_host: dict[str, dict]) -> list[dict]:
    """Rows with published facts overlaid; one enriched row per site when it published a business email."""
    out, done = [], set()
    for row in rows:
        host = _host(row.get("website") or "")
        facts = facts_by_host.get(host)
        if facts and facts.get("quality_rejected"):
            continue
        if not facts:
            out.append(row)
        elif facts["email"]:
            if host not in done:
                done.add(host)
                out.append(merge(row, facts))
        else:
            out.append(merge(row, facts))
    from app.gtm.quality import dedupe
    return dedupe(out)
