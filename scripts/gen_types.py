"""Generate TypeScript types from Pydantic — minimal stub for make types."""
from __future__ import annotations
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "apps" / "console" / "src" / "lib" / "types.gen.ts"

def main() -> None:
    OUT.write_text(
        "// auto-generated stub — expand when OpenAPI export lands\n"
        "export type VentureId = string;\n"
        "export type AgentId = string;\n"
    )
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
