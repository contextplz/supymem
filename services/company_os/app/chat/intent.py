"""Speech-act classifier for founder chat.

Questions look up company state. Imperatives become work. Redirects retarget
the slate. A keyword like “update” in “status update” must never queue a task.
"""

from __future__ import annotations

import re

from app.chat.product_build_request import _KICKOFF_BUILD_RE
from app.chat.site_actions import _PAGE
from app.core.local_mode import is_local

_EMAIL_LOOKUP_RE = re.compile(
    r"\b("
    r"(what('?s| is)|where('?s| is)|show me)\s+(my |the |our )?(email|e-mail)( address)?|"
    r"(my |the |our )?(email|e-mail) address|"
    r"what email (do we|do i|should i) use"
    r")\b",
    re.I,
)
_URL_RE = re.compile(r"\b(url|website|site|preview|repo|repository)\b", re.I)
_SOURCE_QA_RE = re.compile(r"\b(?:qa|quality assurance|independent tests?)\b.{0,120}\b(?:stored|source|archive|coding|code)\b|\b(?:stored|source|archive|coding|code)\b.{0,120}\b(?:qa|quality assurance|independent tests?)\b", re.I)
_USERS_RE = re.compile(r"\b(how many users|revenue|mrr|arr|visitors)\b", re.I)
_PUBLISH_RE = re.compile(r"\b(publish|production|go live|send.*(email|tweet))\b", re.I)
_INSTRUCTION_RE = re.compile(
    r"\b("
    r"rewrite|"
    r"draft|"
    r"do not send|don'?t send|"
    r"waitlist[\s-]*only|"
    r"only (a |the )?waitlist|"
    r"(our |the )?competitors?\s+(are|is|:|aren'?t|are not)|"
    r"not (the )?(nyt|nytimes|new york times)"
    r")\b",
    re.I,
)
_CANCEL_RE = re.compile(
    r"\b("
    r"stop that|"
    r"stop (the )?(generic )?discovery|"
    r"cancel (that|this|it|generic|the \w+)|"
    r"(?:cancel|stop|abort|kill) (?:the |my |this |that |current |running )*(?:build|mac task|mac order|research)|"
    r"don'?t do that|"
    r"park (that|this|it|the work)|"
    r"split the work|"
    r"not that(?: work)?"
    r")\b",
    re.I,
)
_POST_SOCIAL_RE = re.compile(
    r"\b("
    r"(post|tweet|publish|share)\b.{0,80}\b(twitter|x\b|linkedin|instagram|facebook|tiktok|social)|"
    r"(tweet this|post this|publish this|post to (twitter|x|linkedin|instagram))"
    r")\b",
    re.I,
)
_SOCIAL_STATUS_RE = re.compile(
    r"\b("
    r"posting isn'?t working|"
    r"posting (is )?(broken|failing|stuck)|"
    r"is posting (ready|working|connected)|"
    r"social (status|ready|connected)|"
    r"can (we|you|i) post|"
    r"why (can'?t|cannot) (we |i )?post"
    r")\b",
    re.I,
)
_SOCIAL_GUIDE_RE = re.compile(
    r"\b("
    r"connect (posting|social|twitter|x\b|linkedin|instagram|zernio)|"
    r"zernio|"
    r"(how (do i|to)|help me).{0,60}(post|twitter|x\b|social|zernio|api key)|"
    r"explain.{0,40}(x\b|twitter|linkedin|reddit|instagram|zernio|posting|social)|"
    r"(i have|paste[d]?|here'?s).{0,40}(zernio|api key)|"
    r"stuck.{0,50}(zernio|twitter|x\b|linkedin|instagram|posting|social)"
    r")\b",
    re.I,
)
_WHY_RE = re.compile(r"\bwhy\b", re.I)
_CASE_REF_RE = re.compile(r"\b(case stud(?:y|ies)|workbook|historical example)\b", re.I)
_GOD_MODE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:turn|switch|set)\s+(on|off)\s+(?:the\s+|my\s+)?god[\s-]*mode(?:\s*\(beta\))?\s*[.!]*\s*$|"
    r"^\s*(?:please\s+)?(enable|disable|activate|deactivate|start|stop)\s+(?:the\s+|my\s+)?god[\s-]*mode\s*[.!]*\s*$", re.I)
# "I want god mode", "give me god mode access", "god mode chahiye": wishes route like "turn on" (early access reply).
_GOD_WISH_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:i|we)\s+(?:want|need|would\s+like|wanna)|can\s+(?:i|we)\s+(?:get|have)|give\s+me|"
    r"(?:mujhe|mereko|mujhko|hume)|let\s+me\s+(?:use|try))\s+(?:to\s+(?:use|try)\s+)?(?:the\s+|your\s+)?"
    r"(?:early\s+access\s+(?:to\s+)?)?go[dr][\s-]*mode(?:\s+(?:access|chahiye|chahie|chaiye|dedo|de\s*do|do))?\s*[.!?]*\s*$|"
    r"^\s*(?:please\s+)?go[dr][\s-]*mode\s+(?:chahiye|chahie|chaiye|access|dedo|de\s*do|enable\s+kar(?:o|\s*do)?)\s*[.!?]*\s*$",
    re.I)
_RESUME_RE = re.compile(
    r"^\s*(?:please\s+)?(?:resume|unpause|un-pause)(?:\s+(?:work|everything|the\s+venture|my\s+venture|autopilot|"
    r"the\s+company|it|now))?\s*[.!]*\s*$", re.I)
_PAUSE_RE = re.compile(
    r"\b(pause|stop everything|halt|(?:stop|freeze)\s+(?:the\s+|my\s+|this\s+)?(?:venture|company|startup))\b", re.I)
# "how do I pause?" / "should I pause the venture?" ask; only an imperative raises the pause card.
_PAUSE_QUESTION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:how|why|what|when|where|which|who|is|are|does|do|did|should|would|will|has|have|"
    r"can\s+i|could\s+i|may\s+i)\b|\b(?:how (?:do|can|would|to)|should (?:i|we))\b|\?\s*$", re.I)
_POLITE_REQUEST_RE = re.compile(r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\b", re.I)
_APPROVE_ACTION_RE = re.compile(
    r"^\s*(?:(?:please|i|we|can you|could you)\s+)*(?:approve|reject)\b", re.I,
)
_DECLINED_APPROVAL_RE = re.compile(
    r"\b(?:do\s+not|don['’]t|without|never)\b[^.!?;]{0,120}\b"
    r"(?:approve|approving|reject|rejecting)\b",
    re.I,
)
_SLATE_RE = re.compile(r"\b(slate|drop \d|do the first|cold email|reorder|defer)\b", re.I)
_LEADS_RE = re.compile(r"\b(waitlist|signups?|who signed)\b", re.I)
_CREDITS_RE = re.compile(
    r"\b(credit(?:s)?\s+(?:balance|left|remaining)|how many credits|venture credits|(?:what(?:'s|\s+is)\s+)?my\s+(?:credit\s+)?balance)\b",
    re.I,
)
_GTM_DIAG_RE = re.compile(
    r"\b(gtm diagnosis|diagnosis bottleneck|what(?:'s| is) (?:our |the )?bottleneck)\b",
    re.I,
)
_CONNECTIONS_RE = re.compile(
    r"\b(connection(?:s)? status|(?:show|list|view|manage) (?:me )?(?:my |our |the )?(?:connections|integrations)|what(?:'s| is| are) (?:already )?connected|are (?:my )?(?:integrations|connections) (?:up|ok|connected)|gmail status)\b",
    re.I,
)
_CONTENT_DRAFTS_RE = re.compile(
    r"\b(content drafts?|list (?:my )?drafts|draft posts?)\b",
    re.I,
)
_SAVED_WORK_RE = re.compile(
    # Match one retrieval question. A later constraint such as "do not start
    # a task" must not make a product-status question about saved source
    # look like a request to retrieve an unrelated task note.
    r"^\s*(?:please\s+)?(?:show|read|open|find|where|what)\b[^.!?]{0,180}"
    r"\b(?:saved|completed|finished)\b[^.!?]{0,180}\b(?:task|work|notes?|draft|output)\b|"
    r"^\s*(?:please\s+)?(?:show|read|open|find|where|what)\b[^.!?]{0,180}"
    r"\b(?:task|work|notes?|draft|output)\b[^.!?]{0,180}\b(?:saved|completed|finished)\b",
    re.I,
)
_RESEARCH_RE = re.compile(r"\b(research|competitors?|market|dossier|demand)\b", re.I)
_AGENTS_RE = re.compile(r"\b(agents?|team|org chart|roster|who.?s on)\b", re.I)
_NEXT_RE = re.compile(r"\b(what.?s next|next up|slate items|todo|to-?do)\b", re.I)
_REFINE_RE = re.compile(r"\b(refine (the )?site|rebuild (the )?page|update (the )?landing)\b", re.I)
_OW_SEARCH_RE = re.compile(r"\b(search (the )?web|openworker search|google|look up)\b", re.I)
_REACH_RE = re.compile(
    r"\b("
    r"youtube|youtu\.be|transcript|"
    r"github|"
    r"twitter|\btweets?\b|"
    r"reddit|"
    r"bilibili|b[- ]?site|"
    r"v2ex|"
    r"xiaohongshu|rednote|小红书|"
    r"xueqiu|雪球|"
    r"linkedin|instagram|facebook|"
    r"rss feed"
    r")\b",
    re.I,
)
_ADVICE_RE = re.compile(
    r"\b(?:what\s+(?:do\s+you|would\s+you|should\s+(?:i|we))\s+(?:suggest|recommend|advise)|suggest|recommend|advice|advise|"
    r"should\s+(?:i|we)|how\s+(?:do|should|can)\s+(?:i|we)|help\s+me|strateg(?:y|ies)|what\s+do\s+you\s+think)\b", re.I)
_CHAT_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz]e|recap|sum\s+up|tl;?dr)\b.{0,60}\b(?:chat|conversation|thread|discussion|everything\s+we|what\s+we(?:'ve)?\s+(?:discussed|talked))"
    r"|\b(?:summary|recap)\s+of\s+(?:the\s+|our\s+|this\s+)?(?:chat|conversation|thread|discussion)", re.I)
_READ_RECAP_RE = re.compile(
    r"^\s*(?:for\s+(?:this|my|our|the)\s+(?:existing\s+)?venture\s*,\s*)?"
    r"(?:(?:please|can you|could you|would you)\s+)?"
    r"(?:recap|catch me up(?: on)?)\s+\S", re.I)
_REACH_URL_RE = re.compile(
    r"https?://[^\s]*(youtube\.com|youtu\.be|github\.com|x\.com|twitter\.com|"
    r"reddit\.com|redd\.it|bilibili\.com|v2ex\.com|xiaohongshu\.com|"
    r"xueqiu\.com|linkedin\.com|instagram\.com|facebook\.com)",
    re.I,
)
_CONTENT_NOUN = r"(?:posts?|tweets?|threads?|captions?|linkedin|twitter|reddit|content|blog|articles?|newsletter|social)"
_CONTENT_DRAFT_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|go ahead and|hey)[,\s]+)*"
    rf"(?:(?:draft|write|compose|generate|brainstorm|come up with|make|create)\b[^.?!]{{0,60}}\b{_CONTENT_NOUN}\b)", re.I)
_CONTENT_OTHER_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you)[,\s]+)*(?:disconnect|unlink|remove)\s+(?:my\s+|the\s+)?"
    r"(?:linkedin|twitter|x|instagram|facebook|reddit|zernio)\b|"
    r"^\s*(?:(?:please|can you|could you)[,\s]+)*(?:share|post|publish)\b.{0,40}\b(?:on|to)\s+(?:reddit|youtube|github)\b|"
    r"^\s*(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:posted|shared|tweeted|published)\b.{0,60}\b"
    r"(?:reddit|twitter|linkedin|instagram|facebook|youtube|x)\b", re.I)
# "from now on call yourself Jarvis", "your name is Jarvis", "rename yourself to Jarvis".
RENAME_RE = re.compile(
    r"^\s*(?:(?:please|ok(?:ay)?|hey|so)[,\s]+)*(?:from now on[,\s]+|going forward[,\s]+)?"
    r"(?:call yourself|you(?:'re| are) (?:now )?called|your name (?:is|will be|should be)(?: now)?|"
    r"(?:i(?:'ll| will|'m going to) )?call you|rename (?:yourself|you)(?: to)?|change your name to|"
    r"be called|go by)\s+(?P<name>[^.!?,;:\n]{1,40}?)(?:\s+(?:again|instead|please|now|too))*\s*[.!]*\s*$", re.I)
VENTURE_RENAME_RE = re.compile(
    r"^\s*(?:(?:please|ok(?:ay)?|hey|so|can you|could you|would you)[,\s]+)*"
    r"(?:rename\s+(?P<target>(?:this|the|my|our)\s+(?:venture|company|startup|business|project))\s+to|"
    r"call\s+(?:this|the|my|our)\s+(?:venture|company|startup|business|project)|"
    r"change\s+(?:(?:this|the|my|our)\s+)?venture\s+name\s+to)"
    r"\b\s*(?P<name>.*?)\s*(?:!|(?<!\.)\.)?\s*$", re.I)
_GTM_DIAGNOSE_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|go ahead and|let'?s)\s+)*(?:"
    r"diagnose\b.{0,40}\b(?:gtm|go[\s-]?to[\s-]?market|growth|acquisition|funnel)\b|"
    r"(?:run|start|do|perform|get|kick off|generate|create|prepare)\s+(?:me\s+)?(?:a\s+|my\s+|the\s+)?(?:(?:gtm|go[\s-]?to[\s-]?market)\s+)?diagnosis\b|"
    r"give\s+(?:(?:me|[\w\'-]{1,40})\s+)?(?:a|the|my)\s+(?:gtm|go[\s-]?to[\s-]?market)\s+diagnosis\b)", re.I)
_OH_RE = re.compile(r"\b(office hours|is this worth building|forcing questions|demand reality|pressure[- ]?test\s+(?:my|our|the|this)\s+idea)\b", re.I)
_CEO_RE = re.compile(r"\b(ceo review|think bigger|scope (expansion|reduction)|rethink this plan)\b", re.I)
_BRAIN_RE = re.compile(
    r"\b("
    r"what do i (need to )?know about|"
    r"brief me (on|about)|"
    r"prepare me for|"
    r"who works at|"
    r"who founded|"
    r"who invested|"
    r"remember (that|this)|"
    r"company brain"
    r")\b",
    re.I,
)

_QUESTION_WORD = re.compile(
    r"\b(who|what|where|when|why|how|which|whose|whom)\b|"
    r"\b(is|are|was|were|does|did|can|could|should|would|do you)\b",
    re.I,
)
_ASK_COMPANY = re.compile(
    r"\b("
    r"status( update)?|"
    r"standup|"
    r"update me|"
    r"who.{0,24}(work|doing|busy)|"
    r"what.{0,24}(status|doing|working|happening|going on)|"
    r"what.{0,80}(blocks?|prevents?)\b|"
    r"how.{0,16}(going|doing)|"
    r"any (blockers?|updates?)|"
    r"need(s)? (me|you|help)|"
    r"current (task|work)|"
    r"being done|"
    r"blockers?"
    r")\b",
    re.I,
)
_RETARGET = re.compile(
    r"\b("
    r"(change|switch|replace|update)\s+(the\s+)?(current|this|that)\s+(task|work|item)|"
    r"(change|switch)\s+\w+'?s\s+(task|work)|"
    r"do this instead|"
    r"instead,?\s+(do|work on|build|write|make)|"
    r"work on .{3,80} instead|"
    r"reassign"
    r")\b",
    re.I,
)
_DO_START = re.compile(
    r"^\s*(@\S+\s+|hey\s+|guys,?\s+|please\s+|can you\s+|could you\s+|would you\s+|go ahead and\s+)*"
    r"(start|run|begin|do(?!\s+(?:not\b|it[.!?]*\s*$))|add|make|build|create|write|ship|fix|remove|delete|hire|launch|"
    r"send|publish|implement|design|instrument|test|recruit|draft|"
    r"rename|move|find|rewrite|update|focus|narrow|prioritize|research)\b",
    re.I,
)
_PRODUCT_CHANGE = re.compile(
    r"\b(change|update|edit)\b.+\b("
    r"page|site|copy|headline|color|price|offer|landing|button|hero|form"
    r")\b",
    re.I,
)
_QUESTION_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:who|what|where|when|why|how|which|is|are|was|were|"
    r"does|did|should|would|do\s+(?:i|we|you|they|he|she))\b",
    re.I,
)
_DIRECTED_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:can|could|would)\s+you\s+"
    r"(?:start|run|begin|do(?!\s+(?:not\b|it[.!?]*\s*$))|add|make|build|create|write|ship|fix|remove|delete|hire|launch|"
    r"send|publish|implement|design|instrument|test|recruit|draft|"
    r"rename|move|find|rewrite|update|edit|change|focus|narrow|prioritize|research)\b",
    re.I,
)
_ANSWER_ONLY = re.compile(
    r"\b(?:answer\s+only|(?:tell|explain)\s+me\s+what\s+(?:review|approval)\s+is\s+required|"
    r"do\s+not\b[^.!?]{0,80}\b(?:create|start|queue|prepare)\s+"
    r"(?:a\s+)?(?:new\s+)?(?:mac\s+)?tasks?)\b|"
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:ask\s+me|recommend|suggest|advise|help\s+me\s+(?:choose|decide|understand|prioriti[sz]e))\b",
    re.I,
)
_OPEN_BUILD_RE = re.compile(
    r"^\s*(?:please\s+)?(?:open|show(?:\s+me)?|go\s+to|navigate\s+to|take\s+me\s+to)\s+"
    r"(?:(?:the|my|this\s+venture(?:'s)?)\s+)?(?:active\s+|current\s+|latest\s+)?"
    r"(?:[A-Za-z0-9_-]+\s+)?build(?:s|\s+run)?(?:\s+(?:page|screen))?\s*[.!]?"
    r"(?:\s+do\s+not\s+start\s+another\s*[.!]?)?\s*$",
    re.I,
)
_START_PRODUCT_BUILD_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|go ahead and|help me|i want to|i need to|i need you to|let'?s)\s+)*"
    r"(?:build|start building|create|make|launch|start running|start|write|implement|develop)\s+"
    r"(?:(?:me|my|our|this|the|a|an)\s+)*"
    r"(?:(?:[A-Za-z0-9_-]+\s+){0,2})?"
    r"(?:startup|company|venture|product|app|application|project|bot|backend|code)\b",
    re.I,
)
_LEAD = (r"^\s*(?:(?:ok(?:ay)?|yes|please|now|so|just|can you|could you|would you|go ahead and|i want to|"
         r"i want you to|i need to|i need you to|help me|let'?s|lets|skip (?:the )?plan(?:ning)?(?: and)?|skip (?:these|this|them|it|(?:the )?questions?)(?: and)?)[,.!\s]+)*")
_START_LANDING_BUILD_RE = re.compile(
    _LEAD + r"(?:build|create|make|generate|start building|prepare|draft|write|design)\s+"
    rf"(?:(?!(?:for|with|from|to|and)\b)[\w-]+\s+)*{_PAGE}\b"
    r"(?=\s*(?:[.!?;,]|$)|\s+(?:for|with|from|to|and|now|please|first)\b)",
    re.I,
)
# "Build my page" is one click on the founder journey; "get customers" goes to GTM. Neither routes to plan review.
_ONE_CLICK_PAGE_RE = re.compile(
    _LEAD + r"(?:build|create|make|generate|start building|prepare|draft|write|design)\s+(?:me\s+|us\s+)?(?:(?:my|our|the|a|this)\s+)?(?:first\s+)?"
    r"(?:landing\s*page|web\s*site|website|page|site)(?:\s+(?:now|please|for me|first))*"
    r"(?:\s+for\s+[^.!?\n]+)?"
    r"(?:\s*(?:,|and)\s*(?:then\s+)?(?:go|move|head|take me)\s+(?:on\s+)?(?:to\s+)?(?:gtm|go[ -]?to[ -]?market|customers))?"
    r"(?:(?:\s*[,.;]\s*|\s+(?:and\s+)?)(?:(?:keep|leave)\s+(?:it|(?:the\s+)?(?:page|site|website))\s+(?:private|unpublished)"
    r"|(?:do\s+not|don['’]?t|never)\s+(?:send\s+(?:(?:any\s+)?e-?mails?|anything|messages?)|publish(?:\s+it)?|(?:do|start)\s+outreach)"
    r"|no\s+(?:e-?mails?|outreach)))*\s*[.!]*\s*$",
    re.I)
_PAGE_BRIEF_RE = re.compile(
    rf"{_LEAD}(?:\w+-\w+\s+)?{_PAGE}(?:\s+for\s+[^.!?\n]+)?\s*[.!]*\s*$", re.I,
)
_GET_CUSTOMERS_RE = re.compile(
    _LEAD + r"(?:(?:get|find|reach)\s+(?:me\s+|us\s+)?(?:my\s+|our\s+|the\s+|some\s+|more\s+)?(?:first\s+)?(?:"
    r"(?:\d+\s+)?(?:(?:customers|users|leads|contacts|prospects|people)\b[^.!?\n]*"
    r"|[^.!?\n]*\b(?:(?:i|we)\s+(?:can|could|should)\s+|to\s+)(?:e-?mail|contact|talk\s+to|reach\s+out\s+to)\b[^.!?\n]*)"
    r"|\d+\s+[^.!?\n]+\s+(?:in|near|around)\s+(?!(?:my|our|the|this|that)\b)[^.!?\n]+)"
    r"|(?:go|move|head)\s+(?:on\s+)?to\s+(?:gtm|go[ -]?to[ -]?market))(?:\s+now)?\s*[.!]*\s*$", re.I)
_FIND_CUSTOMERS_RE = re.compile(
    r"\s*(?:(?:no\s*,?\s*(?:like|i mean)|like|i(?: am|['’]m) interested in)\s+)?(?:gtm|go[ -]?to[ -]?market)"
    r"|\s*(?:i (?:want|need)\s+)?leads?"
    r"|\s*(?:how many|show(?: me)?|list)\s+(?:my\s+|our\s+)?leads"
    r"|\s*(?:who|whom)\s+should\s+(?:i|we)\s+(?:talk\s+to|reach\s+out\s+to|contact|send\s+(?:them|these|the (?:drafts|emails))\s+to)", re.I)
_DECLINE_GTM_RE = re.compile(
    r"\s*(?:no\s+(?:gtm|go[ -]?to[ -]?market)|not now|(?:do not|don['’]?t)\s+do\s+outreach)\s*[.!?]*", re.I)


_LINKEDIN_PEOPLE_RE = re.compile(
    _LEAD + r"(?:find|get|show)\s+(?:me\s+|us\s+)?(?:more\s+|some\s+)?(?:linked\s*in\s+(?:people|profiles|leads|contacts|prospects)"
    r"|(?:people|profiles|leads|prospects)\s+on\s+linked\s*in)(?:\s+(?:for\s+me|today|now))?\s*", re.I)


def linkedin_people_request(text: str) -> bool:
    return bool(_LINKEDIN_PEOPLE_RE.fullmatch((text or "").strip().rstrip(".!?")))


_ICP_RE = re.compile(
    r"\s*(?:(?:so|ok(?:ay)?|but|then|hey|charlie)[,\s]+)*(?:(?:can|could)\s+you\s+(?:tell\s+me\s+|help\s+me\s+(?:figure\s+out\s+|decide\s+)?)?)?"
    r"(?:(?:who|whom)\s+(?:should|do|can|could|would)\s+(?:i|we)\s+(?:reach\s+out\s+to|contact\s+first|talk\s+to\s+first|target|sell\s+to|go\s+after|approach|pitch\s+to)"
    r"|(?:who|what)\s*(?:is|are|'?s|['’]re)\s+(?:my|our)\s+(?:icp|ideal\s+customers?(?:\s+profile)?|target\s+(?:customers?|audience|market|users?)|customers|audience|buyers)"
    r"|who\s+should\s+(?:my|our)\s+(?:icp|ideal\s+customers?|target\s+(?:customers?|audience)|customers|audience)\s+be"
    r"|(?:find|define|figure\s+out|pick|choose)\s+(?:my|our)\s+(?:icp|ideal\s+customers?(?:\s+profile)?|target\s+(?:customers?|audience)))"
    r"(?:\s+(?:for\s+(?:this|my|our)\s+(?:idea|venture|app|product|startup)|first|now))?"
    r"(?:\s*[?.!,]*\s*(?:and\s+)?(?:find|get|show)\s+(?:me\s+|us\s+)?(?:some\s+)?(?:people|customers|leads|them))?\s*", re.I)


def icp_question(text: str) -> bool:
    """"Who should I reach out to?", "who is my ICP?": Charlie proposes customer groups to pick from."""
    return bool(_ICP_RE.fullmatch((text or "").strip().rstrip(".!?")))


def _public_people_search(raw: str) -> bool:
    from app.chat.guard import _affirmative_clauses, _outreach_requests

    return bool(re.search(r"\b(?:public(?:ly)?|sources?)\b", raw, re.I)
                and re.search(r"\b(?:names?|customers?|contacts?|leads?|prospects?|people)\b", raw, re.I)
                and not re.search(r"\bcompetitors?\b", raw, re.I)
                and not _outreach_requests(raw) and not discovery_copy_request(raw)
                and any(re.match(_LEAD + r"(?:find|identify|research)\b", clause, re.I)
                        for clause in _affirmative_clauses(raw)))


def find_customers_request(text: str) -> bool:
    from app.chat.idea_bridge import request_text

    raw = request_text(text or "").rstrip(".!?")
    return bool((_GET_CUSTOMERS_RE.fullmatch(raw) and re.match(_LEAD + r"(?:go|move|head)\b", raw, re.IGNORECASE))
                or _FIND_CUSTOMERS_RE.fullmatch(raw) or _LINKEDIN_PEOPLE_RE.fullmatch(raw)
                or icp_question(raw) or requested_customer_search(raw))


def requested_customer_search(text: str) -> bool:
    from app.chat.guard import _affirmative_clauses, _outreach_requests
    from app.chat.idea_bridge import request_text
    from app.chat.llm_router import _command_evidence
    from app.chat.search_recovery import search_forbidden
    from app.gtm.discovery import SEARCH_REQUEST_RE, _VENTURE_REFERENCE, request_audience
    from app.intake.smart import audience_phrase

    raw = request_text(text or "").rstrip(".!?")
    if search_forbidden(raw) or _outreach_requests(raw) or discovery_copy_request(raw):
        return False
    if re.fullmatch(_LEAD + r"(?:(?:retry|restart|rerun)\s+(?:(?:the|my|our)\s+)?(?:customer|lead) search|"
                    r"(?:run|start)\s+(?:(?:the|my|our)\s+)?(?:customer|lead) search again)(?:\s+now)?", raw, re.I):
        return True
    evidence = _command_evidence(raw)
    command = re.sub(_LEAD, "", evidence, flags=re.I)
    target = request_audience(command)
    requested_target = target or SEARCH_REQUEST_RE.sub("", _VENTURE_REFERENCE.sub("", command))
    if re.search(r"^(?:(?:my|our|the)\s+)?(?:saved|stored)\b|"
            r"\b(?:you|we)\s+(?:have\s+)?(?:already\s+)?(?:found|identified)\b", requested_target, re.IGNORECASE):
        return False
    audience = audience_phrase(target, inferred=True)
    # shortcut: infer searches from the leading affirmative clause; use grounded spans for mixed prose.
    return bool((_GET_CUSTOMERS_RE.fullmatch(evidence) and re.match(r"(?:get|find|reach)\b", command, re.I) and (audience or not target))
                or _LINKEDIN_PEOPLE_RE.fullmatch(evidence) or re.fullmatch(r"i (?:want|need)\s+leads?", evidence, re.I)
                or (_public_people_search(evidence) and audience)
                or (not target and _VENTURE_REFERENCE.search(command) and SEARCH_REQUEST_RE.match(command)
                    and re.search(r"\b(?:customers|users|leads|contacts|prospects|people)\b", command, re.I))
                or any(SEARCH_REQUEST_RE.match(clause) and audience_phrase(request_audience(clause), inferred=True)
                    for clause in _affirmative_clauses(evidence)[:1]))


# "What are the different ways I could get my first 10 ...": a request for ideas, answered in chat, not a search command.
_STRATEGY_Q_RE = re.compile(
    r"\b(?:what\s+(?:are|r)\s+(?:the\s+|some\s+)?(?:different\s+|other\s+|best\s+|possible\s+|main\s+)?(?:ways|options|ideas|strategies|approaches)"
    r"|how\s+(?:could|can|should|would|might|do)\s+(?:i|we)|any\s+(?:ideas|tips|suggestions)|ways\s+to|ideas\s+(?:for|to)|brainstorm"
    r"|(?:give|suggest|share)\s+(?:me\s+|us\s+)?(?:some\s+|a\s+few\s+)?(?:ideas|ways|options|strategies|tactics))\b", re.I)
_STRATEGY_TOPIC_RE = re.compile(r"\b(?:first\s+(?:\d+|few|hundred)|users?|customers?|clients?|buyers?|people|leads?|signups?|traction)\b", re.I)


def strategy_question(text: str) -> bool:
    raw = text or ""
    return not find_customers_request(raw) and bool(_STRATEGY_Q_RE.search(raw) and _STRATEGY_TOPIC_RE.search(raw))


def declines_gtm(text: str) -> bool:
    return bool(_DECLINE_GTM_RE.fullmatch(text or ""))


_RETRY_LEAD = r"^\s*(?:(?:please|ok(?:ay)?|now|so|can you|could you|would you|let'?s|go ahead and|yes)[,\s]+)*"
_RETRY_JOB_RE = re.compile(
    _RETRY_LEAD + r"(?:(?:retry|re-?run|redo|run)\s+(?:the\s+|my\s+)?(?:validation|research|clarification)(?:\s+(?:again|now|please))*"
    r"|(?:try|run)\s+(?:the\s+|my\s+)?(?:validation|research)\s+again"
    r"|(?:retry|try\s+again|try\s+that\s+again|retry\s+(?:it|that)|run\s+it\s+again|do\s+it\s+again|"
    r"give\s+it\s+another\s+(?:try|go|shot)|one\s+more\s+try))(?:\s+(?:now|please))*\s*[.!]*\s*$",
    re.I,
)
_START_GETTING_USERS_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:start|help me start|let'?s start)\s+getting\s+users|"
    r"(?:i want|help me (?:with|start)|start)\s+(?:gtm|go[ -]?to[ -]?market)|"
    r"(?:i (?:want|need|would like|'d like)|let'?s|(?:can|could|would) you help me)\s+(?:to\s+)?"
    r"(?:(?:do|start|begin|work on|focus on|tackle|plan|figure out|get started|get going)\s+)?(?:with\s+|on\s+)?"
    r"(?:the\s+|my\s+|our\s+)?(?:gtm|go[ -]?to[ -]?market)\b)",
    re.I,
)
_NEXT_GET_USERS_RE = re.compile(
    r"\b(?:what\s+(?:should|can|do)\s+i\s+do\s+next|what(?:'s|\s+is)?\s+next|"
    r"next\s+(?:step|action))\b[^.!?]{0,100}\b"
    r"(?:get|find|reach|acquire)\s+(?:my\s+|our\s+|the\s+|more\s+|first\s+)?users\b",
    re.I,
)
_LATEST_ERROR_RE = re.compile(
    r"^\s*(?:please\s+)?(?:show(?: me)?|check|what(?:'s| is))\s+"
    r"(?:my|our|the)?\s*(?:latest|last|most recent)\s+(?:saved\s+)?error\s*[.!?]?\s*$",
    re.I,
)
_LATEST_ERROR_RECOVERY_RE = re.compile(
    r"^\s*(?:please\s+)?(?:show(?: me)?|check|what(?:'s| is))\s+"
    r"(?:my|our|the)?\s*(?:latest|last|most recent)\s+(?:saved\s+)?error\s+"
    r"and\s+(?:tell|show)\s+me\s+(?:the\s+)?next\s+(?:recovery\s+)?(?:action|step)"
    r"\s*[.!?]?(?:\s+answer\s+only[;.]?)?"
    r"(?:\s+(?:do\s+not|don['’]t)\s+(?:start|prepare|run|approve)\b[^.!?]*[.!?]?)?\s*$",
    re.I,
)

_WHY_FAILED_RE = re.compile(
    r"^\s*(?:(?:so|ok(?:ay)?|hey)[,\s]+)*(?:"
    r"why\s+(?:did|has|does|is|was)\s+(?:the\s+|my\s+)?(?:it|that|this|validation|research|plan(?:ning)?|clarification|job)\s+"
    r"(?:fail(?:ed)?|stop(?:ped)?|break|broke|not\s+(?:finish|work|complete)|(?:didn'?t|did\s+not)\s+(?:finish|work|complete))"
    r"|what\s+(?:went\s+wrong|happened)\s+(?:with|to|in)\s+(?:the\s+|my\s+)?(?:validation|research|plan(?:ning)?|clarification|job|it|that)"
    r"|(?:explain|tell\s+me)\s+(?:why\s+)?(?:the\s+|my\s+)?(?:validation|research|plan(?:ning)?|job|it|that)\s+(?:failed|fail)"
    r")\s*[?.!]*\s*$",
    re.I,
)

VENTURE_SCREEN_ROUTES = {
    "cockpit": "", "dashboard": "", "idea": "idea", "current idea": "idea/current", "idea summary": "idea/current", "now": "now",
    "work": "tasks", "tasks": "tasks", "team": "org", "website": "site",
    "research": "research", "market": "market", "feedback": "feedback",
    "plan": "charter", "execution": "plan", "builds": "builds",
    "analytics": "analytics", "import": "import", "evolution": "evolution",
    "people": "people", "connections": "connections", "settings": "settings",
    "experiments": "experiments", "market map": "market-map", "content": "content",
    "gtm": "gtm", "go to market": "gtm", "go-to-market": "gtm",
    "charter": "charter", "simulate": "simulate", "simulation": "simulate", "suggestions": "suggestions",
    "org chart": "org", "org": "org", "needs you": "now", "right now": "now", "build": "builds",
    "research report": "research", "research dossier": "research", "report": "research",
    "context": "context", "priorities": "context", "startup review": "context",
    "linkedin": "linkedin", "linkedin people": "linkedin",
    "leads": "leads", "my leads": "leads", "market map": "market-map",
    "account": "account", "download": "download", "ads": "ads", "lab": "lab", "marketing": "marketing",
    "media": "media", "community": "community", "guide": "guide", "capabilities": "capabilities",
    "all ventures": "ventures", "new venture": "new", "company connections": "company connections",
    "local settings": "local settings",
    "local capabilities": "local capabilities",
    "support bundle": "support bundle",
}
# Company-level pages live outside /v/<id>; they take the venture as a query.
COMPANY_SCREEN_PATHS = {"content": "/content", "gtm": "/gtm", "account": "/account", "download": "/download", "ads": "/ads",
                        "ventures": "/", "new": "/new", "company connections": "/connections",
                        "lab": "/lab", "marketing": "/marketing", "media": "/media", "community": "/community", "capabilities": "/guide", "local settings": "/settings/local", "local capabilities": "/settings/local/capabilities",
                        "support bundle": "/settings/local/support"}
_OPEN_SCREEN_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:open|show(?:\s+me)?|go\s+to|take\s+me\s+to|navigate\s+to)\s+"
    r"(?:(?:the|my|this\s+venture(?:'s)?)\s+)?"
    r"(?P<screen>[a-z]+(?:[\s-]+[a-z]+){0,2}?)(?:\s+(?:page|screen|tab))?"
    r"(?:\s+in\s+Supymem)?\s*[.!?]?(?:\s+do\s+not\s+modify\s+data\s*[.!]?)?\s*$",
    re.I,
)
_OPEN_NAMED_SCREEN_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:open|show(?:\s+me)?|go\s+to|take\s+me\s+to|navigate\s+to)\s+"
    r"(?P<venture>[A-Za-z0-9][A-Za-z0-9 _-]{0,79}?)\s+"
    r"(?P<screen>" + "|".join(VENTURE_SCREEN_ROUTES) + r")"
    r"(?:\s+(?:page|screen|tab))?(?:\s+in\s+Supymem)?\s*[.!?]?"
    r"(?:\s+do\s+not\s+modify\s+data\s*[.!]?)?\s*$",
    re.I,
)
_OPEN_AND_DESCRIBE_RE = re.compile(
    r"^(?P<command>.+?)\s+and\s+(?:tell|show|report)\s+(?:me\s+)?"
    r"(?P<question>(?:what|which)\b.{1,200}?)\s*[.!]?\s*$",
    re.I,
)
_VIEW_EFFECT = re.compile(r"\b(?:click|press|select|clear|type|write|enter|scroll|delete|edit|change|modify|start|run|create|submit|launch)\b", re.I)
_OPEN_AND_BUILD_EVIDENCE_RE = re.compile(
    r"^(?P<command>.+?)\s+and\s+(?:report|tell|show)\s+(?:me\s+)?"
    r"(?:what|which)\s+build\s+evidence\s+is\s+saved\s+for\s+"
    r"(?P<venture>[A-Za-z0-9][A-Za-z0-9 _-]{0,79}?)\s*[.!]?"
    r"(?:\s+do\s+not\s+start\s+or\s+modify\s+any\s+build\s*[.!]?)?\s*$",
    re.I,
)


def requested_venture_screen(text: str, venture_name: str | None = None) -> str | None:
    """Return an allowlisted venture-local route for an explicit navigation command."""
    from app.core.local_mode import is_local

    command = (text or "").strip()
    if re.search(r"\bsupymem\b", command, re.I):
        from app.chat.mac_offers import MAC_CONTROL_LEADIN

        command = MAC_CONTROL_LEADIN.sub("", command)
    # Read-only constraints describe how to answer. They do not turn a saved
    # Supymem view into a request to control the installed Mac application.
    constraint = re.compile(
        r"(?:[.!?;]\s*)?(?:answer only|do not (?:start|create|queue|modify|change|delete|edit|run)\b[^.!?]*)[.!?]?\s*$",
        re.I,
    )
    while constraint.search(command):
        command = constraint.sub("", command).strip()
    build_report = _OPEN_AND_BUILD_EVIDENCE_RE.fullmatch(command)
    if build_report is not None:
        named = " ".join(build_report.group("venture").casefold().split())
        if venture_name is not None and named != " ".join(venture_name.casefold().split()):
            return None
        command = build_report.group("command")
    else:
        described = _OPEN_AND_DESCRIBE_RE.fullmatch(command)
        if described is not None:
            if _VIEW_EFFECT.search(described.group("question")):
                return None
            command = described.group("command")
    match = _OPEN_SCREEN_RE.fullmatch(command)
    route = VENTURE_SCREEN_ROUTES.get(" ".join(match.group("screen").lower().split())) if match else None
    if route is not None:
        if route in {"local settings", "local capabilities", "support bundle"} and not is_local():
            return None
        return route if build_report is None or route == "builds" else None
    match = _OPEN_NAMED_SCREEN_RE.fullmatch(command)
    if match is None:
        return None
    named = " ".join(match.group("venture").casefold().split())
    if venture_name is not None and named != " ".join(venture_name.casefold().split()):
        return None
    route = VENTURE_SCREEN_ROUTES.get(match.group("screen").lower())
    return None if route in {"local settings", "local capabilities", "support bundle"} and not is_local() else route


def looks_like_recap_request(text: str) -> bool:
    return bool(_READ_RECAP_RE.match(text.strip()))


def looks_like_question(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.endswith("?"):
        return True
    return bool(_QUESTION_WORD.search(raw) or _ASK_COMPANY.search(raw) or looks_like_recap_request(raw))


def looks_like_instruction(text: str) -> bool:
    """True when the founder is directing work, not looking up company state."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _QUESTION_LEAD.match(raw):
        return False
    if _ANSWER_ONLY.search(raw) and not (_DO_START.match(raw) or _DIRECTED_REQUEST.match(raw)):
        return False
    if _DIRECTED_REQUEST.match(raw):
        return True
    if _INSTRUCTION_RE.search(raw):
        return True
    if _DO_START.search(raw) or _PRODUCT_CHANGE.search(raw):
        return True
    return False


def looks_like_cancel(text: str) -> bool:
    return bool(_CANCEL_RE.search((text or "").strip()))


_PLAN_LEAD = r"^\s*(?:(?:please|ok(?:ay)?|now|so|go ahead and|can you|could you|would you|let'?s|i'?d like you to)[,\s]+)*"
_PLAN_TAIL = r"(?:\s+(?:now|please|again|from (?:the |my |our )?research))*\s*[.!?]*$"
_PLAN_NEG = re.compile(r"\b(?:don['’]?t|do not|not yet|hold off|wait|never|without|no need|stop)\b", re.I)
_PLAN_WORD = re.compile(r"\bplan\b", re.I)
_NATIVE_PLAN_REVISION_RE = re.compile(
    _PLAN_LEAD + r"(?:revise|replace|update|prepare|draft|create)\b[^?]*\bplan\b", re.I)
_NATIVE_PLAN_SCOPE_RE = re.compile(
    r"\bcoding\.subscription_static\b|\bnative\s+subscription(?:\s+coding)?\s+plan\b", re.I)
_NATIVE_PLAN_DECLINE_RE = re.compile(
    r"\b(?:do not|don['’]?t|never)\s+(?:revise|replace|update|prepare|draft|create|generate)\b[^.!?]{0,80}\bplan\b", re.I)
# shortcut: single explicit edits only, use semantic routing for longer or compound plan requests.
_PLAN_REVISION_RE = re.compile(
    _PLAN_LEAD + r"(?:revise|update|change)\s+(?:the|my|our|this)\s+(?:(?:execution|build|startup)\s+)?plan"
    r"\s*(?::|\s+(?:to|so|with)\b)\s*[^.!?;\n]+[.!]?\s*$", re.I)


def native_plan_revision_request(raw: str) -> bool:
    return bool(_NATIVE_PLAN_REVISION_RE.match(raw) and _NATIVE_PLAN_SCOPE_RE.search(raw)
                and not _NATIVE_PLAN_DECLINE_RE.search(raw))


def local_plan_revision_request(raw: str) -> bool:
    return is_local() and bool(_PLAN_REVISION_RE.match(raw) and not _PLAN_NEG.search(raw))


_PLAN_NONE_RE = re.compile(
    r"^\s*(?:i\s+(?:don['’]?t|do not)\s+have\s+any|i\s+have\s+no|no)\s+(?:existing\s+)?(?:tools?|resources?|assets?)\b[^?]*$", re.I)
_PLAN_GENERATE_RE = re.compile(
    _PLAN_LEAD + r"(?:(?:make|generate|create|draft|prepare|write)\s+(?:me\s+)?(?:the|my|our|a|another)\s+(?:(?:new|fresh|different|another|provisional|execution|build|startup)\s+)*plan"
    r"|(?:retry|redo|re-?generate|re-?run)\s+(?:(?:the|my|our)\s+)?(?:(?:execution|build|startup)\s+)?plan(?:ning)?"
    r"|(?:continue|proceed|move\s+on|go\s+on|go)\s+(?:on\s+)?to\s+(?:the\s+)?plan(?:ning)?"
    r"|plan\s+it|plan\s+this(?:\s+out)?|turn\s+(?:this|the\s+research)\s+into\s+a\s+plan)"
    r"(?:\s+(?:from|using|for)\s+[^\n]+)?" + _PLAN_TAIL, re.I)
_PLAN_APPROVE_RE = re.compile(
    _PLAN_LEAD + r"(?:(?:approve|accept|sign\s+off\s+on)\s+(?:the|this|my|our)\s+(?:(?:execution|build)\s+)?plan"
    r"|(?:looks\s+good|lgtm|that\s+works)[,.!\s]+(?:go\s+ahead\s+and\s+)?approve(?:\s+(?:it|the\s+plan|this))?)" + _PLAN_TAIL, re.I)
# A retry/replan request anywhere in the message ("I clicked retry, I want planning to start again").
_PLAN_RETRY_ANY_RE = re.compile(
    r"\b(?:re-?plan(?:ning)?|(?:retry|redo|re-?run|re-?start|re-?generate|try)\s+(?:(?:the|my|our)\s+)?(?:(?:execution|build|startup)\s+)?plan(?:ning)?"
    r"|plan(?:ning)?\s+(?:to\s+|should\s+|will\s+)?(?:start|run|try|restart|begin)s?\s+again|plan(?:ning)?\s+again)\b", re.I)
_PLAN_QUESTION_LEAD = re.compile(r"^\s*(?:did|does|do|has|have|why|how|is|was|were|what|when)\b", re.I)
_PLAN_QUOTE_RE = re.compile(
    _PLAN_LEAD + r"(?:(?:estimate|quote|price)\s+(?:the\s+)?plan(?:ning)?|how\s+much\s+(?:will|would|does)\s+(?:the\s+)?plan(?:ning)?\s+(?:cost|take)"
    r"|(?:what(?:'s|\s+is)\s+)?the\s+plan(?:ning)?\s+(?:estimate|quote|cost))[^.!]*[.!?]*$", re.I)
_NEXT_TASK_RE = re.compile(
    _PLAN_LEAD + r"(?:(?:start|do|run|begin|kick\s+off|work\s+on)\s+(?:the\s+)?next\s+(?:task|work\s+item|item)"
    r"|what(?:'s|\s+is)\s+(?:the\s+)?next\s+(?:task|work\s+item))(?:\s+(?:now|please))*\s*[.!?]*$", re.I)


def _plan_chain_intent(raw: str) -> str | None:
    if native_plan_revision_request(raw) or local_plan_revision_request(raw):
        return "plan_revision"
    if _PLAN_NONE_RE.match(raw):
        return "plan_resources_none"
    if _PLAN_NEG.search(raw):
        # "don't approve the plan yet" answers, it never acts.
        return "assistant" if _PLAN_WORD.search(raw) else None
    for pattern, intent in ((_PLAN_APPROVE_RE, "plan_approve"), (_PLAN_GENERATE_RE, "plan_generate"),
                            (_PLAN_QUOTE_RE, "plan_quote"), (_NEXT_TASK_RE, "work_start")):
        if pattern.match(raw):
            return intent
    if _PLAN_RETRY_ANY_RE.search(raw) and not _PLAN_QUESTION_LEAD.match(raw):
        return "plan_generate"  # always a confirmation card, never a claim that planning is queued
    return None


_EXP_NOUN = r"(?:tests?|experiments?|interviews?|surveys?|outreach|pilot|smoke test|landing page test)"
_EXP_LIST_RE = re.compile(
    r"\b(?:what|which|show|list|any|my|our)\b.{0,30}\bexperiments?\b|\bexperiments?\b.{0,20}\b(?:running|status|so far|ledger)\b", re.I)
_EXP_REVIEW_RE = re.compile(
    r"\bwhat (?:have )?we(?:'ve| have)? learn(?:ed|t)\b|\breview (?:what we learned|the (?:experiments?|results?))\b", re.I)
# shortcut: explicit English planning commands only, use structured intent for indirect planning requests.
_EXP_PLAN_RE = re.compile(
    r"^\s*(?:please\s+)?(?:design|plan|prepare|draft|write)\s+(?:(?:a|an|the|my|our)\s+)?"
    r"(?:(?!landing\s*page\b(?!\s+test\b))[\w-]+\s+){0,4}(?:test|experiment|interview plan)\b", re.I)
_REQUEST_COMMAND_RE = re.compile(r"\s*(?:request form:|reply to request req_[A-Za-z0-9_-]+:)", re.I)
_EXP_START_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:start|begin|kick off)\s+(?:the\s+|this\s+|that\s+|my\s+)?(?:next\s+)?(?:experiment|test)\b"
    r"|i(?:'m| am) (?:starting|running|about to run) (?:the\s+|this\s+)?(?:experiment|test|interviews?))", re.I)
_EXP_RUN_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|go ahead and|let'?s)\s+)*"
    r"(?:run|start|do)\s+(?P<title>[^.!?\n]+?)\s*[.!]*\s*$", re.I)


def references_prepared_experiment(text: str) -> bool:
    request = _EXP_RUN_RE.match(text)
    return bool(request and re.fullmatch(r"(?:it|this|that)(?:\s+for\s+me)?", request.group("title"), re.I))


_EXP_RECORD_RE = re.compile(
    rf"^\s*(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:ran|run|finished|completed|did|done)\s+(?:the|that|our|my|a|an)?\s*{_EXP_NOUN}"
    rf"|\b(?:the|our|my)\s+{_EXP_NOUN}\s+(?:went|was|were|came back|turned out)\b"
    r"|^\s*(?:nobody|no ?one)\s+(?:signed up|replied|responded|clicked|bought|answered|showed up|cared)\b"
    r"|^\s*(?:zero|no)\s+(?:sign ?ups?|replies|responses|reply|takers)\b"
    r"|\b(?:nobody|no ?one)(?:'s|\s+is|\s+has|\s+have|\s+are)?\s+(?:really\s+|actually\s+|even\s+|been\s+)?"
    r"(?:using|signed|signing|clicking|buying|paying|replying|responding|coming|visiting|interested|cares?|cared)\b"
    r"|^\s*(?:i|we)\s+(?:got|have|had)\s+(?:zero|no|0)\s+(?:sign ?ups?|users|customers|replies|responses|sales|"
    r"traffic|visitors)\b|^\s*(?:zero|no)\s+(?:users|customers|traffic|sales|visitors)\b", re.I)


_COUNT = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|none|nobody|no\s?one|all|both|half|most|several|a\s+few)"
_SAID = (r"(?:said|says|replied|responded|agreed|signed|declined|rejected|refused|wanted|want|answered|told|paid|bought|"
         r"clicked|showed|turned\s+down|would\s+(?:pay|buy|use|sign|switch|try|not)|won'?t\s+(?:pay|buy|use))")
_EXP_RESULT_RE = re.compile(
    rf"^\s*(?:results?\s*:\s*)?(?:i|we)(?:'ve|\s+have)?\s+(?:just\s+)?(?:called|phoned|emailed|e-mailed|interviewed|talked\s+to|spoke\s+(?:to|with)|"
    rf"messaged|dm'?d|surveyed|asked|pinged|met\s+with|reached\s+out\s+to)\s+{_COUNT}\b|"
    rf"\b{_COUNT}\s+(?:of\s+(?:the\s+|them\s+|\d+\s+)?)?(?:\w+\s+){{0,3}}?{_SAID}\b|"
    r"\b(?:the\s+|our\s+|my\s+)?(?:test|experiment|interviews?|calls?|survey|outreach|pilot)s?\s+"
    r"(?:results?|outcomes?)\s*(?:is|are|was|were)?\s*[:\-]", re.I)
# "yes, log the results" / "log them": the result is the founder's previous message.
EXP_LOG_FOLLOWUP_RE = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|yup|ok(?:ay)?|sure|please|go\s+ahead|do\s+it|alright)[,.!\s]+)*"
    r"(?:(?:log|record|save|add|store|track)\s+(?:them|it|that|those|these|this|all\s+(?:of\s+)?that|"
    r"(?:the|those|these)\s+(?:results?|outcomes?|calls?|answers?|numbers?|test)(?:\s+(?:now|please))?)"
    r"|go\s+ahead\s+and\s+(?:log|record|save)(?:\s+(?:them|it|that))?)"
    r"(?:\s+(?:now|please|on\s+the\s+experiment))*\s*[.!]*\s*$", re.I)


_EXP_FROM_REVIEW_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|so|yes|please|now|alright)[,\s]+)*"
    r"(?:(?:let'?s|let\s+us|we\s+should|i(?:'d|\s+would)\s+(?:like|love)\s+to|i\s+want\s+to|i(?:'m|\s+am)\s+going\s+to|can\s+we|can\s+you)\s+)?"
    r"(?:(?:run|try|do|start|begin|kick\s+off)\s+(?:out\s+)?(?:the|that|this|our|my)\s+(?:(?:proposed|suggested|first|recommended|saved)\s+)?"
    r"(?:test|experiment)\b(?:\s+(?:from|in|of|suggested\s+in|proposed\s+in)\s+(?:the\s+)?(?:review|validation|report))?"
    r"|(?:run|try|do|start)\s+(?:a|the|that)\s+test\s+(?:from|in)\s+(?:the\s+)?(?:review|validation|report))", re.I)


def _experiment_intent(raw: str) -> str | None:
    """Experiment ledger phrases; a question or an instruction never records a result."""
    if re.fullmatch(r"\s*(?:show|list)(?: my| your)? requests[.!]?\s*", raw, re.I) or _REQUEST_COMMAND_RE.match(raw):
        return "experiment_list"
    if re.search(r"\bmarket (?:beliefs|map)\b|\btest (?:my|our|these) beliefs\b", raw, re.I):
        return "experiment_list"
    if _SOURCE_QA_RE.search(raw):
        return None
    if _EXP_FROM_REVIEW_RE.match(raw) and not _QUESTION_LEAD.match(raw) and not raw.rstrip().endswith("?") \
            and re.search(r"\bfrom\s+(?:the\s+)?(?:review|validation|report)\b|"
                          r"\b(?:proposed|suggested|recommended|saved)\s+(?:test|experiment)\b", raw, re.I):
        return "experiment_from_review"
    if _EXP_START_RE.search(raw) or _EXP_PLAN_RE.search(raw) or references_prepared_experiment(raw):
        return "experiment_start"
    if (request := _EXP_RUN_RE.match(raw)) and re.fullmatch(
            r"(?:(?:a|an|the|my|our)\s+)?experiments?", request.group("title"), re.I):
        return "experiment_list"
    if re.fullmatch(r"\s*review\s+(?:my|your|our|the)\s+(?:tests|experiments)[.!]?\s*", raw, re.I):
        return "experiment_list"
    if _EXP_REVIEW_RE.search(raw):
        return "experiment_review"
    from app.core.local_mode import is_local

    if (is_local() and _QUESTION_LEAD.match(raw) and re.search(r"\bnext\b", raw, re.I)
            and re.search(r"\b(?:results?|observations?)\b", raw, re.I)
            and re.search(rf"\b{_EXP_NOUN}\b", raw, re.I)):
        return "experiment_review"
    local_report = False
    if is_local():
        from app.experiments.ledger import reports_performed_test

        local_report = bool(re.search(rf"\b{_EXP_NOUN}\b", raw, re.IGNORECASE)) and reports_performed_test(raw)
    if (_EXP_RECORD_RE.search(raw) or _EXP_RESULT_RE.search(raw) or EXP_LOG_FOLLOWUP_RE.match(raw) or local_report) \
            and not _QUESTION_LEAD.match(raw) and not raw.rstrip().endswith("?"):
        return "experiment_record"
    if _EXP_LIST_RE.search(raw):
        return "experiment_list"
    return None


LOOKUP_INTENTS = frozenset(
    {
        "research.competitors",
        "experiment_list",
        "experiment_review",
        "plan_quote",
        "factual",
        "status",
        "assistant",
        "pause_help",
        "why",
        "money",
        "leads",
        "research",
        "agents",
        "whats_next",
        "social_status",
        "office_hours",
        "brain_brief",
        "social_guide",
        "general",
        "open_build",
        "open_screen",
        "gtm.diagnosis",
        "credits.balance",
        "connections.status",
        "connections.connect",
        "connections.disconnect",
        "content.drafts",
        "outreach.drafts",
        "saved_work",
        "gtm_review",
        "latest_error",
        "content.draft",
    }
)
WORK_INTENTS = frozenset(
    {
        "idea.pressure_test.prepare",
        "research.start",
        "experiment.export_sheet",
        "research.update_and_rerun",
        "experiment_record",
        "experiment_start",
        "experiment_from_review",
        "retry_job",
        "outreach_draft",
        "email_revision",
        "plan_resources_none",
        "plan_generate",
        "plan_approve",
        "work_start",
        "change",
        "retarget",
        "cancel",
        "refine_site",
        "site.rewrite",
        "waitlist.enable",
        "waitlist.close",
        "manager.rename",
        "venture.rename",
        "site.publish",
        "pause",
        "resume",
        "god_on",
        "god_off",
        "send_off",
        "mac_prepare",
        "approve",
        "slate_override",
        "social_post",
        "social_connect",
        "ceo_review",
        "openworker_search",
        "agent_reach",
        "source_qa",
        "start_product_build",
        "build.tool.start",
        "pagebuilder.build",
    }
)

_CLAUSE_SPLIT = re.compile(
    r"(?<=[?])\s+|"
    r"(?<=[.!])\s+(?=(?:Also|And|Please|Rewrite|Draft|Update|Change|Cancel|Stop|Do )\b)",
    re.I,
)
_ANSWER_FORMAT_LEAD = re.compile(
    r"^(?:distinguish|compare|explain|summarize|clarify|state|cite|list)\b", re.I
)
_QUESTION_REPLY_SPLIT = re.compile(
    r"(?<=[.!?;])\s+|\n+|,\s+|\s+(?=(?:and\s+also|also|and|aur|phir)\s+)", re.I,
)


def _split_clauses(text: str, *, question_reply: bool = False) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    if _REQUEST_COMMAND_RE.match(raw):
        return [raw]
    splitter = _QUESTION_REPLY_SPLIT if question_reply else _CLAUSE_SPLIT
    parts = [p.strip() for p in splitter.split(raw) if p and p.strip()]
    if question_reply:
        return parts or [raw]
    from app.chat.llm_router import _command_evidence
    evidence = _command_evidence(raw, preserve_length=True)
    boundaries = {match.end() for match in splitter.finditer(raw)}
    boundaries.update(match.end() for match in re.finditer(r"(?<=[.!])\s+", raw)
                      if _DO_START.match(evidence[match.end():]) or _DIRECTED_REQUEST.match(evidence[match.end():]))
    positions = sorted({0, len(raw), *boundaries})
    parts = [raw[start:end].strip() for start, end in zip(positions, positions[1:]) if raw[start:end].strip()]
    # "Do not send/publish" is a constraint on the preceding work, not a new
    # speech act. Keep it attached so routing and execution retain the safety
    # scope the founder supplied.
    merged: list[str] = []
    for part in parts:
        if merged and re.match(r"^(?:do not|don'?t)\b", part, re.I):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged or [raw]


def _question_with_answer_format_followup(text: str) -> bool:
    """Keep answer-format directions attached to a question, not the work queue.

    A trailing prohibition may list several actions ("do not send, publish,
    or start a task"), so checking only for the literal "do not start a task"
    is insufficient. A positive operation in the follow-up still splits as
    real work, e.g. "What changed? Draft an email, but do not send it."
    """
    clauses = _split_clauses(text)
    if len(clauses) < 2 or not looks_like_question(clauses[0]) or looks_like_instruction(clauses[0]):
        return False
    for clause in clauses[1:]:
        if not _ANSWER_FORMAT_LEAD.match(clause):
            return False
        positive = re.split(r"\b(?:do not|don'?t)\b", clause, maxsplit=1, flags=re.I)[0]
        if _DO_START.search(positive) or _PRODUCT_CHANGE.search(positive):
            return False
    return True


def discovery_copy_request(text: str) -> bool:
    if re.search(r"\bmarket\s+beliefs?\b", text or "", re.I):  # belief cards have their own prepared test plan
        return False
    from app.chat.guard import _affirmative_clauses, _outreach_requests

    raw = " ".join(_affirmative_clauses(text or ""))
    return bool(re.search(r"\b(?:write|draft|create|prepare|build|give me)\b[^.!?]{0,180}\b"
                          r"(?:plan|schedule|script|copy|market test)\b", raw, re.I)
                and re.search(r"\b(?:market|demand|discovery|interview\w*|pilot|validation)\b", raw, re.I)
                and not _outreach_requests(text or ""))


def split_speech_acts(text: str) -> list[tuple[str, str]]:
    """Classify each clause. A question never becomes work; a mixed prompt splits.

    Compound instructions without a question stay one ``change`` so
    “rewrite X, competitors are Y, draft Z” still steers as a unit.
    """
    raw = (text or "").strip()
    if not raw:
        return [("status", raw)]
    if VENTURE_RENAME_RE.fullmatch(raw):
        return [("venture.rename", raw)]
    from app.chat.research_revision import parse as research_correction
    if research_correction(raw):
        return [("research.update_and_rerun", raw)]
    from app.chat.new_venture import idea_of
    if idea_of(raw) is not None:
        return [("new_venture", raw)]
    if _GTM_DIAGNOSE_RE.match(raw) and classify_intent(raw) == "gtm_review":
        return [("gtm_review", raw)]  # a diagnosis keeps its whole brief, later sentences are context
    if icp_question(raw):
        return [("gtm_review", raw)]  # "who are my customers?" asks for ICP help, not a memory recall
    from app.chat.memory_commands import parse as memory_command
    if command := memory_command(raw):
        return [(command.action, raw)]  # a complete statement is never split into work
    from app.chat.send_control import stop_sending_request
    if stop_sending_request(raw):
        return [("send_off", raw)]
    if _EXP_FROM_REVIEW_RE.match(raw) and (review_intent := classify_intent(raw)) in {"experiment_from_review", "assistant"}:
        return [(review_intent, raw)]
    if find_customers_request(raw):
        return [("gtm_review", raw)]
    from app.chat.idea_bridge import informational_request
    from app.chat.guard import _outreach_requests
    if classify_intent(raw) == "connections.connect" and informational_request(raw) and not _outreach_requests(raw):
        return [("connections.connect", raw)]
    if declines_gtm(raw):
        return [("assistant", raw)]
    from app.chat.product_build_request import is_product_build_request, local_build_intent
    if build_intent := local_build_intent(raw):
        return [(build_intent, raw)]
    if is_local() and is_product_build_request(raw):
        return [("build.tool.start", raw)]
    from app.chat.mac_offers import explicit_mac_control_request, non_authorizing_mac_mention
    if non_authorizing_mac_mention(raw):
        return [("assistant", raw)]
    if explicit_mac_control_request(raw):
        return [("mac_prepare", raw)]  # retain the exact goal and every read-only constraint
    from app.chat.site_actions import named_synthetic_waitlist_test
    if named_synthetic_waitlist_test(raw):
        return [("waitlist.test", raw)]  # exact internal-only scope stays attached
    from app.chat.email_revision import target as email_revision_target
    if email_revision_target(raw):
        return [("email_revision", raw)]
    from app.chat.static_qa_guidance import request_run_id
    if request_run_id(raw):
        return [("assistant", raw)]
    from app.chat.founder_feedback_guidance import requested as founder_feedback_requested
    if founder_feedback_requested(raw):
        return [("assistant", raw)]
    if discovery_progress_question(raw):
        return [("assistant", raw)]
    from app.chat.other_venture import supplied_portfolio_context
    if supplied_portfolio_context(raw):
        return [("assistant", raw)]
    if discovery_copy_request(raw):
        return [("assistant", raw)]
    if native_plan_revision_request(raw) or local_plan_revision_request(raw):
        return [("plan_revision", raw)]  # keep all scope and no-execution constraints together
    if recruitment_draft_request(raw) or single_interview_email_request(raw) or outreach_email_request(raw):
        return [("outreach_draft", raw)]
    from app.chat.search_recovery import manual_discovery_request
    if manual_discovery_request(raw):
        return [("assistant", raw)]  # keep the no-search constraint attached to the manual guide
    if _LATEST_ERROR_RECOVERY_RE.fullmatch(raw):
        return [("latest_error", raw)]
    if _question_with_answer_format_followup(raw):
        return [("assistant", raw)]
    clauses = _split_clauses(raw)
    if len(clauses) <= 1:
        return [(classify_intent(raw), raw)]
    acts = [(classify_intent(clause), clause) for clause in clauses]
    if discovery_progress_question(clauses[0]):
        return acts  # later positive operations keep their own existing reviewed route
    lookups = [a for a in acts if a[0] in LOOKUP_INTENTS]
    works = [a for a in acts if a[0] in WORK_INTENTS]
    if lookups and (works or any(_DO_START.match(clause) or _DIRECTED_REQUEST.match(clause)
                               for _, clause in acts[1:])):
        return acts
    return [(classify_intent(raw), raw)]


_OUTREACH_DRAFT_RE = re.compile(r"\b(?:e-?mails|e-?mail\s+drafts|invitations)\b", re.I)  # plural: a batch of drafts, not one named note


def discovery_progress_question(text: str) -> bool:
    """A founder's acquisition report is context for advice, not a new lookup."""
    if re.search(r"(?:^|[.!?;:]\s*)(?:(?:please|now|also|and)\s+)*(?:search|look up|find|research|"
                 r"start|run|build|create|launch|send|publish|draft|write|prepare|approve|record|save)\b"
                 r"(?!\s+(?:results?|status|is|was|are|were|has|had|remains)\b)", text, re.I):
        return False  # keep separately requested effects on their existing reviewed routes
    timing_followup = bool(re.match(r"^The\s+(?:three|\d+)\s+invitations\s+were\s+sent\b", text.strip(), re.I)
        and re.search(r"\bRefine the next action\b[^.!?]{0,120}\bresponse window\b", text, re.I)
        and re.search(r"\bspecific time to check for replies\b", text, re.I)
        and re.search(r"\badvice only\b", text, re.I))
    if timing_followup:
        return not re.search(r"\b(?:but|then|instead|however)\s+(?:please\s+)?(?:search|look up|find|research|start|run|build|create|launch|send|publish|draft|write|prepare|approve|record|save|contact)\b", text, re.I)
    named_update = re.match(r"^(?:[A-Za-z][A-Za-z0-9_-]{0,39}\s+)?(?:discovery|customer discovery|outreach|interview)\s+(?:progress\s+update|update|progress|results)\b", text.strip(), re.I)
    reported_invitation = re.match(r"^I\s+(?:sent|have sent)\b[^.!?]{0,500}\b(?:interview|discovery)\s+invitations?\b", text.strip(), re.I)
    if not (named_update or reported_invitation):
        return False
    question = re.search(r"\b(?:what(?: exact)? next (?:action|step) should i take|what should i do next|what(?:'s| is) next)"
                         r"[^.!?]{0,180}\?"
                         r"|\bwhat is the next(?:\s+[A-Za-z-]+){0,4}\s+(?:step|action)\b[^.!?]{0,180}\?", text, re.I)
    if question is None:
        return False
    trailing = text[question.end():].strip()
    if not trailing:
        return True
    # A prohibition after the question belongs to the same advisory request.
    # Do not swallow a later positive operation or a 'but/then' exception.
    return bool(re.match(r"^(?:do not|don['’]?t)\b", trailing, re.I)
                and not re.search(r"\b(?:but|then|instead|however)\b", trailing, re.I)
                and all(re.match(r"^(?:do not|don['’]?t)\b", clause.strip(), re.I)
                        for clause in re.split(r"[.!?;]\s*", trailing) if clause.strip()))


def recruitment_draft_request(text: str) -> bool:
    """Explicit composition wins over a reference to an existing GTM diagnosis."""
    raw = (text or "").strip()
    return bool(re.match(
        r"^(?:(?:the|our|my) saved (?:gtm )?diagnosis\b[^.!?]{0,350}[.!]\s*)?"
        r"(?:(?:based on|using|use|from|with|referring to)\b[^.!?]{0,250}?(?:[,;:]\s*|\s+))?"
        r"(?:(?:please|can you|could you)\s+)?(?:draft|write|compose|prepare)\b"
        r"[^.!?]{0,250}\b(?:posts?|messages?)\b", raw, re.I)
        and re.search(r"\b(?:recruit(?:ment|ing)?|pilot|early[- ]access|first users)\b", raw, re.I))


def single_interview_email_request(text: str) -> bool:
    """Explicit composition of one interview email, including a supplied recipient."""
    raw = (text or "").strip()
    from app.chat.search_recovery import without_negated_effect_lists
    effect_text = without_negated_effect_lists(raw)
    compose = (r"(?:please\s+)?(?:draft|write|compose|prepare)\s+(?:an?|one)\s+"
               r"(?:(?:short|brief|honest|truthful|personalized|non-sales|interview|discovery)\s+)*e-?mail\b")
    direct = re.match(r"^" + compose, raw, re.I)
    # Founder-supplied own-site contact facts may precede the command. Keep
    # this bounded case on the same reviewed card, not the manual lookup guide.
    contextual = (len(raw) <= 4000
                  and len(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)) == 1
                  and re.search(r"\b(?:public business|business enquiries|publicly (?:listed|published))\b", raw, re.I)
                  and re.search(r"(?:^|[.!?]\s+)" + compose, raw, re.I)
                  and not re.search(r"(?:^|[.!?;,:]\s*)(?:(?:but|then|instead|however|also)\s+)?"
                                    r"(?:please\s+)?(?:search|find|lookup|look up|build|read|inspect|retrieve|list|switch)\b|"
                                    r"\band\s+(?:then\s+)?(?:search|find|lookup|look up|build|read|inspect|retrieve|list|switch)\b", effect_text, re.I))
    return bool((direct or contextual)
                and re.search(r"\b(?:interview|discovery)\b", raw, re.I)
                and len(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)) <= 1
                and not re.search(r"(?:^|[.!?;,:]\s*)(?:(?:but|instead|however)\s+)?(?:then\s+)?(?:please\s+)?(?:send|post|publish|schedule|search|run\s+a\s+Mac\s+task|contact(?!\s+lists?\b))\b|"
                                  r"\band\s+(?:then\s+)?(?:send|post|publish|schedule|search|run\s+a\s+Mac\s+task|contact)\s+"
                                  r"(?:it|this|that|them|to|now)\b|\band\s+(?:then\s+)?(?:search|run\s+a\s+Mac\s+task)\b", effect_text, re.I))


_EMAIL_COUNTS = {"a": 1, "an": 1, "one": 1, "ek": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_EMAIL_SPEC = (r"(?:(?P<count>" + "|".join(_EMAIL_COUNTS) + r"|\d+)\s+(?!words?\b))?"
               r"(?:(?!(?:that|which|who|to|for|about|with|of|from|and|or|on|in)\b)[\w-]+\s+){0,8}?(?P<noun>e-?mails?|invitations)\b")  # a page that collects emails is not an email


def outreach_email_count(text: str) -> int:
    match = re.search(r"\b(?:draft|write|compose|prepare|make)\s+(?:me\s+)?" + _EMAIL_SPEC, text or "", re.I)
    value = match.group("count").lower() if match and match.group("count") else ""
    if value:
        return int(value) if value.isdigit() else _EMAIL_COUNTS[value]
    return 3 if match and match.group("noun").lower().endswith("s") else 1


def single_outreach_email_request(text: str) -> bool:
    return outreach_email_request(text) and outreach_email_count(text) == 1


def outreach_email_request(text: str) -> bool:
    """Email composition only; preserve the request without consuming delivery commands."""
    raw = (text or "").strip()
    from app.chat.search_recovery import without_negated_effect_lists
    from app.chat.idea_bridge import _IMPERATIVE_SUFFIX, informational_request, request_text

    effect_text = without_negated_effect_lists(raw)
    command = request_text(raw)
    email = _EMAIL_SPEC
    compose = re.search(
        r"(?:^|[.!]\s+)(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:just\s+)?(?:draft|write|compose|prepare|make)\s+(?:me\s+)?" + email, command, re.I)
    if not compose and not informational_request(command) and not re.match(r"^(?:do not|don't|never)\b", command, re.I):
        compose = re.search(email, command, re.I) if _IMPERATIVE_SUFFIX.search(command) else None
    return bool(len(raw) <= 4000 and compose
        and re.search(r"\b(?:outreach|prospect|cold|interview|discovery)\b|\b(?:to|for)\s+\S", raw, re.I)
        and len(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)) <= 1
        and not re.search(r"\b(?:do\s+not|don['’]?t|never)\b[^.!?\n]*\b(?:prepare|create|start|run)\b[^.!?\n]*\btasks?\b", raw, re.I)
        and not re.search(
            r"(?:^|[.!?;,:]\s*)(?:(?:but|instead|however|also|then)\s+)?(?:please\s+)?"
            r"(?:send|post|publish|schedule|search|find|look\s+up|build|read|inspect|retrieve|switch|contact|run)\b|\band\s+(?:then\s+)?"
            r"(?:send|post|publish|schedule|search|find|look\s+up|build|read|inspect|retrieve|switch|contact|run)\b", effect_text, re.I))


_BOARD_RE = re.compile(_LEAD + r"(?:pressure[- ]?test\s+(?:(?:my|our|the|this)\s+)?idea\b(?=.*\b(?:independent|board)\b)|"
    r"(?:run|start|prepare|request)\s+(?:(?:an?|my|our|the|independent)\s+)*(?:idea\s+)?board\s+review\b)", re.I)
_MARKET_RESEARCH_REQUEST = re.compile(_LEAD + r"(?:"
    r"pressure[- ]?test\b[^.!?;\n]*\b(?:with|using|through)\s+(?:some\s+)?(?:(?:market|competitor)\s+)?research\b|"
    r"research\s+(?:(?:the|my|our|this)\s+)?market\b|(?:competitors|"
    r"(?:(?:run|start|do|conduct|begin|perform)\s+(?:(?:some|the|my|our|a)\s+)?)?(?:market|competitor)\s+research)"
    r"(?=\s*(?:[.!?:;,]|$)|\s+(?:for|on|into|about|please|now)\b))", re.I)
_OFFICE_HOURS_REQUEST = re.compile(_LEAD + r"(?:"
    r"(?:(?:run|start|do|hold)\s+)?office hours\b|coach\s+me\b|"
    r"(?:pressure[- ]?test\b[^.!?;\n]*\bby\s+)?ask(?:ing)?\s+me\s+(?:\w+\s+){0,3}questions\b)", re.I)


def market_research_request(text: str) -> bool:
    from app.chat.link_context import read_prohibited
    from app.chat.llm_router import _command_evidence, _prohibited_act

    return bool(_MARKET_RESEARCH_REQUEST.match(_command_evidence(text)) and not _ANSWER_ONLY.search(text)
                and not read_prohibited(text) and not _prohibited_act("research", text))


def competitor_research_request(text: str) -> bool:
    from app.chat.llm_router import _command_evidence, _prohibited_act

    source = _command_evidence(text)
    return bool(re.match(_LEAD + r"(?:find|identify|research)\s+(?:me\s+|my\s+|our\s+|the\s+)?"
        r"(?:(?:main|direct|top|relevant)\s+)*competitors?\b", source, re.I)
        and not _ANSWER_ONLY.search(text) and not re.search(r"\b(?:saved|recorded)\s+(?:(?:main|direct|top|relevant)\s+)*competitors?\b|\bcompetitors?\s+(?:you|we)\s+(?:found|identified|saved|recorded)\b", source, re.I)
        and not _prohibited_act("research", text))


def sheets_export_request(text: str) -> bool:
    from app.chat.llm_router import _command_evidence

    return bool(re.fullmatch(_LEAD + r"export\s+(?:(?:my|our|the)\s+)?(?:saved\s+)?"
        r"(?:(?:experiment|test)\s+)?results\s+(?:to|into)\s+(?:google\s+)?sheets\s*[.!?]*",
        _command_evidence(text), re.I) and not _ANSWER_ONLY.search(text))


def classify_intent(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "status"
    from app.chat.product_build_request import is_product_build_request, local_build_intent
    if build_intent := local_build_intent(raw):
        return build_intent
    if is_local() and is_product_build_request(raw):
        return "build.tool.start"
    if is_local() and sheets_export_request(raw):
        return "experiment.export_sheet"
    from app.chat.llm_router import _command_evidence
    if is_local() and market_research_request(raw):
        return "research.start"
    if _BOARD_RE.match(_command_evidence(raw)) and not _ANSWER_ONLY.search(raw):
        return "idea.pressure_test.prepare"
    if is_local() and competitor_research_request(raw):
        return "research.start"
    if is_local() and re.match(_LEAD + r"(?:list|compare)\s+(?:my\s+|our\s+|the\s+)?"
            r"(?:(?:main|direct|top|relevant)\s+)*competitors?\b", _command_evidence(raw), re.I):
        return "research.competitors"
    if VENTURE_RENAME_RE.fullmatch(raw):
        return "venture.rename"
    from app.chat.research_revision import parse as research_correction
    if research_correction(raw):
        return "research.update_and_rerun"
    from app.chat.new_venture import idea_of
    if idea_of(raw) is not None:
        return "new_venture"
    if icp_question(raw):
        return "gtm_review"  # "who are my customers?" asks for ICP help, not a memory recall
    from app.chat.memory_commands import parse as memory_command
    if command := memory_command(raw):
        return command.action
    from app.chat.send_control import stop_sending_request
    if stop_sending_request(raw):
        return "send_off"
    if _EXP_FROM_REVIEW_RE.match(raw):
        if _ANSWER_ONLY.search(raw):
            return "assistant"
        if _experiment_intent(raw) == "experiment_from_review":
            return "experiment_from_review"
    if find_customers_request(raw):
        return "gtm_review"
    if declines_gtm(raw):
        return "assistant"
    if is_local() and (_ONE_CLICK_PAGE_RE.match(raw) or _START_LANDING_BUILD_RE.match(raw)
                       or _PAGE_BRIEF_RE.fullmatch(raw)
                       or (_KICKOFF_BUILD_RE.fullmatch(raw) and re.search(_PAGE, raw, re.I))):
        return "pagebuilder.build"
    if _ONE_CLICK_PAGE_RE.match(raw):
        return "start_product_build"
    from app.chat import jarvis
    if (golden := jarvis.classify(raw)) is not None:
        if is_local() and golden == "start_product_build" and re.search(_PAGE, raw, re.I):
            return "pagebuilder.build"
        return golden
    from app.chat.mac_offers import explicit_mac_control_request, non_authorizing_mac_mention
    if non_authorizing_mac_mention(raw):
        return "assistant"
    if explicit_mac_control_request(raw):
        return "mac_prepare"
    if re.fullmatch(r"(?:please\s+)?(?:show|list|review)(?:\s+me)?\s+(?:(?:my|our|the)\s+)?outreach\s+drafts[.!?]*", raw, re.I):
        return "outreach.drafts"
    from app.chat.email_revision import target as email_revision_target
    if email_revision_target(raw):
        return "email_revision"
    from app.chat.static_qa_guidance import request_run_id
    if request_run_id(raw):
        return "assistant"
    from app.chat.founder_feedback_guidance import requested as founder_feedback_requested
    if founder_feedback_requested(raw):
        return "assistant"
    if discovery_progress_question(raw):
        return "assistant"
    if _LATEST_ERROR_RECOVERY_RE.fullmatch(raw):
        return "latest_error"
    if _question_with_answer_format_followup(raw):
        return "assistant"

    if _GOD_WISH_RE.match(raw):
        return "god_on"
    if (god := _GOD_MODE_RE.match(raw)):
        return "god_on" if (god.group(1) or god.group(2)).lower() in {"on", "enable", "activate", "start"} else "god_off"
    if discovery_copy_request(raw):
        return "assistant"
    plan_intent = _plan_chain_intent(raw)
    if plan_intent:
        return plan_intent
    if _CHAT_SUMMARY_RE.search(raw):
        return "assistant"  # answered from the venture's saved chat history
    if _OPEN_BUILD_RE.search(raw):
        return "open_build"
    if (_START_PRODUCT_BUILD_RE.search(raw) or _START_LANDING_BUILD_RE.search(raw)
            or _KICKOFF_BUILD_RE.match(raw)):
        return "assistant" if is_local() else "start_product_build"
    if _RETRY_JOB_RE.match(raw):
        return "retry_job"
    if recruitment_draft_request(raw) or single_interview_email_request(raw) or outreach_email_request(raw):
        return "outreach_draft"
    if _OUTREACH_DRAFT_RE.search(raw) and "@" not in raw:
        from app.chat.steer import is_review_draft

        if is_review_draft(raw):
            return "outreach_draft"  # opens the reviewed "Draft outreach emails" card, never the task queue
    # A founder often adds a question or a no-send constraint to the command.
    # Keep that compound request on the reviewed GTM path rather than sending
    # it to the generic task worker or an ungrounded assistant reply.
    if _START_GETTING_USERS_RE.match(raw) or _NEXT_GET_USERS_RE.search(raw) or _GTM_DIAGNOSE_RE.match(raw):
        return "gtm_review"
    if _LATEST_ERROR_RE.fullmatch(raw) or _WHY_FAILED_RE.fullmatch(raw):
        return "latest_error"
    if RENAME_RE.match(raw):
        return "manager.rename"
    if (exp_intent := _experiment_intent(raw)) is not None:
        return exp_intent
    from app.chat.connect import wants_disconnect

    if wants_disconnect(raw):
        return "connections.disconnect"
    if _CONNECTIONS_RE.search(raw):
        return "connections.status"
    if requested_venture_screen(raw) is not None:
        return "open_screen"
    from app.chat.site_actions import _REWRITE, classify_site_intent

    if (site_intent := classify_site_intent(raw)) is not None:
        if site_intent == "site.rewrite" and not _REWRITE.search(raw):
            return "change"
        return site_intent

    from app.concierge.social_guide import extract_zernio_key

    if extract_zernio_key(raw):
        return "social_connect"
    if (_CONTENT_DRAFT_RE.match(raw) and not _CONTENT_DRAFTS_RE.fullmatch(raw.strip(" .!?"))) \
            or _CONTENT_OTHER_RE.match(raw):
        return "content.draft"
    if is_local():
        from app.chat.link_context import read_prohibited

        if _OFFICE_HOURS_REQUEST.match(_command_evidence(raw)):
            return "office_hours"
        if _RESEARCH_RE.search(raw) and (read_prohibited(raw)
                or _ADVICE_RE.search(raw) and not looks_like_instruction(raw)):
            return "assistant"
    elif _OH_RE.search(raw):
        return "office_hours"
    if _CEO_RE.search(raw):
        return "ceo_review"
    if _BRAIN_RE.search(raw):
        return "brain_brief"
    if _RESUME_RE.fullmatch(raw):
        return "resume"
    if _PAUSE_RE.search(raw) and not _ASK_COMPANY.search(raw):
        asked = _PAUSE_QUESTION_RE.search(raw) and not _POLITE_REQUEST_RE.match(raw)
        if not asked:
            return "pause"
        # "How do I pause?" has one true answer; a judgement question ("should I?") still goes to the assistant.
        return "pause_help" if re.search(r"\b(?:how|where|can i|could i|may i)\b", raw, re.I) else "assistant"
    # A prohibition is a constraint on the answer, never a request to make a
    # decision. In particular, a status question ending in "do not approve"
    # must not offer an actionable approval card.
    if _DECLINED_APPROVAL_RE.search(raw):
        return "assistant"
    # Approval is an explicit decision speech act, not a word mentioned in a
    # status question or in a constraint such as "do not build or approve".
    if _APPROVE_ACTION_RE.search(raw):
        return "approve"
    if _RETARGET.search(raw):
        return "retarget"
    if _CANCEL_RE.search(raw) and not looks_like_instruction(raw):
        return "cancel"
    if _REFINE_RE.search(raw) and not looks_like_question(raw) and not looks_like_instruction(raw):
        return "refine_site"
    if _POST_SOCIAL_RE.search(raw) and not looks_like_question(raw):
        return "social_post"
    from app.chat.connect import wants_connect
    from app.chat.assistant import _asks_about_latest_mac_task, _asks_about_venture_metrics

    mac_receipt_question = _asks_about_latest_mac_task(raw)
    # A saved task's link is a receipt noun, not the account-linking verb.
    # Strip only that noun phrase for this predicate; an explicit "connect
    # Codex ... and give the task link" still retains its connection command.
    connect_text = re.sub(r"\b(?:task|receipt|report)\s+links?\b", "", raw, flags=re.I) if mac_receipt_question else raw
    from app.chat.connect import service_from, _VERB
    from app.chat.idea_bridge import informational_request
    service = service_from(connect_text)
    if not mac_receipt_question and service and service not in {"x", "linkedin", "reddit"} and _VERB.search(connect_text) and informational_request(connect_text):
        return "connections.connect"
    if wants_connect(connect_text):
        return "connections.connect"  # a link plus exact steps, for any service (never the old social/Zernio guide)
    if _SOCIAL_STATUS_RE.search(raw):
        return "social_status"
    if _SOCIAL_GUIDE_RE.search(raw):
        return "social_guide"
    if re.search(r"\b(?:yc|y combinator|pmf|product.market fit)\b", raw, re.I) and re.search(r"\b(?:advice|coaching)\b", raw, re.I):
        return "assistant"
    if _CASE_REF_RE.search(raw) and (looks_like_question(raw) or re.match(
        r"^\s*(?:@\S+\s+)?(?:show|give|cite|find)\b", raw, re.I
    )):
        return "why"
    # A platform name in an advice request ("what do you suggest for LinkedIn?") is not a command to
    # scrape that platform; only a link or an explicit lookup is.
    if _REACH_URL_RE.search(raw) or (_REACH_RE.search(raw) and not _ADVICE_RE.search(raw)):
        return "agent_reach"
    if _OW_SEARCH_RE.search(raw):
        return "openworker_search"
    # Task-specific usage questions belong to the saved Mac receipt. The
    # generic credit-balance tool only reports the venture-wide balance.
    if mac_receipt_question:
        return "assistant"
    if _asks_about_venture_metrics(raw):
        return "assistant"
    if _CREDITS_RE.search(raw):
        return "credits.balance"
    if _GTM_DIAG_RE.search(raw):
        return "gtm.diagnosis"
    if _CONTENT_DRAFTS_RE.search(raw) and not looks_like_instruction(raw):
        return "content.drafts"
    if _SAVED_WORK_RE.search(raw):
        return "saved_work"
    from app.chat.journey import asks_for_guidance

    if asks_for_guidance(raw):
        return "assistant"
    from app.chat.idea_bridge import request_text
    if re.fullmatch(r"(?:tell\s+me\s*[, :]\s*)?(?:who|what|which)\s+(?:are|is)\s+(?:my|our|the)\s+"
                    r"(?:(?:main|direct|top|relevant)\s+)*competitors[.!?]*", request_text(raw), re.I):
        return "research.competitors"
    if is_local() and re.match(_LEAD + r"compare\s+(?:my\s+|our\s+|the\s+)?"
            r"(?:(?:direct|main|top|relevant)\s+)*competitors?\b", request_text(raw), re.I):
        return "research.competitors"
    if _ASK_COMPANY.search(raw):
        # Status / "what's going on" questions go to the assistant, not a card dump.
        return "assistant"
    if _ANSWER_ONLY.search(raw) and not (_DO_START.match(raw) or _DIRECTED_REQUEST.match(raw)):
        return "assistant"
    # A multi-part next-user decision may mention "URL" or "website". The
    # legacy factual lookup only knows the preview slot, so it must not steal
    # that decision from the fresh venture-state answer.
    from app.chat.assistant import _asks_next_step_toward_user

    if _asks_next_step_toward_user(raw):
        return "assistant"
    if (re.match(r"^(?:please\s+)?(?:run|start|perform|check|test)\b", raw, re.I)
            and _SOURCE_QA_RE.search(raw)):
        return "source_qa"
    # The founder can append evidence requirements ("say what remains
    # unverified") to a new research imperative. Those question words do not
    # turn the research request into an unsourced model-only answer.
    if re.match(r"^(?:please\s+)?research\s+", raw, re.I):
        return "research"
    if re.fullmatch(r"(?:please\s+)?(?:find|identify|list|compare)\s+(?:me\s+|my\s+|our\s+|the\s+)?"
                    r"(?:(?:direct|main|top|relevant)\s+)*competitors(?:\s+for\s+(?:this|my|our)\s+(?:venture|product|idea))?[.!?]*", raw, re.I):
        return "research"
    # "Tell me what ... . Do not send ..." asks for an answer; the final
    # sentence constrains it. Treating that constraint as a work imperative
    # sends a second clause through change/narration and can append invented
    # product status to an otherwise grounded answer.
    if re.match(r"^(?:please\s+)?tell me\s+(?:what|which|whether|how|why|where|when|who)\b", raw, re.I):
        return "assistant"
    if looks_like_instruction(raw):
        instruction = _DO_START.match(raw)
        if instruction and instruction[2].lower() == "research":
            return "research"
        return "slate_override" if _SLATE_RE.search(raw) else "change"
    if _EMAIL_LOOKUP_RE.search(raw) or (
        _URL_RE.search(raw) and re.search(r"\b(what|where|my)\b", raw, re.I) and looks_like_question(raw)
    ):
        return "factual"
    if _LEADS_RE.search(raw) and not _DO_START.search(raw):
        # "Show me the waitlist" is a question → assistant; bare "waitlist" stays leads.
        return "assistant" if looks_like_question(raw) else "leads"
    if _NEXT_RE.search(raw):
        return "assistant" if looks_like_question(raw) else "whats_next"
    if _AGENTS_RE.search(raw):
        return "assistant" if looks_like_question(raw) else "agents"
    if _RESEARCH_RE.search(raw) and not _ASK_COMPANY.search(raw):
        # Questions about competitors/research → assistant; "Research three…" → action.
        return "assistant" if looks_like_question(raw) else "research"
    if _SLATE_RE.search(raw):
        return "slate_override"
    if _USERS_RE.search(raw):
        return "money"
    if _WHY_RE.search(raw) and looks_like_question(raw):
        # Build-failure and other "why" questions: assistant uses the state brief.
        # Memory-only "why" still available via brain_brief / remember paths.
        return "assistant"
    if _PUBLISH_RE.search(raw) and not looks_like_question(raw):
        return "change"
    if looks_like_question(raw):
        return "assistant"
    return "general"
