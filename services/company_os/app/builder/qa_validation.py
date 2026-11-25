"""Validate observations against the controller's immutable input, not report claims."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
import struct
import zlib

from core.static_qa_contracts import Input, MAX_EVIDENCE_BYTES, MAX_REPORT_BYTES, Report, VIEWPORTS
from core.artifact_snapshot import canonical
from .model_proxy import BuildModelError


def png(blob, *, viewport):
    """Bounded lossless Chromium RGB/RGBA PNG only; no executable attachments."""
    if not isinstance(blob, bytes) or not 0 < len(blob) <= 8 * 1024 * 1024 or not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BuildModelError("qa_png_invalid")
    offset, header, chunks, ended = 8, None, [], False
    try:
        while offset < len(blob):
            length = struct.unpack(">I", blob[offset:offset + 4])[0]
            kind, data = blob[offset + 4:offset + 8], blob[offset + 8:offset + 8 + length]
            crc = struct.unpack(">I", blob[offset + 8 + length:offset + 12 + length])[0]
            if length > 8 * 1024 * 1024 or zlib.crc32(kind + data) & 0xffffffff != crc:
                raise ValueError()
            offset += length + 12
            if kind == b"IHDR":
                if header is not None or offset != 33 or length != 13:
                    raise ValueError()
                header = struct.unpack(">IIBBBBB", data)
            elif kind == b"IDAT":
                if header is None:
                    raise ValueError()
                chunks.append(data)
            elif kind == b"IEND":
                if length != 0 or offset != len(blob):
                    raise ValueError()
                ended = True
                break
            elif kind not in {b"sRGB", b"gAMA", b"cHRM", b"pHYs"}:
                raise ValueError()
        width, height, depth, color, compression, filtering, interlace = header or (0,) * 7
        if (not ended or not chunks or (width, height) != (viewport["width"], viewport["height"])
                or depth != 8 or color not in {2, 6} or (compression, filtering, interlace) != (0, 0, 0)):
            raise ValueError()
        expected = height * (1 + width * (3 if color == 2 else 4))
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(chunks), expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError()
        stride = 1 + width * (3 if color == 2 else 4)
        if any(pixels[row * stride] > 4 for row in range(height)):
            raise ValueError()
    except (ValueError, TypeError, struct.error, zlib.error) as exc:
        raise BuildModelError("qa_png_invalid") from exc


def validate(blob, *, request: Input, build_id, qa_id, generation, policy, evidence):
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_REPORT_BYTES:
        raise BuildModelError("qa_report_limit")
    try:
        report = Report.model_validate_json(blob)
    except ValueError as exc:
        raise BuildModelError("qa_report_invalid") from exc
    if canonical(report.model_dump(mode="json")) != blob:
        raise BuildModelError("qa_report_noncanonical")
    # Keep binding failures precise and secret-free. These fixed codes make a
    # deterministic, completed QA report diagnosable without logging report
    # bytes, source contents, network locations, credentials, or expected and
    # observed values.
    if report.build_run_id != build_id or report.qa_run_id != qa_id or report.generation != generation:
        raise BuildModelError("qa_report_identity_binding_mismatch")
    if (report.source_artifact_id != request.source_artifact_id or report.source_sha256 != request.source_sha256
            or report.source_manifest_digest != request.source_manifest_digest):
        raise BuildModelError("qa_report_source_binding_mismatch")
    if report.contract_digest != request.contract.digest or report.input_digest != request.digest:
        raise BuildModelError("qa_report_input_binding_mismatch")
    if report.harness_version != request.contract.profile:
        raise BuildModelError("qa_report_profile_mismatch")
    if report.runtime_uid != 10001:
        raise BuildModelError("qa_report_runtime_uid_mismatch")
    if report.browser_version != policy["browser_version"]:
        raise BuildModelError("qa_report_browser_version_mismatch")
    if not report.browser_sandbox_verified:
        raise BuildModelError("qa_report_browser_sandbox_unverified")
    try:
        started, finished = (datetime.fromisoformat(value) for value in (report.started_at, report.finished_at))
        if (started.tzinfo is None or finished.tzinfo is None or finished < started
                or (finished - started).total_seconds() > 195 or finished > datetime.now(timezone.utc) + timedelta(seconds=60)):
            raise ValueError()
    except ValueError as exc:
        raise BuildModelError("qa_report_time_invalid") from exc
    cases = {case.id: case for case in report.cases}
    expected = {"artifact:source": ("artifact", "source"), "artifact:unchanged": ("artifact", "source"), "runtime:sandbox": ("runtime", "browser")}
    if request.contract.profile == "founder_product_v1":
        served = sorted(item["path"].removeprefix("product/static/") for item in request.manifest
                        if item["path"].startswith("product/static/"))
    else:
        served = [item["path"] for item in request.manifest]
    for index, path in enumerate(served):
        expected[f"http:{index}"] = ("http", path[:200])
    pages = sorted(path for path in served if path.endswith(".html"))
    for action in request.contract.actions:
        expected["declared:" + action.id] = ("coverage", action.page)
    for viewport in VIEWPORTS:
        for index, path in enumerate(pages):
            label = f"{viewport}-{index}"
            for suffix, suite in (("render", "render"), ("layout", "render"), ("network", "network"), ("javascript", "render")):
                expected[label + ":" + suffix] = (suite, path[:200])
            expected[label + ":public_copy"] = ("render", path[:200])
            if path == "index.html":
                expected[label + ":offer"] = ("render", path)
                expected[label + ":images"] = ("render", path)
                if request.contract.profile == "static_landing_v1":
                    expected[label + ":empty_footer"] = ("render", path)
                    expected[label + ":analytics_referrer"] = ("render", path)
                    expected[label + ":analytics_conversion"] = ("actions", path)
                if request.contract.managed_request_form is not None:
                    prefix = f"{label}:form:request_form:"
                    for suffix in ("backend", "visible"):
                        expected[prefix + suffix] = ("forms", "form 1")
                    expected[prefix + "submit"] = ("forms", "request_submit")
                    for number, control in enumerate(request.contract.managed_request_form.controls):
                        target = f"form request_form input {number + 1}"
                        for suffix in ("visible", "label"):
                            expected[f"{prefix}{suffix}:{number}"] = ("forms", target)
                        if "required" in control and control.get("type") in {None, "text", "email", "url", "tel", "number"}:
                            expected[f"{prefix}required:{number}"] = ("forms", target)
                        if control.get("type") in {"email", "url"}:
                            expected[f"{prefix}type:{number}"] = ("forms", target)
            for action in request.contract.actions:
                if action.page == path:
                    for mode in ("pointer", "keyboard"):
                        expected[f"{label}:action:{action.id}:{mode}"] = ("actions", action.id)
    # Incomplete runs may have an explicit fatal observation, never fabricated
    # normal case counts. They remain failed and cannot supply verified QA.
    complete = report.coverage.get("complete") is True
    if complete and any(key not in cases or (cases[key].suite, cases[key].target) != pair for key, pair in expected.items()):
        raise BuildModelError("qa_report_cases_missing")
    for key, case in cases.items():
        if key in expected:
            if (case.suite, case.target) != expected[key]:
                raise BuildModelError("qa_report_case_binding_invalid")
        elif key == "runtime:failure" and case.suite == "runtime" and case.status == "failed":
            if complete:
                raise BuildModelError("qa_report_coverage_invalid")
        elif re.fullmatch(r"(?:desktop|mobile)-[0-9]:control:[1-9][0-9]{0,2}", key):
            if case.suite != "coverage":
                raise BuildModelError("qa_report_case_invalid")
        elif re.fullmatch(r"(?:desktop|mobile)-[0-9]:form:[a-f0-9]{12}:(?:backend|(?:label|required|type):[0-9]{1,2})", key):
            if case.suite != "forms" or key.endswith(":backend") and case.status != "unsupported":
                raise BuildModelError("qa_report_form_invalid")
        else:
            raise BuildModelError("qa_report_case_invalid")
    counts = {name: sum(case.status == name for case in report.cases) for name in ("passed", "failed", "unsupported")}
    visible = sum(":control:" in case.id for case in report.cases)
    expected_coverage = {"complete": complete, "declared_actions": len(request.contract.actions), "visible_controls": visible,
                         "observed_cases": len(report.cases), "unsupported_cases": counts["unsupported"]}
    if report.coverage != expected_coverage or not complete and "runtime:failure" not in cases:
        raise BuildModelError("qa_report_coverage_invalid")
    actual_outcome = "passed" if complete and counts["failed"] == counts["unsupported"] == 0 else "failed"
    if report.outcome != actual_outcome:
        raise BuildModelError("qa_report_outcome_invalid")
    if (not isinstance(evidence, dict) or any(not isinstance(value, bytes) for value in evidence.values())
            or set(evidence) != {item.id for item in report.evidence}
            or len(blob) + sum(map(len, evidence.values())) > MAX_EVIDENCE_BYTES):
        raise BuildModelError("qa_evidence_set_invalid")
    # At most the first six viewport/page captures, in the fixed traversal order.
    all_ids = [f"{viewport}-{index}.png" for viewport in VIEWPORTS for index, _ in enumerate(pages)]
    if complete and [item.id for item in report.evidence] != all_ids[:6]:
        raise BuildModelError("qa_evidence_missing")
    for item in report.evidence:
        if item.id not in all_ids or len(evidence[item.id]) != item.size_bytes or hashlib.sha256(evidence[item.id]).hexdigest() != item.sha256:
            raise BuildModelError("qa_evidence_hash_invalid")
        png(evidence[item.id], viewport=VIEWPORTS[item.id.split("-", 1)[0]])
        render_id = item.id[:-4] + ":render"
        if render_id not in cases or cases[render_id].evidence_ids != [item.id]:
            raise BuildModelError("qa_evidence_case_invalid")
    return report, counts
