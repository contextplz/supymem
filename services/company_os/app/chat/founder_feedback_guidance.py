"""No-effect manual discovery for external feedback on Supymem itself."""
import re
from urllib.parse import urlencode


def requested(text: str) -> bool:
    raw = text or ''
    if (len(raw) > 4000 or not re.search(r'\badvice only\b', raw, re.I)
            or not re.search(r'\bconsenting independent founder\b', raw, re.I)
            or not re.search(r'\bSupymem\b', raw, re.I)
            or not re.search(r'\bfeedback\b', raw, re.I)
            or not re.search(r'\bmanual(?:ly)?\b', raw, re.I)
            or re.search(r'\b(?:ven_|wsp_)[A-Za-z0-9_-]+\b', raw)):
        return False
    positive = '. '.join(re.split(r'\b(?:do not|don[\'’]t|never)\b', sentence,
                                  maxsplit=1, flags=re.I)[0]
                         for sentence in re.split(r'[.!?;]\s*', raw))
    return not re.search(r'(?:^|[.!?;:]\s*|\band\s+)(?:(?:but|then|instead|also)\s+)?(?:please\s+)?'
                         r'(?:create|prepare|run|draft|write|send|post|publish|contact|collect|search|find|read|inspect|retrieve|switch|list|build|invite)\b', positive, re.I)


def reply() -> dict:
    query = '"founder" "startup" "official website" "contact"'
    url = 'https://www.google.com/search?' + urlencode({'q': query})
    return {'kind': 'text', 'body_md': (
        'This is discovery for feedback on Supymem, separate from the five internal startup hypotheses and Groomer customer outreach. '
        'No external founder, consent, onboarding or product success has been established.\n\n'
        f'One small manual step: open [Google]({url}) and use the query `{query}`. '
        'Open one candidate’s own business website, then its About or Team page. Verify that it explicitly identifies the person as a founder or operator working on their own startup. '
        'Read its homepage or Product page to identify the current startup or proposed offering; past employment alone is not founder fit. Open that same website’s Contact page to check whether it offers a public business contact path; do not use private profiles, scraped directories or guessed addresses. '
        'If founder role or current startup ownership is unclear, keep fit unknown and skip the candidate.\n\n'
        'Stop after identifying one candidate. Return here with the public website URL, the public founder-role evidence and any unknown fit. '
        'A public listing is not consent to participate. A voluntary feedback invitation and any onboarding would require a separate reviewed decision. '
        'Any later invitation must say that Supymem has useful guidance and some working steps, while native app control and full product hosting still have gaps; do not promise end-to-end success.\n\n'
        'This reply created no task, draft, account, venture or invitation, and performed no search, contact, send or post.'
    ), 'actions': [], 'citations': []}
