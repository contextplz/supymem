"""Run vendored OpenLeads as a subprocess and shape lead exports.

OpenLeads lives under ``vendor/research/openleads`` (its own venv). We do not
import its package into company_os — we call the CLI and parse JSON/CSV output.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from coding_agents.env import child_env

REPO_ROOT = Path(__file__).resolve().parents[4]
OPENLEADS_DIR = REPO_ROOT / "vendor" / "research" / "openleads"
OPENLEADS_BIN = OPENLEADS_DIR / ".venv" / "bin" / "openleads"
OPENLEADS_HOME = REPO_ROOT / "data" / "openleads"

# Map OpenLeads CSV headers → stable API keys
_FIELD_MAP = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Email": "email",
    "Title": "title",
    "Organization Name": "organization",
    "Industry": "industry",
    "# Employees": "employees",
    "LinkedIn Url": "linkedin_url",
    "City": "city",
    "Country": "country",
    "Website": "website",
    "Email Confidence": "email_confidence",
    "Email Score": "email_score",
    "Source": "source",
    "Vertical": "vertical",
    "Email Tier": "email_tier",
    "Confidence %": "confidence_pct",
}


def status() -> dict[str, Any]:
    """A binary's presence does not authorize commercial or paid execution.

    See docs/company-os/115_GTM_RELATIONSHIP_ENGINE.md: the repository keeps
    OpenLeads as a PolyForm Noncommercial research reference only. No license
    grant or founder-scoped resource/credit adapter exists in this service.
    """
    return {
        "ready": False,
        "installed": OPENLEADS_BIN.is_file() and os.access(OPENLEADS_BIN, os.X_OK),
        "code": "commercial_license_and_resource_authorization_required",
        "hint": "OpenLeads execution requires a commercial license and founder-scoped resource authorization. Use public web discovery.",
    }


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    # OpenLeads CSV headers
    if "Email" in row or "Organization Name" in row:
        out = {key: _as_str(row.get(src)) for src, key in _FIELD_MAP.items()}
        return out
    # OpenLeads Lead.to_dict() / already-normalized
    return {
        "first_name": _as_str(row.get("first_name")),
        "last_name": _as_str(row.get("last_name")),
        "email": _as_str(row.get("email")),
        "title": _as_str(row.get("title")),
        "organization": _as_str(row.get("organization")),
        "industry": _as_str(row.get("industry")),
        "employees": _as_str(row.get("employees")),
        "linkedin_url": _as_str(row.get("linkedin_url")),
        "city": _as_str(row.get("city")),
        "country": _as_str(row.get("country")),
        "website": _as_str(row.get("website")),
        "email_confidence": _as_str(row.get("email_confidence") or row.get("confidence")),
        "email_score": _as_str(row.get("email_score") if row.get("email_score") is not None else row.get("score")),
        "source": _as_str(row.get("source")),
        "vertical": _as_str(row.get("vertical")),
        "email_tier": _as_str(row.get("email_tier") or row.get("tier")),
        "confidence_pct": _as_str(
            row.get("confidence_pct") if row.get("confidence_pct") is not None else row.get("Confidence %")
        ),
    }


def _parse_output(path: Path, fmt: str) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if fmt == "json":
        data = json.loads(raw)
        if isinstance(data, dict) and "leads" in data:
            data = data["leads"]
        if isinstance(data, dict):
            data = [data]
        return [_normalize_row(r) for r in data if isinstance(r, dict)]
    # csv
    reader = csv.DictReader(io.StringIO(raw))
    return [_normalize_row(dict(r)) for r in reader]


_NO_LEADS_MARKERS = (
    "No leads produced",
    "done, 0 lead(s)",
)

# Wikidata P452 needs real industry labels; "B2B" rarely resolves.
_INDUSTRY_ALIASES = {
    "ai": "artificial intelligence",
    "fintech": "financial technology",
}


def _normalize_industry(industry: str | None, source: str | None) -> str | None:
    if not industry or not industry.strip():
        return None
    raw = industry.strip()
    if (source or "") == "companies":
        alias = _INDUSTRY_ALIASES.get(raw.lower())
        if alias:
            return alias
    return raw


def _is_empty_find(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}"
    return any(m in blob for m in _NO_LEADS_MARKERS)


def find_leads(
    *,
    query: str = "founders",
    count: int = 50,
    industry: str | None = "B2B",
    source: str | None = "yc",
    deep: bool = True,
    verified_only: bool = False,
    max_companies: int | None = None,
    timeout_sec: int = 600,
    _allow_fallback: bool = True,
) -> dict[str, Any]:
    """Run ``openleads find`` and return normalized leads."""
    st = status()
    if not st["ready"]:
        raise RuntimeError(st.get("hint") or "OpenLeads is not installed")

    OPENLEADS_HOME.mkdir(parents=True, exist_ok=True)
    count = max(1, min(int(count), 200))
    max_companies = max(1, min(int(max_companies or count * 2), 200))
    timeout_sec = max(1, min(int(timeout_sec), 60))
    industry_eff = _normalize_industry(industry, source)
    query_eff = (query or "").strip() or "founders"
    source_eff = source

    # Keep the exact source and target. Do not substitute YC or unrelated cities.
    source_eff = source or "auto"

    with tempfile.TemporaryDirectory(prefix="ol_find_") as tmp:
        out_path = Path(tmp) / "leads.json"
        cmd: list[str] = [
            str(OPENLEADS_BIN),
            "find",
            query_eff,
            "-n",
            str(count),
            "--format",
            "json",
            "-o",
            str(out_path),
            "--max-companies",
            str(max_companies),
        ]
        if source_eff and source_eff not in ("auto", ""):
            cmd.extend(["-s", source_eff])
        if industry_eff:
            cmd.extend(["--industry", industry_eff])
        if deep:
            cmd.append("--deep")
        if verified_only:
            cmd.append("--verified-only")

        env = child_env()
        env["OPENLEADS_HOME"] = str(OPENLEADS_HOME)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(OPENLEADS_DIR),
            env=env,
            check=False,
        )
        leads: list[dict[str, Any]] = []
        if out_path.exists():
            try:
                leads = _parse_output(out_path, "json")
            except Exception:
                csv_path = Path(tmp) / "leads.csv"
                if csv_path.exists():
                    leads = _parse_output(csv_path, "csv")

        empty = _is_empty_find(proc.stderr or "", proc.stdout or "")

        # Soft empty: OpenLeads exits non-zero when it finds nothing — not a crash.
        if not leads and empty:
            tip = "No leads matched this source and founder target. No alternative source was substituted."
            return {
                "leads": [],
                "count": 0,
                "safe": 0,
                "risky": 0,
                "query": query_eff,
                "industry": industry_eff or industry,
                "source": source_eff or "auto",
                "deep": deep,
                "message": tip,
                "log_tail": None,
            }

        if not leads and proc.returncode != 0:
            raise RuntimeError("openleads_execution_failed")

        safe = sum(1 for L in leads if str(L.get("email_tier", "")).lower() == "safe")
        risky = sum(1 for L in leads if str(L.get("email_tier", "")).lower() == "risky")
        return {
            "leads": leads,
            "count": len(leads),
            "safe": safe,
            "risky": risky,
            "query": query_eff,
            "industry": industry_eff or industry,
            "source": source_eff or "auto",
            "deep": deep,
            "message": None,
            "log_tail": None,
        }


def leads_to_json_bytes(leads: list[dict[str, Any]]) -> bytes:
    payload = {
        "count": len(leads),
        "leads": leads,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


_EXPORT_HEADERS = [
    "first_name",
    "last_name",
    "email",
    "title",
    "organization",
    "industry",
    "employees",
    "linkedin_url",
    "city",
    "country",
    "website",
    "email_tier",
    "confidence_pct",
    "email_confidence",
    "email_score",
    "source",
    "source_url",
]


def leads_to_csv_bytes(leads: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_HEADERS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        writer.writerow({h: str(lead.get(h) or "") for h in _EXPORT_HEADERS})
    return buf.getvalue().encode("utf-8")


def leads_to_xlsx_bytes(leads: list[dict[str, Any]]) -> bytes:
    """Minimal XLSX (Office Open XML) — opens in Excel / Numbers / Sheets."""
    headers = list(_EXPORT_HEADERS)

    def col_letter(n: int) -> str:
        s = ""
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    rows_xml: list[str] = []
    # header row
    cells = []
    for i, h in enumerate(headers, start=1):
        cells.append(
            f'<c r="{col_letter(i)}1" t="inlineStr"><is><t>{escape(h)}</t></is></c>'
        )
    rows_xml.append(f'<row r="1">{"".join(cells)}</row>')
    for r_idx, lead in enumerate(leads, start=2):
        cells = []
        for c_idx, h in enumerate(headers, start=1):
            val = str(lead.get(h) or "")
            cells.append(
                f'<c r="{col_letter(c_idx)}{r_idx}" t="inlineStr">'
                f"<is><t>{escape(val)}</t></is></c>"
            )
        rows_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows_xml)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Leads" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()
