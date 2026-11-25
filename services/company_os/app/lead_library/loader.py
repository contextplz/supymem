"""Load a licensed Lead Library batch. Default is a dry run with no writes.

Usage: python -m app.lead_library.loader --file <path or s3://bucket/key> [--manifest <path or s3 uri>] [--apply]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import delete, select

from app.gtm.email_learning import opaque
from app.lead_library import taxonomy as tx
from app.lead_library.models import LeadLibraryEntry, LeadLibraryTag
from core.clock import utcnow

BATCH = 1000
_TAG_KINDS = ("industry", "buyer_tag", "category_raw", "niche_raw")
_KEEP_EMAIL = frozenset({"delivered", "replied"})
_LEAD_TYPES = frozenset(tx.LEAD_TYPES)
_FUNCTIONS = frozenset(tx.FUNCTIONS)
_SENIORITIES = frozenset(tx.SENIORITIES)
_STAGES = frozenset(tx.COMPANY_STAGES)
_TAG_ALLOWED = {"industry": frozenset(tx.INDUSTRIES), "buyer_tag": frozenset(tx.BUYER_TAGS)}
_OPTIONAL = ("role_title", "company", "company_domain", "website", "linkedin_url", "x_url", "email", "phone",
    "country_code", "region", "city", "geography_raw", "source_url", "source_host", "discovery_method", "batch_id", "collected_at")
_COUNT_KEYS = ("rows", "insert", "update", "unchanged", "invalid", "tags", "email_hash_unavailable")


class Refuse(Exception):
    pass


def _blank(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _entry_id(external: str) -> str:
    return "ll_" + hashlib.sha256(external.encode()).hexdigest()[:24]


def _email_hash(email, counts):
    if not email:
        return None
    try:
        return opaque(email)
    except ValueError as exc:
        if str(exc) != "gtm_shared_secret_unavailable":
            raise
        counts["email_hash_unavailable"] += 1
        return None


def _search(row) -> str:
    tags = row["tags"]
    parts = [row.get(key) for key in ("person_name", "role_title", "company", "company_domain")]
    parts.extend(tags["industry"])
    parts.extend(tags["buyer_tag"])
    parts.extend(row.get(key) for key in ("country_code", "region", "city", "lead_type", "function", "seniority"))
    return " ".join(str(part).strip().lower() for part in parts if part is not None and str(part).strip())


def _prepare(payload):
    if not isinstance(payload, dict):
        return None
    row = dict(payload)
    external = row.get("external_lead_id")
    if not isinstance(external, str) or not external.strip():
        return None
    row["external_lead_id"] = external.strip()
    for field, allowed in (("lead_type", _LEAD_TYPES), ("function", _FUNCTIONS), ("seniority", _SENIORITIES), ("company_stage", _STAGES)):
        value = row.get(field)
        if not isinstance(value, str) or value.strip() not in allowed:
            return None
        row[field] = value.strip()
    for field in ("person_name", "license_source", "content_hash"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        row[field] = value.strip()
    raw_tags = row.get("tags", {})
    if raw_tags is None:
        raw_tags = {}
    if not isinstance(raw_tags, dict):
        return None
    tags = {}
    for kind in _TAG_KINDS:
        values = raw_tags.get(kind) or []
        if not isinstance(values, list):
            return None
        cleaned = []
        for value in values:
            text = str(value).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        if kind in _TAG_ALLOWED and any(value not in _TAG_ALLOWED[kind] for value in cleaned):
            return None
        tags[kind] = cleaned
    row["tags"] = tags
    for field in ("usable_channels", "quality_flags"):
        value = row.get(field) or []
        if not isinstance(value, list):
            return None
        row[field] = value
    provenance = row.get("provenance") or {}
    if not isinstance(provenance, dict):
        return None
    row["provenance"] = provenance
    for field in ("confidence", "quality_score"):
        value = row.get(field)
        if value is None or value == "":
            row[field] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        else:
            row[field] = float(value)
    score = row.get("fit_score")
    if score is None or score == "":
        row["fit_score"] = None
    elif isinstance(score, bool) or not isinstance(score, (int, float)) or isinstance(score, float) and not score.is_integer():
        return None
    else:
        row["fit_score"] = int(score)
    return row


def _external(payload) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("external_lead_id"), str) and payload["external_lead_id"].strip():
        return payload["external_lead_id"].strip()
    return "?"


def _tags(entry_id, row):
    out = []
    for kind in _TAG_KINDS:
        for tag in row["tags"][kind]:
            out.append(LeadLibraryTag(entry_id=entry_id, tag_type=kind, tag=tag))
    return out


def _fill(entry, row, email_hash, email_status):
    from app.lead_library.service import PUBLISHED, _email_published
    published = row.get("email_status") in PUBLISHED or row["provenance"].get("email_published") is True or (
        bool(entry.email) and entry.email.casefold() == (row.get("email") or "").casefold()
        and (entry.source_url or entry.website) == (_blank(row.get("source_url")) or _blank(row.get("website")))
        and _email_published(entry))
    for name in _OPTIONAL:
        setattr(entry, name, _blank(row.get(name)))
    for name in ("person_name", "lead_type", "function", "seniority", "company_stage", "license_source"):
        setattr(entry, name, row[name])
    entry.email_hash = email_hash
    entry.email_status = email_status
    entry.phone_status = _blank(row.get("phone_status")) or "none"
    entry.confidence = row["confidence"]
    entry.fit_score = row["fit_score"]
    entry.quality_score = row["quality_score"]
    entry.usable_channels = json.dumps(row["usable_channels"])
    entry.quality_flags = json.dumps(row["quality_flags"])
    entry.provenance_json = json.dumps(row["provenance"] | {"email_published": published})
    entry.content_hash = row["content_hash"]
    entry.search_text = _search(row)


def _hash_of(current):
    if current is None:
        return None
    return current["content_hash"] if isinstance(current, dict) else current.content_hash


def _email_of(current):
    if current is None:
        return None
    return current["email_status"] if isinstance(current, dict) else current.email_status


def _id_of(current, external):
    if current is None:
        return _entry_id(external)
    if isinstance(current, dict):
        return current["id"]
    return current.id


def _batch(rows, apply, counts):
    from app.state import db

    ids = [row["external_lead_id"] for row in rows]
    with db()() as session:
        found = {}
        if ids:
            found = {item.external_lead_id: item for item in session.scalars(
                select(LeadLibraryEntry).where(LeadLibraryEntry.external_lead_id.in_(ids)))}
        overlay = {}
        for row in rows:
            external = row["external_lead_id"]
            current = overlay.get(external) or found.get(external)
            if current is not None and _hash_of(current) == row["content_hash"]:
                counts["unchanged"] += 1
                continue
            email_status = _blank(row.get("email_status")) or "none"
            if _email_of(current) in _KEEP_EMAIL:
                email_status = _email_of(current)
            entry_id = _id_of(current, external)
            tags = _tags(entry_id, row)
            counts["tags"] += len(tags)
            email_hash = _email_hash(_blank(row.get("email")), counts)
            planned = {"content_hash": row["content_hash"], "email_status": email_status, "id": entry_id}
            if not apply:
                counts["insert" if current is None else "update"] += 1
                overlay[external] = planned
                continue
            if current is None:
                entry = LeadLibraryEntry(id=entry_id, external_lead_id=external, imported_at=utcnow(), status="active")
                _fill(entry, row, email_hash, email_status)
                session.add(entry)
                session.add_all(tags)
                found[external] = entry
                counts["insert"] += 1
            else:
                # A suppressed or retired row keeps that status. The file cannot reopen it.
                _fill(current, row, email_hash, email_status)
                session.execute(delete(LeadLibraryTag).where(LeadLibraryTag.entry_id == current.id))
                session.add_all(tags)
                counts["update"] += 1
            session.flush()
        if apply:
            session.commit()
        else:
            session.rollback()


def _split_s3(uri: str) -> tuple[str, str]:
    bucket, _, key = uri[5:].partition("/")
    if not bucket or not key:
        raise Refuse(f"lead library S3 URI is invalid: {uri}")
    return bucket, key


def _sibling_manifest(uri: str) -> str:
    if uri.startswith("s3://"):
        bucket, key = _split_s3(uri)
        parent = Path(key).parent.as_posix()
        manifest_key = "manifest.json" if parent == "." else parent + "/manifest.json"
        return f"s3://{bucket}/{manifest_key}"
    return str(Path(uri).expanduser().parent / "manifest.json")


def _materialize(uri: str) -> tuple[Path, list[Path]]:
    if not uri.startswith("s3://"):
        return Path(uri).expanduser(), []
    import boto3
    bucket, key = _split_s3(uri)
    handle = tempfile.NamedTemporaryFile(prefix="leadlib-", suffix="-" + Path(key).name, delete=False)
    handle.close()
    path = Path(handle.name)
    boto3.client("s3").download_file(bucket, key, str(path))
    return path, [path]


def _lines(path: Path) -> tuple[bytes, list[str]]:
    raw = path.read_bytes()
    try:
        payload = gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw
    except gzip.BadGzipFile as exc:
        raise Refuse("lead library file is not valid gzip") from exc
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refuse("lead library file is not UTF-8 JSONL") from exc
    return raw, [line for line in text.splitlines() if line.strip()]


def load(file_uri: str, manifest_uri: str | None, apply: bool) -> dict:
    from app.core.local_mode import is_local

    if is_local():
        raise Refuse("You can import contacts into your workspace instead.")
    temps = []
    try:
        data_path, created = _materialize(file_uri)
        temps.extend(created)
        manifest_uri = manifest_uri or _sibling_manifest(file_uri)
        manifest_path, created = _materialize(manifest_uri)
        temps.extend(created)
        if not data_path.is_file():
            raise Refuse(f"lead library file missing: {file_uri}")
        if not manifest_path.is_file():
            raise Refuse(f"lead library manifest missing: {manifest_uri}")
        raw, lines = _lines(data_path)
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise Refuse("lead library manifest is not JSON") from exc
        if not isinstance(manifest, dict):
            raise Refuse("lead library manifest must be a JSON object")
        digest = hashlib.sha256(raw).hexdigest()
        if str(manifest.get("sha256") or "").strip().lower() != digest:
            raise Refuse(f"lead library manifest sha256 mismatch: file {digest} manifest {manifest.get('sha256')}")
        if manifest.get("rows") != len(lines):
            raise Refuse(f"lead library manifest rows mismatch: file {len(lines)} manifest {manifest.get('rows')}")
        counts = {key: 0 for key in _COUNT_KEYS}
        counts["rows"] = len(lines)
        prepared, invalid_ids = [], []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            row = _prepare(payload) if payload is not None else None
            if row is None:
                counts["invalid"] += 1
                if len(invalid_ids) < 5:
                    invalid_ids.append(_external(payload))
                continue
            prepared.append(row)
        if invalid_ids:
            print(f"lead library invalid rows: {counts['invalid']} e.g. {', '.join(invalid_ids)}", file=sys.stderr)
        for start in range(0, len(prepared), BATCH):
            _batch(prepared[start:start + BATCH], apply, counts)
        return {key: counts[key] for key in _COUNT_KEYS}
    finally:
        for path in temps:
            path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.lead_library.loader")
    parser.add_argument("--file", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        counts = load(args.file, args.manifest, args.apply)
    except Refuse as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
