#!/usr/bin/env python3
"""Generate >=200 deterministic shopping task instructions from the seed catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_VERSION = "taskgen-v1"
DEFAULT_SEED = 42


def task_id_for(parts: list[str]) -> str:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"task_{h[:16]}"


def load_products(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["attributes"] = json.loads(d["attributes_json"] or "{}")
        d["options"] = json.loads(d["options_json"] or "[]")
        d["features"] = json.loads(d["features_json"] or "[]")
        out.append(d)
    return out


def infer_product_type(product: dict) -> str:
    title = product["title"].lower()
    # Prefer last distinctive noun-ish token groups from known vocabularies.
    vocab = [
        "moisturizer",
        "serum",
        "shampoo",
        "conditioner",
        "sunscreen",
        "face wash",
        "toaster",
        "blender",
        "air fryer",
        "coffee maker",
        "kettle",
        "vacuum",
        "cream",
        "lotion",
        "mask",
        "cleaner",
        "mixer",
        "iron",
    ]
    for v in vocab:
        if v in title:
            return v
    # Fallback: second token after brand if present.
    tokens = product["title"].split()
    if len(tokens) >= 2:
        return tokens[1].lower().strip(",.-")
    return tokens[0].lower() if tokens else "product"


def pick_constraints(product: dict, rng: random.Random) -> tuple[dict[str, str], dict[str, str]]:
    attrs = product["attributes"]
    required_attrs: dict[str, str] = {}
    required_opts: dict[str, str] = {}

    if product.get("brand"):
        if rng.random() < 0.8:
            required_attrs["brand"] = product["brand"]

    for key in ("color", "material", "size", "form", "scent"):
        vals = attrs.get(key) or []
        if vals and rng.random() < 0.7:
            required_attrs[key] = vals[0]
        if len(required_attrs) >= 3:
            break

    # Options: prefer color/size/voltage if present in options list.
    for opt in product["options"]:
        name = opt["name"]
        if name in required_attrs:
            continue
        if name in {"color", "size", "voltage", "capacity"} and rng.random() < 0.5:
            required_opts[name] = opt["value"]
        if len(required_opts) >= 1:
            break

    # Ensure at least one attribute besides maybe brand.
    if not required_attrs and attrs:
        key = sorted(attrs.keys())[0]
        required_attrs[key] = attrs[key][0]
    return required_attrs, required_opts


def render_instruction(
    product_type: str,
    category: str,
    required_attrs: dict[str, str],
    required_opts: dict[str, str],
    price_ceiling: float,
    rating_floor: float,
) -> str:
    bits = [f"Buy a {product_type}"]
    if "brand" in required_attrs:
        bits.append(f"from {required_attrs['brand']}")
    for k, v in required_attrs.items():
        if k == "brand":
            continue
        bits.append(f"with {k} {v}")
    for k, v in required_opts.items():
        bits.append(f"option {k}={v}")
    bits.append(f"under ${price_ceiling:.2f}")
    if rating_floor > 0:
        bits.append(f"with at least a {rating_floor:.1f}-star rating")
    bits.append(f"(category {category})")
    return " ".join(bits) + "."


def generate_tasks(products: list[dict], count: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    # Stable eligible products with enough signal.
    eligible = []
    for p in products:
        attrs = p["attributes"]
        if p["price_usd"] <= 0:
            continue
        if not p["title"]:
            continue
        if not attrs and not p.get("brand"):
            continue
        eligible.append(p)
    if not eligible:
        raise SystemExit("No eligible products for task generation")

    tasks: list[dict] = []
    # Round-robin over sorted products for coverage.
    idx = 0
    attempts = 0
    while len(tasks) < count and attempts < count * 20:
        attempts += 1
        product = eligible[idx % len(eligible)]
        idx += 1
        product_type = infer_product_type(product)
        required_attrs, required_opts = pick_constraints(product, rng)
        # Price ceiling slightly above gold price.
        bump = 1.0 + (rng.randrange(5, 35) / 10.0)
        price_ceiling = round(math.ceil(product["price_usd"] + bump) + 0.99, 2)
        rating_floor = 0.0
        if product["rating"] >= 4.0 and rng.random() < 0.6:
            rating_floor = round(min(4.0, product["rating"] - 0.1), 1)
            if rating_floor < 3.5:
                rating_floor = 0.0

        instruction = render_instruction(
            product_type,
            product["category"],
            required_attrs,
            required_opts,
            price_ceiling,
            rating_floor,
        )
        tid = task_id_for(
            [
                GENERATOR_VERSION,
                str(seed),
                product["id"],
                product_type,
                json.dumps(required_attrs, sort_keys=True),
                json.dumps(required_opts, sort_keys=True),
                f"{price_ceiling:.2f}",
                f"{rating_floor:.1f}",
            ]
        )
        if any(t["id"] == tid for t in tasks):
            continue
        tasks.append(
            {
                "id": tid,
                "instruction": instruction,
                "gold_product_id": product["id"],
                "category": product["category"],
                "product_type": product_type,
                "attributes_json": json.dumps(required_attrs, ensure_ascii=False, sort_keys=True),
                "options_json": json.dumps(required_opts, ensure_ascii=False, sort_keys=True),
                "price_ceiling": price_ceiling,
                "rating_floor": rating_floor,
                "generator_version": GENERATOR_VERSION,
                "seed": seed,
            }
        )
    if len(tasks) < count:
        raise SystemExit(f"Only generated {len(tasks)} tasks, needed {count}")
    tasks.sort(key=lambda t: t["id"])
    return tasks[:count]


def write_tasks(db_path: Path, tasks: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks")
    cur.executemany(
        """
        INSERT INTO tasks (
          id, instruction, gold_product_id, category, product_type,
          attributes_json, options_json, price_ceiling, rating_floor,
          generator_version, seed
        ) VALUES (
          :id, :instruction, :gold_product_id, :category, :product_type,
          :attributes_json, :options_json, :price_ceiling, :rating_floor,
          :generator_version, :seed
        )
        """,
        tasks,
    )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=220)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "seed" / "catalog.seed.sqlite")
    args = parser.parse_args()

    products = load_products(args.db)
    tasks = generate_tasks(products, args.count, args.seed)
    write_tasks(args.db, tasks)
    out = ROOT / "data" / "seed" / "tasks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(json.dumps({"task_count": len(tasks), "db": str(args.db), "jsonl": str(out)}, indent=2))


if __name__ == "__main__":
    main()
