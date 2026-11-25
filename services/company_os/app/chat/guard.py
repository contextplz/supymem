"""Deterministic pre-dispatch guard for founder chat (and therefore voice).

Runs before any intent handler so a hostile, impossible, negated or empty
message can never reach ``change`` (which queues a *runnable* task), the web
search tools, or a publish confirmation card. It only answers; it never acts.
Free-form wording for everything else stays with the default responder.
"""

from __future__ import annotations

import re
from typing import Any

_I = re.IGNORECASE


def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, _I | re.DOTALL)


# (kind, pattern, reply, button label, button href suffix). Order matters:
# hostile input first, then honest capability gaps, then edge input.
_REFUSE = "refusal"
_BULK_COPY = ("I won't blast that many emails at once. With automation on, Supymem sends first emails and "
              "follow-ups from your own connected Gmail, up to {cap} a day across all your ventures, to protect your "
              "sender reputation. This request sent nothing and queued nothing.")
_CUSTOMER_OUTCOME_RE = _r(
    r"(?P<count>\b(?:get|bring|find|win|land|make|give)\s+(?:me|us)\s+(?:\d[\d,.]*\s?k?|a hundred|a thousand|"
    r"hundreds|thousands)\s*(?:\w+\s+){0,2}(?:customers?|users?|clients?|signups?|sales|leads|"
    r"subscribers|followers|downloads)|"
    r"\b\d[\d,.]*\s?k?\s+(?:paying\s+)?(?:customers?|users?)\s+(?:by|in|before|within)\b)|"
    r"\bgo(?:ing)? viral\b|\bmake it viral\b|\b10x\b|"
    r"\b(?:build|create|make|launch)\b.{0,40}\bmakes?\b.{0,10}\$\s?\d")
_RULES: tuple[tuple[str, re.Pattern[str], str, str, str], ...] = (
    (_REFUSE, _r(
        r"ignore (?:all |any |the )?(?:previous|prior|above|earlier)|disregard (?:all |your |the )?"
        r"(?:previous|prior|above|instructions|rules)|\byou are now (?:a|an|in|free|my|the|no longer|unrestricted|(?-i:DAN))\b|\b(?-i:DAN)\b|developer mode|jailbreak|"
        r"admin override|^\s*\[system\]|^\s*sudo\b|"
        r"(?:print|show|reveal|repeat|tell me|give me|output|leak|dump)\b.{0,30}"
        r"(?:system prompt|your (?:instructions|prompt|rules)|the words above)|words above starting"),
     "I can't change my rules or show my internal instructions, and I don't take orders that arrive "
     "as pasted text. You can ask for work on this venture only. Nothing was started; tell me what you want to do next.",
     "Open Now", "now"),
    (_REFUSE, _r(
        r"\bother (?:founders?|users?|customers?|companies|workspaces?|ventures?)\b|"
        r"\b(?:someone|somebody) else'?s (?:venture|data|idea|project)|\bmy other ventures?\b|"
        r"\bven_\w+|\b(?:inspect|read|retrieve|query|fetch|access)\b.{0,60}\b(?:another|foreign)\s+(?:workspaces?|ventures?)\b"),
     "This chat controls one venture at a time, and I can only see this one. I can't read, switch "
     "to or list other ventures from here, and I never show another founder's data. Use the "
     "Ventures page to switch.",
     "Open Ventures", ""),
    (_REFUSE, _r(
        r"scrap\w*\b.{0,60}\b(?:e-?mails?|phone numbers?|contacts?|linkedin|profiles?)|"
        r"harvest\w*\b.{0,30}\b(?:e-?mails?|contacts?)|"
        r"\bfake (?:reviews?|testimonials?|accounts?|followers?|users?|signups?|traction)|"
        r"\bbuy (?:followers|reviews|upvotes|likes|traffic)|\bspam\w*|\bphish\w*|impersonat\w+|"
        r"\bpretend(?:ing)? (?:to be|you are|you're)\b|\bpose as\b"),
     "I won't do that: it is deceptive or breaks platform and privacy rules, and it can get your "
     "accounts banned or worse. Nothing was started. Honest version I can help with: find real "
     "early users, and draft an honest invitation that you review before anything goes out.",
     "Review go-to-market", "GTM"),
    (_REFUSE, _r(
        r"(?:show|reveal|print|tell me|give me|what(?:'s| is)|list|dump|read)\b.{0,25}"
        r"\b(?:api[- ]?keys?|secrets?|passwords?|tokens?|vault|credentials?)\b"),
     "I never show stored keys or passwords, not even to you; I can only see that one is set. "
     "To add or replace one, use the secure form under Connections. Never paste it in chat.",
     "Open Connections", "connections"),
    (_REFUSE, _r(
        r"\bapprove (?:everything|all(?: of it| requests?| future)?)\b|\byes to (?:all|everything)\b|"
        r"\b(?:auto|always)[- ]approve|\b(?:don'?t|do not|dont|never|stop) ask(?:ing)? me\b.{0,15}\b(?:again|ever|anything|everything|future)\b|"
        r"\bwithout (?:asking|confirm\w*|my (?:approval|ok))\b.{0,30}\b(?:anything|everything|future)\b|"
        r"\bblanket approval\b|\bspend (?:all|everything|whatever)\b"),
     "I won't approve things in bulk or in advance. You can request specific work, and it runs without another confirmation. A blanket request does not "
     "authorize future work you have not asked for. Tell me the next task you want to run.",
     "Open Now", "now"),
    (_REFUSE, _r(
        r"\bdelete (?:everything|all(?: of it| my (?:data|ventures))?|my (?:venture|account|data|company|"
        r"workspace)|the (?:venture|account|workspace))\b|\b(?:wipe|erase|destroy)\b.{0,20}\b(?:everything|"
        r"all|venture|data)\b"),
     "I won't delete anything from chat. A venture can be archived from the Ventures page with a "
     "typed confirmation, and restored afterwards. Nothing was deleted.",
     "Open Ventures", ""),
    # Honest capability gaps: requests to *do* what Supymem cannot do or must not do alone.
    (_REFUSE, _r(
        r"\bguarantee\w*|\bpromise me\b|\bmake sure\b.{0,30}\b(?:succeed|win|millionaire|billionaire|rich|profit)|"
        r"\bmake me (?:rich|a millionaire)|\bget rich\b|\b100% (?:sure|success|certain)"),
     "I can't guarantee outcomes and I won't claim results I haven't seen; nobody honestly can. "
     "What I can do is shorten the path: validate the idea, research the market, prepare a plan "
     "and a landing page with a waitlist, and show you real signals as they arrive.",
     "See next step", "now"),
    (_REFUSE, _CUSTOMER_OUTCOME_RE,
     "I can't promise customer counts, revenue or virality, and I won't say I got you results. "
     "What I can do: diagnose your go-to-market bottleneck, prepare a landing page with a waitlist, "
     "and draft outreach you review. Real numbers appear only after real people respond.",
     "Review go-to-market", "GTM"),
    (_REFUSE, _r(
        r"\braise\b.{0,15}\b(?:seed|pre-seed|series|round|funding|money|capital)\b|\bfind (?:me )?investors\b|"
        r"\bget (?:me |us )?funded\b|\bpitch (?:to )?investors\b|"
        r"\b(?:i|we)\s+(?:need|want|have to (?:get|find))\s+(?:some\s+|an?\s+)?(?:investors?|funding|funded|to raise|money)\b"),
     "I can't raise money or contact investors for you, and nobody can honestly promise funding by a date; "
     "that stays with you. I can help you get ready: a pitch narrative built from your saved research and a "
     "metrics checklist investors ask for, so your pitch rests on evidence.",
     "Open research", "research"),
    (_REFUSE, _r(
        r"\bfile\b.{0,15}\b(?:llc|inc\b|incorporation|taxes|tax return|trademark|patent|company|gst)|"
        r"\b(?:incorporate|register)\b.{0,15}\b(?:company|business|llc)|\btax(?:es)? for me\b"),
     "I can't file legal or tax paperwork or give legal or tax advice. A qualified legal or tax professional "
     "can help with that. Ask me to organize your existing notes for that conversation.",
     "Open Now", "now"),
    (_REFUSE, _r(
        r"\b(?:hire|recruit|onboard)\b.{0,20}\b(?:developers?|devs?|engineers?|designers?|freelancers?|"
        r"contractors?|employees?|someone|marketers?|people|a person|a team)\b|"
        r"\bfind me a (?:co-?founder|developer|designer)"),
     "I can't hire or pay people. The team in Supymem is AI workers that do tasks inside the "
     "product after you approve. I can write a role brief you can post yourself. Nothing was queued.",
     "Open team", "org"),
    (_REFUSE, _r(
        r"\brun (?:my |some |the )?ads\b|\b(?:facebook|google|meta|linkedin|tiktok) ads\b|"
        r"\bbuy (?:ads|a domain|domain|.{0,15}domain)\b|\bspend\b.{0,20}\b(?:ads?|marketing|promo)\b"),
     "I can't spend money on ads, domains or vendors for you. I can draft the ad copy or a channel "
     "plan for your review; you place the spend. Nothing was queued.",
     "Review go-to-market", "GTM"),
    # ponytail: obvious fictional or unbounded audiences only; judge reachability with the ICP model next.
    (_REFUSE, _r(
        r"\b(?:change|set)\b.{0,30}\b(?:audience|customers?|icp)\b.{0,20}\b(?:aliens?|martians?|"
        r"extraterrestrials?|ghosts?|zombies?|dragons?|unicorns?|fair(?:y|ies)|wizards?|superhero(?:es)?|"
        r"cartoon characters|everyone on earth|everybody)\b"),
     "That is not a real, reachable audience for this venture. Which real people have the problem "
     "your product solves? I did not change your saved audience.",
     "Find customers", "GTM"),
)

# --- generalized outreach / publishing / money / destructive requests --------------------------------
_RECIPIENT = (r"(?:e-?mails?|messages?|dms?|texts?|people|persons|contacts?|leads?|users?|customers?|founders?|"
              r"investors?|prospects?|folks|companies|recruiters|influencers|strangers|accounts|professionals|"
              r"engineers|startups|subscribers|followers|friends|humans)")
_NOT_NOUN = "".join(rf"(?<!\b{word} )" for word in ("the", "a", "an", "my", "this", "that", "our", "your", "to", "as", "in", "interview", "potential", "possible"))
_OUT_VERB = _r(rf"\b(?:send(?:ing)?|blast|dm|mass[- ]?\w+|e-?mail(?!s\b)(?!\s+(?:subject|template|copy|draft|body|address|sequence|campaign|list|marketing|newsletter))|reach out to|cold[- ]?(?:e-?mail|dm|call)|"
               rf"whatsapp|{_NOT_NOUN}(?:message|text|mail|ping|contact|invite))\b(?!\s+(?:me|us)\b)")
_QTY = _r(rf"\b(\d[\d,]*)(\s?k)?\b(?:\s+[\w'-]+){{0,3}}?\s+{_RECIPIENT}\b|"
          rf"\b(?:hundreds|thousands|dozens|a hundred|a thousand|lots|tons|loads)\b")
_EVERYONE = _r(r"\b(?:everyone|everybody|every single|all (?:of )?(?:my |our |the )?(?:contacts?|leads?|customers?|users?|"
               r"subscribers|followers|investors|people|friends|founders)|(?:a|my|the|our|entire|whole|contact|mailing|"
               r"e-?mail|lead|customer) list\b)")
_BULK_DIRECT = _r(
    r"\b(?:send|blast|dm|mass[- ]?(?:e-?mail|message))\b.{0,40}\b(?:to|at)\b\s+(?:\d{2,}|\d+k|all|every|"
    r"hundreds|thousands)\b|\b(?:e-?mail|dm|message)\s+(?:\d{2,}|\d+k|all|every|hundreds|thousands)\b|"
    r"\bmass[- ](?:e-?mail|dm|message)|\bemail blast\b|\bcold email blast\b")


def _bulk_outreach(text: str) -> bool:
    """A requested send to ten or more recipients, not research counts or draft copy."""
    for request in _outreach_requests(text):
        audience = re.sub(r"\b\d+(?:\s*[-–]\s*\d+)?\s*[- ]?\s*(?:minutes?|mins?|hours?|hrs?)\b", " ", request, flags=re.I)
        if _BULK_DIRECT.search(audience) or any(match.group(1) is None or int(match.group(1).replace(",", "") or 0) * (1000 if match.group(2) else 1) >= 10
               for match in _QTY.finditer(audience)) or _EVERYONE.search(audience):
            return True
    return False


def _bulk_reply(venture_id: str, text: str, *, read_context: bool = True) -> dict[str, Any]:
    if _RULES[2][1].search(" ".join(_affirmative_clauses(text.replace(",", ";")))):
        return _reply(_REFUSE, _RULES[2][2], [_button(venture_id, "Review go-to-market", "GTM")])
    if _r(r"\b(?:dms?|direct[- ]messages?|linkedin|whatsapp|sms|texts?|texting)\b").search(text):
        return _reply(_REFUSE, "I don't automate DMs, LinkedIn messages or texts. I can draft messages for you "
                      "to review and send yourself. Nothing went out and nothing was queued.",
                      [_button(venture_id, "Review go-to-market", "GTM")])
    from app.autopilot.guard import DEFAULT_SEND_MAX, effective_send_max

    cap = DEFAULT_SEND_MAX
    actions = [{"label": "Find customers", "action": "chat.reply", "text": "find me customers", "style": "primary"}]
    kind = _REFUSE
    if venture_id and read_context:
        from sqlalchemy import select
        from app.chat.customers import automate_card
        from app.db.models import GtmContact, Venture
        from app.db.supymem_models import AutopilotSetting
        from app.gtm import journey
        from app.state import db

        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture is not None:
                settings = session.get(AutopilotSetting, venture_id)
                cap = effective_send_max(session, settings) or DEFAULT_SEND_MAX
                row = journey._row(session, venture_id)
                automated = bool(settings and settings.allow_send and
                                 journey._json(row.provenance_json if row else "").get("auto_approve_first_emails"))
                prospects = session.scalar(select(GtmContact.id).where(
                    GtmContact.workspace_id == venture.workspace_id,
                    GtmContact.source.like(f"venture:{venture_id}:%")).limit(1))
                workspace_id = venture.workspace_id
        if venture is not None:
            if not journey._mailbox(workspace_id):
                actions = [{"label": "Connect Gmail", "action": "connections.connect",
                            "payload": {"query": "connect gmail"}, "style": "primary"}]
            elif prospects and not automated:
                card = automate_card(daily_cap=cap)
                actions, kind = card["actions"], card["kind"]
            elif prospects:
                actions = [{"label": "Check outreach", "action": "gtm.status", "style": "primary"}]
    return _reply(kind, _BULK_COPY.format(cap=cap), actions)


_LEAD = (r"^\s*(?:(?:hey|please|ok|okay|now|so|then|just|can you|could you|would you|will you|go ahead and|"
         r"i want you to|i need you to|you should|you must|let'?s)[,\s]+)*")
_OUTREACH_TASK = _r(r"(?:" + _LEAD + r"|\band\s+(?:then\s+)?|[,;:]\s*(?:then\s+)?)"
                    r"(?:send|e-?mail|dm|message|text|tweet|post|publish|share|announce|blast|reach out|"
                    r"contact|invite|forward|reply to|follow up with|cold[- ]?(?:e-?mail|dm|call))\b(?!\s+(?:me|us)\b)")
_COPY_REQUEST = _r(_LEAD + r"(?:write|draft|prepare|give|show|list|find|research|plan|explain|describe|summari[sz]e|how|what|why|when|where|who)\b")
_MONEY_COPY_REQUEST = _r(_COPY_REQUEST.pattern + "|" + _LEAD + r"(?:use|include)\b")
_MONEY_TASK = _r(_LEAD + r"(?:pay|purchase(?!\s+orders?\b)|buy|wire|transfer|donate|subscribe to|renew|refund|charge|invoice|"
                 # Booking calls/meetings with people is scheduling, not spending.
                 r"book(?!\s+(?:(?:a|an|\d+|one|two|three|four|five|some|more|my|the|our)\s+)?(?:\w+\s+){0,2}"
                 r"(?:calls?|meetings?|demos?|interviews?|chats?)\b)|order(?!\s+(?:notes|details)\b))\b")
_DELETE_TASK = _r(_LEAD + r"(?:delete|erase|wipe|destroy|drop)\b(?!.{0,30}\b(?:section|copy|text|line|paragraph|"
                  r"headline|button|word|sentence|task|work item|todo|draft|note)\b)")
_HIRE_TASK = _r(_LEAD + r"(?:hire|recruit|onboard)\b")


def _affirmative_clauses(text: str) -> list[str]:
    # shortcut: explicit English attribution and negation only; extend the corpus before adding another language.
    text = re.sub(r'''\b(?:said|says|replied|reported|asked|wrote|told\s+(?:me|us))\s*[:,]?\s*'''
                  r'''(?:"[^"]*"|“[^”]*”|'(?:[^']|(?<=\w)'(?=\w))*'|‘(?:[^’]|(?<=\w)’(?=\w))*’)''', " ", text, flags=re.I)
    clauses = re.split(r"(?<=[.!?])\s+|[;\n]|\b(?:but|instead|however)\b|\band\s+(?=you\b)|,\s*(?=(?:then|please|now|you)\b)", text, flags=re.I)
    affirmative = []
    for clause in clauses:
        candidate = re.sub(
            _LEAD + r"(?:do not|don['’]?t|never)\s+\w+\s+"
            rf"(?:(?!\b(?:to|then|only)\b|{_OUT_VERB.pattern})[^,;:.!?\n])+(?=,\s*(?:or\s+)?"
            r"(?:contact|send|e-?mail|message|text|invite)\b).*",
            " ", clause, flags=_I)
        candidate = re.sub(
            r"\b(?:do not|don['’]?t|(?P<modal>(?:must|should|will|would|can|could|shall|may|might)\s+not|"
            r"(?:must|should|would|could|might|wo|ca|sha)n['’]?t|cannot)|never|without|no need to|avoid|not to)\s+(?:want\s+to\s+)?"
            rf"(?P<coordinated>(?:(?!\b(?:to|and|then|only)\b|{_OUT_VERB.pattern})[^,;:.!?\n])*?\bor\s+)?"
            r"(?:send\w*|e-?mail\w*|dm\w*|messag\w*|text\w*|contact\w*|invit\w*|post\w*|publish\w*|"
            r"tweet\w*|blast\w*|harvest\w*|scrap\w*|phish\w*|spam\w*|impersonat\w*|hire\w*|spend\w*|"
            r"launch\w*|build\w*|deploy\w*|approv\w*|buy\w*|pay\w*|charg\w*|delet\w*|start\w*|run\w*|"
            r"connect\w*|open\w*|enable\w*|disable\w*)\b(?(modal)[^,:]*|(?(coordinated)[^,:]*|.*))",
            " ", candidate, flags=re.I).strip(" ,")
        if candidate and not re.fullmatch(r"(?:please|hey|ok|okay)", candidate, re.I):
            affirmative.append(candidate)
    return affirmative


def _outreach_requests(text: str) -> list[str]:
    requests = []
    for clause in _affirmative_clauses(text):
        candidate = re.sub(r"\b(?:i|we)\s+(?:will|can|shall|would|plan to|am going to|are going to)\b(?!.*\byou\b).*", " ", clause, flags=re.I)
        outreach = bool(_OUTREACH_TASK.search(candidate))
        for part in candidate.split(","):
            if outreach or _COPY_REQUEST.match(part) or re.match(r"\s*(?:not|no|never|without)\b", part, _I):
                continue
            # shortcut: bounded English noun phrases and relative clauses; extend the corpus before supporting other syntax.
            part = re.sub(r"^\s*(?:and\s+)?(?:a|an|the|my|our|your|their|this|that)\s+"
                          r"(?:(?!\b(?:and|or|then|to|who|whose|you|please|now|just|only|must|should|will|would|"
                          r"can|could|shall|may|might)\b)[\w-]+\s+){0,3}"
                          r"(?:contact|message|text|mail|ping|invite)\b", " ", part, flags=_I)
            if re.match(_LEAD + r"(?:only\s+)?(?:use|include|choose|select|filter|limit|restrict)\b", part, _I):
                part = re.sub(rf"\b{_RECIPIENT}\s+(?:who|that|whose)\b"
                              r"(?:(?!\b(?:you|and|or|then|to|please|now|just|before|after|once|until|when|while|"
                              r"send(?:ing)?|blast|dm|mass[- ]?\w+|whatsapp|cold[- ]?|reach out)\b)[^,;:.!?\n])*", " ", part, flags=_I)
            outreach = bool(_OUT_VERB.search(re.sub(r"\bcontact\s+(?:routes?|details?|info(?:rmation)?|pages?|forms?|points?)\b", " ", part, flags=_I)))
        if outreach:
            requests.append(candidate)
    return requests


def outreach_task_reply(venture_id: str, text: str) -> dict[str, Any] | None:
    """Requests to send, post, publish, pay, delete or hire must never become a runnable queued task.

    Only called for the generic ``change``/``general`` path, so social posts and site publishing that have
    their own confirmation cards are unaffected. Returns an honest refusal with a button, else None.
    """
    clauses = _affirmative_clauses((text or "").strip())
    raw = " ".join(clauses)
    commands = [part for clause in clauses for part in ([clause] if _MONEY_COPY_REQUEST.match(clause) else re.split(r"[:,]", clause))]
    commands.extend(part for clause in clauses if _MONEY_COPY_REQUEST.match(clause)
                    for part in re.split(r"\band\s+(?:then\s+)?", clause, flags=_I)[1:])
    commands.extend(part for clause in clauses if _MONEY_COPY_REQUEST.match(clause) for part in re.split(r"[:,]", clause)[1:]
                    if _MONEY_TASK.match(part) and re.match(_LEAD + r"(?:(?:pay|charge)\b|\w+(?:\s+to)?\s+(?:a|an|the|my|our|your|their|this|that|these|those|\d+|\$)\b)", part, _I)
                    and not (_COPY_REQUEST.match(clause) and re.fullmatch(
                        r"\s*charge\s+\$[\d,]+(?:\.\d+)?\s+per\s+(?:day|week|month|year)\s*[.!?]*\s*", part, _I)))
    if _bulk_outreach(raw):
        return _bulk_reply(venture_id, raw)
    if _outreach_requests(text or ""):
        return _reply("refusal", "I don't send emails, messages or posts from a task, so I didn't queue this and "
                                 "nothing went out. I can draft it for your review; you send it, or approve it "
                                 "when it is ready.", [_button(venture_id, "Draft outreach for review", "GTM")])
    if any(_MONEY_TASK.search(command) for command in commands):
        return _reply("refusal", "I can't pay for things, buy anything or move money. I can prepare a checklist or "
                                 "a note; you make the payment. Nothing was queued.",
                      [_button(venture_id, "Open Now", "now")])
    if any(_DELETE_TASK.search(command) for command in commands):
        return _reply("refusal", "I won't delete things from chat. Nothing was deleted or queued.",
                      [_button(venture_id, "Open Now", "now")])
    if any(_HIRE_TASK.search(command) for command in commands):
        return _reply("refusal", "I can't hire or pay people. The team in Supymem is AI workers that do tasks inside "
                                 "the product after you approve. I can write a role brief you can post yourself. "
                                 "Nothing was queued.", [_button(venture_id, "Open team", "org")])
    return None


def paused_reply(venture_id: str) -> dict[str, Any]:
    return _reply("text", "Your venture is paused, so nothing new will start. Resume it when you are ready and "
                          "ask again. Nothing was queued.",
                  [{"label": "Resume", "action": "resume", "style": "primary"}])


_IDENTITY = _r(
    r"^\W*(?:(?:hey|hi|hello|yo|so|ok|okay)[\s,]+)*(?:"
    r"who (?:are|r) (?:you|u)(?: (?:really|exactly))?|what (?:are|r) (?:you|u)(?: exactly)?|"
    r"(?:are|r) (?:you|u) (?:a |an |really |actually )*(?:real|human|person|people|bot|robot|ai|a\.i\.|chat ?bot|"
    r"machine|gpt|chatgpt|claude|gemini|openai|llm|alive|man|woman|guy)\b.*|"
    r"(?:what|which) (?:model|llm|ai|language model|gpt|version|engine|provider)\b.{0,30}\b(?:you|u|this|powers?|"
    r"running|runs?|use[sd]?|behind)\b.*|"
    r"what (?:are|r) you (?:running |built |powered )?(?:on|with|by)\b.*|"
    r"who (?:made|built|created|trained|owns|developed|powers?) (?:you|u)\b.*|"
    r"(?:what|which) (?:company|provider|vendor) (?:made|built|trained|powers?|runs?)\b.*|"
    r"(?:are|r) you (?:built|powered|based) (?:on|by|with)\b.*|"
    r"tell me about yourself|introduce yourself|what(?:'s| is) your name)\W*$")
_THANKS = _r(r"^\W*(?:(?:ok|okay|cool|great|awesome|perfect|nice)[\s,!.]+)?(?:thanks?|thank you|thx|ty|cheers|awesome|perfect|nice one|great|"
             r"cool|got it|appreciate it)(?:\s+(?:a lot|so much|very much|again|mate|buddy|for (?:that|this|the help)|\w+))?\W*$")

_SELF_WORDS = {"the", "my", "our", "this", "current", "own", "your", "venture", "company", "startup", "project"}
# "status of the Acme venture", "switch to Acme": a named venture, self or not (resolved in guard_reply).
_NAMED = _r(
    r"\b(?:(?:status|state) of|how(?:'s| is)|what about|(?:switch|change|move|go|jump)(?: me)? to|open|show(?: me)?|"
    r"check on|update on|tell me about)\s+(?:the\s+|my\s+|our\s+)?(?P<n>[\w'-]+(?:\s+[\w'-]+){0,3}?)"
    r"\s+(?:venture|company|startup)\b")


def _norm_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[\w'-]+", name.casefold()) if t not in _SELF_WORDS]


def _self_pattern(name: str) -> re.Pattern[str] | None:
    """Full name, or a prefix of at least two words, tolerant of case, spacing and "the/my ... venture"."""
    toks = _norm_tokens(name)
    if not toks:
        return None
    esc = [re.escape(t) for t in toks[:4]]
    body = esc[0] if len(esc) == 1 else esc[0] + r"\s+" + esc[1] + "".join(
        rf"(?:\s+{t}" for t in esc[2:]) + ")?" * (len(esc) - 2)
    return _r(rf"(?:\b(?:the|my|our)\s+)?\b{body}\b(?:\s+(?:venture|company|startup|project))?")


def _scrub_self(raw: str, venture_id: str, own_name: str | None) -> str:
    """Replace every reference to the current venture (id, name, "this venture") with "it"."""
    out = raw.replace(venture_id, "it") if venture_id else raw
    if own_name:
        if (pat := _self_pattern(own_name)):
            out = pat.sub("it", out)
        out = re.sub(re.escape(own_name.strip()), "it", out, flags=_I)
    return out


_GENERIC_SELF = _r(r"\b(?:this|my|our|the|current|my current|the current)\s+(?:venture|company|startup|project)\b")


_NEGATED = _r(
    r"^\s*(?:(?:please|hey|ok|okay)[\s,]+)*(?:do not|don'?t|dont|never|no need to|not yet)\b"
    r".{0,60}\b(?:post|tweet|publish|send|e-?mail|hire|spend|launch|build|deploy|approve|contact|buy|pay|"
    r"charge|delete|start|run)\w*")
_BARE_CONFIRM = _r(
    r"^\s*(?:yes|yeah|yep|yup|y|ok(?:ay)?|sure|no|nope|n|do it|just do it|go|go ahead|confirm(?:ed)?|"
    r"approved?|proceed|cancel|nevermind|never mind|stop)"
    r"(?:[,;\s]+(?:(?:do not|don'?t) ask me|without (?:(?:another|further) )?confirmation))?\s*[.!]*\s*$")
_AFFIRM = _r(r"^\s*(?:yes|yeah|yep|yup|y|ok(?:ay)?|sure|do it|just do it|go|go ahead|confirm(?:ed)?|approved?|proceed)"
             r"(?:[,;\s]+(?:(?:do not|don'?t) ask me|without (?:(?:another|further) )?confirmation))?\s*[.!]*\s*$")
_UNDO = _r(r"^\s*(?:please\s+)?(?:undo|revert|roll ?back)\b")
_KEYBOARD_RUN = re.compile(r"asdf|sdfg|qwer|wert|zxcv|hjkl|uiop|jkl;|(.)\1{3,}", _I)


def looks_like_noise(text: str) -> bool:
    """Keyboard mashing ("asdf qwerty lol"), not an answer: at least half the words are gibberish."""
    words = re.findall(r"[A-Za-z]+", text or "")
    if not words or len(words) > 6:
        return False
    from app.chat.intent import find_customers_request
    from app.idea.agent import is_unsure_answer
    if is_unsure_answer(text) or find_customers_request(text):
        return False
    junk = sum(1 for w in words if len(w) >= 3 and (_KEYBOARD_RUN.search(w) or not re.search(r"[aeiouy]", w, _I)))
    return junk >= 1 and junk * 2 >= len(words)


def pending_confirmation(venture_id: str) -> dict | None:
    """The latest confirmation in this conversation, ignoring its own reminders."""
    if not venture_id:
        # The router's text-only prohibition classifier has no history scope.
        # Its empty sentinel cannot authorize any conversation read.
        return None
    from app.chat.store import list_messages

    for msg in reversed(list_messages(venture_id, limit=20)):
        if msg.get("role") != "assistant" or msg.get("kind") == "next_moves":
            continue
        for a in msg.get("actions") or []:
            if isinstance(a, dict) and a.get("confirmation"):
                return msg
        if "on the card above" in str(msg.get("body_md") or "") or "nothing will run" in str(msg.get("body_md") or ""):
            continue  # our own reminder; the card it points at is still the pending ask
        return None  # the latest assistant turn carried no card
    return None


def _pending_card_label(venture_id: str) -> str | None:
    message = pending_confirmation(venture_id)
    if message:
        return next(str(action.get("label") or "Do it") for action in message.get("actions") or []
                    if isinstance(action, dict) and action.get("confirmation"))
    return None


def is_tenant_refusal(reply: dict[str, Any]) -> bool:
    body = str(reply.get("body_md") or "")
    return body.startswith(("This chat controls one venture", "I don't see a venture called"))


def _button(venture_id: str, label: str, suffix: str) -> dict[str, str]:
    if suffix == "GTM":
        href = f"/gtm?venture={venture_id}"
    elif label == "Open Ventures":
        href = "/"
    else:
        href = f"/v/{venture_id}" + (f"/{suffix}" if suffix else "")
    return {"label": label, "href": href, "style": "primary"}


def _reply(kind: str, body: str, actions: list[dict[str, str]]) -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions, "citations": []}


def hostile_input(text: str) -> bool:
    """Injection, cross-venture probing or a deceptive venture: the guard rules that also apply to an idea.

    The capability-gap rules (taxes, hiring, ads, guarantees...) refuse chat *commands*; an idea
    that merely mentions those topics ("an app that files taxes") is a normal venture.
    """
    text = text or ""
    if any(pattern.search(text) for _, pattern, *_ in _RULES[:2]):
        return True
    # Deception (phishing, spam, scraping contacts, fake reviews) still refuses, unless the idea defends against it.
    return bool(_RULES[2][1].search(text)) and not _DEFENSIVE.search(text)


_DEFENSIVE = re.compile(r"\b(?:detect\w*|filter\w*|block\w*|prevent\w*|protect\w*|against|anti-?\w*|stop\w*|flag\w*|"
                        r"report\w*|identif\w*|spot\w*|fight\w*|defen[cs]\w*|secur\w*|verif\w*|remov\w*|catch\w*)\b", re.I)


def guard_reply(venture_id: str, text: str, *, own_name: str | None = None,
                manager: str | None = None, read_history: bool = True) -> dict[str, Any] | None:
    """Return a side-effect-free reply when the message must not reach a handler, else None."""
    from app.chat.intent import find_customers_request

    raw = (text or "").strip()
    if not raw:
        return _reply("text", "I didn't get a message. Ask me something like \"what's next?\".", [])
    # Our own id and name (any phrasing) are self, never a cross-venture request; a foreign id still is.
    scrubbed = _scrub_self(raw, venture_id, own_name)
    negated = bool(_NEGATED.match(raw) and "?" not in raw and not _affirmative_clauses(raw))
    for kind, pattern, body, label, suffix in (_RULES[:4] if negated else _RULES):
        candidate = scrubbed
        if pattern is _RULES[2][1]:
            candidate = " ".join(_affirmative_clauses(candidate.replace(",", ";")))
        if (pattern is _CUSTOMER_OUTCOME_RE and find_customers_request(candidate)
                and all(match["count"] for match in pattern.finditer(candidate))):
            continue
        if body.startswith("I won't approve things in bulk"):
            # Requesting the reviewed autonomy mode is distinct from approving
            # every future effect. Only remove bounded repetitive-check phrasing;
            # bulk approval, uncapped spend and other refusals still apply.
            evidence = re.sub(r"```.*?```|\"[^\"\n]*\"|“[^”\n]*”|(?<!\w)'[^'\n]+'", " ", candidate, flags=re.S)
            enable = re.search(r"\b(?:turn|switch)\s+(?:on\s+(?:the\s+)?(?:god\s*mode|autopilot)|(?:the\s+)?(?:god\s*mode|autopilot)\s+on)\b|\b(?:enable|activate|start)\s+(?:the\s+)?(?:god\s*mode|autopilot)\b", evidence, re.I)
            if enable and not re.search(r"\b(?:don'?t|do not|never)\b.{0,40}\b(?:god\s*mode|autopilot)\b", evidence, re.I):
                candidate = re.sub(r"\bwithout asking me\s+(?:for|about)\s+every\s+(?:little\s+)?(?:thing|step|task)\b", " ", candidate, flags=re.I)
        if body.startswith("I can't guarantee outcomes"):
            # A founder limiting promises is not requesting guaranteed results. Keep
            # affirmative clauses separate so "no guarantees, but guarantee X" still refuses.
            parts = re.split(r"\bbut\b|[.!?;\n]", candidate, flags=re.I)
            candidate = " ".join(re.sub(
                r"\b(?:no|without|not|don'?t|do\s+not|never|cannot|can'?t|won'?t|will\s+not|avoid|exclude)\b"
                r".{0,100}?\b(?:guarantee\w*|promise\w*)\b", " ", part, flags=re.I)
                for part in parts)
        if pattern.search(candidate):
            if body.startswith("This chat controls one venture"):
                from app.chat.other_venture import supplied_portfolio_context
                if supplied_portfolio_context(raw):
                    continue  # supplied facts plus negated inspection grant no foreign read
            return _reply(kind, body, [_button(venture_id, label, suffix)])
    if negated:
        return _reply("text", "Understood, I won't do that. Nothing was started, posted, spent "
                              "or made public. Tell me what you want to work on next.", [])
    if _IDENTITY.match(raw):
        from app.org.manager import DEFAULT_MANAGER_NAME, identity_reply

        return _reply("text", identity_reply(manager or DEFAULT_MANAGER_NAME, own_name), [])
    if _THANKS.match(raw):
        return _reply("text", "You're welcome. Ask me whenever you want the next step.", [])
    if _bulk_outreach(scrubbed):
        return _bulk_reply(venture_id, scrubbed, read_context=read_history)
    if (m := _NAMED.search(_GENERIC_SELF.sub("it", scrubbed))) and not set(_norm_tokens(m["n"])) <= {"it"}:
        name = " ".join(m["n"].split())[:60]
        if _norm_tokens(name):
            here = f"you're in {own_name}" if own_name else "you're in this venture"
            return _reply("text", f"I don't see a venture called {name} in your account; {here}. This chat "
                                  "works on one venture at a time, so use the Ventures page to switch.",
                          [])
    if not re.search(r"\w", raw) and raw != "?":
        return _reply("text", "I didn't catch a question in that. Try \"what's next?\" or \"where are we?\".", [])
    if raw == "?":
        return _reply("text", "What would you like to know? Try \"what's next?\" or \"where are we?\".",
                      [_button(venture_id, "Open Now", "now")])
    if _UNDO.match(raw):
        return _reply("text", "Tell me which action you want to undo. "
                              "What can be reversed: a pause (say \"resume\") and a queued work item "
                              "(say \"cancel <task title>\").", [_button(venture_id, "Open Now", "now")])
    if _BARE_CONFIRM.match(raw):
        label = _pending_card_label(venture_id) if read_history else None
        if label and _AFFIRM.match(raw):
            return _reply("text", "Tell me which suggested action you want to run, so I do not choose for you.", [])
        if label:
            return _reply("text", "Nothing will run. Tell me what you want to do instead, or ask for your next step.", [])
        return _reply("text", "Nothing is waiting for a yes or no, so nothing was started. "
                              "Tell me what you want, or ask \"what's next?\".",
                      [_button(venture_id, "Open Now", "now")])
    if len(raw.split()) == 1 and looks_like_noise(raw):
        return _reply("text", "I didn't understand that. Try \"what's next?\", \"where are we?\" or ask "
                              "about your venture.", [])
    return None
