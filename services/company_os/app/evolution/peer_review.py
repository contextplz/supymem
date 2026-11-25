"""Peer review — self-review is structurally impossible."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Review:
    proposal_id: str
    authored_by_agent_id: str
    reviewed_by_agent_id: str
    verdict: str


def review(proposal_id: str, *, author_id: str, reviewer_id: str, verdict: str = "approve") -> Review:
    if not reviewer_id or reviewer_id == author_id:
        raise PermissionError("self_review_structurally_impossible")
    return Review(proposal_id, author_id, reviewer_id, verdict)
