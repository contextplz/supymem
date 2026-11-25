"""Deterministic page excerpts budgeted by JSON bytes, including escaping."""
import bisect
import json
import re


def bounded_page_text(text, task, max_bytes):
    if len(json.dumps(text).encode()) <= max_bytes:
        return text
    prior = re.search(r"\n\[truncated ([0-9]{1,12}) chars\]$", text)
    source_length = len(text)
    if prior:
        source_length = prior.start() + int(prior.group(1))
        text = text[:prior.start()]
    marker = f"\n[truncated {source_length} chars]"
    remaining = max_bytes - len(json.dumps(marker).encode())
    if remaining <= 0:
        return ""
    chunks, starts = [], []
    identities, headings, viewport, page = [], [], [], []
    section = page
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.startswith(("PAGE TITLE:", "PAGE URL:")):
            section = identities
        elif line.startswith("HEADING:"):
            section = headings
        elif line.startswith("VIEWPORT:"):
            section = viewport
        elif line.startswith("PAGE TEXT:"):
            section = page
        start = 0
        while start < len(line):
            end = min(len(line), start + 160)
            if end < len(line):
                boundary = max(line.rfind(" ", start, end), line.rfind("\t", start, end))
                if boundary > start:
                    end = boundary + 1
            section.append(len(chunks))
            starts.append(offset + start)
            chunks.append(line[start:end])
            start = end
        offset += len(line)
    terms = set(re.findall(r"[^\W_]{3,}", task.casefold()))
    heading_terms = set(terms)
    page_set, heading_set = set(page), set(headings)
    matches, heading_matches = {}, []
    for match in re.finditer(r"[^\W_]{3,}", text):
        word = match.group().casefold()
        index = bisect.bisect_right(starts, match.start()) - 1
        if word in terms:
            if index in heading_set and word in heading_terms:
                heading_matches.append(index)
                heading_terms.remove(word)
            elif index in page_set:
                matches.setdefault(index, match.start() - starts[index])
                terms.remove(word)
    # ponytail: representative task-term excerpts, use bounded indexed reads for exhaustive documents.
    first_screens = [*viewport[:3], *page[:4]] if identities else []
    priorities = dict.fromkeys([*identities, *heading_matches, *headings[:6], *first_screens,
        *matches, *headings, *viewport, *page])
    kept = {}
    for index in priorities:
        chunk = chunks[index]
        cost = len(json.dumps(chunk).encode())
        if cost > remaining:
            if kept and index not in matches:
                continue
            if index in matches:
                chunk = chunk[matches[index]:]
            low, high = 0, len(chunk)
            while low < high:
                middle = (low + high + 1) // 2
                if len(json.dumps(chunk[:middle]).encode()) <= remaining:
                    low = middle
                else:
                    high = middle - 1
            chunk = chunk[:low]
            cost = len(json.dumps(chunk).encode())
        if chunk:
            kept[index] = chunk
            remaining -= cost
        if remaining <= 0:
            break
    parts, previous = [], -1
    for index in sorted(kept):
        if parts and (index != previous + 1 or len(kept[previous]) != len(chunks[previous])):
            parts.append("\n")
        parts.append(kept[index])
        previous = index
    retained = "".join(parts)
    return retained + f"\n[truncated {source_length - len(retained)} chars]"


def bounded_page_observation(observation, page_bytes):
    context = bounded_page_text(observation.context, observation.goal, max(0, page_bytes // 2))
    available = page_bytes - len(json.dumps(context).encode())
    controls = []
    for control in observation.controls:
        label = bounded_page_text(control.label, observation.goal, 120) or "?"
        projected = control.model_copy(update={"label": label})
        cost = len(json.dumps(projected.model_dump()).encode()) + 2
        if cost > available:
            break
        controls.append(projected)
        available -= cost
    if len(controls) < len(observation.controls):
        marker = "\n[controls omitted; absence is not verified]"
        budget = max(0, page_bytes // 2)
        context = bounded_page_text(context, observation.goal, max(0, budget - len(json.dumps(marker).encode())))
        if len(json.dumps(context + marker).encode()) <= budget:
            context += marker
    return observation.model_copy(update={"context": context, "controls": controls})
