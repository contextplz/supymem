"""First-launch Charlie greeting: name, TTS (mocked), cache, once-per-account flag."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

H = {"X-Subject": "usr_cog_test"}


class _Resp:
    content = b"ID3fake-mp3"

    def raise_for_status(self) -> None:
        pass


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/greet.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "sk-test")
    main = load_company_os_app()
    from app import state as state_mod
    from app.api import greeting_api

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    greeting_api._cache.clear()
    calls: list[dict] = []
    monkeypatch.setattr(greeting_api.httpx, "post", lambda url, **kw: calls.append(kw) or _Resp())
    with TestClient(main.create_app()) as c:
        c.calls = calls
        yield c


def test_requires_auth(client):
    assert client.get("/me/greeting").status_code == 401
    assert client.post("/me/greeting/audio").status_code == 401
    assert client.post("/me/greeting/done", json={"outcome": "completed"}).status_code == 401


def test_name_validation_and_greeting_text(client):
    assert client.get("/me/greeting", headers=H).json()["text"].startswith("Hey there, I'm Charlie")
    for bad in ["", "  ", "x" * 41, "Bob<script>", "R2D2"]:
        assert client.post("/me/profile", json={"first_name": bad}, headers=H).status_code == 422
    assert client.post("/me/profile", json={"first_name": "  Mary-Jane  "}, headers=H).status_code == 200
    got = client.get("/me/greeting", headers=H).json()
    assert got["first_name"] == "Mary-Jane" and got["text"].startswith("Hey Mary-Jane, I'm Charlie")


def test_sign_in_sync_never_overwrites_an_edited_name(client):
    sync = {"first_name": "Google", "if_unset": True}
    client.post("/me/profile", json=sync, headers=H)  # first sign-in fills it
    assert client.get("/me/greeting", headers=H).json()["first_name"] == "Google"
    client.post("/me/profile", json={"first_name": "Maya"}, headers=H)  # the person edits it
    client.post("/me/profile", json=sync, headers=H)  # next sign-in
    assert client.get("/me/greeting", headers=H).json()["first_name"] == "Maya"


@pytest.mark.parametrize("voice_override", [None, "ash"])
def test_audio_is_mocked_cached_and_named(client, monkeypatch, voice_override):
    if voice_override:
        monkeypatch.setenv("SUPYMEM_VOICE_VOICE", voice_override)
    else:
        monkeypatch.delenv("SUPYMEM_VOICE_VOICE", raising=False)
    client.post("/me/profile", json={"first_name": "Ada"}, headers=H)
    a = client.post("/me/greeting/audio", headers=H).json()
    b = client.post("/me/greeting/audio", headers=H).json()
    assert a == b and a["mime"] == "audio/mpeg" and "Hey Ada" in a["text"]
    assert base64.b64decode(a["audio_base64"]) == b"ID3fake-mp3"
    assert len(client.calls) == 1
    sent = client.calls[0]["json"]
    assert sent["model"] == "gpt-4o-mini-tts" and sent["voice"] == (voice_override or "marin") and sent["response_format"] == "mp3"


def test_greeted_flag_is_once_and_blocks_audio(client):
    assert client.get("/me/greeting", headers=H).json()["greeted_at"] is None
    assert client.post("/me/greeting/done", json={"outcome": "completed"}, headers=H).status_code == 200
    first = client.get("/me/greeting", headers=H).json()["greeted_at"]
    assert first
    client.post("/me/greeting/done", json={"outcome": "completed"}, headers=H)
    assert client.get("/me/greeting", headers=H).json()["greeted_at"] == first
    assert client.post("/me/greeting/audio", headers=H).status_code == 404
    assert client.post("/me/greeting/audio?replay=1", headers=H).status_code == 200
    other = {"X-Subject": "usr_cog_other"}
    assert client.get("/me/greeting", headers=other).json()["greeted_at"] is None


@pytest.mark.parametrize("outcome", ["completed", "skipped"])
def test_failed_audio_keeps_intro_available_until_explicit_terminal_outcome(client, outcome):
    assert client.post("/me/greeting/done", json={"outcome": "failed"}, headers=H).status_code == 200
    failed = client.get("/me/greeting", headers=H).json()
    assert failed["greeted_at"] is None and failed["greeting_outcome"] == "failed"
    assert client.post("/me/greeting/audio", headers=H).status_code == 200
    assert client.post("/me/greeting/done", json={"outcome": outcome}, headers=H).status_code == 200
    done = client.get("/me/greeting", headers=H).json()
    assert done["greeted_at"] and done["greeting_outcome"] == outcome
    client.post("/me/greeting/done", json={"outcome": "failed"}, headers=H)
    assert client.get("/me/greeting", headers=H).json() == done


@pytest.mark.parametrize("body", [{}, {"outcome": "heard"}, {"outcome": "failed", "transcript": "private"}])
def test_greeting_outcome_is_required_and_closed(client, body):
    assert client.post("/me/greeting/done", json=body, headers=H).status_code == 422
    assert client.get("/me/greeting", headers=H).json()["greeted_at"] is None
