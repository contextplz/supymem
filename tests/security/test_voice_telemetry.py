"""Voice observations bind closed content-free metrics to a minted, owned session."""
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.security.test_voice_session import _mock_openai, _venture
from tests.security.test_core_signed_boundary import client, headers, make_workspace  # noqa: F401


def minted(http, monkeypatch, vid=None, actor="usr_alice"):
    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "sk-test-voice-no-real-network")
    _mock_openai(monkeypatch, [])
    prefix = f"/ventures/{vid}" if vid else "/entry"
    path = f"{prefix}/voice/session"
    reply = http.post(path, headers=headers(path, "POST", actor))
    assert reply.status_code == 200, reply.text
    return f"{prefix}/voice/telemetry", reply.json()["voice_telemetry_id"]


def metric(session_id, name="barge_in_ignored", **extra):
    return {"event_id": str(uuid4()), "session_event_id": session_id, "metric": name, "mode": "vad", **extra}


def post(http, path, body, actor="usr_alice"):
    return http.post(path, json=body, headers=headers(path, "POST", actor))


def test_owned_session_metrics_deduplicate_and_summary_uses_only_closed_data(client, monkeypatch):  # noqa: F811
    from app.analytics.funnel import voice_summary
    from app.db.models import AnalyticsEvent
    from app.state import db

    vid = _venture(client)
    path, session_id = minted(client, monkeypatch, vid)
    body = metric(session_id)
    first = post(client, path, body)
    assert first.status_code == 200, first.text
    assert post(client, path, body).json() == first.json()
    changed = {**body, "metric": "barge_in_accepted"}
    assert post(client, path, changed).status_code == 409
    for name in ("barge_in_accepted", "response_cancelled", "duplicate_response_blocked", "transcription_failed"):
        assert post(client, path, metric(session_id, name)).status_code == 200
    for duration in (400, 800):
        assert post(client, path, metric(session_id, "turn_latency", duration_ms=duration)).status_code == 200
    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(AnalyticsEvent.event_name == "voice.metric")).all()
        assert len(rows) == 7
        props = json.loads(rows[0].props_json)
        assert set(props) == {"session_event_id", "metric", "mode"}
        summary = voice_summary(session, {"usr_alice"})
        assert summary["sessions_minted"] == summary["sessions_observed"] == 1
        assert summary["modes"]["vad"] == {
            "barge_in_accepted": 1, "barge_in_ignored": 1, "response_cancelled": 1,
            "duplicate_response_blocked": 1, "transcription_failed": 1,
            "turn_latency_samples": 2, "average_turn_latency_ms": 600,
        }
        assert summary["modes"]["ptt"]["average_turn_latency_ms"] is None
        assert voice_summary(session, {"usr_bob"})["sessions_observed"] == 0


@pytest.mark.parametrize("change", [
    {"transcript": "private founder words"}, {"error": "private provider words"},
    {"metric": "any_private_words"}, {"mode": "secret"}, {"duration_ms": 10},
    {"metric": "turn_latency"}, {"metric": "turn_latency", "duration_ms": -1},
    {"metric": "turn_latency", "duration_ms": 600001},
    {"metric": "turn_latency", "duration_ms": "500"},
    {"session_event_id": "private text"},
])
def test_voice_metrics_reject_text_unknown_fields_and_invalid_timings(client, monkeypatch, change):  # noqa: F811
    path, session_id = minted(client, monkeypatch)
    result = post(client, path, {**metric(session_id), **change})
    assert result.status_code == 422, result.text


def test_foreign_wrong_scope_unknown_and_expired_session_are_hidden(client, monkeypatch):  # noqa: F811
    from core.clock import utcnow

    vid = _venture(client)
    path, session_id = minted(client, monkeypatch, vid)
    make_workspace(client, "usr_bob")
    assert post(client, path, metric(session_id), "usr_bob").status_code == 404
    assert post(client, "/entry/voice/telemetry", metric(session_id)).status_code == 404
    assert post(client, path, metric("ane_unknown")).status_code == 404
    assert client.post(path, json=metric(session_id), headers={"X-Subject": "usr_alice"}).status_code == 401
    future = utcnow() + timedelta(minutes=21)
    monkeypatch.setattr("app.api.voice_api.utcnow", lambda: future)
    assert post(client, path, metric(session_id)).status_code == 404


def test_entry_metrics_work_without_a_venture(client, monkeypatch):  # noqa: F811
    path, session_id = minted(client, monkeypatch, actor="usr_new_voice")
    result = post(client, path, metric(session_id, "turn_latency", duration_ms=950, mode="ptt"), "usr_new_voice")
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("name", ["mic_permission_ms", "mint_ms", "sdp_ms", "first_remote_audio_ms",
                                 "local_playing_ms", "transcription_ms", "response_ms"])
def test_playback_timings_are_closed_bounded_and_require_duration(client, monkeypatch, name):  # noqa: F811
    path, session_id = minted(client, monkeypatch)
    assert post(client, path, metric(session_id, name, duration_ms=123)).status_code == 200
    assert post(client, path, metric(session_id, name)).status_code == 422
    assert post(client, path, metric(session_id, name, duration_ms=600001)).status_code == 422
    assert post(client, path, metric(session_id, name, duration_ms=123, device_name="private")).status_code == 422
    assert post(client, path, metric(session_id, "playback_blocked")).status_code == 200
    assert post(client, path, metric(session_id, "playback_blocked", duration_ms=123)).status_code == 422


def test_concurrent_free_entry_sessions_share_the_daily_cap(client, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    import time

    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    def mint(*args):
        time.sleep(0.02)  # creates overlapping reservations without provider/network spend
        return {"client_secret": "ephemeral-test"}
    monkeypatch.setattr("app.api.voice_api._mint", mint)
    path = "/entry/voice/session"
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: client.post(path, headers=headers(path, "POST", "usr_voice_cap_race")).status_code, range(8)))
    assert sorted(statuses) == [200] * 5 + [429] * 3


def test_failed_entry_mint_releases_daily_reservation(client, monkeypatch):  # noqa: F811
    from fastapi import HTTPException
    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    def failed(*args):
        raise HTTPException(502, "provider unavailable")
    monkeypatch.setattr("app.api.voice_api._mint", failed)
    path = "/entry/voice/session"
    for _ in range(7):
        assert client.post(path, headers=headers(path, "POST", "usr_voice_failed_mint")).status_code == 502
    monkeypatch.setattr("app.api.voice_api._mint", lambda *args: {"client_secret": "ephemeral-test"})
    assert client.post(path, headers=headers(path, "POST", "usr_voice_failed_mint")).status_code == 200
