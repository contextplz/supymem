"""Offline publication signatures and canonical outbox; no live feed/send."""

import base64
import hashlib
import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.integration.test_postgres_auth_security import service_modules
from tests.unit.test_email_engine import env as env  # noqa: F401


def signed(payload):
    private = Ed25519PrivateKey.generate()
    key_id = b"testkey1"
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key = (
        "untrusted comment: offline fixture\n"
        + base64.b64encode(b"Ed" + key_id + public).decode()
        + "\n"
    )
    signature = private.sign(hashlib.blake2b(payload, digest_size=64).digest())
    comment = "timestamp:0 file:Supymem.app.tar.gz"
    sig = (
        "untrusted comment: offline fixture\n"
        + base64.b64encode(b"ED" + key_id + signature).decode()
        + "\ntrusted comment: "
        + comment
        + "\n"
        + base64.b64encode(private.sign(signature + comment.encode())).decode()
        + "\n"
    )
    return base64.b64encode(key.encode()).decode(), base64.b64encode(
        sig.encode()
    ).decode()


def fixture_feed(source, monkeypatch):
    # Genuine cryptographic fixture signatures, synthetic bytes; not a packaged/notarized app.
    payload = b"owned offline artifact fixture" * 8000
    key, signature = signed(payload)
    monkeypatch.setattr(source, "UPDATER_PUBLIC_KEY", key)
    sha = hashlib.sha256(payload).hexdigest()
    manifest = {
        "version": "0.1.84",
        "notes": '<!--cos-update {"mandatory":true}-->Required Mac release.',
        "platforms": {
            "darwin-aarch64": {
                "url": f"https://supymem.com/downloads/0.1.84/{sha}/Supymem.app.tar.gz",
                "signature": signature,
            }
        },
    }
    data = {
        "manifest": manifest,
        "artifact": payload,
        "status": 200,
        "hits": [],
        "headers": {},
    }

    def request(req):
        data["hits"].append(str(req.url))
        if str(req.url) == source.MANIFEST_URL:
            return httpx.Response(
                data["status"],
                content=json.dumps(data["manifest"]).encode(),
                headers=data["headers"],
            )
        assert str(req.url) == manifest["platforms"]["darwin-aarch64"]["url"]
        return httpx.Response(
            200,
            stream=httpx.ByteStream(data["artifact"]),
            headers={"Content-Length": str(len(data["artifact"]))},
        )

    monkeypatch.setattr(
        source,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(request),
            follow_redirects=False,
            trust_env=False,
        ),
    )
    return data


@pytest.fixture
def publication(monkeypatch):
    with service_modules("company_os"):
        from app.release_notices import publication as source

        yield source, fixture_feed(source, monkeypatch)


def test_production_key_matches_publisher_config():
    with service_modules("company_os"):
        from app.release_notices import publication as source

        root = Path(__file__).resolve().parents[3]
        assert (
            source.UPDATER_PUBLIC_KEY
            == json.loads((root / "src-tauri/tauri.conf.json").read_text())["plugins"][
                "updater"
            ]["pubkey"]
        )


def test_client_refuses_redirect_proxy_and_uses_bounded_timeouts(monkeypatch):
    with service_modules("company_os"):
        from app.release_notices import publication as source

        captured = {}
        monkeypatch.setattr(
            source.httpx, "Client", lambda **kwargs: captured.update(kwargs)
        )
        source.client()
        assert captured["follow_redirects"] is False and captured["trust_env"] is False
        assert captured["timeout"].read == 5 and captured["timeout"].connect == 3
        assert captured["headers"] == {"Accept-Encoding": "identity"}


def test_streamed_artifact_pins_both_digests_and_current_signature(publication):
    source, data = publication
    with source.client() as client:
        release = source.fetch_manifest(client, time.monotonic() + 60)
        witness = source.verify_artifact(client, release, time.monotonic() + 60)
    assert witness["artifact_sha256"] == hashlib.sha256(data["artifact"]).hexdigest()
    assert (
        witness["artifact_blake2b"]
        == hashlib.blake2b(data["artifact"], digest_size=64).hexdigest()
    )
    assert witness["artifact_bytes"] == len(data["artifact"])
    source.verify_witness(release, witness)
    assert release.required_for("0.1.83") and not release.required_for("0.1.84")
    assert not release.required_for(None) and not release.required_for("0.1.83beta")
    for changed in (
        {**witness, "artifact_blake2b": "a" * 128},
        {**witness, "artifact_sha256": "a" * 64},
        {**witness, "artifact_url": "https://foreign.invalid/"},
        {**witness, "artifact_bytes": True},
    ):
        with pytest.raises(source.ReleaseUnavailable):
            source.verify_witness(release, changed)


@pytest.mark.parametrize(
    "mutation",
    [
        "truthy",
        "duplicate",
        "malformed_first_marker",
        "no_policy",
        "security_prose",
        "security_flag",
        "fixture",
        "foreign",
        "query",
        "control_bytes",
        "redirect_url",
        "traversal",
        "encoded_path",
        "platform",
        "version",
        "future_minimum",
        "signature",
    ],
)
def test_malformed_or_untrusted_metadata_fails_closed(publication, mutation):
    source, data = publication
    m = data["manifest"]
    if mutation == "truthy":
        m["notes"] = '<!--cos-update {"mandatory":"false"}-->'
    elif mutation == "duplicate":
        m["notes"] = '<!--cos-update {"mandatory":true,"mandatory":false}-->'
    elif mutation == "malformed_first_marker":
        m["notes"] = '<!--cos-update bad-->\n<!--cos-update {"mandatory":true}-->'
    elif mutation == "no_policy":
        m["notes"] = "A release without explicit policy"
    elif mutation == "security_prose":
        m["notes"] = "Security! Update immediately"
    elif mutation == "security_flag":
        m["notes"] = '<!--cos-update {"mandatory":false,"security":true}-->'
    elif mutation == "fixture":
        m["test_fixture"] = True
    elif mutation == "foreign":
        m["platforms"]["darwin-aarch64"]["url"] = (
            "https://foreign.invalid/downloads/0.1.84/"
            + "a" * 64
            + "/Supymem.app.tar.gz"
        )
    elif mutation == "query":
        m["platforms"]["darwin-aarch64"]["url"] += "?download=1"
    elif mutation == "control_bytes":
        m["platforms"]["darwin-aarch64"]["url"] += "\n"
    elif mutation == "redirect_url":
        m["platforms"]["darwin-aarch64"]["url"] = m["platforms"]["darwin-aarch64"][
            "url"
        ].replace("supymem.com", "user@supymem.com")
    elif mutation == "traversal":
        m["platforms"]["darwin-aarch64"]["url"] += "/../other"
    elif mutation == "encoded_path":
        m["platforms"]["darwin-aarch64"]["url"] = m["platforms"]["darwin-aarch64"][
            "url"
        ].replace("Supymem.app", "%2e%2e%2fSupymem.app")
    elif mutation == "platform":
        m["platforms"]["linux-x86_64"] = m["platforms"]["darwin-aarch64"]
    elif mutation == "version":
        m["version"] = "0.1.84beta"
    elif mutation == "future_minimum":
        m["notes"] = '<!--cos-update {"mandatory":false,"min_version":"0.1.85"}-->'
    elif mutation == "signature":
        m["platforms"]["darwin-aarch64"]["signature"] = "unsigned-local-fixture"
    with pytest.raises(source.ReleaseUnavailable):
        source.parse_manifest(json.dumps(m).encode())
    assert not data["hits"]


@pytest.mark.parametrize(
    "failure",
    [
        "redirect",
        "404",
        "compressed",
        "manifest_bound",
        "artifact_bound",
        "bytes",
        "expired",
    ],
)
def test_unavailable_bounded_or_tampered_feed_has_no_verified_artifact(
    publication, monkeypatch, failure
):
    source, data = publication
    if failure == "redirect":
        data["status"], data["headers"] = 302, {"Location": "https://foreign.invalid/"}
    elif failure == "404":
        data["status"] = 404
    elif failure == "compressed":
        data["headers"] = {"Content-Encoding": "gzip"}
    elif failure == "manifest_bound":
        data["headers"] = {"Content-Length": str(source.MAX_MANIFEST_BYTES + 1)}
    elif failure == "artifact_bound":
        monkeypatch.setattr(source, "MAX_ARTIFACT_BYTES", 5)
    elif failure == "bytes":
        data["artifact"] = b"tampered"
    with source.client() as client, pytest.raises(source.ReleaseUnavailable):
        deadline = (
            time.monotonic() - 1 if failure == "expired" else time.monotonic() + 60
        )
        release = source.fetch_manifest(client, deadline)
        source.verify_artifact(client, release, deadline)
    assert all(url.startswith("https://supymem.com/downloads/") for url in data["hits"])


def enable(service, monkeypatch):
    from app.email import templates

    assert templates.category_of("app_release") == "product_updates"
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "app_release")


def test_deep_under_limit_json_is_source_unavailable(publication):
    source, _data = publication
    raw = b"[" * 4000 + b"0" + b"]" * 4000
    assert len(raw) < source.MAX_MANIFEST_BYTES
    with pytest.raises(source.ReleaseUnavailable, match="release_manifest_invalid"):
        source.parse_manifest(raw)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_manifest_accepts_only_native_updater_utf8(publication, encoding):
    source, data = publication
    with pytest.raises(source.ReleaseUnavailable, match="release_manifest_invalid"):
        source.parse_manifest(json.dumps(data["manifest"]).encode(encoding))


def test_non_ascii_content_length_is_typed_source_unavailable(publication):
    source, _data = publication
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, content=b"{}", headers=[(b"Content-Length", b"\xb2")]))) as client:
        with pytest.raises(source.ReleaseUnavailable, match="release_size_invalid"):
            source.fetch_manifest(client, time.monotonic() + 60)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numbers_cannot_authorize_native_release(publication, constant):
    source, data = publication
    raw = json.dumps(data["manifest"]).encode()
    raw = b'{"ignored":' + constant.encode() + b"," + raw[1:]
    with pytest.raises(source.ReleaseUnavailable, match="release_manifest_invalid"):
        source.parse_manifest(raw)


@pytest.mark.parametrize("date", [123, "not-a-date", "2026-13-05T22:30:00Z", "2026-10-05 22:30:00Z"])
def test_invalid_native_publication_date_is_source_unavailable(publication, date):
    source, data = publication
    data["manifest"]["pub_date"] = date
    with pytest.raises(source.ReleaseUnavailable, match="release_manifest_invalid"):
        source.parse_manifest(json.dumps(data["manifest"]).encode())


@pytest.mark.parametrize("date", [None, "2026-10-05T22:30:00Z", "2026-10-06T04:00:00.123+05:30"])
def test_current_native_publication_date_is_accepted(publication, date):
    source, data = publication
    data["manifest"]["pub_date"] = date
    assert source.parse_manifest(json.dumps(data["manifest"]).encode()).version == "0.1.84"


def test_real_hourly_handler_runs_registered_release_reconciliation(monkeypatch):
    with service_modules("company_os"):
        from app.email import triggers
        from app.release_notices import service
        from app.worker.handlers import _email_schedule
        calls = []
        monkeypatch.setattr(triggers, "run_schedule", lambda: calls.append("canonical"))
        monkeypatch.setattr(service, "reconcile", lambda: calls.append("release"))
        _email_schedule({})
        assert calls == ["canonical", "release"]


@pytest.fixture
def notices(env, monkeypatch):
    from app.db.supymem_models import DesktopDevice
    from app.email import service as email
    from app.release_notices import service, publication as source

    data = fixture_feed(source, monkeypatch)
    enable(service, monkeypatch)
    state = env[1]
    now = email.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(email, "utcnow", lambda: now)
    with state.db()() as s:
        s.add(
            DesktopDevice(
                id="dev_release_owned",
                person_id="usr_a",
                name="Owned Mac",
                platform="macos",
                app_version="0.1.83",
            )
        )
        s.commit()
    return service, source, state, data, now


def test_explicit_rollout_gate_precedes_any_feed_io(notices, monkeypatch):
    service, _source, _state, data, _now = notices
    for flag, allowlist in [
        ("", "app_release"),
        ("0", "app_release"),
        ("1", ""),
        ("1", "milestone"),
    ]:
        monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", flag)
        monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", allowlist)
        assert service.reconcile() == {"status": "disabled", "queued": 0}
    assert data["hits"] == []


def test_durable_public_witness_and_person_version_dedupe_survive_policy_change(
    notices,
):
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import EmailMessage
    from sqlalchemy import select

    service, source, state, data, _now = notices
    assert service.reconcile() == {"status": "verified", "queued": 1}
    artifact_hits = sum(url != source.MANIFEST_URL for url in data["hits"])
    assert service.reconcile()["queued"] == 0
    data["manifest"]["notes"] = (
        '<!--cos-update {"mandatory":true,"min_version":"0.1.80"}-->Changed public notes.'
    )
    assert service.reconcile()["queued"] == 0
    assert sum(url != source.MANIFEST_URL for url in data["hits"]) == artifact_hits == 1
    with state.db()() as s:
        event = s.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_name == service.EVENT)
        )
        assert event.person_id is event.workspace_id is event.venture_id is None
        assert all(
            x not in event.props_json
            for x in ["usr_a", "example.test", "Owned Mac", "Changed public notes"]
        )
        row = s.scalar(
            select(EmailMessage).where(EmailMessage.template == "app_release")
        )
        ctx = json.loads(row.context_json)
        assert row.category == "product_updates" and row.idempotency_key == __import__(
            "app.email.service", fromlist=["_key"]
        )._key("usr_a", "app_release", "0.1.84")
        fresh = service.refresh_context(s, "usr_a", ctx)
        assert (
            fresh
            and fresh["_release_manifest_sha256"] != ctx["_release_manifest_sha256"]
        )


@pytest.mark.parametrize(
    "change",
    [
        "revoked",
        "owner",
        "updated",
        "unknown",
        "linux",
        "optional",
        "minimum_satisfied",
        "version_changed",
        "witness_changed",
    ],
)
def test_delivery_rechecks_owned_device_and_current_public_policy(notices, change):
    from app.db.supymem_models import DesktopDevice, EmailMessage
    from sqlalchemy import select

    service, _source, state, data, now = notices
    assert service.reconcile()["queued"] == 1
    with state.db()() as s:
        row = s.scalar(
            select(EmailMessage).where(EmailMessage.template == "app_release")
        )
        ctx = json.loads(row.context_json)
        device = s.get(DesktopDevice, "dev_release_owned")
        if change == "revoked":
            device.revoked_at = now
        elif change == "owner":
            device.person_id = "usr_b"
        elif change == "updated":
            device.app_version = "0.1.84"
        elif change == "unknown":
            device.app_version = None
        elif change == "linux":
            device.platform = "linux"
        elif change == "optional":
            data["manifest"]["notes"] = (
                '<!--cos-update {"mandatory":false}-->Security-looking prose.'
            )
        elif change == "minimum_satisfied":
            data["manifest"]["notes"] = (
                '<!--cos-update {"mandatory":false,"min_version":"0.1.80"}-->'
            )
        elif change == "version_changed":
            ctx["version"] = "0.1.85"
        elif change == "witness_changed":
            ctx["_release_witness"] = "forged"
        s.commit()
        assert service.refresh_context(s, "usr_a", ctx) is None


def test_unknown_source_defers_integration_without_rebuilding_or_sending(notices):
    from app.db.supymem_models import EmailMessage
    from sqlalchemy import select

    service, source, state, data, _now = notices
    assert service.reconcile()["queued"] == 1
    with state.db()() as s:
        ctx = json.loads(
            s.scalar(
                select(EmailMessage).where(EmailMessage.template == "app_release")
            ).context_json
        )
        data["status"] = 404
        with pytest.raises(source.ReleaseUnavailable, match="publication_unavailable"):
            service.refresh_context(s, "usr_a", ctx)
    assert service.reconcile()["status"] == "unavailable"


@pytest.mark.parametrize("policy", ["opt_out", "suppression", "quiet", "cap"])
def test_canonical_preferences_suppression_quiet_and_cap_still_apply(
    notices, monkeypatch, policy
):
    from app.db.models import AnalyticsEvent
    from app.email import service as email
    from app.db.supymem_models import EmailMessage
    from sqlalchemy import select

    service, _source, state, _data, now = notices
    if policy == "opt_out":
        email.set_prefs("usr_a", {"product_updates": False})
    elif policy == "suppression":
        email.suppress("a@example.test", "fixture_bounce")
    elif policy == "quiet":
        with state.db()() as s:
            s.add(
                AnalyticsEvent(
                    id="ane_release_activity",
                    person_id="usr_a",
                    event_name="session.active",
                    props_json="{}",
                    occurred_at=now,
                )
            )
            s.commit()
    elif policy == "cap":
        monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "app_release,milestone")
        assert email.send_email(
            "usr_a", "milestone", {"what": "plan"}, ref="occupied_daily_slot"
        )
    assert service.reconcile()["queued"] == 0
    with state.db()() as s:
        rows = s.scalars(
            select(EmailMessage).where(EmailMessage.template == "app_release")
        ).all()
        if policy in {"opt_out", "suppression"}:
            assert len(rows) == 1 and rows[0].status == "suppressed"
            assert rows[0].error == (
                "opted_out" if policy == "opt_out" else "address_suppressed"
            )
        else:
            assert (
                rows == []
            )  # Temporary refusal does not consume the release/version event.
    if policy in {"quiet", "cap"}:
        later = now + (
            timedelta(hours=1, seconds=1)
            if policy == "quiet"
            else timedelta(days=1, seconds=1)
        )
        monkeypatch.setattr(email, "utcnow", lambda: later)
        assert service.reconcile()["queued"] == 1


def test_scoped_forged_witness_cannot_authorize_and_template_ignores_urls_notes(
    notices,
):
    from app.db.models import AnalyticsEvent
    from sqlalchemy import select

    service, _source, state, _data, _now = notices
    assert service.reconcile()["queued"] == 1
    # Append-only row mutation is forbidden by the actual DB trigger.
    with state.db()() as s:
        event = s.scalar(
            select(AnalyticsEvent).where(AnalyticsEvent.event_name == service.EVENT)
        )
        publication = _source.parse_manifest(json.dumps(_data["manifest"]).encode())
        event.person_id = "usr_a"
        with pytest.raises(Exception):
            s.commit()
        s.rollback()
        assert service._saved(s, publication)
    result = service.build_template(
        {
            "version": "0.1.84",
            "url": "https://foreign.invalid/",
            "notes": "security scare",
        }
    )
    assert result["url"] == "https://supymem.com/downloads/Supymem.dmg"
    assert "security scare" not in json.dumps(result)
    assert len(result["subject"].split()) <= 6


def test_cancelled_artifact_stream_cannot_persist_witness_or_queue_mail(
    notices, monkeypatch
):
    from concurrent.futures import CancelledError
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import EmailMessage
    from sqlalchemy import select

    service, source, state, data, _now = notices

    class CancelledStream(httpx.SyncByteStream):
        def __iter__(self):
            yield data["artifact"][: 64 * 1024]
            raise CancelledError()

    def transport(request):
        if str(request.url) == source.MANIFEST_URL:
            return httpx.Response(200, content=json.dumps(data["manifest"]).encode())
        return httpx.Response(200, stream=CancelledStream())

    monkeypatch.setattr(
        source,
        "client",
        lambda: httpx.Client(transport=httpx.MockTransport(transport), trust_env=False),
    )
    with pytest.raises(CancelledError):
        service.reconcile()
    with state.db()() as s:
        assert (
            s.scalar(
                select(AnalyticsEvent.id).where(
                    AnalyticsEvent.event_name == service.EVENT
                )
            )
            is None
        )
        assert (
            s.scalar(
                select(EmailMessage.id).where(EmailMessage.template == "app_release")
            )
            is None
        )


def test_person_scoped_cached_evidence_cannot_become_public_release_authority(notices):
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import EmailMessage
    from sqlalchemy import select

    service, source, state, data, now = notices
    with source.client() as client:
        publication = source.fetch_manifest(client, time.monotonic() + 60)
        witness = source.verify_artifact(client, publication, time.monotonic() + 60)
    with state.db()() as s:
        s.add(
            AnalyticsEvent(
                id=publication.witness_id,
                person_id="usr_a",
                event_name=service.EVENT,
                props_json=json.dumps(witness),
                occurred_at=now,
            )
        )
        s.commit()
    assert service.reconcile()["status"] == "unavailable"
    with state.db()() as s:
        assert (
            s.scalar(
                select(EmailMessage.id).where(EmailMessage.template == "app_release")
            )
            is None
        )


def test_slow_small_transport_chunks_cannot_hide_the_total_deadline(
    publication, monkeypatch
):
    source, _data = publication
    clock, reads, closed = [0], [], []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(10):
                clock[0] += 20
                reads.append(index)
                yield b"tiny"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(source.time, "monotonic", lambda: clock[0])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=SlowStream())
        )
    ) as client:
        with pytest.raises(source.ReleaseUnavailable, match="fetch_bound_exceeded"):
            list(
                source._chunks(
                    client, source.MANIFEST_URL, source.MAX_MANIFEST_BYTES, 60
                )
            )
    assert reads == [0, 1, 2] and closed == [True]
