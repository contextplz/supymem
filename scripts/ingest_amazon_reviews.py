#!/usr/bin/env python3
"""Ingest McAuley-Lab/Amazon-Reviews-2023 metadata for Appliances + All_Beauty.

Produces:
  data/seed/catalog.seed.sqlite
  data/seed/manifest.json
  data/images/<product_id>.png   (deterministic local placeholders)

If Hugging Face download fails, falls back to a deterministic synthetic catalog
with the same schema so local development remains unblocked. Re-run with
--require-hf to fail instead of falling back.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "environment"))

CATEGORIES = [
    ("raw_meta_All_Beauty", "All_Beauty"),
    ("raw_meta_Appliances", "Appliances"),
]

DEFAULT_PER_CATEGORY = 150
DATASET_ID = "McAuley-Lab/Amazon-Reviews-2023"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_key(key: str) -> str:
    return (
        key.strip()
        .lower()
        .replace("-", " ")
        .replace("_", " ")
        .replace("  ", " ")
        .strip()
        .replace(" ", "_")
    )


def parse_details(raw: Any) -> dict[str, list[str]]:
    attrs: dict[str, list[str]] = {}
    if raw is None:
        return attrs
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return attrs
    if not isinstance(raw, dict):
        return attrs
    allow = {
        "brand",
        "color",
        "colour",
        "size",
        "material",
        "style",
        "voltage",
        "capacity",
        "dimensions",
        "form",
        "scent",
        "finish",
    }
    for k, v in raw.items():
        nk = normalize_key(str(k))
        # Map colour -> color
        if nk == "colour":
            nk = "color"
        base = nk.split("(")[0].strip("_")
        if base not in allow and nk not in allow:
            continue
        key = "color" if base in {"color", "colour"} else base
        if isinstance(v, list):
            vals = [str(x).strip() for x in v if str(x).strip()]
        else:
            vals = [str(v).strip()] if str(v).strip() else []
        if vals:
            attrs.setdefault(key, [])
            for val in vals:
                if val not in attrs[key]:
                    attrs[key].append(val)
    return attrs


def extract_options(attrs: dict[str, list[str]]) -> list[dict[str, str]]:
    options = []
    for key in ("color", "size", "voltage", "capacity"):
        for val in attrs.get(key, [])[:5]:
            options.append({"name": key, "value": val})
    return options


def make_png(product_id: str, title: str, out_path: Path) -> None:
    """Write a tiny deterministic RGB PNG (no Pillow dependency)."""
    # 64x64 solid color derived from product_id hash.
    digest = hashlib.sha256(product_id.encode()).digest()
    r, g, b = digest[0], digest[1], digest[2]
    width = height = 64
    raw_rows = b""
    for _y in range(height):
        raw_rows += b"\x00" + bytes([r, g, b]) * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw_rows, 9)) + chunk(b"IEND", b"")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(png)


def candidate_ok(row: dict[str, Any]) -> tuple[bool, str]:
    title = (row.get("title") or "").strip()
    if not title:
        return False, "missing_title"
    price = row.get("price")
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return False, "bad_price"
    if not (price_f > 0):
        return False, "nonpositive_price"
    rating = row.get("average_rating")
    try:
        rating_f = float(rating)
    except (TypeError, ValueError):
        return False, "bad_rating"
    if rating_f <= 0:
        return False, "nonpositive_rating"
    parent = (row.get("parent_asin") or "").strip()
    if not parent:
        return False, "missing_parent_asin"
    features = row.get("features") or []
    description = row.get("description") or []
    text_evidence = False
    if isinstance(features, list) and any(str(x).strip() for x in features):
        text_evidence = True
    if isinstance(description, list) and any(str(x).strip() for x in description):
        text_evidence = True
    if isinstance(description, str) and description.strip():
        text_evidence = True
    if not text_evidence and not (row.get("store") or "").strip():
        return False, "no_text_evidence"
    return True, "ok"


def normalize_row(row: dict[str, Any], category: str, revision: str, images_dir: Path) -> dict[str, Any]:
    parent = str(row["parent_asin"]).strip()
    title = str(row.get("title") or "").strip()
    brand = str(row.get("store") or "").strip()
    features = row.get("features") or []
    if not isinstance(features, list):
        features = [str(features)]
    features = [str(f).strip() for f in features if str(f).strip()][:12]
    desc = row.get("description") or []
    if isinstance(desc, list):
        description = " ".join(str(x).strip() for x in desc if str(x).strip())
    else:
        description = str(desc).strip()
    attrs = parse_details(row.get("details"))
    if brand:
        attrs.setdefault("brand", [brand])
    options = extract_options(attrs)
    image_rel = f"{category}/{parent}.png"
    make_png(parent, title, images_dir / image_rel)
    search_text = " ".join(
        [
            title,
            brand,
            category,
            " ".join(features),
            description[:500],
            " ".join(f"{k}:{','.join(v)}" for k, v in attrs.items()),
        ]
    ).lower()
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False).encode()
    return {
        "id": parent,
        "category": category,
        "title": title,
        "brand": brand,
        "description": description[:4000],
        "features_json": json.dumps(features, ensure_ascii=False),
        "price_usd": float(row["price"]),
        "rating": float(row.get("average_rating") or 0),
        "rating_count": int(row.get("rating_number") or 0),
        "image_path": image_rel,
        "attributes_json": json.dumps(attrs, ensure_ascii=False, sort_keys=True),
        "options_json": json.dumps(options, ensure_ascii=False),
        "search_text": search_text,
        "source_hash": sha256_bytes(payload),
        "source_revision": revision,
    }


def synthetic_catalog(per_category: int) -> list[dict[str, Any]]:
    """Deterministic fallback catalog when HF is unavailable."""
    beauty_brands = ["Lumina", "CoralGlow", "PureNest", "VelvetLeaf", "AquaBloom"]
    appliance_brands = ["HeatWave", "FrostPeak", "VoltHome", "CleanSpin", "BrewMaster"]
    beauty_types = ["moisturizer", "serum", "shampoo", "conditioner", "sunscreen", "face wash"]
    appliance_types = ["toaster", "blender", "air fryer", "coffee maker", "kettle", "vacuum"]
    colors = ["black", "white", "silver", "blue", "pink"]
    materials = ["plastic", "glass", "stainless steel", "ceramic"]
    rows: list[dict[str, Any]] = []
    for cat, brands, types in [
        ("All_Beauty", beauty_brands, beauty_types),
        ("Appliances", appliance_brands, appliance_types),
    ]:
        for i in range(per_category):
            brand = brands[i % len(brands)]
            ptype = types[i % len(types)]
            color = colors[i % len(colors)]
            material = materials[i % len(materials)]
            price = round(8.99 + (i % 40) * 2.5 + (hash(f"{cat}-{i}") % 100) / 100.0, 2)
            rating = round(3.5 + (i % 15) / 10.0, 1)
            if rating > 5:
                rating = 5.0
            pid = f"SYN{cat[:3].upper()}{i:04d}"
            title = f"{brand} {ptype.title()} {color.title()} {material.title()}"
            features = [
                f"{ptype} for everyday use",
                f"Color: {color}",
                f"Material: {material}",
                f"Brand: {brand}",
            ]
            details = {"Brand": brand, "Color": color, "Material": material}
            if cat == "Appliances":
                details["Voltage"] = "120V"
                features.append("Voltage: 120V")
            rows.append(
                {
                    "parent_asin": pid,
                    "title": title,
                    "store": brand,
                    "price": price,
                    "average_rating": rating,
                    "rating_number": 50 + i * 3,
                    "features": features,
                    "description": [f"A high quality {ptype} from {brand}."],
                    "details": details,
                    "main_category": cat,
                }
            )
    return rows


def download_hf_rows(config_name: str, revision: Optional[str], limit_scan: int) -> tuple[list[dict[str, Any]], str]:
    from huggingface_hub import hf_hub_download

    # Prefer raw JSONL if present; otherwise use datasets parquet via hub file list.
    rev = revision or "main"
    # Try common paths used by the dataset repo.
    candidates = [
        f"raw/{config_name}/{config_name}.jsonl",
        f"{config_name}/train-00000-of-00001.parquet",
    ]
    last_err: Exception | None = None
    path = None
    for rel in candidates:
        try:
            path = Path(hf_hub_download(DATASET_ID, rel, revision=rev, repo_type="dataset"))
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    if path is None:
        # Fall back to datasets library streaming.
        try:
            from datasets import load_dataset

            ds = load_dataset(DATASET_ID, config_name, split="full", revision=rev, trust_remote_code=True)
            rows = []
            for i, row in enumerate(ds):
                rows.append(dict(row))
                if i + 1 >= limit_scan:
                    break
            return rows, rev
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"HF download failed for {config_name}: {last_err}; datasets: {exc}") from exc

    rows: list[dict[str, Any]] = []
    if path.suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if len(rows) >= limit_scan:
                    break
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    else:
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(path)
            for i in range(min(limit_scan, table.num_rows)):
                rows.append({col: table.column(col)[i].as_py() for col in table.column_names})
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed reading {path}: {exc}") from exc
    return rows, rev


def write_sqlite(products: list[dict[str, Any]], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE products (
          id TEXT PRIMARY KEY,
          category TEXT,
          title TEXT,
          brand TEXT,
          description TEXT,
          features_json TEXT,
          price_usd REAL,
          rating REAL,
          rating_count INTEGER,
          image_path TEXT,
          attributes_json TEXT,
          options_json TEXT,
          search_text TEXT,
          source_hash TEXT,
          source_revision TEXT
        );
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY,
          instruction TEXT,
          gold_product_id TEXT,
          category TEXT,
          product_type TEXT,
          attributes_json TEXT,
          options_json TEXT,
          price_ceiling REAL,
          rating_floor REAL,
          generator_version TEXT,
          seed INTEGER
        );
        CREATE TABLE shopping_sessions (
          id TEXT PRIMARY KEY,
          task_id TEXT,
          created_at TIMESTAMP,
          updated_at TIMESTAMP,
          status TEXT
        );
        CREATE TABLE cart_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT,
          product_id TEXT,
          quantity INTEGER,
          selected_options_json TEXT
        );
        CREATE TABLE orders (
          id TEXT PRIMARY KEY,
          session_id TEXT,
          task_id TEXT,
          product_id TEXT,
          quantity INTEGER,
          selected_options_json TEXT,
          total_usd REAL,
          created_at TIMESTAMP,
          status TEXT
        );
        CREATE TABLE reward_results (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id TEXT UNIQUE,
          task_id TEXT,
          score REAL,
          success BOOLEAN,
          type_match BOOLEAN,
          matched_attributes INTEGER,
          matched_options INTEGER,
          price_ok BOOLEAN,
          rating_ok BOOLEAN,
          breakdown_json TEXT,
          scorer_version TEXT
        );
        CREATE TABLE agent_runs (
          id TEXT PRIMARY KEY,
          task_id TEXT,
          session_id TEXT,
          status TEXT,
          mode TEXT,
          created_at TIMESTAMP,
          started_at TIMESTAMP,
          finished_at TIMESTAMP,
          score REAL,
          success BOOLEAN,
          steps INTEGER,
          error TEXT,
          events_json TEXT,
          artifacts_json TEXT,
          provider_meta_json TEXT
        );
        """
    )
    cur.executemany(
        """
        INSERT INTO products (
          id, category, title, brand, description, features_json, price_usd, rating,
          rating_count, image_path, attributes_json, options_json, search_text,
          source_hash, source_revision
        ) VALUES (
          :id, :category, :title, :brand, :description, :features_json, :price_usd, :rating,
          :rating_count, :image_path, :attributes_json, :options_json, :search_text,
          :source_hash, :source_revision
        )
        """,
        products,
    )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-category", type=int, default=DEFAULT_PER_CATEGORY)
    parser.add_argument("--scan-limit", type=int, default=5000)
    parser.add_argument("--revision", default=os.getenv("AMAZON_REVIEWS_REVISION", "main"))
    parser.add_argument("--require-hf", action="store_true")
    parser.add_argument("--synthetic-only", action="store_true")
    args = parser.parse_args()

    seed_dir = ROOT / "data" / "seed"
    images_dir = ROOT / "data" / "images"
    seed_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    skipped: dict[str, int] = {}
    products: list[dict[str, Any]] = []
    source_mode = "huggingface"
    revision = args.revision

    if args.synthetic_only:
        source_mode = "synthetic"
        for cat in ("All_Beauty", "Appliances"):
            raw_rows = [r for r in synthetic_catalog(args.per_category) if r["main_category"] == cat]
            for row in sorted(raw_rows, key=lambda r: r["parent_asin"]):
                ok, reason = candidate_ok(row)
                if not ok:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                products.append(normalize_row(row, cat, "synthetic-v1", images_dir))
    else:
        try:
            for config_name, category in CATEGORIES:
                print(f"Downloading {config_name}...")
                rows, revision = download_hf_rows(config_name, args.revision, args.scan_limit)
                kept = []
                for row in rows:
                    ok, reason = candidate_ok(row)
                    if not ok:
                        skipped[reason] = skipped.get(reason, 0) + 1
                        continue
                    kept.append(row)
                kept.sort(key=lambda r: str(r.get("parent_asin") or ""))
                kept = kept[: args.per_category]
                for row in kept:
                    products.append(normalize_row(row, category, revision, images_dir))
        except Exception as exc:  # noqa: BLE001
            if args.require_hf:
                raise
            print(f"HF ingest failed ({exc}); falling back to synthetic catalog.", file=sys.stderr)
            source_mode = "synthetic_fallback"
            revision = "synthetic-v1"
            products = []
            for cat in ("All_Beauty", "Appliances"):
                raw_rows = [r for r in synthetic_catalog(args.per_category) if r["main_category"] == cat]
                for row in sorted(raw_rows, key=lambda r: r["parent_asin"]):
                    products.append(normalize_row(row, cat, revision, images_dir))

    products.sort(key=lambda p: p["id"])
    db_path = seed_dir / "catalog.seed.sqlite"
    write_sqlite(products, db_path)

    by_cat: dict[str, int] = {}
    for p in products:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1

    manifest = {
        "dataset_id": DATASET_ID,
        "revision": revision,
        "source_mode": source_mode,
        "categories": by_cat,
        "product_count": len(products),
        "skipped": skipped,
        "seed_db": str(db_path.relative_to(ROOT)),
        "seed_db_sha256": sha256_file(db_path),
        "per_category_cap": args.per_category,
        "license_note": (
            "Amazon Reviews 2023 is distributed for research; McAuley Lab does not assign a "
            "simple open redistribution license. Review rights before commercial redistribution."
        ),
    }
    (seed_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
