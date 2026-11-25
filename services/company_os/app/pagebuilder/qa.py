"""Fail-closed page checks and real mobile/desktop render evidence."""
from __future__ import annotations

import json
import re
import textwrap
from html import escape, unescape
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from core.static_qa_contracts import Action, Contract, Visual
from app.core.local_mode import is_local

from .models import PageInput, PagePlan

NATIVE_CONTROLS = {"button", "input", "select", "textarea", "a", "details", "summary"}
INTERACTIVE_ROLES = {"button", "link", "checkbox", "radio", "switch", "tab", "textbox", "combobox",
                     "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "slider", "spinbutton", "treeitem"}
GENERIC_COPY_LABELS = {"Overview", "What it is", "The problem", "What it does", "Early access", "How it works", "Page sections", "Primary navigation"}
COPY_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li"}
PRIVATE_PREVIEW_NOTE = "Preview only. This page does not collect signups."
REQUIRED_SECTIONS = ("hero", "problem", "how-it-works", "audience", "faq", "waitlist")
PRIMARY_CTA_SECTIONS = {"hero_cta": "hero", "waitlist_cta": "waitlist"}
HOW_IT_WORKS_STEP_COUNT = 3
HOW_IT_WORKS_CAPABILITY_LABEL = "What it does"
HOW_IT_WORKS_RULE = (
    "Choose how-it-works using only the confirmed idea and clarified answers, not research, category assumptions or plan copy. "
    "Confirmed customer workflows need three meaningful supported body sentences rendered as three li steps; capability/offer restatements are not steps. "
    f"Without a confirmed workflow, headline and nav_label must each be exactly {HOW_IT_WORKS_CAPABILITY_LABEL}; "
    "render confirmed capabilities/offer in paragraphs or an unordered list: "
    "no step count, numbering or implied order. Restatements may overlap proof; do not require three capabilities or reject missing workflow facts. "
    "Never invent customer actions or infer service requests, booking, delivery, availability or launch timing from a waitlist."
)


def copy_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if part.strip()]


def _copy_key(text: str) -> str:
    return " ".join(text.split()).strip(" .,:;!?…→↗↓←✓●•–—[]()").casefold()


def is_workflow_section(section) -> bool:
    return section.id == "how-it-works" and _copy_key(section.headline) != _copy_key(HOW_IT_WORKS_CAPABILITY_LABEL)


def customer_copy_failures(inputs: PageInput, sections, cta: str) -> list[str]:
    if not is_local():
        return []
    failures = []
    by_id = {section.id: section for section in sections}
    required = set(REQUIRED_SECTIONS)
    if not required <= by_id.keys() or sections[0].id != "hero" or sections[-1].id != "waitlist":
        failures.append("customer_copy:sections_required:hero,problem,how-it-works,audience,faq,waitlist")
    if hero := by_id.get("hero"):
        headline = _copy_key(hero.headline)
        if (headline == _copy_key(inputs.name) or _copy_key(cta) in headline
                or re.search(r"\b(?:explore early access|welcome|coming soon)\b", headline)):
            failures.append("customer_copy:headline_must_state_customer_value_not_brand_or_cta")
        if len(copy_sentences(hero.body)) != 1:
            failures.append("customer_copy:hero_needs_one_sentence_subhead")
    if (len(cta.split()) < 2 or not re.search(r"\b(?:join|request|book|apply|reserve|get|try|start)\b", cta, re.I)
            or _copy_key(cta) in {"get started", "start now", "try now", "join now", "request access",
                "request early access", "explore early access"}):
        failures.append("customer_copy:cta_must_name_the_customer_action")
    if steps := by_id.get("how-it-works"):
        if is_workflow_section(steps) and len(copy_sentences(steps.body)) != HOW_IT_WORKS_STEP_COUNT:
            failures.append("customer_copy:how_it_works_needs_three_steps")
    if faq := by_id.get("faq"):
        if not re.search(r"\bto[ -]be[ -]confirmed\b", faq.headline + " " + faq.body, re.I):
            failures.append("customer_copy:faq_must_mark_unconfirmed_details")
    return failures


def local_signup(inputs: PageInput) -> dict | None:
    if not is_local() or inputs.page_test:
        return None
    from .local_publish import waitlist_destination
    return inputs.research.get("local_signup") or waitlist_destination(inputs.venture_id, inputs.name,
        deployed=False, verify=False)


def waitlist_href(inputs: PageInput) -> str:
    from app.experiments.page_tests import waitlist_target
    signup = local_signup(inputs)
    return (signup["url"] if signup["mode"] in {"endpoint", "mailto"} else "#waitlist") if signup else waitlist_target(inputs)


def reviewed_copy_spans(inputs: PageInput, plan: PagePlan, imagery=()) -> list[str]:
    # Controller-derived illustrative alts may also appear as visible captions.
    copy = [inputs.name, plan.brief.promise, plan.brief.concept_disclosure, *plan.brief.proof_points,
            *[action.name for action in qa_contract(inputs, plan).actions], *GENERIC_COPY_LABELS,
            *[image.alt for image in imagery]]
    if signup := local_signup(inputs):
        copy.extend([signup["notice"], PRIVATE_PREVIEW_NOTE])
    for section in plan.sections:
        copy.extend([section.headline, section.body])
    for asset in plan.assets:
        copy.extend([asset.alt, " ".join(ET.fromstring(asset.svg).itertext())])
    spans = {_copy_key(part) for value in copy for part in [value, *copy_sentences(value)] if _copy_key(part)}
    # The controller's consent is authoritative as one complete span, never fragment authority.
    if inputs.waitlist_consent_text.strip():
        spans.add(_copy_key(inputs.waitlist_consent_text))
    return sorted(spans, key=len, reverse=True)


def unreviewed_copy(text: str, spans: list[str]) -> str:
    original = _copy_key(text)
    covered = bytearray(len(original))
    for span in spans:
        # Match the original block: overlapping reviewed sentences/whole consent
        # must not erase each other's authority or join unrelated fragments.
        for match in re.finditer(r"(?<!\w)" + re.escape(span) + r"(?!\w)", original):
            covered[match.start():match.end()] = b"\1" * (match.end() - match.start())
    remaining = "".join(" " if approved else char for char, approved in zip(original, covered))
    return remaining[:200] if any(char.isalnum() for char in remaining) else ""


def waitlist_copy_failures(text: str, consent_text: str = "") -> list[str]:
    """Signup assurances require the exact authoritative consent, not model inference."""
    copy = " ".join(text.casefold().replace("’", "'").split())
    consent = " ".join(consent_text.casefold().replace("’", "'").split())
    if consent:
        copy = copy.replace(consent, " ")
    patterns = {
        "unsubscribe_or_opt_out": r"\b(?:unsubscrib\w*|opt[ -]?out|withdraw(?:al)?\s+(?:your\s+)?consent)\b",
        "no_spam": r"\b(?:no|zero|without)\s+spam\b|\bspam[ -]free\b|\b(?:never|won't|will not|don't|do not)\s+(?:send\s+(?:you\s+)?)?spam\b",
        "data_sale_or_sharing": r"\b(?:never|won't|will not|don't|do not|not)\s+(?:sell|share)\b.{0,60}\b(?:email|data|information|details)\b|\b(?:email|data|information|details)\b.{0,60}\b(?:sold|shared)\b|\bno\s+data\s+(?:sales?|sharing)\b",
        "privacy_or_security": r"\b(?:your\s+)?(?:email|data|information|details)\b.{0,60}\b(?:safe|secure|private|protected|confidential|encrypted)\b|\b(?:guarantee|promise|ensure|protect)\w*\b.{0,40}\b(?:privacy|security)\b|\b(?:secure|encrypted|confidential)\s+(?:storage|signup|sign-up|email|data)\b",
        "retention_or_deletion": r"\b(?:delete|erase|remove)\w*\b.{0,50}\b(?:email|data|information|details|any\s*time)\b|\b(?:retain|retention)\w*\b|\b(?:email|data|information|details)\b.{0,50}\b(?:stored|kept)\s+(?:only|for\s+\d)\b",
        "email_verification": r"\b(?:verified|verify|verification)\b.{0,30}\b(?:email|address)\b|\b(?:email|address)\b.{0,30}\b(?:verified|verify|verification)\b",
        "delivery_or_frequency": r"\b(?:you\s+(?:will|'ll)|we\s+(?:will|'ll))\s+(?:receive|send|email)\b|\b(?:confirmation|welcome)\s+email\b|\b(?:occasional|weekly|monthly|daily|regular|periodic)\s+(?:emails?|updates?|messages?)\b",
    }
    failures = []
    for category, pattern in patterns.items():
        match = re.search(pattern, copy)
        if match:
            failures.append("unsupported_waitlist_claim:" + category + ":" + match.group()[:180])
    return failures


def contrast(left: str, right: str) -> float:
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))
    values = sorted([luminance(left), luminance(right)])
    return (values[1] + .05) / (values[0] + .05)


def qa_contract(inputs: PageInput, plan: PagePlan, imagery=()) -> Contract:
    from app.experiments.wizard_tests import native_form
    from app.experiments.page_tests import price_target, waitlist_target
    replaced = {image.replaces: image for image in imagery if image.replaces}
    body = [chunk for section in plan.sections for sentence in copy_sentences(section.body)
            for chunk in textwrap.wrap(sentence, width=500, break_long_words=True, break_on_hyphens=False)]
    href = waitlist_href(inputs)
    actions = [Action(id="hero_cta", kind="anchor" if not is_local() or href == "#waitlist" else "external_link",
        role="link", name=plan.brief.cta if is_local() else "Explore early access",
        target="waitlist" if not is_local() or href == "#waitlist" else href),
        Action(id="waitlist_cta", kind="anchor" if href == "#waitlist" else "external_link", role="link",
            name=plan.brief.cta, target="waitlist" if href == "#waitlist" else href),
        *[Action(id="nav_" + str(index), kind="anchor", role="link", name=section.nav_label, target=section.id)
          for index, section in enumerate(plan.sections) if section.nav_label]]
    test = inputs.page_test
    request_copy = ([field.label for field in test.request.fields] + ["Send request", inputs.waitlist_consent_text]
        if test and test.request else [])
    if test:
        actions.extend(Action(id="price_" + price.key, kind="external_link", role="link", name=f"{price.label}: {price.price}",
            target=price_target(inputs, price)) for price in test.prices)
        actions.extend(Action(id="persona_" + variant.key, kind="local_navigation", role="link", name=variant.headline,
            target=f"variant_{variant.key}.html") for variant in test.headlines)
        main_actions = list(actions)
        for index, variant in enumerate(test.headlines):
            actions.extend(action.model_copy(update={"id": f"v{index}_" + action.id, "page": f"variant_{variant.key}.html",
                **({"target": waitlist_target(inputs, variant.key)} if action.id in {"hero_cta", "waitlist_cta"}
                    and action.kind == "external_link" else {})}) for action in main_actions)
    return Contract(profile="static_landing_v1", product_name=inputs.name,
        required_text=list(dict.fromkeys([inputs.name, plan.brief.concept_disclosure,
            *[section.headline for section in plan.sections], *plan.brief.proof_points, *body, *request_copy,
            *([PRIVATE_PREVIEW_NOTE] if local_signup(inputs) and href == "#waitlist" else [])])),
        actions=actions,
        managed_request_form=({"html": native_form(inputs),
            "target": inputs.waitlist_url} if test and test.request else None),
        visuals=[*[Visual(id=replaced[asset.id].id, name=replaced[asset.id].alt) if asset.id in replaced
                   else Visual(id=asset.id, name=asset.alt) for asset in plan.assets if asset.role in {"hero", "section"}
                   and not (is_local() and asset.provenance == "original_model_svg")],
                 *[Visual(id=image.id, name=image.alt) for image in imagery if image.replaces is None]])


def tool_contract(inputs: PageInput, plan=None) -> dict:
    from .pipeline import _plan_contract

    contract = _plan_contract(inputs, plan).model_dump(mode="json") if plan is not None else {
        "required_text": [inputs.name], "actions": []}
    href = waitlist_href(inputs)
    if plan is None:
        if local_signup(inputs) and href == "#waitlist":
            contract["required_text"].append(PRIVATE_PREVIEW_NOTE)
        contract["actions"] = [
            {"id": action_id, "kind": "anchor" if href.startswith("#") else "external_link", "role": "link",
                "name": "brief.cta", "page": "index.html", "target": href[1:] if href.startswith("#") else href}
            for action_id in PRIMARY_CTA_SECTIONS]
        contract["actions"].extend({"id": "nav_" + str(index), "kind": "anchor", "role": "link",
            "name": f"sections[{index}].nav_label", "page": "index.html", "target": section_id}
            for index, section_id in enumerate(REQUIRED_SECTIONS[:-1]))
    contract["actions"] = [{**action,
        "href": "#" + action["target"] if action["kind"] == "anchor" else action["target"]}
        for action in contract["actions"]]
    contract["sections"] = [{"id": section.id, "headline": section.headline, "nav_label": section.nav_label, "body": section.body,
        "cta_ids": [action_id for action_id, section_id in PRIMARY_CTA_SECTIONS.items() if section_id == section.id],
        **({"steps": copy_sentences(section.body)} if is_workflow_section(section) else {})}
        for section in plan.sections] if plan is not None else [{"id": section_id,
            "cta_ids": [action_id for action_id, target in PRIMARY_CTA_SECTIONS.items() if target == section_id]}
            for section_id in REQUIRED_SECTIONS]
    contract["artwork"] = [{"id": asset.id, "path": "assets/" + asset.id + ".svg", "section_id": asset.section_id,
        "width": 1200, "height": 900} for asset in plan.assets] if plan is not None else [
            {"id": f"assets[{index}].id", "path": f"assets/<assets[{index}].id>.svg",
                "section_id": "hero" if index == 0 else "assets[1].section_id", "width": 1200, "height": 900}
            for index in range(2)]
    contract.update(how_it_works_rule=HOW_IT_WORKS_RULE,
        how_it_works_step_count=(HOW_IT_WORKS_STEP_COUNT if plan is not None
            and any(is_workflow_section(section) for section in plan.sections) else None),
        plan_first=("Write page-plan.txt first in required section order, with five unique nav_label values before waitlist "
            "and waitlist.nav_label=null. Resolve contract references from that plan before HTML."
            if plan is None else "Use the supplied reviewed plan unchanged."),
        required_text_rule="Render the product name, brief.concept_disclosure, every section.headline, every section.body sentence and every brief.proof_points entry verbatim and visibly. Keep bodies inside their own sections.",
        copy_rule="Every visible sentence must come from page-plan.txt copy. Do not paraphrase while rendering. Document title, descriptions, ARIA labels, captions and CSS text also use reviewed copy or the exact approved labels. The supplied consent is authoritative and must stay whole.",
        generic_labels=sorted(GENERIC_COPY_LABELS),
        artwork_rule='Create both artwork files; render one img each in its section with exact id/src, width="1200" height="900", eager loading. Resolve new-plan references from page-plan.txt. SVG: viewBox="0 0 1200 900", 5-1000 elements, <=100000 UTF-8 bytes, no active/external content. Alt: plain picture description, 1-160 characters, no product claims, performance numbers, guarantees, results or private data; no source quote or geometry review. Purely decorative: alt="" aria-hidden="true".',
        markup_rule="Use semantic section elements with the exact IDs, hero h1 and its one-sentence body. Follow how_it_works_rule for the workflow or What it does section. Place each primary CTA inside its declared section. Render each approved action exactly once on its declared page as an a element with its exact id, href and visible name. External links need target=_blank and rel=noopener noreferrer. Apart from a declared managed_request_form, do not add controls or an inline signup form to index.html; build the declared consent form only in waitlist.html. Preserve accessible names, labels, focus and contrast. Keep CSP, consent text/version/digest and the empty honeypot.")
    return contract


def explicitly_hidden(attrs):
    # shortcut: explicit HTML and inline styles only; use render checks for computed visibility.
    return ("hidden" in attrs or (attrs.get("aria-hidden") or "").casefold() == "true"
            or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!\s*important\s*)?(?:;|$)",
                              re.sub(r"/\*.*?\*/", "", attrs.get("style") or "", flags=re.S), re.I)))


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements, self.text, self.styles, self.stack = [], [], [], []
        self.waitlist_text = []
        self.copy_stack, self.copy_blocks, self.section_text = [], {}, {}
        self.visible_text, self.visible_headings, self.hidden_elements = [], {}, set()
        self.title_text = ""
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.text.append(" ")
            self.visible_text.append(" ")
        if tag in {"header", "footer", "section", "p", "h1", "h2", "h3", "li"}:
            self.text.append("\n")
            self.visible_text.append("\n")
        attributes = dict(attrs)
        if explicitly_hidden(attributes) or self.copy_stack and self.copy_stack[-1][1] in self.hidden_elements:
            self.hidden_elements.add(len(self.elements))
        self.elements.append((tag, attributes, tuple(self.stack)))
        if tag == "a":
            self.anchor = attributes
            self.anchor["_label"] = ""
            self.anchor["_starttag"] = self.get_starttag_text()
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attributes.get("id", "")))
            self.copy_stack.append((tag, len(self.elements) - 1))

    def handle_endtag(self, tag):
        if tag in {"header", "footer", "section", "p", "h1", "h2", "h3", "li"}:
            self.text.append("\n")
            self.visible_text.append("\n")
        if tag == "a":
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                del self.copy_stack[index:]
                break

    def handle_data(self, value):
        if any(tag == "title" for tag, _ in self.stack):
            self.title_text += value
        visible = not self.copy_stack or self.copy_stack[-1][1] not in self.hidden_elements
        if self.anchor is not None and visible:
            self.anchor["_label"] += value
        if any(tag == "style" for tag, _ in self.stack):
            self.styles.append(value)
        elif not any(tag in {"script", "head", "title"} for tag, _ in self.stack):
            self.text.append(value)
            if visible:
                self.visible_text.append(value)
                heading = next((index for tag, index in reversed(self.copy_stack) if tag == "h1"), None)
                if heading is not None:
                    self.visible_headings[heading] = self.visible_headings.get(heading, "") + value
            owner = next((index for tag, index in reversed(self.copy_stack) if tag in COPY_BLOCKS), None)
            if owner is None:
                owner = next((index for tag, index in reversed(self.copy_stack) if tag in {"a", "div", "header", "footer", "nav", "section", "main", "body"}), 0)
            self.copy_blocks[owner] = self.copy_blocks.get(owner, "") + value
            for tag, element_id in self.stack:
                if tag == "section" and element_id:
                    self.section_text[element_id] = self.section_text.get(element_id, "") + " " + value
            if any(tag == "footer" or element_id == "waitlist" for tag, element_id in self.stack):
                self.waitlist_text.append(value)


class _EditSource(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = [0]
        for line in source.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.stack = []
        self.forms = []
        self.nodes = []
        self.copy = []
        self.text = []
        self.feed(source)
        self.close()
        if any(tag == "form" for tag, *_ in self.stack):
            raise ValueError("page_edit_form_invalid")

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == "form" and any(parent[0] == "form" for parent in self.stack):
            raise ValueError("page_edit_form_invalid")
        start = self.source_offset()
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, start, start + len(self.get_starttag_text()), dict(attrs)))
        else:
            self.nodes.append((tag, start, start + len(self.get_starttag_text()), dict(attrs), tuple(self.stack)))

    def handle_startendtag(self, tag, attrs):
        if tag == "form":
            raise ValueError("page_edit_form_invalid")
        start = self.source_offset()
        self.nodes.append((tag, start, start + len(self.get_starttag_text()), dict(attrs), tuple(self.stack)))

    def _record_text(self, length):
        if any(tag in {"script", "style"} or explicitly_hidden(attrs)
               for tag, _, _, attrs in self.stack):
            return
        start = self.source_offset()
        end = start + length
        if self.text and self.text[-1][1] == start:
            self.text[-1] = (self.text[-1][0], end)
        else:
            self.text.append((start, end))

    def handle_data(self, value):
        self._record_text(len(value))

    def handle_entityref(self, name):
        length = len(name) + 1
        self._record_text(length + (self.source[self.source_offset() + length:self.source_offset() + length + 1] == ";"))

    def handle_charref(self, name):
        length = len(name) + 2
        self._record_text(length + (self.source[self.source_offset() + length:self.source_offset() + length + 1] == ";"))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                _, start, content, attrs = self.stack[index]
                end = self.source_offset()
                self.nodes.append((tag, start, self.source.index(">", end) + 1, attrs, tuple(self.stack[:index])))
                if tag == "form":
                    self.forms.append(self.source[start:self.source.index(">", end) + 1])
                if tag in {"h1", "h2", "p", "div", "a", "button"}:
                    visible = not any(explicitly_hidden(parent_attrs) for _, _, _, parent_attrs in self.stack)
                    self.copy.append((tag, content, end, attrs,
                        any(parent[0] == "form" for parent in self.stack), visible))
                del self.stack[index:]
                break
        else:
            if tag == "form":
                raise ValueError("page_edit_form_invalid")

    def protected(self):
        forms = [node for node in self.nodes if node[0] == "form"]
        controls = [node for node in self.nodes if any(parent[0] == "form" for parent in node[4])
                    or node[3].get("form") in {form[3].get("id") for form in forms if form[3].get("id")}]
        identifiers = {node[3]["id"] for node in controls if node[3].get("id")}
        references = {identifier for node in controls for attribute in ("aria-labelledby", "aria-describedby")
                      for identifier in (node[3].get(attribute) or "").split()}
        labels = [node for node in self.nodes if node[0] == "label" and node[3].get("for") in identifiers]
        referenced = [node for node in self.nodes if node[3].get("id") in references]
        if references - {node[3].get("id") for node in referenced}:
            raise ValueError("page_edit_form_invalid")
        consent = [node for node in self.nodes if node[0] not in {"html", "body", "head", "script", "style"}
                   and re.search(r"\b(?:consent|privacy|opt[ -]?out|unsubscrib\w*|"
                                 r"(?:i|you)\s+(?:agree|accept|authorize)|"
                                 r"(?:email|contact)\b.{0,80}\b(?:me|you|updates|offers|access)|"
                                 r"(?:store|use|retain|process)\b.{0,80}\b(?:email|personal|data))\b",
                                 unescape(re.sub(r"<[^>]*>", " ", self.source[node[1]:node[2]])), re.IGNORECASE)]
        consent = [node for node in consent if not any(node[1] < child[1] and child[2] < node[2]
                   and child[0] in {"p", "label", "li", "div"} for child in consent)]
        return sorted({(node[1], node[2]) for node in [*forms, *labels, *referenced, *consent]})


def has_signup_submit(parsed, form_start=None):
    from core.public_copy import public_html_text

    return any(tag == "button" and (attrs.get("type") or "submit").lower() == "submit"
               and "disabled" not in attrs and "form" not in attrs and not explicitly_hidden(attrs)
               and not any(parent[0] == "fieldset" and "disabled" in parent[3] for parent in parents)
               and any(parent[0] == "form" and (form_start is None or parent[1] == form_start) for parent in parents)
               and any(start <= left and right <= end and public_html_text(parsed.source[left:right]).strip()
                       for left, right in parsed.text)
               for tag, start, end, attrs, parents in parsed.nodes)


def _primary_pair(parsed, candidates):
    if len(candidates) != 2 or {element[0] for element in candidates} != {"a", "button"}:
        return False
    anchor = next(element for element in candidates if element[0] == "a")
    button = next(element for element in candidates if element[0] == "button")
    if not button[4] or (button[3].get("type") or "submit").casefold() != "submit":
        return False
    form = next((node for node in parsed.nodes if node[0] == "form" and node[1] < button[1] < node[2]), None)
    if (form is None or button[3].get("form") not in {None, form[3].get("id")}
            or button[3].get("formaction", form[3].get("action")) != form[3].get("action")):
        return False
    target = anchor[3].get("href", "")
    return bool(target and (target == form[3].get("action") or target.startswith("#") and any(
        node[3].get("id") == target[1:] and node[1] <= form[1] and form[2] <= node[2] for node in parsed.nodes)))


def _protected_copy(parsed, allow_primary):
    protected = parsed.protected()
    form_ranges = {(node[1], node[2]) for node in parsed.nodes if node[0] == "form"}
    primary = []
    for button in (parsed.copy if allow_primary else ()):
        if button[0] != "button" or not button[5]:
            continue
        raw_label = parsed.source[button[1]:button[2]]
        label = " ".join(unescape(raw_label).split())
        matching = [element for element in parsed.copy if element[0] == "a" and element[5]
                    and " ".join(unescape(parsed.source[element[1]:element[2]]).split()) == label]
        if ("<" not in raw_label and _primary_pair(parsed, [button, *matching])
                and not any(start <= button[1] < button[2] <= end
                            for start, end in protected if (start, end) not in form_ranges)):
            primary.append((button[1], button[2], matching[0][3].get("href")))
    result = []
    for start, end in protected:
        body = parsed.source[start:end]
        targets = []
        for content_start, content_end, target in sorted(primary, reverse=True):
            if start <= content_start < content_end <= end:
                body = body[:content_start - start] + body[content_end - start:]
                targets.append(target)
        result.append((body, targets))
    return result


def forms_preserved(before: bytes, after: bytes, instruction: str = "") -> bool:
    try:
        allow_primary = any(match["field"].casefold().startswith("button")
            or " ".join(match["field"].casefold().split()) in {"cta", "call to action"}
            for match in _copy_requests(instruction))
        return _protected_copy(_EditSource(before.decode("utf-8")), allow_primary) == _protected_copy(
            _EditSource(after.decode("utf-8")), allow_primary)
    except (UnicodeError, ValueError):
        return False


def _copy_requests(instruction):
    pattern = r'''"[^"]*"|“[^”]*”|'[^']*'|\b(?P<field>headline|subhead\w*|tagline|cta|call\s+to\s+action|button(?:\s+(?:text|label))?)\b'''
    return [match for match in re.finditer(pattern, instruction, re.IGNORECASE) if match["field"]
            and not re.search(r"\b(?:keep|preserve|retain|leave)\s+(?:(?:the|our|your|my|existing|current)\s+)*$",
                              instruction[:match.start()], re.IGNORECASE)]


def _copy_replacement(instruction, requested):
    replacement = re.match(r'''\s+(?:to|with|say|reads?)\s+(?:"([^"]+)"|“([^”]+)”|'([^']+)'|([^\n]+))''',
        instruction[requested.end():], re.IGNORECASE)
    if not replacement:
        return None
    wanted = next(value for value in replacement.groups() if value is not None)
    if replacement[4] is not None:
        wanted = re.split(r"[,.;]\s*(?:and\s+)?(?:keep|preserve|retain|leave|without)\b|"
                          r"\s+and\s+(?:keep|preserve|retain|leave)\b",
                          wanted, maxsplit=1, flags=re.IGNORECASE)[0].rstrip(" ,.;")
    return replacement, wanted


def literal_copy_edit(before: bytes, instruction: str, copy=None):
    """Recognize literal edits; only hosted mode applies their replacements."""
    requests = _copy_requests(instruction)
    if len(requests) != 1:
        return None
    requested = requests[0]
    parsed_replacement = _copy_replacement(instruction, requested)
    if not parsed_replacement:
        return None
    replacement, wanted = parsed_replacement
    remainder = instruction[requested.end() + replacement.end():].strip(" .,;")
    if remainder and not re.match(r"(?:and\s+)?(?:keep|preserve|retain|leave|without)\b", remainder, re.IGNORECASE):
        return None
    if re.search(r"\band\s+(?:change|update|set|make|replace|add|remove|turn|move|hide|show)\b",
                 instruction[:requested.start()], re.IGNORECASE):
        return None
    field = requested["field"].casefold()
    field = "headline" if field == "headline" else "cta" if field.startswith("button") or field in {"cta", "call to action"} else "subhead"
    tags = {"h1"} if field == "headline" else {"a", "button"} if field == "cta" else {"p", "h2", "div"}
    source = before.decode("utf-8")
    parsed = _EditSource(source)
    def text_value(element):
        return " ".join(unescape("".join(source[start:end] for start, end in parsed.text
                        if element[1] <= start < end <= element[2])).split())
    candidates = [element for element in parsed.copy if element[0] in tags and element[5]]
    current = (copy or {}).get(field)
    current = current.get("label") if isinstance(current, dict) else current
    if current:
        matching = [element for element in candidates if text_value(element) == " ".join(current.split())]
        if matching:
            candidates = matching
    if field == "cta" and len(candidates) > 2:
        pairs = []
        for button in candidates:
            if button[0] != "button":
                continue
            label = text_value(button)
            matching = [element for element in candidates if element[0] == "a"
                        and text_value(element) == label]
            if _primary_pair(parsed, [button, *matching]):
                pairs.append([button, *matching])
        if len(pairs) == 1:
            candidates = pairs[0]
    primary = _primary_pair(parsed, candidates)
    if len(candidates) != 1 and not primary:
        raise ValueError("page_edit_target_unavailable")
    local = is_local()
    changed = False
    for _, start, end, _, in_form, _ in sorted(candidates, key=lambda element: element[1], reverse=True):
        spans = [(text_start, text_end) for text_start, text_end in parsed.text
                 if start <= text_start < text_end <= end and unescape(source[text_start:text_end]).strip()]
        if len(spans) != 1:
            raise ValueError("page_edit_target_unavailable")
        text_start, text_end = spans[0]
        if " ".join(unescape(source[text_start:text_end]).split()) == " ".join(wanted.split()):
            continue
        if in_form and not primary:
            raise ValueError("page_edit_form_changed")
        changed = True
        if not local:
            source = source[:text_start] + escape(wanted, quote=False) + source[text_end:]
    after = source.encode("utf-8")
    if not forms_preserved(before, after, instruction):
        raise ValueError("page_edit_form_changed")
    if local and changed:
        return None
    return field, wanted, after


def edit_applied(before: bytes, after: bytes, instruction: str) -> bool:
    class VisibleCopy(PageParser):
        def handle_data(self, value):
            if not self.copy_stack or self.copy_stack[-1][1] not in self.hidden_elements:
                super().handle_data(value)

    if not before or not after or not forms_preserved(before, after, instruction):
        return False
    parsers = [VisibleCopy(), VisibleCopy()]
    try:
        for parser, html in zip(parsers, (before, after)):
            parser.feed(html.decode("utf-8"))
    except (UnicodeError, ValueError):
        return False
    def normalize(text):
        return " ".join(text.split())
    if normalize(" ".join(parsers[0].text)) == normalize(" ".join(parsers[1].text)):
        return False
    for requested in _copy_requests(instruction):
        field = normalize(requested["field"].casefold())
        tags = {"h1"} if field == "headline" else {"a", "button"} if field in {"cta", "call to action"} or field.startswith("button") else {"p", "div", "h2"}
        candidates = [[normalize(copy) for index, copy in parser.copy_blocks.items() if parser.elements[index][0] in tags]
                      for parser in parsers]
        if "a" in tags:
            for parser, values in zip(parsers, candidates):
                values.extend(normalize(attrs.get("_label", "")) for tag, attrs, _ in parser.elements if tag == "a")
        replacement = _copy_replacement(instruction, requested)
        if replacement:
            _, wanted = replacement
            if normalize(wanted) not in candidates[1]:
                return False
        elif candidates[0] == candidates[1]:
            return False
    return True


def pin_control_names(html: str, contract: Contract) -> str:
    """Pin hosted accessible names without rewriting local tool-owned source."""
    if is_local():
        return html
    parser = PageParser()
    parser.feed(html)
    actions = {action.id: action for action in contract.actions if action.page == "index.html"}
    for tag, attrs, _ in parser.elements:
        action = actions.get(attrs.get("id"))
        if tag != "a" or action is None:
            continue
        attributes = [(key, value) for key, value in attrs.items()
                      if key not in {"_label", "_starttag", "aria-label", "aria-labelledby"}]
        attributes.append(("aria-label", action.name))
        starttag = "<a" + "".join(" " + key + ("" if value is None else '="' + escape(value, quote=True) + '"')
                                  for key, value in attributes) + ">"
        html = html.replace(attrs["_starttag"], starttag, 1)
    return html


def answer_texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in answer_texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in answer_texts(item)]
    return []


_SPECIFIC_CLAIM_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|percent\b)|\b(?:guarantee\w*|certif\w*)\b|"
    r"\b(?:used|trusted|adopted|chosen|partnered)\s+by\b|\b(?:customers|partners)\s+include\b|"
    r"\b(?:has|have)\s+(?:already\s+)?(?:\w+ed|found|cut|made|saved)\b|"
    r"\b(?:already|currently)\s+(?:live|integrated)\b|"
    r"\b(?:live|available|ready)\s+(?:now|today|integration|app|software)\b|"
    r"\bavailable\s+in\s+(?:(?:our|the|this)\s+)?(?:app|software|platform)\b", re.I)


def _confirmed_capability(sentence: str, inputs: PageInput) -> bool:
    from app.firstlook.lint import _CAPABILITY_RE, _NUMBER_RE

    equivalents = {"detect": "find", "identify": "find", "found": "find", "sell": "offer",
        "sold": "offer", "provide": "offer", "providing": "offer", "provided": "offer",
        "offering": "service", "using": "use", "used": "use", "uses": "use", "utilizing": "use", "percent": "%"}
    stopwords = set("a an the and or of for to in on at as by from with that which who is are be it its "
        "we our you your this these those can do does use".split())

    def lemma(word):
        if word in stopwords:
            return word
        word = equivalents.get(word, word)
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 5 and word.endswith(("ing", "ed")):
            word = word[:-3] if word.endswith("ing") else word[:-2]
            if len(word) > 2 and word[-1] == word[-2]:
                word = word[:-1]
        elif word.endswith(("ches", "shes", "sses", "xes", "zes")):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us")):
            word = word[:-1]
        return equivalents.get(word, word)

    venture_words = {lemma(word) for word in re.findall(r"\w+", inputs.name.casefold())}

    def tokens(text):
        return [word for token in re.findall(r"\w+|%", text.casefold())
            if (word := lemma(token)) not in venture_words]

    def words(text):
        return [word for word in tokens(text) if word not in stopwords]

    facts = {key: value for key, value in inputs.clarified_answers.items()
        if key in {"offer", "what_it_does", "workflow", "audience"}}
    sources = [part for value in [inputs.idea, *answer_texts(facts)] for part in copy_sentences(value)
        if not re.search(r"\b(?:not|no|never|cannot|\w+n['’]t|without|unverified|unconfirmed|unknown|might|could|may|if|goal|aims?)\b|\?", part, re.I)]
    match = _CAPABILITY_RE.search(sentence)
    content = words(sentence)
    if match and words(match["verb"]) == ["offer"]:
        content = [word for word in content if word != "offer"]
    confirmed = [words(source) for source in sources]
    if not content or not set(content) <= {word for source in confirmed for word in source}:
        return False
    if (_SPECIFIC_CLAIM_RE.search(sentence) or _NUMBER_RE.search(sentence)) and not any(
            " " + " ".join(tokens(sentence)) + " " in " " + " ".join(tokens(source)) + " " for source in sources):
        return False
    # shortcut: deterministic word coverage and clause roles, upgrade to sourced semantic review for ambiguous relationships.
    if match and (verb := words(match["verb"])):
        if verb == ["offer"]:
            allowed = set()
            for source in sources:
                allowed.update(words(re.split(r"\b(?:that|which|of|for|with|using|to)\b", source, maxsplit=1, flags=re.I)[0]))
                for part in re.split(r"\b(?:with|using|sold as)\b", source, flags=re.I)[1:]:
                    allowed.update(words(re.split(r",|\bto\b", part, maxsplit=1, flags=re.I)[0]))
                audience_parts = re.split(r"\bsold as\b", source, flags=re.I)
                audience = audience_parts[-1].partition(" to ")[2] if len(audience_parts) > 1 else ""
                allowed.update(words(re.split(r"\b(?:to|with|using|that|which)\b", audience, maxsplit=1, flags=re.I)[0]))
            offered = re.split(r"\b(?:of|for|with|using|to|as|that|which)\b", sentence[match.end():], maxsplit=1, flags=re.I)[0]
            if not set(words(offered)) <= allowed:
                return False
        else:
            objects = set()
            for source in sources:
                for action in re.finditer(r"\w+", source):
                    if words(action.group()) != verb:
                        continue
                    objects.update(words(re.split(r",|\b(?:with|using|sold as)\b", source[action.end():], maxsplit=1, flags=re.I)[0]))
                    prefix = re.split(r"\b(?:that|which)\b", source[:action.start()], maxsplit=1, flags=re.I)[0]
                    objects.update(words(prefix.partition(" for ")[2]))
            target = re.split(r",|\b(?:with|using|as|to|for)\b", sentence[match.end():], maxsplit=1, flags=re.I)[0]
            if objects and not set(words(target)) <= objects:
                return False
    return True


def honest_copy(text: str, inputs: PageInput) -> list[str]:
    # Preserve the complete copy in assumptions: the legacy vocabulary/claim lint
    # still inspects every sentence without imposing its old three-card layout.
    from app.firstlook.lint import _SENTENCE_SPLIT, lint_copy
    confirmed = inputs.idea + " " + json.dumps(inputs.clarified_answers, ensure_ascii=False)
    if inputs.page_test:
        confirmed += " " + json.dumps([price.model_dump() for price in inputs.page_test.prices])
    result = lint_copy({"headline": inputs.name[:90], "subhead": "",
        "points": [{"title": "", "body": ""}] * 3, "cta": {"label": "Join the waitlist"},
        "assumptions": [text]}, idea_text=confirmed)
    reasons = [failure.rule + ": " + failure.detail for failure in result.failures
        if failure.rule != "capability_claim" or not _confirmed_capability(failure.detail, inputs)]
    for sentence in _SENTENCE_SPLIT.split(text):
        if (_SPECIFIC_CLAIM_RE.search(sentence) and not _confirmed_capability(sentence, inputs)
                and "capability_claim: " + sentence.strip() not in reasons):
            reasons.append("capability_claim: " + sentence.strip())
    if re.search(r"\byou\s+(?:(?:want|plan|hope|intend|aim)\s+to|are\s+trying\s+to)\s+"
                 r"(?:offer|sell|build|launch|create)\b|\byour\s+(?:idea|venture|business plan)\b|"
                 r"\byou\s+(?:have|don't have|do not have)\s+(?:no|any)\s+customers\b|"
                 r"\b(?:idea_summary|clarified_answers|research_review|executive_summary)\b", text, re.I):
        reasons.append("founder_copy:write_for_the_customer_not_about_the_founder")
    if inputs.page_test and inputs.page_test.prices and re.search(
            r"\b(?:buy now|purchase now|subscribe now|pay now|available (?:now|today)|checkout|"
            r"start (?:your )?(?:free |paid )?trial|get (?:instant|immediate) access|only \d+ .{0,30}left)\b", text, re.I):
        reasons.append("fake_door_availability_claim")
    for match in re.finditer(r"\b(?:nothing\s+(?:gets|is)\s+lost|never\s+lose)\b", text, re.I):
        if match.group().casefold() not in confirmed.casefold():
            reasons.append("unsupported_outcome_claim:" + match.group())
    timing_sources = {_copy_key(sentence) for source in [inputs.idea, *answer_texts(inputs.clarified_answers)]
                      for sentence in copy_sentences(source)}
    for sentence in copy_sentences(text):
        if re.search(r"\b(?:launch(?:es|ing|ed)?|releas(?:e|es|ing|ed)|com(?:e|es|ing))\s+"
                     r"(?:soon|now|today|tomorrow|next|this|in|on|by)\b|"
                     r"\b(?:ready|available)\s+(?:soon|now|today|tomorrow|next|this)\b|"
                     r"\b(?:is|are|will\s+be)\s+(?:already\s+)?(?:ready|available)\b|"
                     r"\bwill\s+(?:launch|release)\b", sentence, re.IGNORECASE) and _copy_key(sentence) not in timing_sources:
            reasons.append("unsupported_launch_timing:" + sentence)
    return reasons


def artwork_alt_failures(alt: str, inputs: PageInput) -> list[str]:
    from app.firstlook.lint import lint_copy
    from core.logging import _local_secret_values
    from core.redact import redact_string

    if (redact_string(alt, exact_values=_local_secret_values()) != alt
            or re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+?\d[\d ()-]{7,}\d", alt)):
        return ["private data is not allowed in alt; Replace it with a plain description of the illustration."]
    if not alt:
        return []
    claims = lint_copy({"headline": alt[:90], "points": [{"title": "", "body": ""}] * 3,
        "cta": {"label": "Join the waitlist"}, "assumptions": [alt]})
    picture_copy = re.sub(r"\b\d+(?:\.\d+)?\s*(?:%|percent\b)(?=\s+"
        r"(?:(?:black|white|gr[ae]y|red|orange|yellow|green|blue|purple|pink|brown)\s+)?"
        r"(?:gradient|opacity|fill|stroke|transparency|saturation|brightness)\b)", "some", alt, flags=re.I)
    # shortcut: visual percentages require basic colors and drawing properties, upgrade to an alt-only semantic check for other descriptions or claim forms.
    if (any(failure.rule != "language" for failure in claims.failures)
            or re.search(r"\bguarantee\w*\b|\b(?:product|app|tool|platform|service|our|your|you|we|it)\b.{0,80}\b(?:reduces?|increases?|improves?|boosts?|results?|outcomes?)\b|\d+(?:\.\d+)?\s*%.{0,30}\b(?:faster|slower|more|less|fewer|better|accuracy|success|results?)\b", alt, re.I)
            or any(not reason.startswith("language:") for reason in honest_copy(picture_copy, inputs))):
        return ["product claims are not allowed in alt; Replace them with a plain description of the illustration, without performance numbers, guarantees or results."]
    return []



_MOTION = {"animation", "animation-name", "animation-duration", "animation-delay", "animation-iteration-count"}


def _motion_failures(rules) -> list[str]:
    """Safe CSS motion only: opt-in under no-preference, bounded, never scroll-driven or endless."""
    import tinycss2
    failures = []

    def seconds(token):
        if token.type == "dimension" and token.lower_unit in {"s", "ms"}:
            return token.value / (1000 if token.lower_unit == "ms" else 1)
        return None

    def declarations(tokens, guarded):
        for item in tinycss2.parse_declaration_list(tokens, skip_whitespace=True, skip_comments=True):
            if item.type != "declaration":
                continue
            if item.lower_name.endswith(("-timeline", "-range", "-range-start", "-range-end")):
                failures.append("motion_scroll_driven")
            value = tinycss2.serialize(item.value).strip().casefold()
            if item.lower_name not in _MOTION or value in {"none", "initial", "unset", "0s", "0"}:
                continue
            if not guarded:
                failures.append("motion_not_reduced_motion_guarded")
            if "infinite" in value:
                failures.append("motion_unbounded")
            if item.lower_name == "animation":
                # Entrances and decorative loops end within 5s (WCAG 2.2.2 pause threshold).
                for part in value.split(","):
                    tokens = tinycss2.parse_component_value_list(part)
                    times = [time for time in map(seconds, tokens) if time is not None] + [0, 0]
                    count = next((token.value for token in tokens if token.type == "number"), 1)
                    if times[0] * max(1, count) + times[1] > 5:
                        failures.append("motion_too_long")

    def walk(tokens, guarded):
        for rule in tokens:
            if rule.type == "at-rule" and rule.content is not None and rule.lower_at_keyword in {"media", "supports", "layer", "container"}:
                prelude = "".join(tinycss2.serialize(rule.prelude).casefold().split())
                walk(tinycss2.parse_rule_list(rule.content, skip_whitespace=True, skip_comments=True),
                     guarded or (rule.lower_at_keyword == "media" and "prefers-reduced-motion:no-preference" in prelude))
            elif rule.type == "qualified-rule":
                declarations(rule.content, guarded)
    walk(rules, False)
    return failures


def check_page(html: str, files: dict, inputs: PageInput, plan: PagePlan, imagery=()) -> dict:
    source_html, native = html, ""
    if inputs.page_test and inputs.page_test.request:
        from app.experiments.wizard_tests import native_form
        native = native_form(inputs)
        if html.count(native) != 1:
            return {"passed": False, "failures": ["managed_request_form_changed"]}
        html = html.replace(native, "", 1)
    parser = PageParser()
    parser.feed(html)
    failures, diagnostics = [], {}
    failures.extend(customer_copy_failures(inputs, plan.sections, plan.brief.cta))
    if is_local():
        hero_headings = [_copy_key(parser.visible_headings.get(index, "")) for index, (tag, _, parents) in enumerate(parser.elements)
                         if tag == "h1" and ("section", "hero") in parents]
        if hero_headings != [_copy_key(plan.sections[0].headline)]:
            failures.append("customer_copy:hero_heading_must_match_reviewed_customer_value")
        for section in plan.sections:
            if not any(tag == "section" and attrs.get("id") == section.id for tag, attrs, _ in parser.elements):
                failures.append("customer_copy:missing_section:" + section.id)
        steps = [tag for tag, _, parents in parser.elements if tag == "li" and ("section", "how-it-works") in parents]
        workflow = any(is_workflow_section(section) for section in plan.sections)
        if workflow and len(steps) != HOW_IT_WORKS_STEP_COUNT:
            failures.append("customer_copy:render_three_how_it_works_steps")
        if not workflow and any(
                tag == "ol" and ("section", "how-it-works") in parents for tag, _, parents in parser.elements):
            failures.append("customer_copy:what_it_does_must_not_be_ordered_steps")
        for action_id, section_id in PRIMARY_CTA_SECTIONS.items():
            if not any(tag == "a" and attrs.get("id") == action_id and ("section", section_id) in parents
                       for tag, attrs, parents in parser.elements):
                failures.append("customer_copy:cta_required_in_section:" + section_id)
    visible = " ".join(" ".join(parser.text).split())
    spans = reviewed_copy_spans(inputs, plan, imagery)
    def check_reviewed_copy(text, field):
        if remainder := unreviewed_copy(text, spans):
            failures.append("honest_copy:unreviewed_visible_copy:" + field)
            diagnostics.setdefault("unreviewed_copy", []).append({"field": field, "text": text[:240], "unreviewed": remainder})
    for index, copy in parser.copy_blocks.items():
        check_reviewed_copy(copy, "element_" + str(index))
    check_reviewed_copy(parser.title_text, "title")
    for tag, attrs, _ in parser.elements:
        for attribute in ("aria-label", "title"):
            if attrs.get(attribute):
                check_reviewed_copy(attrs[attribute], tag + "." + attribute)
        if tag == "meta" and (attrs.get("name") or attrs.get("property") or "").casefold() in {"description", "og:description", "og:title", "twitter:description", "twitter:title"}:
            check_reviewed_copy(attrs.get("content") or "", "meta.content")
    copy_failures = honest_copy("".join(parser.text), inputs)
    failures.extend("honest_copy:" + failure for failure in copy_failures)
    for failure in copy_failures:
        category, _, detail = failure.partition(":")
        if category in {"numeric_claim", "banned_vocabulary", "unsupported_outcome_claim"} and detail.strip():
            excerpts = []
            for match in re.finditer(re.escape(detail.strip()), visible, re.I):
                excerpts.append(visible[max(0, match.start() - 80):match.end() + 80])
                if len(excerpts) == 6:
                    break
            diagnostics.setdefault("honest_copy", []).append({"failure": failure, "visible_excerpts": excerpts})
    failures.extend(waitlist_copy_failures(" ".join(parser.waitlist_text), inputs.waitlist_consent_text))
    source_parser = PageParser()
    source_parser.feed(source_html)
    public_text = [*source_parser.text, source_parser.title_text]
    rendered_assets = {attrs.get("src") for tag, attrs, _ in source_parser.elements if tag == "img"}
    public_text.extend(text for asset in plan.assets if asset.path in rendered_assets
                       for text in ET.fromstring(asset.svg).itertext())
    for tag, attrs, _ in source_parser.elements:
        public_text.extend(attrs.get(attribute) or "" for attribute in
                           ("alt", "aria-label", "title", "placeholder"))
        if tag == "meta" and (attrs.get("name") or attrs.get("property") or "").casefold() in {
                "description", "og:description", "og:title", "twitter:description", "twitter:title"}:
            public_text.append(attrs.get("content") or "")
        if tag in {"input", "button"}:
            public_text.append(attrs.get("value") or "")
    if any("\u2014" in text for text in public_text):
        failures.append("honest_copy:em_dash")
    elements = source_parser.elements
    required_visible = " ".join(" ".join(source_parser.visible_text).split())
    ids = {attrs.get("id") for _, attrs, _ in elements if attrs.get("id")}
    if len(ids) != len([1 for _, attrs, _ in elements if attrs.get("id")]):
        failures.append("duplicate_dom_id")
    if len(html.encode()) > 250000 or sum(len(value.encode() if isinstance(value, str) else value) for value in files.values()) > 1500000:
        failures.append("size_budget")
    if len([1 for tag, _, _ in elements if tag == "h1"]) != 1 or not any(tag == "main" for tag, _, _ in elements):
        failures.append("semantic_landmarks")
    if not any(tag == "html" and attrs.get("lang") for tag, attrs, _ in elements):
        failures.append("document_language")
    if not any(tag == "meta" and attrs.get("name") == "viewport" for tag, attrs, _ in elements):
        failures.append("viewport")
    for section in plan.sections:
        if section.id not in ids or section.headline not in visible:
            failures.append("section_missing:" + section.id)
        for sentence in copy_sentences(section.body):
            if _copy_key(sentence) not in _copy_key(parser.section_text.get(section.id, "")):
                failures.append("honest_copy:reviewed_body_missing:" + section.id)
    if plan.brief.concept_disclosure not in visible:
        failures.append("concept_disclosure_missing")
    for required in qa_contract(inputs, plan, imagery).required_text:
        if required not in required_visible:
            failures.append("required_text_missing:" + required)
    assets = {asset.path: asset for asset in plan.assets}
    rasters = {image.path: image for image in imagery}
    seen = set()
    for tag, attrs, parents in elements:
        if tag in {"script", "iframe", "object", "embed", "base", "svg", "video", "audio"} or any(name.lower().startswith("on") for name in attrs):
            failures.append("active_content:" + tag)
        if any(name in attrs for name in {"formaction", "formmethod", "formtarget", "formenctype", "srcdoc", "ping", "download", "srcset"}):
            failures.append("action_override")
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
            failures.append("active_content:refresh")
        for name in {"src", "href", "poster"} & attrs.keys():
            value = attrs[name] or ""
            if tag == "a" and value.startswith("#"):
                if value[1:] not in ids:
                    failures.append("broken_anchor:" + value)
            elif value not in files and not (tag == "a" and value in {action.target for action in qa_contract(inputs, plan).actions if action.kind != "anchor"}):
                failures.append("resource_not_pinned:" + value[:100])
        if tag == "img" and attrs.get("src") in rasters:
            image = rasters[attrs["src"]]
            if attrs.get("alt") != image.alt or attrs.get("id") != image.id:
                failures.append("image_not_pinned_or_alt_invalid")
            elif image.section_id not in {parent_id for _, parent_id in parents}:
                failures.append("image_wrong_section:" + image.id)
            else:
                seen.add(image.id)
            if attrs.get("width") != str(image.width) or attrs.get("height") != str(image.height):
                failures.append("image_dimensions_missing")
            if attrs.get("loading", "eager").casefold() != "eager":
                failures.append("image_not_eager:" + image.id)
        elif tag == "img":
            asset = assets.get(attrs.get("src"))
            if not asset or attrs.get("alt") != asset.alt or attrs.get("id") != asset.id:
                failures.append("image_not_pinned_or_alt_invalid")
            elif asset.role in {"hero", "section"}:
                seen.add(asset.id)
                if asset.section_id not in {parent_id for _, parent_id in parents}:
                    failures.append("image_wrong_section:" + asset.id)
            if asset and is_local() and asset.provenance == "original_model_svg":
                if "alt" not in attrs or (not asset.alt.strip() and (asset.alt or attrs.get("aria-hidden") != "true")):
                    failures.append("pagebuilder_tool_artwork_invalid:" + asset.id + ': Set nonempty alt, or alt="" with aria-hidden="true" for a purely decorative image.')
                failures.extend("pagebuilder_tool_artwork_invalid:" + asset.id + ": " + reason
                    for reason in artwork_alt_failures(asset.alt, inputs))
            if not attrs.get("width") or not attrs.get("height"):
                failures.append("image_dimensions_missing")
            if asset and asset.role in {"hero", "section"} and attrs.get("loading", "eager").casefold() != "eager":
                failures.append("image_not_eager:" + asset.id)
        if tag == "link" and attrs.get("rel") not in {"icon", "stylesheet"}:
            failures.append("unexpected_link_resource")
    replaced = {image.replaces for image in imagery if image.replaces}
    for asset in plan.assets:
        if asset.role in {"hero", "section"} and asset.id not in seen and asset.id not in replaced:
            failures.append("contextual_image_missing:" + asset.id)
    for image in imagery:
        if image.id not in seen:
            failures.append("contextual_image_missing:" + image.id)
    if any(tag == "form" and not (native and attrs.get("id") == "request_form") for tag, attrs, _ in elements):
        failures.append("inline_form_backend_unsupported_use_hosted_consent")
    signup = inputs.research.get("local_signup") if is_local() else None
    if signup:
        from app.pagebuilder.local_publish import _check_public_files
        from app.pagebuilder.tool import signup_valid
        checked = [{"path": path, "body": body.encode("utf-8") if isinstance(body, str) else body}
            for path, body in (files | {"index.html": html}).items()]
        try:
            _check_public_files(checked, inputs.name, signup, inputs.waitlist_url, deployed=False)
            if not signup_valid(files, inputs):
                failures.append("local_signup_changed")
        except (ValueError, UnicodeError):
            failures.append("local_signup_changed")
    waitlist_links = [(attrs, parents) for tag, attrs, parents in elements
                      if tag == "a" and attrs.get("href") == waitlist_href(inputs)]
    if not waitlist_links or not any("waitlist" in {parent_id for _, parent_id in parents} for _, parents in waitlist_links):
        failures.append("hosted_waitlist_unwired")
    if plan.brief.cta not in visible:
        failures.append("waitlist_cta_missing")
    for action in qa_contract(inputs, plan, imagery).actions:
        if action.page != "index.html":
            continue
        target = "#" + action.target if action.kind == "anchor" else action.target
        links = [(attrs, parents) for tag, attrs, parents in elements if tag == "a" and attrs.get("href") == target
                 and (not is_local() or action.id not in {"hero_cta", "waitlist_cta"} or attrs.get("id") == action.id)
                 and _copy_key(attrs.get("_label", "")) == _copy_key(action.name)
                 and attrs.get("aria-label", " ".join(attrs.get("_label", "").split())) == action.name
                 and not attrs.get("aria-labelledby")]
        if len(links) != 1:
            failures.append("approved_action_missing_or_ambiguous:" + action.id)
        elif links[0][0].get("id") != action.id:
            failures.append("approved_action_id_missing:" + action.id)
        elif action.kind == "external_link" and (links[0][0].get("target") != "_blank"
                or not {"noopener", "noreferrer"} <= set(links[0][0].get("rel", "").lower().split())):
            failures.append("waitlist_preview_navigation_unsafe")
    actions = {action.id: action for action in qa_contract(inputs, plan).actions if action.page == "index.html"}
    controls = [(tag, attrs) for tag, attrs, parents in elements if not (native and "request_form" in {identity for _, identity in parents}) and (tag in NATIVE_CONTROLS
                or set((attrs.get("role") or "").casefold().split()) & INTERACTIVE_ROLES
                or "tabindex" in attrs and (attrs.get("tabindex") or "").strip() != "-1"
                or "contenteditable" in attrs and (attrs.get("contenteditable") or "").casefold() in {"", "true", "plaintext-only"})]
    extras, matched = [], set()
    for tag, attrs in controls:
        action = actions.get(attrs.get("id"))
        label = " ".join((attrs.get("aria-label", attrs.get("_label", attrs.get("value", ""))) or "").split())
        target = attrs.get("href", attrs.get("formaction", "")) or ""
        approved = (action and tag == "a" and (attrs.get("role") or "link").casefold() == "link"
                    and ("contenteditable" not in attrs or (attrs.get("contenteditable") or "").casefold() == "false")
                    and "hidden" not in attrs and "disabled" not in attrs
                    and (attrs.get("aria-hidden") or "").casefold() != "true"
                    and (attrs.get("aria-disabled") or "").casefold() != "true"
                    and action.id not in matched and label == action.name and not attrs.get("aria-labelledby")
                    and _copy_key(attrs.get("_label", "")) == _copy_key(action.name)
                    and target == ("#" + action.target if action.kind == "anchor" else action.target))
        if approved:
            matched.add(action.id)
        else:
            extras.append({"id": (attrs.get("id") or "")[:160], "text": label[:160],
                           "target": target[:200], "type": (attrs.get("type") or tag)[:40]})
            for attribute in ("role", "tabindex", "contenteditable"):
                if attribute in attrs:
                    extras[-1][attribute] = (attrs[attribute] or "")[:80]
    if len(controls) != len(actions) or extras:
        failures.append("unapproved_controls")
        diagnostics["unapproved_controls"] = {"expected_count": len(actions), "actual_count": len(controls), "extras": extras[:20]}
    # tinycss2 decodes escapes; a regex alone would miss escaped url/@import.
    import tinycss2
    def inspect_content(declaration):
        if declaration.type == "declaration" and declaration.lower_name == "content":
            for token in declaration.value:
                if token.type == "string":
                    check_reviewed_copy(token.value, "css.content")
                elif token.type not in {"whitespace", "comment", "literal"} and not (token.type == "ident" and token.value in {"none", "normal"}):
                    failures.append("honest_copy:unreviewed_dynamic_css_content")
    def inspect_css(tokens):
        for token in tokens:
            inspect_content(token)
            if getattr(token, "content", None):
                for declaration in tinycss2.parse_declaration_list(token.content):
                    inspect_content(declaration)
            if token.type == "at-rule" and token.lower_at_keyword == "import":
                failures.append("css_external_import")
            if token.type == "url":
                if token.value not in files:
                    failures.append("css_resource_not_pinned")
            if token.type == "function" and token.lower_name == "url":
                url = tinycss2.serialize(token.arguments).strip().strip("\"'")
                if url not in files:
                    failures.append("css_resource_not_pinned")
            for name in ("content", "arguments", "prelude"):
                nested = getattr(token, name, None)
                if nested:
                    inspect_css(nested)
    stylesheet = tinycss2.parse_stylesheet("\n".join(parser.styles), skip_whitespace=True, skip_comments=True)
    inspect_css(stylesheet)
    failures.extend(_motion_failures(stylesheet))
    for _, attrs, _ in elements:
        if "style" in attrs:
            inspect_css(tinycss2.parse_declaration_list(attrs["style"]))
            if any(item.type == "declaration" and (item.lower_name in _MOTION or "timeline" in item.lower_name)
                   for item in tinycss2.parse_declaration_list(attrs["style"])):
                failures.append("motion_not_reduced_motion_guarded")
    return {"passed": not failures, "failures": sorted(set(failures)), "diagnostics": diagnostics,
            "contract": qa_contract(inputs, plan, imagery).model_dump(mode="json"),
            "checks": ["honest_copy", "reviewed_copy_pinning", "grounded_waitlist_copy", "semantic_html", "pinned_images", "image_placement", "hosted_consent_waitlist", "no_external_resources", "size_budget", "reduced_motion_safe_css"]}
