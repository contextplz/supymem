"""First-launch Charlie greeting: sign-in first name, spoken greeting, once-per-account flag.

State lives in the person's Desk ``settings_json`` (``first_name``, ``greeted_at``), so it is
per account, not per device, and needs no migration. The first name is display data only.
The greeting text is built server-side; the client never supplies text to synthesize.
"""

from __future__ import annotations

from app.workspaces import service as _service

import base64
import hashlib
import os
import re
import threading
from typing import Literal
from collections import OrderedDict

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.voice_api import DEFAULT_VOICE, VOICE_SETUP_MESSAGE, _api_key
from app.authz.workspace import ensure_subject_person
from app.core.local_mode import is_local
from app.credits.errors import CreditError
from core.clock import utcnow

router = APIRouter(tags=["greeting"])

TTS_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
_NAME_OK = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '’-])*$")
_CACHE_MAX = 64
_cache: OrderedDict[str, bytes] = OrderedDict()
_lock = threading.Lock()


def clean_first_name(value: object) -> str | None:
    name = value.strip() if isinstance(value, str) else ""
    return name if 0 < len(name) <= 40 and _NAME_OK.match(name) else None


def greeting_text(first_name: str | None) -> str:
    who = first_name or "there"
    return (f"Hey {who}, I'm Charlie, think of me as your Jarvis. I live here and I'm here to guide "
            "you on your product and your journey. Feel free to call me in anytime if something's unclear.")


def _cache_key(voice: str, text: str) -> str:
    return hashlib.sha256(f"{voice}\n{text}".encode()).hexdigest()


def _state(person_id: str) -> dict:
    return _service.greeting_state(person_id)


def _update(person_id: str, **fields: str) -> None:
    return _service.greeting_update(person_id, **fields)


def _caller(request: Request, x_subject: str | None) -> str:
    from app.admin.authn import verify_caller

    if not x_subject:
        raise HTTPException(401, "unauthenticated")
    return ensure_subject_person(verify_caller(request).sub)


def synthesize(text: str, voice: str, key: str) -> bytes:
    """mp3 bytes for ``text``; cached in a bounded in-memory LRU."""
    ck = _cache_key(voice, text)
    with _lock:
        if ck in _cache:
            _cache.move_to_end(ck)
            return _cache[ck]
    model = DEFAULT_TTS_MODEL if is_local() else os.environ.get("SUPYMEM_GREETING_TTS_MODEL") or DEFAULT_TTS_MODEL
    def speak():
        response = httpx.post(
            TTS_URL, timeout=30,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "voice": voice, "input": text, "response_format": "mp3",
                  "instructions": "Warm, friendly and calm, like a trusted guide welcoming someone."},
        )
        response.raise_for_status()
        return response.content
    if is_local():
        from app.credits.metered import call_sync, price_micro
        # shortcut: reserve estimated speech tokens for this fixed greeting; use provider audio usage when available.
        estimate = price_micro("tts_input_token", len(text)) + price_micro("tts_output_audio_token", len(text) * 2)
        audio = call_sync(speak, purpose="greeting.tts", estimate_micro=estimate, provider="openai")
    else:
        audio = speak()
    with _lock:
        _cache[ck] = audio
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return audio


class ProfileBody(BaseModel):
    first_name: str
    if_unset: bool = False  # sign-in sync sets this so a name the person edited is never overwritten


@router.get("/me/greeting")
def get_greeting(request: Request, x_subject: str | None = Header(default=None, alias="X-Subject")):
    state = _state(_caller(request, x_subject))
    name = clean_first_name(state.get("first_name"))
    return {"first_name": name, "greeted_at": state.get("greeted_at"),
            "greeting_outcome": state.get("greeting_outcome"), "text": greeting_text(name)}


@router.post("/me/profile")
def set_profile(body: ProfileBody, request: Request,
                x_subject: str | None = Header(default=None, alias="X-Subject")):
    name = clean_first_name(body.first_name)
    if name is None:
        raise HTTPException(422, detail={"error": {"code": "invalid_first_name"}})
    person = _caller(request, x_subject)
    if not (body.if_unset and clean_first_name(_state(person).get("first_name"))):
        _update(person, first_name=name)
    return {"ok": True}


@router.post("/me/greeting/audio")
def greeting_audio(request: Request, replay: int = 0, x_subject: str | None = Header(default=None, alias="X-Subject")):
    person = _caller(request, x_subject)
    state = _state(person)
    if state.get("greeted_at") and not replay:  # replay is a dev/test aid; it only re-reads cached audio
        raise HTTPException(404, "not_found")
    key = _api_key()
    if not key:
        raise HTTPException(503, detail={"error": {"code": "greeting_audio_unavailable", "message": VOICE_SETUP_MESSAGE}})
    text = greeting_text(clean_first_name(state.get("first_name")))
    try:
        voice = DEFAULT_VOICE if is_local() else os.environ.get("SUPYMEM_VOICE_VOICE") or DEFAULT_VOICE
        audio = synthesize(text, voice, key)
    except (httpx.HTTPError, CreditError) as exc:
        raise HTTPException(503, detail={"error": {"code": "greeting_audio_unavailable",
            "message": "Open Settings > Keys and connections to check your OpenAI key and spending cap, then retry your spoken greeting. You can skip it and keep typing."}}) from exc
    return {"text": text, "mime": "audio/mpeg", "audio_base64": base64.b64encode(audio).decode()}


class GreetingDone(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["completed", "skipped", "failed"]


@router.post("/me/greeting/done")
def greeting_done(request: Request, body: GreetingDone,
                  x_subject: str | None = Header(default=None, alias="X-Subject")):
    person = _caller(request, x_subject)
    fields = {"greeting_outcome": body.outcome}
    if body.outcome in {"completed", "skipped"}:
        fields["greeted_at"] = utcnow().isoformat()
    _update(person, **fields)
    return {"ok": True}
