"""Run vendored OpenGTM as a subprocess and normalize into Company OS leads.

OpenGTM lives under ``vendor/research/opengtm`` (its own venv). We do not
import its package into company_os — we call the CLI and parse JSON output.

Upstream: https://github.com/buildingopen/opengtm (MIT)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from coding_agents.env import child_env

REPO_ROOT = Path(__file__).resolve().parents[4]
OPENGTM_DIR = REPO_ROOT / "vendor" / "research" / "opengtm"
OPENGTM_BIN = OPENGTM_DIR / ".venv" / "bin" / "opengtm"
OPENGTM_HOME = REPO_ROOT / "data" / "opengtm"

_ICP_PROFILES = ("default", "saas", "agency", "professional_services")


def _gemini_requirement() -> dict[str, Any]:
    """Expose OpenGTM's separate provider boundary without substituting Foundry."""
    return {
        "provider": "google_gemini",
        "ready": False,
        "code": "founder_scoped_account_required",
        "platform_foundry_compatible": False,
        "required_for": ["discover", "research", "pipeline", "context", "analytics", "keywords", "blog"],
        "note": "OpenGTM's Gemini-dependent commands need a founder-scoped account connection. Supymem does not pass platform credentials to this CLI.",
    }


def status() -> dict[str, Any]:
    """Binary availability and capabilities backed by a scoped account connection."""
    ready = OPENGTM_BIN.is_file() and os.access(OPENGTM_BIN, os.X_OK)
    version = None
    if ready:
        try:
            proc = subprocess.run(
                [str(OPENGTM_BIN), "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                cwd=str(OPENGTM_DIR),
                env=_child_env(),
            )
            # CLI has no --version; stamp from package metadata via help ok + known pin.
            version = "opengtm 0.1.0" if proc.returncode == 0 else None
            if proc.returncode != 0:
                return {
                    "ready": False,
                    "bin": str(OPENGTM_BIN),
                    "home": str(OPENGTM_HOME),
                    "error": (proc.stderr or proc.stdout or "help failed")[:400],
                    "gemini": _gemini_requirement(),
                }
        except Exception as exc:  # noqa: BLE001
            return {
                "ready": False,
                "bin": str(OPENGTM_BIN),
                "home": str(OPENGTM_HOME),
                "error": str(exc),
                "gemini": _gemini_requirement(),
            }
    hint = None
    if not ready:
        hint = (
            "Install OpenGTM: cd vendor/research/opengtm && "
            "python3 -m venv .venv && .venv/bin/pip install -e ."
        )
    else:
        hint = "OpenGTM's Gemini and CRM commands need a founder-scoped account connection. Offline scoring and message templates remain available."
    return {
        "ready": ready,
        "bin": str(OPENGTM_BIN),
        "home": str(OPENGTM_HOME),
        "version": version,
        "gemini": _gemini_requirement(),
        "crm_webhook": {"ready": False, "code": "founder_scoped_account_required"},
        "hint": hint,
        # Pure-Python stages work without Gemini once input JSON exists.
        "offline_ok": ready,  # qualify + message
    }


def _child_env() -> dict[str, str]:
    env = child_env()
    env["OPENGTM_HOME"] = str(OPENGTM_HOME)
    return env


def _require_bin() -> None:
    st = status()
    if not st.get("ready"):
        raise RuntimeError(st.get("hint") or "OpenGTM CLI not installed")


def _run(
    args: list[str],
    *,
    timeout_sec: int = 600,
    require_gemini: bool = False,
) -> subprocess.CompletedProcess[str]:
    if require_gemini or (args and args[0] == "sync" and "--dry-run" not in args):
        raise RuntimeError("OpenGTM requires a founder-scoped account connection for this command. Platform credentials are not passed to the CLI.")
    _require_bin()
    OPENGTM_HOME.mkdir(parents=True, exist_ok=True)
    cmd = [str(OPENGTM_BIN), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
        cwd=str(OPENGTM_DIR),
        env=_child_env(),
    )


def _read_json(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return json.loads(raw)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in full.strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def normalize_lead(row: dict[str, Any]) -> dict[str, Any]:
    """Map an OpenGTM pipeline/discover row → Company OS ingest lead."""
    if not row:
        return {}
    company = _as_str(row.get("company") or row.get("organization"))
    domain = _as_str(row.get("domain") or row.get("website")).removeprefix("https://").removeprefix("http://")
    domain = domain.split("/")[0].removeprefix("www.")
    contact = ""
    title = ""
    email = ""
    linkedin = ""
    # Flatten research contact if nested
    audit = row.get("audit") or row.get("research") or {}
    if isinstance(audit, dict) and "research" in audit and isinstance(audit["research"], dict):
        audit = audit["research"]
    if isinstance(audit, dict):
        c = audit.get("contact") or {}
        if isinstance(c, dict):
            contact = _as_str(c.get("name") or contact)
            title = _as_str(c.get("title") or title)
            email = _as_str(c.get("email") or email)
            linkedin = _as_str(c.get("linkedin_url") or linkedin)
    contact = _as_str(row.get("contact_name") or contact)
    title = _as_str(row.get("contact_title") or title)
    email = _as_str(row.get("contact_email") or row.get("email") or email)
    linkedin = _as_str(row.get("linkedin_url") or linkedin)
    first, last = _split_name(contact)

    qual = row.get("qualification") if isinstance(row.get("qualification"), dict) else {}
    messages = row.get("messages") if isinstance(row.get("messages"), dict) else {}
    region = _as_str(row.get("region") or row.get("city"))
    industry = _as_str(row.get("industry"))

    website = _as_str(row.get("website"))
    if not website and domain:
        website = f"https://{domain}"

    out: dict[str, Any] = {
        "first_name": first,
        "last_name": last,
        "email": email,
        "title": title,
        "organization": company,
        "industry": industry,
        "employees": "",
        "linkedin_url": linkedin,
        "city": region,
        "country": "",
        "website": website,
        "email_confidence": "published" if email else "",
        "email_score": "",
        "source": "opengtm",
        "vertical": industry,
        "email_tier": "risky" if email else "",
        "confidence_pct": "",
        "domain": domain,
        "icp_score": qual.get("score"),
        "icp_tier": qual.get("tier"),
        "outreach_pattern": messages.get("pattern"),
        "opengtm_messages": messages or None,
        "opengtm_qualification": qual or None,
        "opengtm_audit": audit or None,
    }
    return out


def normalize_leads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_lead(r) for r in rows if isinstance(r, dict)]


def discover(
    *,
    industry: str = "B2B SaaS",
    region: str = "San Francisco",
    limit: int = 10,
    validate: bool = True,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    """``opengtm discover`` → normalized company rows (may lack email until research)."""
    limit = max(1, min(int(limit), 50))
    with tempfile.TemporaryDirectory(prefix="opengtm-discover-") as tmp:
        out = Path(tmp) / "discovered.json"
        args = [
            "discover",
            "--industry",
            industry,
            "--region",
            region,
            "--limit",
            str(limit),
            "--output",
            str(out),
        ]
        if not validate:
            args.append("--no-validate")
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=True)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm discover failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        data = _read_json(out) or []
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        # Stamp industry/region if missing
        for row in data:
            if isinstance(row, dict):
                row.setdefault("industry", industry)
                row.setdefault("region", region)
        leads = normalize_leads(data)
        return {
            "leads": leads,
            "raw": data,
            "count": len(leads),
            "industry": industry,
            "region": region,
            "log_tail": (proc.stderr or proc.stdout or "")[-1200:],
        }


def research(
    *,
    domain: str | None = None,
    company: str | None = None,
    industry: str | None = None,
    leads: list[dict[str, Any]] | None = None,
    timeout_sec: int = 900,
) -> dict[str, Any]:
    """``opengtm research`` for one domain or a batch of lead dicts."""
    with tempfile.TemporaryDirectory(prefix="opengtm-research-") as tmp:
        out = Path(tmp) / "researched.json"
        args = ["research", "--output", str(out)]
        if domain:
            args.extend(["--domain", domain])
            if company:
                args.extend(["--company", company])
            if industry:
                args.extend(["--industry", industry])
        elif leads:
            # Accept Company OS or OpenGTM shapes
            payload = []
            for lead in leads:
                d = _as_str(lead.get("domain") or lead.get("website")).removeprefix("https://").removeprefix("http://")
                d = d.split("/")[0].removeprefix("www.")
                if not d:
                    continue
                payload.append(
                    {
                        "domain": d,
                        "company": _as_str(lead.get("company") or lead.get("organization") or d),
                        "industry": _as_str(lead.get("industry") or industry or ""),
                    }
                )
            if not payload:
                raise RuntimeError("research requires --domain or non-empty leads with domains")
            inp = Path(tmp) / "input.json"
            _write_json(inp, payload)
            args.extend(["--input", str(inp)])
        else:
            raise RuntimeError("research requires domain or leads")
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=True)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm research failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        data = _read_json(out) or []
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        return {
            "leads": normalize_leads(data),
            "raw": data,
            "count": len(data),
            "log_tail": (proc.stderr or proc.stdout or "")[-1200:],
        }


def qualify(
    leads: list[dict[str, Any]],
    *,
    icp_profile: str = "saas",
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """``opengtm qualify`` — pure scoring, no Gemini required."""
    profile = icp_profile if icp_profile in _ICP_PROFILES else "default"
    with tempfile.TemporaryDirectory(prefix="opengtm-qualify-") as tmp:
        inp = Path(tmp) / "input.json"
        out = Path(tmp) / "qualified.json"
        # Prefer raw OpenGTM rows if present; else wrap Company OS leads
        payload: list[dict[str, Any]] = []
        for lead in leads:
            if lead.get("audit") or lead.get("research") or lead.get("qualification"):
                payload.append(lead)
                continue
            # Rebuild a minimal research-shaped row from Company OS fields
            domain = _as_str(lead.get("domain") or lead.get("website")).removeprefix("https://").removeprefix("http://")
            domain = domain.split("/")[0].removeprefix("www.")
            payload.append(
                {
                    "domain": domain,
                    "company": _as_str(lead.get("organization") or lead.get("company") or domain),
                    "industry": _as_str(lead.get("industry")),
                    "contact_name": " ".join(
                        x for x in [_as_str(lead.get("first_name")), _as_str(lead.get("last_name"))] if x
                    ),
                    "contact_title": _as_str(lead.get("title")),
                    "contact_email": _as_str(lead.get("email")),
                    "linkedin_url": _as_str(lead.get("linkedin_url")),
                    "audit": lead.get("opengtm_audit") or {
                        "contact": {
                            "name": " ".join(
                                x
                                for x in [_as_str(lead.get("first_name")), _as_str(lead.get("last_name"))]
                                if x
                            ),
                            "title": _as_str(lead.get("title")),
                            "email": _as_str(lead.get("email")),
                            "linkedin_url": _as_str(lead.get("linkedin_url")),
                        },
                        "findings": [],
                    },
                }
            )
        _write_json(inp, payload)
        proc = _run(
            [
                "qualify",
                "--input",
                str(inp),
                "--icp-profile",
                profile,
                "--output",
                str(out),
            ],
            timeout_sec=timeout_sec,
            require_gemini=False,
        )
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm qualify failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        data = _read_json(out) or []
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        hot = sum(1 for r in data if (r.get("qualification") or {}).get("tier") == "hot")
        warm = sum(1 for r in data if (r.get("qualification") or {}).get("tier") == "warm")
        cold = sum(1 for r in data if (r.get("qualification") or {}).get("tier") == "cold")
        return {
            "leads": normalize_leads(data),
            "raw": data,
            "count": len(data),
            "hot": hot,
            "warm": warm,
            "cold": cold,
            "icp_profile": profile,
            "log_tail": (proc.stderr or proc.stdout or "")[-1200:],
        }


def _message_ready_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Normalize Company OS audit fields to OpenGTM's strict message schema."""
    row = dict(lead)
    audit = row.get("audit") or row.get("opengtm_audit") or {"findings": []}
    audit = dict(audit) if isinstance(audit, dict) else {"findings": []}
    findings = audit.get("findings")
    normalized: list[dict[str, Any]] = []
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            item = dict(finding)
            item["type"] = _as_str(item.get("type") or item.get("category") or "other")
            item["severity"] = _as_str(item.get("severity") or "low")
            item["detail"] = _as_str(
                item.get("detail") or item.get("finding") or item.get("recommendation")
            )
            item["evidence"] = _as_str(
                item.get("evidence") or item.get("source") or item.get("finding")
            )
            normalized.append(item)
    audit["findings"] = normalized
    row["audit"] = audit
    return row


def message(
    leads: list[dict[str, Any]],
    *,
    language: str = "en",
    region: str = "",
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """``opengtm message`` — pattern A/B/C/D/E templates, no Gemini required."""
    lang = language if language in ("en", "de") else "en"
    with tempfile.TemporaryDirectory(prefix="opengtm-message-") as tmp:
        inp = Path(tmp) / "input.json"
        out = Path(tmp) / "messages.json"
        payload: list[dict[str, Any]] = []
        for lead in leads:
            if lead.get("audit") or lead.get("messages"):
                payload.append(_message_ready_lead(lead))
                continue
            domain = _as_str(lead.get("domain") or lead.get("website")).removeprefix("https://").removeprefix("http://")
            domain = domain.split("/")[0].removeprefix("www.")
            payload.append(
                _message_ready_lead(
                    {
                        "domain": domain,
                        "company": _as_str(
                            lead.get("organization") or lead.get("company") or domain
                        ),
                        "industry": _as_str(lead.get("industry")),
                        "contact_name": " ".join(
                            x
                            for x in [
                                _as_str(lead.get("first_name")),
                                _as_str(lead.get("last_name")),
                            ]
                            if x
                        ),
                        "contact_title": _as_str(lead.get("title")),
                        "audit": lead.get("opengtm_audit") or {"findings": []},
                    }
                )
            )
        _write_json(inp, payload)
        args = ["message", "--input", str(inp), "--language", lang, "--output", str(out)]
        if region:
            args.extend(["--region", region])
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=False)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm message failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        data = _read_json(out) or []
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        patterns: dict[str, int] = {}
        for row in data:
            p = ((row.get("messages") or {}).get("pattern") or "?")
            patterns[str(p)] = patterns.get(str(p), 0) + 1
        return {
            "leads": normalize_leads(data),
            "raw": data,
            "count": len(data),
            "patterns": patterns,
            "language": lang,
            "log_tail": (proc.stderr or proc.stdout or "")[-1200:],
        }


def pipeline(
    *,
    industry: str = "B2B SaaS",
    region: str = "San Francisco",
    limit: int = 10,
    language: str = "en",
    icp_profile: str = "saas",
    no_sync: bool = True,
    dry_run: bool = True,
    timeout_sec: int = 1800,
) -> dict[str, Any]:
    """Full ``opengtm pipeline``: discover → research → qualify → message."""
    limit = max(1, min(int(limit), 30))
    profile = icp_profile if icp_profile in _ICP_PROFILES else "default"
    lang = language if language in ("en", "de") else "en"
    with tempfile.TemporaryDirectory(prefix="opengtm-pipeline-") as tmp:
        out = Path(tmp) / "pipeline.json"
        args = [
            "pipeline",
            "--industry",
            industry,
            "--region",
            region,
            "--limit",
            str(limit),
            "--language",
            lang,
            "--icp-profile",
            profile,
            "--output",
            str(out),
        ]
        if no_sync:
            args.append("--no-sync")
        if dry_run:
            args.append("--dry-run")
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=True)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm pipeline failed: {(proc.stderr or proc.stdout or '')[:1200]}"
            )
        data = _read_json(out) or []
        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []
        leads = normalize_leads(data)
        hot = sum(1 for r in data if (r.get("qualification") or {}).get("tier") == "hot")
        warm = sum(1 for r in data if (r.get("qualification") or {}).get("tier") == "warm")
        cold = sum(1 for r in data if (r.get("qualification") or {}).get("tier") == "cold")
        return {
            "leads": leads,
            "raw": data,
            "count": len(leads),
            "hot": hot,
            "warm": warm,
            "cold": cold,
            "industry": industry,
            "region": region,
            "icp_profile": profile,
            "source": "opengtm",
            "message": f"OpenGTM pipeline: {len(leads)} leads ({hot} hot / {warm} warm / {cold} cold)",
            "log_tail": (proc.stderr or proc.stdout or "")[-2000:],
        }


def context(
    *,
    url: str,
    country: str | None = None,
    language: str | None = None,
    timeout_sec: int = 180,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="opengtm-context-") as tmp:
        out = Path(tmp) / "context.json"
        args = ["context", "--url", url, "--output", str(out)]
        if country:
            args.extend(["--country", country])
        if language:
            args.extend(["--language", language])
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=True)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm context failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        return {"result": _read_json(out), "log_tail": (proc.stderr or proc.stdout or "")[-800:]}


def analytics(
    *,
    url: str | None = None,
    company: str | None = None,
    industry: str | None = None,
    health_only: bool = False,
    mentions_only: bool = False,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="opengtm-analytics-") as tmp:
        out = Path(tmp) / "analytics.json"
        args = ["analytics", "--output", str(out)]
        if url:
            args.extend(["--url", url])
        if company:
            args.extend(["--company", company])
        if industry:
            args.extend(["--industry", industry])
        if health_only:
            args.append("--health-only")
        if mentions_only:
            args.append("--mentions-only")
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=True)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm analytics failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        return {"result": _read_json(out), "log_tail": (proc.stderr or proc.stdout or "")[-800:]}


def keywords(
    *,
    domain: str,
    limit: int = 50,
    language: str = "en",
    region: str = "US",
    timeout_sec: int = 600,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="opengtm-keywords-") as tmp:
        out = Path(tmp) / "keywords.json"
        proc = _run(
            [
                "keywords",
                "--domain",
                domain,
                "--limit",
                str(max(1, min(int(limit), 200))),
                "--language",
                language,
                "--region",
                region,
                "--output",
                str(out),
            ],
            timeout_sec=timeout_sec,
            require_gemini=True,
        )
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm keywords failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        data = _read_json(out) or []
        return {
            "keywords": data if isinstance(data, list) else [],
            "count": len(data) if isinstance(data, list) else 0,
            "log_tail": (proc.stderr or proc.stdout or "")[-800:],
        }


def blog(
    *,
    domain: str,
    keyword: str,
    word_count: int = 2000,
    language: str = "en",
    country: str = "United States",
    timeout_sec: int = 600,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="opengtm-blog-") as tmp:
        out = Path(tmp) / "blog.json"
        proc = _run(
            [
                "blog",
                "--domain",
                domain,
                "--keyword",
                keyword,
                "--word-count",
                str(max(500, min(int(word_count), 5000))),
                "--language",
                language,
                "--country",
                country,
                "--output",
                str(out),
            ],
            timeout_sec=timeout_sec,
            require_gemini=True,
        )
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm blog failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        return {"result": _read_json(out), "log_tail": (proc.stderr or proc.stdout or "")[-800:]}


def sitemap(*, url: str, max_urls: int = 500, validate: bool = False, timeout_sec: int = 180) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="opengtm-sitemap-") as tmp:
        out = Path(tmp) / "sitemap.json"
        args = [
            "sitemap",
            "--url",
            url,
            "--max-urls",
            str(max(1, min(int(max_urls), 2000))),
            "--output",
            str(out),
        ]
        if validate:
            args.append("--validate")
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=False)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"opengtm sitemap failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        return {"result": _read_json(out), "log_tail": (proc.stderr or proc.stdout or "")[-800:]}


def sync_crm(
    leads: list[dict[str, Any]],
    *,
    dry_run: bool = True,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """Push OpenGTM-shaped leads to Sheets via CRM_WEBHOOK_URL."""
    with tempfile.TemporaryDirectory(prefix="opengtm-sync-") as tmp:
        inp = Path(tmp) / "input.json"
        # Prefer raw rows that already have messages; else wrap normalized
        payload = []
        for lead in leads:
            if lead.get("messages") or lead.get("opengtm_messages"):
                row = dict(lead)
                if not row.get("messages") and row.get("opengtm_messages"):
                    row["messages"] = row["opengtm_messages"]
                payload.append(row)
            else:
                payload.append(
                    {
                        "domain": lead.get("domain"),
                        "company": lead.get("organization") or lead.get("company"),
                        "messages": {"pattern": lead.get("outreach_pattern") or "A"},
                        "qualification": lead.get("opengtm_qualification")
                        or {"score": lead.get("icp_score"), "tier": lead.get("icp_tier")},
                    }
                )
        _write_json(inp, payload)
        args = ["sync", "--input", str(inp)]
        if dry_run:
            args.append("--dry-run")
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=False)
        ok = proc.returncode == 0
        return {
            "ok": ok,
            "dry_run": dry_run,
            "count": len(payload),
            "log_tail": (proc.stderr or proc.stdout or "")[-1200:],
            "error": None if ok else (proc.stderr or proc.stdout or "sync failed")[:800],
        }


def _state_path(name: str | None = None) -> Path:
    if name is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.json", name):
        raise ValueError("opengtm_state_file_invalid")
    _require_bin()
    home = OPENGTM_HOME.resolve()
    path = (home / (name or "outreach-state.json")).resolve()
    path.relative_to(home)
    OPENGTM_HOME.mkdir(parents=True, exist_ok=True)
    return path


def outreach_load(
    leads: list[dict[str, Any]],
    *,
    state_file: str | None = None,
    daily_limit: int = 20,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """``opengtm outreach load`` — file-backed 4-touch sequence state."""
    path = _state_path(state_file)
    with tempfile.TemporaryDirectory(prefix="opengtm-outreach-") as tmp:
        inp = Path(tmp) / "input.json"
        payload = []
        for lead in leads:
            row = dict(lead)
            if not row.get("messages") and row.get("opengtm_messages"):
                row["messages"] = row["opengtm_messages"]
            if not row.get("domain") and row.get("website"):
                row["domain"] = _as_str(row["website"]).removeprefix("https://").removeprefix("http://").split("/")[0]
            payload.append(row)
        _write_json(inp, payload)
        args = [
            "outreach",
            "load",
            "--input",
            str(inp),
            "--state-file",
            str(path),
            "--daily-limit",
            str(max(1, min(int(daily_limit), 100))),
        ]
        proc = _run(args, timeout_sec=timeout_sec, require_gemini=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"opengtm outreach load failed: {(proc.stderr or proc.stdout or '')[:800]}"
            )
        return outreach_status(state_file=state_file) | {
            "log_tail": (proc.stderr or proc.stdout or "")[-800:],
            "state_file": str(path),
        }


def outreach_status(*, state_file: str | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    path = _state_path(state_file)
    if not path.is_file():
        return {"total_leads": 0, "due_today": 0, "state_file": str(path), "exists": False}
    proc = _run(
        ["outreach", "status", "--state-file", str(path)],
        timeout_sec=timeout_sec,
        require_gemini=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"opengtm outreach status failed: {(proc.stderr or proc.stdout or '')[:800]}"
        )
    try:
        stats = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        stats = {"raw": (proc.stdout or "")[:1000]}
    if isinstance(stats, dict):
        stats["state_file"] = str(path)
        stats["exists"] = True
    return stats if isinstance(stats, dict) else {"stats": stats, "state_file": str(path)}


def outreach_queue(
    *,
    state_file: str | None = None,
    touch: int | None = None,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    path = _state_path(state_file)
    if not path.is_file():
        return {"queue": [], "state_file": str(path), "exists": False}
    args = ["outreach", "queue", "--state-file", str(path)]
    if touch is not None:
        args.extend(["--touch", str(touch)])
    proc = _run(args, timeout_sec=timeout_sec, require_gemini=False)
    # queue prints human text; also return status JSON for machine use
    stats = outreach_status(state_file=state_file)
    return {
        "queue_text": (proc.stdout or "")[-4000:],
        "stats": stats,
        "state_file": str(path),
        "ok": proc.returncode == 0,
    }
