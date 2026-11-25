"""Independent checks for fresh, owned QA outputs; never app execution tools."""
from pathlib import Path


def exact_text(path: Path, expected: str) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 200_000:
        return False
    try:
        return path.read_text(encoding="utf-8").rstrip("\n") == expected.rstrip("\n")
    except (OSError, UnicodeError):
        return False


def exact_move(source: Path, destination: Path, expected: str) -> bool:
    return not source.exists() and not source.is_symlink() and exact_text(destination, expected)


def equivalent_png(source: Path, output: Path) -> bool:
    from PIL import Image
    if source.is_symlink() or output.is_symlink() or not output.is_file():
        return False
    try:
        with Image.open(source) as original, Image.open(output) as exported:
            if exported.format != "PNG" or original.size != exported.size:
                return False
            original.load()
            exported.load()
            return original.convert("RGB").tobytes() == exported.convert("RGB").tobytes()
    except (OSError, ValueError):
        return False
