"""Counterexamples to weak existence, substring, and PNG-prefix QA proofs."""
import importlib.util
from pathlib import Path

from PIL import Image

spec = importlib.util.spec_from_file_location("cu_artifact_checks", Path(__file__).resolve().parents[2] / "scripts/desktop/cu_artifact_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def test_exact_text_rejects_substring_and_symlink(tmp_path):
    target = tmp_path / "headline.txt"
    target.write_text("prefix Real headline suffix")
    assert not checks.exact_text(target, "Real headline")
    target.write_text("Real headline\n")
    assert checks.exact_text(target, "Real headline")
    link = tmp_path / "borrowed.txt"
    link.symlink_to(target)
    assert not checks.exact_text(link, "Real headline")


def test_move_requires_exact_destination_bytes_and_source_absence(tmp_path):
    source, destination = tmp_path / "source.txt", tmp_path / "final.txt"
    (tmp_path / "any-renamed.txt").write_text("expected")
    assert not checks.exact_move(source, destination, "expected")
    destination.write_text("different")
    assert not checks.exact_move(source, destination, "expected")
    destination.write_text("expected")
    source.write_text("expected")
    assert not checks.exact_move(source, destination, "expected")
    source.unlink()
    assert checks.exact_move(source, destination, "expected")


def test_png_requires_decoding_dimensions_and_pixels(tmp_path):
    source, output = tmp_path / "source.jpg", tmp_path / "export.png"
    Image.new("RGB", (24, 16), "orange").save(source)
    output.write_bytes(b"\x89PNGgarbage")
    assert not checks.equivalent_png(source, output)
    Image.new("RGB", (1, 1), "orange").save(output)
    assert not checks.equivalent_png(source, output)
    Image.new("RGB", (24, 16), "blue").save(output)
    assert not checks.equivalent_png(source, output)
    with Image.open(source) as decoded:
        decoded.save(output, format="PNG")
    assert checks.equivalent_png(source, output)


def test_png_rejects_borrowed_output(tmp_path):
    source, output = tmp_path / "source.png", tmp_path / "export.png"
    Image.new("RGB", (2, 2), "orange").save(source)
    output.symlink_to(source)
    assert not checks.equivalent_png(source, output)
