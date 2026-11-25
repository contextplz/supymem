"""Static observations from vetted bytes, never code execution or readiness claims."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import tomllib

from core.artifact_policy import (ArtifactLimits, ArtifactRejected, extract_zip,
                                  inspect_blob, read_file, safe_name, write_file)
from core.artifact_snapshot import freeze_source, manifest_digest
from core.redact import redact_string

from .contracts import (ImportFailure, MAX_ARCHIVE_BYTES, MAX_CONFIG_BYTES,
                        MAX_PARSE_BYTES, MAX_REPORT_BYTES, POLICY_VERSION, canonical)

MAX_OBSERVATIONS = 100


@dataclass(frozen=True)
class AnalyzedSource:
    archive: bytes
    manifest: list[dict]
    report: dict
    provenance: dict


def _plain(value, limit=200):
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value if c.isprintable())[:limit]
    return redact_string(value)


def _license(path):
    name = Path(path).name.upper()
    return name == "COPYING" or name.startswith(("LICENSE", "LICENCE", "NOTICE"))


def _report(root, manifest, provenance):
    observations, gaps, parsed, parse_bytes, candidates = [], [], 0, 0, set()
    test_files, licenses, declared_dependencies = [], [], 0

    def observe(item, parser, facts):
        if len(observations) < MAX_OBSERVATIONS:
            observations.append({"path": item["path"], "file_sha256": item["sha256"], "parser": parser, "facts": facts})
        else:
            gaps.append("observation_limit")

    for item in manifest:
        path = item["path"]
        name = Path(path).name
        if name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", "_test.go")) or name in {"pytest.ini", "conftest.py"}:
            if len(test_files) < 40:
                test_files.append(path)
        if name in {".gitmodules", ".gitattributes"}:
            observe(item, "filename", {"external_source_configuration": True, "fetched": False})
            gaps.append("external_sources_require_review")
        if name == "Dockerfile":
            observe(item, "filename", {"container_recipe_present": True, "executed": False})
        if name in {"Gemfile", "go.mod", "pom.xml", "build.gradle", "composer.json"}:
            candidate = {"Gemfile": "ruby", "go.mod": "go", "pom.xml": "jvm", "build.gradle": "jvm", "composer.json": "php"}[name]
            candidates.add(candidate)
            observe(item, "filename", {"stack_candidate": candidate, "runtime_verified": False})
        interested = name in {"package.json", "pyproject.toml", "requirements.txt"} or _license(path)
        if not interested:
            continue
        if item["size_bytes"] > MAX_CONFIG_BYTES or parse_bytes + item["size_bytes"] > MAX_PARSE_BYTES:
            gaps.append("config_parse_limit")
            continue
        raw = read_file(root, path)
        parse_bytes += len(raw)
        try:
            text = raw.decode("utf-8", errors="strict")
            if _license(path):
                spdx = sorted(set(re.findall(r"SPDX-License-Identifier:\s*([A-Za-z0-9.+-]{1,100})", text)))[:10]
                licenses.append({"path": path, "sha256": item["sha256"], "observed_spdx": spdx, "compatibility": "unverified"})
                observe(item, "license_text", {"license_or_notice_present": True, "observed_spdx": spdx, "rights_verified": False})
            elif name == "package.json":
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError()
                dependencies = {}
                for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    group = data.get(field, {})
                    if not isinstance(group, dict):
                        raise ValueError()
                    dependencies.update(group)
                candidates.add("node")
                for framework in ("next", "react", "vue", "svelte", "express"):
                    if framework in dependencies:
                        candidates.add(framework)
                declared_dependencies += len(dependencies)
                scripts = data.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError()
                observe(item, "package_json", {"name": _plain(data.get("name")),
                    "declared_dependency_count": len(dependencies),
                    "dependency_names": sorted(_plain(k) for k in dependencies)[:40],
                    "declared_scripts": {_plain(k, 80): _plain(v, 300) for k, v in list(scripts.items())[:20] if isinstance(v, str)},
                    "commands_approved": False, "workspaces_declared": "workspaces" in data,
                    "declared_license": _plain(data.get("license"))})
                if len(dependencies) > 40 or len(scripts) > 20:
                    gaps.append("declaration_display_limit")
                if any(isinstance(v, str) and v.startswith(("file:", "link:", "workspace:", "git+", "http:", "https:")) for v in dependencies.values()):
                    gaps.append("external_sources_require_review")
            elif name == "pyproject.toml":
                data = tomllib.loads(text)
                project = data.get("project", {})
                if not isinstance(project, dict):
                    raise ValueError()
                deps = project.get("dependencies", [])
                if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
                    raise ValueError()
                candidates.add("python")
                declared_dependencies += len(deps)
                names = [re.split(r"[<>=!~\[; @]", dep, 1)[0].lower() for dep in deps]
                candidates.update(framework for framework in ("flask", "fastapi", "django") if framework in names)
                observe(item, "pyproject_toml", {"name": _plain(project.get("name")),
                    "declared_dependencies": [_plain(d) for d in deps[:40]], "build_system_declared": "build-system" in data,
                    "runtime_verified": False})
                if "dynamic" in project or len(deps) > 40:
                    gaps.append("dynamic_or_truncated_metadata")
            else:
                lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
                candidates.add("python")
                declared_dependencies += len(lines)
                observe(item, "requirements_text", {"declaration_count": len(lines),
                    "declarations": [_plain(line) for line in lines[:40]], "dependencies_installed": False})
                if any(line.startswith(("-", ".", "/")) or "://" in line for line in lines):
                    gaps.append("external_sources_require_review")
                if len(lines) > 40:
                    gaps.append("declaration_display_limit")
            parsed += 1
        except (ValueError, TypeError, UnicodeError, RecursionError):
            gaps.append("config_parse_failed")
            observe(item, "static_parse", {"parsed": False, "error": "config_parse_failed"})
    if not licenses:
        gaps.append("license_not_identified")
    if provenance["parent_license_files"]:
        gaps.append("parent_license_scope_requires_review")
    if not candidates:
        candidates.add("unknown")
    report = {"policy_version": POLICY_VERSION, "analysis_kind": "static_inventory", "source_verified": True,
        "source_sha256": provenance["normalized_archive_sha256"], "manifest_digest": manifest_digest(manifest),
        "file_count": len(manifest), "size_bytes": sum(x["size_bytes"] for x in manifest),
        "stack_candidates": sorted(candidates), "parsed_config_files": parsed, "parsed_config_bytes": parse_bytes,
        "declaration_count": declared_dependencies, "observations": observations, "license_files": licenses[:40],
        "test_files": test_files, "tests_executed": False, "excluded_file_count": provenance["excluded_file_count"],
        "coverage_gaps": sorted(set(gaps)), "rights_status": "review_required", "execution_supported": False,
        "execution_blocker": "import_managed_source_ingress_unavailable", "qa_status": "not_run", "deployment_status": "not_run"}
    if len(canonical(report)) > MAX_REPORT_BYTES:
        raise ImportFailure("import_report_limit")
    return report


def analyze_archive(blob: bytes, *, github_wrapper=False, selected_root=None) -> AnalyzedSource:
    """Validate complete archive before root selection; preserve build/dist bytes."""
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_ARCHIVE_BYTES:
        raise ImportFailure("import_archive_size_invalid")
    try:
        inspect_blob(blob, limits=ArtifactLimits(max_file_bytes=MAX_ARCHIVE_BYTES))
        if selected_root is not None:
            safe_name(selected_root)
        with tempfile.TemporaryDirectory(prefix="supymem-import-parse-") as temporary:
            base = Path(temporary).resolve()
            original = extract_zip(blob, base / "raw")
            if not original:
                raise ImportFailure("import_empty_source")
            prefix = ""
            if github_wrapper:
                roots = {row["path"].split("/")[0] for row in original}
                if len(roots) != 1 or any("/" not in row["path"] for row in original):
                    raise ImportFailure("import_github_wrapper_invalid")
                prefix = next(iter(roots)) + "/"
            if selected_root:
                prefix += selected_root + "/"
            selected = [row for row in original if row["path"].startswith(prefix)]
            if not selected:
                raise ImportFailure("import_selected_root_missing")
            (base / "source").mkdir(mode=0o700)
            for row in selected:
                name = row["path"][len(prefix):]
                safe_name(name)
                write_file(base / "source", name, read_file(base / "raw", row["path"]))
            # Imported build/dist can be the actual product, not generated cache.
            manifest, normalized = freeze_source(base / "source", skip_dirs=frozenset())
            parent_licenses = [{"path": row["path"], "sha256": row["sha256"]} for row in original
                               if not row["path"].startswith(prefix) and _license(row["path"])]
            provenance = {"policy_version": POLICY_VERSION, "raw_sha256": hashlib.sha256(blob).hexdigest(),
                "normalized_archive_sha256": hashlib.sha256(normalized).hexdigest(), "selected_prefix": prefix,
                "excluded_file_count": len(original) - len(selected), "parent_license_files": parent_licenses[:40],
                "exclusion_policy": "explicit_selected_root_only"}
            return AnalyzedSource(normalized, manifest, _report(base / "source", manifest, provenance), provenance)
    except ArtifactRejected as exc:
        raise ImportFailure(str(exc)) from None
    except ImportFailure:
        raise
    except (OSError, ValueError, RecursionError):
        raise ImportFailure("import_archive_invalid") from None
