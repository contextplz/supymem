"""Conservative display gate for quoted product pricing evidence."""
from __future__ import annotations

import re


_NUMBER = r"\d+(?:[.,]\d+)*(?:[KMB])?"
_MONEY = rf"(?:[$€£₹]\s*{_NUMBER}|\b(?:USD|EUR|GBP|INR)\s*{_NUMBER}|\b{_NUMBER}\s*(?:USD|EUR|GBP|INR))"
_AMOUNT = re.compile(_MONEY + r"(?!\w|[.,]\d)", re.I)
_OFFER = re.compile(r"\b(?:pric(?:e|es|ing)|plans?|tiers?|subscriptions?|licen[cs]es?|packages?|fees?|billed|billing|one[- ]time)\b|\bper[- ](?:seat|user|license|request)\b|/(?:seat|user|license|request)\b", re.I)
_DIRECT_COST = re.compile(r"\bcosts?\s*(?:(?:is|are|starts? at)\s*|:\s*)?" + _MONEY + r"(?!\w|[.,]\d)", re.I)
_BILLING = re.compile(r"\b(?:per month|per year|monthly|annually|yearly)\b|/(?:mo|month|yr|year)\b", re.I)
_TIER_ROW = re.compile(r"^(?:\|\s*)?(?:basic|starter|standard|pro|premium|team|business|growth|enterprise)\b\s*(?:(?:plan|tier)\s*)?(?:[:|—–-]\s*|\s+(?=[$€£₹]|\d|USD|EUR|GBP|INR))", re.I)
_FREE = re.compile(r"\b(?:free (?:plan|tier)|(?:plan|tier) is free|no[- ]cost (?:plan|tier))\b", re.I)
_FINANCIAL_METRIC = re.compile(r"\b(?:salar(?:y|ies)|wages?|compensation|paychecks?|earnings|earned|earns|revenue|profits?|income|funding|fundrais(?:ing|ed)|raised|valuation|turnover|MRR|ARR|GMV)\b|\b(?:operating costs?|marketing costs?|cost of living|market size)\b", re.I)
_FINANCIAL_AMOUNT_VERB = re.compile(
    r"\b(?:makes?|made|earn(?:s|ed|ing)?|generat(?:e|es|ed|ing)|produc(?:e|es|ed|ing)|gross(?:es|ed|ing)?|net(?:s|ted|ting)?|(?:bring(?:s|ing)?|brought|pull(?:s|ing)?|tak(?:e|es|ing))\s+in)\s+"
    r"(?:(?:about|around|over|roughly|approximately|up to|more than|less than)\s+)?"
    + _MONEY + r"(?!\w|[.,]\d)", re.I)
_MISSING_PRICE = re.compile(r"\b(?:pric(?:e|es|ing)\s+(?:(?:is|are)\s+)?(?:unknown|unavailable|undisclosed|not (?:available|published|disclosed))|(?:no|without)\s+(?:a\s+)?free\s+(?:plan|tier)|free\s+(?:plan|tier)\s+(?:(?:is|are)\s+)?(?:unavailable|not available))\b", re.I)
_PROPOSED_PRICE = re.compile(r"\b(?:suggested|proposed|hypothetical|estimated)\s+(?:price|pricing|plan|cost)|\b(?:plan|planning|hope|intend)\s+to\s+(?:charge|price|sell)|\b(?:could|might|would)\s+(?:cost|charge|price)|\b(?:price|pricing)\s+(?:could|might|would|maybe)\b", re.I)
_CLAUSE_BREAK = re.compile(r";\s*|,\s+|\s+(?:and|but|while|whereas)\s+", re.I)


def _offer_clause(clause: str) -> bool:
    return not (_financial_clause(clause) or _MISSING_PRICE.search(clause)
                or _PROPOSED_PRICE.search(clause)) and bool(
        _FREE.search(clause) or (_AMOUNT.search(clause) and (
            _OFFER.search(clause) or _DIRECT_COST.search(clause)
            or (_TIER_ROW.match(clause) and _BILLING.search(clause)))))


def _financial_clause(clause: str) -> bool:
    return bool(_FINANCIAL_METRIC.search(clause) or _FINANCIAL_AMOUNT_VERB.search(clause))


def pricing_evidence_quote(quote: str | None) -> str | None:
    """Keep only an excerpt sentence that actually states a product price or free tier.

    Money and a billing period alone can describe revenue, wages or funding.
    Require offer/charge wording, an explicit unit rate or a named pricing row.
    Keep a whole bounded source sentence or a separately stated offer clause;
    never turn a clipped number or a revenue clause into price proof.
    """
    if not isinstance(quote, str):
        return None
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", quote.strip()):
        sentence = sentence.strip()
        if len(sentence) > 2000:
            continue
        if _offer_clause(sentence):
            amount = _AMOUNT.search(sentence)
            period = _BILLING.search(sentence, amount.end()) if amount else None
            if period and re.search(r"\b(?:demo|youtube|subscribers|tap to unmute)\b", sentence, re.I):
                return sentence[:period.end()].rstrip() + (")" if sentence[period.end():].startswith(")") else "")
            return sentence
        # A story can state revenue and an actual plan price in the same sentence.
        # Admit only a distinct, complete source clause that qualifies on its own.
        # An unresolved mixed clause remains unknown rather than borrowing its
        # amount or offer words from another part of the sentence.
        if _financial_clause(sentence):
            for clause in _CLAUSE_BREAK.split(sentence):
                clause = clause.strip()
                if _offer_clause(clause):
                    return clause
    return None


def review_metrics(rating_quote: str | None, count_quote: str | None) -> tuple[float | None, int | None]:
    """Read explicit review metrics only, never numbers proposed by a model."""
    ratings = re.findall(r"(?<![\w.,])([0-5](?:\.\d+)?)\s*(?:/\s*5(?![\d.,])|out of\s+5(?![\d.,])|stars?\b)", rating_quote or "", re.I)
    counts = re.findall(r"(?<![\w.,])([\d,]+(?:[ \u00a0]\d+)*)\s+(?:customer\s+)?reviews?\b", count_quote or "", re.I)
    rating = float(ratings[0]) if len(ratings) == 1 and float(ratings[0]) <= 5 else None
    count = int(counts[0].replace(",", "")) if len(counts) == 1 and re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", counts[0]) else None
    if re.search(r"\b(?:about|around|over|under|roughly|approximately|at least|more than|less than)\s+\d", count_quote or "", re.I):
        count = None
    return rating, count


def pricing_point(value: str | None, product: str, source: str) -> dict | None:
    quote = pricing_evidence_quote(value)
    if not quote:
        return None
    amount = _AMOUNT.search(quote)
    if amount:
        nearby = quote[max(0, amount.start() - 50):amount.end() + 50]
        period = _BILLING.search(quote, amount.end()) or _BILLING.search(nearby)
        billing = ("month" if re.search(r"mo", period[0], re.I) else "year") if period else "one-time" if re.search(r"one[- ]time", nearby, re.I) else "billing period not stated"
        price = amount[0]
    else:
        price, billing = "Free", "free tier"
    return {"product": product or "Product", "price": price, "billing_period": billing, "source": source}
