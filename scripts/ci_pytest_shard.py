#!/usr/bin/env python3
"""Deterministic pytest sharding and collection-completeness guard for CI.

Shard mode (each matrix job):
  python scripts/ci_pytest_shard.py --splits 4 --group 1 \\
    --pytest-bin .venv/bin/python --nodeids-out shard-1.txt -- paths...

Verify mode (aggregate gate):
  python scripts/ci_pytest_shard.py --verify --splits 4 \\
    --pytest-bin .venv/bin/python --nodeids-dir artifacts/ -- paths...

Every collected nodeid maps to exactly one group via sha256(nodeid) % splits.
Legacy --verify recollects independently. --verify-attestations instead requires
all successful shards to attest identical fresh collections at the exact same
commit, run attempt and selection; it verifies the complete deterministic partition
using only the standard library. No test selection or execution is cached.
"""

from __future__ import annotations

import argparse
import re
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parse_collect(stdout: str) -> list[str]:
    nodeids: list[str] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("="):
            continue
        if text.startswith("no tests") or text.startswith("ERROR"):
            continue
        if "::" in text or text.endswith(".py"):
            nodeids.append(text)
    return nodeids


def collect_nodeids(pytest_parts: list[str], paths: list[str], extra: list[str]) -> list[str]:
    cmd = [*pytest_parts, "--collect-only", "-q", *extra, *paths]
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    sys.stdout.write(proc.stdout or "")
    sys.stdout.flush()
    if proc.returncode != 0:
        raise SystemExit(f"collect-only failed with exit {proc.returncode}")
    nodeids = _parse_collect(proc.stdout or "")
    if len(nodeids) != len(set(nodeids)):
        raise SystemExit("duplicate collected nodeids")
    return sorted(nodeids)


def assign_group(nodeid: str, splits: int) -> int:
    digest = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()
    return int(digest, 16) % splits


def partition(nodeids: list[str], splits: int) -> list[list[str]]:
    groups: list[list[str]] = [[] for _ in range(splits)]
    for nodeid in sorted(nodeids):
        groups[assign_group(nodeid, splits)].append(nodeid)
    return groups


def write_nodeids(path: Path, nodeids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(nodeids) + ("\n" if nodeids else ""), encoding="utf-8")


def read_nodeids(path: Path) -> list[str]:
    if not path.is_file():
        raise SystemExit(f"missing nodeids file: {path}")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split_paths(remainder: list[str]) -> tuple[list[str], list[str]]:
    extra: list[str] = []
    paths: list[str] = []
    for item in remainder:
        if paths or item.startswith("tests/") or item.endswith(".py") or "/tests/" in item:
            paths.append(item)
        else:
            extra.append(item)
    if not paths:
        return [], remainder
    return extra, paths


def pytest_parts_from_bin(pytest_bin: str) -> list[str]:
    if not pytest_bin:
        return [sys.executable, "-m", "pytest"]
    parts = pytest_bin.split()
    if len(parts) == 1:
        return [parts[0], "-m", "pytest"]
    return parts


def run_shard(
    *,
    pytest_parts: list[str],
    paths: list[str],
    extra: list[str],
    splits: int,
    group: int,
    nodeids_out: Path | None,
    attest: bool = False,
    run_args: list[str] | None = None,
) -> int:
    if group < 1 or group > splits:
        raise SystemExit(f"--group must be in 1..{splits}, got {group}")
    identity = attestation_identity(paths, extra, splits) if attest else None
    if attest and nodeids_out is None:
        raise SystemExit("--attest requires --nodeids-out")
    if attest:
        nodeids_out.with_name(f"shard-{group}.json").unlink(missing_ok=True)
    all_ids = collect_nodeids(pytest_parts, paths, extra)
    groups = partition(all_ids, splits)
    selected = groups[group - 1]
    print(
        f"ci_pytest_shard: {len(all_ids)} collected; "
        f"shard {group}/{splits} runs {len(selected)}",
        flush=True,
    )
    for i, members in enumerate(groups, start=1):
        print(f"  group {i}: {len(members)}", flush=True)
    if nodeids_out is not None:
        write_nodeids(nodeids_out, selected)
        write_nodeids(nodeids_out.with_name(f"shard-{group}-all.txt"), all_ids)
    if not selected:
        print("ci_pytest_shard: empty shard (ok)", flush=True)
        result = 0
    else:
        # run_args (e.g. xdist -n) apply to execution only: not to collection or the
        # attested identity, so the nodeid partition stays byte-identical.
        cmd = [*pytest_parts, "-q", *(run_args or []), *extra, *selected]
        print("+", " ".join(cmd[:8]), f"... ({len(selected)} nodeids)", flush=True)
        result = subprocess.run(cmd, cwd=ROOT).returncode
    if identity is not None and result == 0:
        # Written only after successful execution, never recovered from a cache.
        assert nodeids_out is not None
        receipt = {**identity, "group": group, "test_exit": result,
                   "collection_sha256": nodeids_digest(all_ids),
                   "selected_sha256": nodeids_digest(selected)}
        nodeids_out.with_name(f"shard-{group}.json").write_text(
            json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return result


def nodeids_digest(nodeids: list[str]) -> str:
    return hashlib.sha256(json.dumps(nodeids, ensure_ascii=True).encode()).hexdigest()


def attestation_identity(paths: list[str], extra: list[str], splits: int) -> dict:
    """Trusted expected identity comes from this job's checkout and GitHub context."""
    if splits < 1:
        raise SystemExit("splits must be positive")
    names = ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW")
    context = {name: os.environ.get(name, "") for name in names}
    if not all(context.values()):
        raise SystemExit("attestations require complete GitHub run context")
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        # The workflow checks out the PR head, not the synthetic merge commit in GITHUB_SHA.
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        context["GITHUB_SHA"] = event["pull_request"]["head"]["sha"]
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if checkout != context["GITHUB_SHA"]:
        raise SystemExit("checkout differs from GITHUB_SHA")
    return {"schema": 1, "context": context, "paths": paths, "extra": extra,
            "splits": splits, "assignment": "sha256-mod-v1",
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate receipt field: {key}")
        result[key] = value
    return result


def strict_nodeids(path: Path) -> list[str]:
    ids = read_nodeids(path)
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError(f"unsorted or duplicate nodeids: {path.name}")
    return ids


def verify_attestations(*, paths: list[str], extra: list[str], splits: int, nodeids_dir: Path,
                        artifact_prefix: str = "") -> int:
    """No pytest/dependencies: require every successful shard's same-run collection."""
    identity = attestation_identity(paths, extra, splits)
    expected_files = {f"shard-{g}{suffix}" for g in range(1, splits + 1)
                      for suffix in (".txt", "-all.txt", ".json")}
    try:
        locations = {g: nodeids_dir for g in range(1, splits + 1)}
        attempts = {g: identity["context"]["GITHUB_RUN_ATTEMPT"] for g in locations}
        if artifact_prefix:
            # "Re-run failed jobs" only produces artifacts for the rerun shards, so accept the
            # highest attempt (<= this one) per shard; older attempts of a shard are ignored.
            current = int(attempts[1])
            found: dict[int, int] = {}
            pattern = re.compile(rf"{re.escape(artifact_prefix)}-(\d+)-(\d+)")
            for entry in nodeids_dir.iterdir():
                match = pattern.fullmatch(entry.name)
                if not match or not 1 <= int(match[2]) <= splits or int(match[1]) > current:
                    raise ValueError("missing, duplicate or unexpected shard artifact directories")
                found[int(match[2])] = max(found.get(int(match[2]), 0), int(match[1]))
            if set(found) != set(locations):
                raise ValueError("missing, duplicate or unexpected shard artifact directories")
            attempts = {g: str(found[g]) for g in locations}
            locations = {g: nodeids_dir / f"{artifact_prefix}-{attempts[g]}-{g}" for g in locations}
            for group, directory in locations.items():
                want_files = {f"shard-{group}{suffix}" for suffix in (".txt", "-all.txt", ".json")}
                if {p.name for p in directory.iterdir()} != want_files:
                    raise ValueError("missing or unexpected files inside shard artifact")
        elif {p.name for p in nodeids_dir.iterdir()} != expected_files:
            raise ValueError("missing or unexpected artifact files")
        reference = None
        union: set[str] = set()
        for group in range(1, splits + 1):
            directory = locations[group]
            all_ids = strict_nodeids(directory / f"shard-{group}-all.txt")
            selected = strict_nodeids(directory / f"shard-{group}.txt")
            receipt = json.loads((directory / f"shard-{group}.json").read_text(),
                                 object_pairs_hook=unique_object)
            want = {**identity, "context": {**identity["context"], "GITHUB_RUN_ATTEMPT": attempts[group]},
                    "group": group, "test_exit": 0,
                    "collection_sha256": nodeids_digest(all_ids),
                    "selected_sha256": nodeids_digest(selected)}
            if json.dumps(receipt, sort_keys=True) != json.dumps(want, sort_keys=True):
                raise ValueError(f"shard {group} receipt identity/content mismatch")
            if not all_ids:
                raise ValueError("empty full collection")
            if reference is None:
                reference = all_ids
            elif reference != all_ids:
                raise ValueError(f"shard {group} full collection differs")
            if selected != partition(all_ids, splits)[group - 1]:
                raise ValueError(f"shard {group} assignment differs")
            if union.intersection(selected):
                raise ValueError(f"shard {group} overlaps")
            union.update(selected)
        if reference is None or union != set(reference):
            raise ValueError("shard union differs from fresh full collections")
    except (OSError, ValueError, TypeError) as exc:
        print(f"ci_pytest_shard attestations: FAIL: {exc}")
        return 1
    print(f"ci_pytest_shard attestations: PASS ({len(union)} tests, {splits} fresh collections)")
    return 0


def verify(
    *,
    pytest_parts: list[str],
    paths: list[str],
    extra: list[str],
    splits: int,
    nodeids_dir: Path,
) -> int:
    expected = collect_nodeids(pytest_parts, paths, extra)
    expected_set = set(expected)
    shard_sets: list[set[str]] = []
    missing_files: list[str] = []
    for group in range(1, splits + 1):
        path = nodeids_dir / f"shard-{group}.txt"
        if not path.is_file():
            missing_files.append(str(path))
            continue
        shard_sets.append(set(read_nodeids(path)))
    if missing_files:
        print("ci_pytest_shard verify FAILED: missing shard nodeid files:")
        for item in missing_files:
            print(f"  - {item}")
        return 1

    union: set[str] = set()
    overlaps: list[str] = []
    for idx, members in enumerate(shard_sets, start=1):
        overlap = union & members
        if overlap:
            overlaps.extend(f"shard-{idx}: {nodeid}" for nodeid in sorted(overlap)[:20])
        union |= members

    recomputed = partition(expected, splits)
    mismatch_assignment = False
    for group, (actual, want) in enumerate(zip(shard_sets, recomputed, strict=True), start=1):
        if actual != set(want):
            mismatch_assignment = True
            only_actual = sorted(actual - set(want))[:10]
            only_want = sorted(set(want) - actual)[:10]
            print(f"ci_pytest_shard verify: shard {group} assignment drift")
            if only_actual:
                print(f"  unexpected: {only_actual}")
            if only_want:
                print(f"  missing: {only_want}")

    missing = sorted(expected_set - union)
    extra_ids = sorted(union - expected_set)
    ok = not missing and not extra_ids and not overlaps and not mismatch_assignment
    print(
        f"ci_pytest_shard verify: collected={len(expected_set)} "
        f"union={len(union)} overlaps={len(overlaps)} "
        f"missing={len(missing)} extra={len(extra_ids)}",
        flush=True,
    )
    if overlaps:
        print("overlapping nodeids across shards:")
        for row in overlaps[:50]:
            print(f"  - {row}")
    if missing:
        print("missing from shards (present in collect-only):")
        for row in missing[:50]:
            print(f"  - {row}")
    if extra_ids:
        print("extra in shards (absent from collect-only):")
        for row in extra_ids[:50]:
            print(f"  - {row}")
    if ok:
        print("ci_pytest_shard verify: PASS")
        return 0
    print("ci_pytest_shard verify: FAIL")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--splits", type=int, required=True)
    parser.add_argument("--group", type=int, default=0)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--attest", action="store_true")
    parser.add_argument("--verify-attestations", action="store_true")
    parser.add_argument("--artifact-prefix", default="")
    parser.add_argument(
        "--pytest-bin",
        default="",
        help="Python that can run '-m pytest' (default: sys.executable)",
    )
    parser.add_argument("--run-args", default="", help="extra pytest args for the execution step only")
    parser.add_argument("--nodeids-out", type=Path, default=None)
    parser.add_argument("--nodeids-dir", type=Path, default=None)
    parser.add_argument("remainder", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    remainder = list(args.remainder)
    if remainder and remainder[0] == "--":
        remainder = remainder[1:]
    if not remainder:
        raise SystemExit("pass pytest paths after --")

    pytest_parts = pytest_parts_from_bin(args.pytest_bin)
    extra, paths = split_paths(remainder)

    if args.splits < 1:
        raise SystemExit("--splits must be positive")
    if args.verify_attestations:
        if args.nodeids_dir is None:
            raise SystemExit("--verify-attestations requires --nodeids-dir")
        return verify_attestations(paths=paths, extra=extra, splits=args.splits,
                                   nodeids_dir=args.nodeids_dir, artifact_prefix=args.artifact_prefix)

    if args.verify:
        if args.nodeids_dir is None:
            raise SystemExit("--verify requires --nodeids-dir")
        return verify(
            pytest_parts=pytest_parts,
            paths=paths,
            extra=extra,
            splits=args.splits,
            nodeids_dir=args.nodeids_dir,
        )

    if args.group < 1:
        raise SystemExit("shard mode requires --group N (1-based)")
    return run_shard(
        pytest_parts=pytest_parts,
        paths=paths,
        extra=extra,
        splits=args.splits,
        group=args.group,
        nodeids_out=args.nodeids_out,
        attest=args.attest,
        run_args=shlex.split(args.run_args),
    )


if __name__ == "__main__":
    raise SystemExit(main())
