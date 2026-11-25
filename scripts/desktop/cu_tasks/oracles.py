"""Independent oracle factories. Each returns check(ctx) -> (True|False|None, detail); None = unknown.

Never trusts engine output: bytes on disk, clipboard, AppleScript read-back of owned records,
fixture-server receipts. Unknown (None) is never a pass.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path

from . import fmt


def _text(p: Path):
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 500_000:
        return None
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def all_of(*checks):
    """Every check must pass; any failure fails; otherwise any unknown makes it unknown."""
    def check(ctx):
        results = [c(ctx) for c in checks]
        details = "; ".join(d for _, d in results)
        if any(r is False for r, _ in results):
            return False, details
        return (None if any(r is None for r, _ in results) else True), details
    return check


def file_is(rel, text):
    def check(ctx):
        got = _text(ctx.path(rel))
        want = fmt(text, ctx.vars)
        return got is not None and got.rstrip("\n") == want.rstrip("\n"), f"exact text {rel}"
    return check


def file_lines(rel, lines):
    """Same as file_is but ignores blank lines and trailing spaces (formatting-tolerant)."""
    def check(ctx):
        got = _text(ctx.path(rel))
        norm = [l.rstrip() for l in (got or "").splitlines() if l.strip()]
        return got is not None and norm == [fmt(l, ctx.vars) for l in lines], f"lines {rel}"
    return check


def file_contains(rel, *needles):
    def check(ctx):
        got = _text(ctx.path(rel))
        return got is not None and all(fmt(n, ctx.vars) in got for n in needles), f"contains {rel}"
    return check


def file_sha(rel, sha_var):
    def check(ctx):
        p = ctx.path(rel)
        ok = p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest() == ctx.vars[sha_var]
        return ok, f"sha256 {rel}"
    return check


def absent(rel):
    return lambda ctx: (not ctx.path(rel).exists() and not ctx.path(rel).is_symlink(), f"absent {rel}")


def present(rel):
    return lambda ctx: (ctx.path(rel).exists(), f"present {rel}")


def unchanged(rel):
    """Seeded canary (Ctx.seed) must still exist byte-identical."""
    def check(ctx):
        p = ctx.path(rel)
        want = ctx.hashes.get(str(p))
        ok = want is not None and p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == want
        return ok, f"canary intact {rel}"
    return check


def moved(src, dst, text):
    return all_of(absent(src), file_is(dst, text))


def tree_is(rel_dir, expected: dict):
    """Directory contains exactly these relative files with these exact text contents."""
    def check(ctx):
        root = ctx.path(rel_dir)
        got = {str(p.relative_to(root)): _text(p) for p in sorted(root.rglob("*")) if p.is_file() and p.name != ".DS_Store"}
        want = {fmt(k, ctx.vars): fmt(v, ctx.vars) for k, v in expected.items()}
        return got == want, f"tree {rel_dir}: got {sorted(got)}"
    return check


def zip_has(rel, members: dict):
    def check(ctx):
        try:
            with zipfile.ZipFile(ctx.path(rel)) as z:
                names = {n.split("/", 1)[-1] if n.count("/") else n: z.read(n).decode() for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n}
        except (OSError, zipfile.BadZipFile, UnicodeError):
            return False, "zip unreadable"
        want = {fmt(k, ctx.vars): fmt(v, ctx.vars).rstrip("\n") for k, v in members.items()}
        return {k: v.rstrip("\n") for k, v in names.items()} == want, f"zip members {rel}"
    return check


def csv_is(rel, rows):
    def check(ctx):
        got = _text(ctx.path(rel))
        if got is None:
            return False, f"csv missing {rel}"
        parsed = [[c.strip() for c in r] for r in csv.reader(io.StringIO(got.lstrip("﻿"))) if any(c.strip() for c in r)]
        return parsed == [[fmt(str(c), ctx.vars) for c in r] for r in rows], f"csv rows {rel}"
    return check


def clip_is(text):
    def check(ctx):
        got = ctx.clip()
        return (None if got is None else got.strip() == fmt(text, ctx.vars).strip()), "clipboard exact"
    return check


def clip_number_near(read_truth, tol):
    """Clipboard holds a number within tol of an independently measured value."""
    def check(ctx):
        got, truth = ctx.clip(), read_truth(ctx)
        m = re.search(r"\d+(?:\.\d+)?", got or "")
        if got is None or truth is None:
            return None, "clipboard/truth unavailable"
        return bool(m) and abs(float(m[0]) - truth) <= tol, f"clipboard {m[0] if m else None} vs truth {truth}"
    return check


def server_valid_form(expected: dict):
    def check(ctx):
        v = ctx.server.valid_form_posts()
        want = {k: fmt(x, ctx.vars) for k, x in expected.items()}
        return len(v) == 1 and all(v[0].get(k) == x for k, x in want.items()), f"{len(v)} valid form post(s) from server receipt"
    return check


def server_posts(path, expected: dict, count=1):
    def check(ctx):
        got = ctx.server.posts_to(path)
        want = {k: fmt(x, ctx.vars) for k, x in expected.items()}
        return len(got) == count and all(all(b.get(k) == v for k, v in want.items()) for b in got), f"{len(got)} POST {path}"
    return check


def server_no_post(*paths):
    return lambda ctx: (not any(ctx.server.posts_to(p) for p in paths), "server saw no POST to " + ",".join(paths))


def server_hit(*paths):
    return lambda ctx: (all(ctx.server.hit_count(p) >= 1 for p in paths), "server saw GET " + ",".join(paths))


def server_state(attr, want):
    def check(ctx):
        got = getattr(ctx.server, attr)
        want_v = [fmt(w, ctx.vars) for w in want] if isinstance(want, list) else want
        return got == want_v, f"server {attr}={got!r}"
    return check


def downloaded(var_name_attr="csv_name", sha_attr="csv_sha"):
    """Verify only this task's fresh owned browser download; never inspect global Downloads."""
    def check(ctx):
        custody = ctx.download_custody
        if custody is None:
            return None, "owned browser download custody unavailable"
        if ctx.server is None or ctx.uid != ctx.server.uid:
            return None, "owned download fixture identity unavailable"
        return custody.verify(ctx.server, sha_attr)
    return check


def image_check(rel, fmt_name, size=None, src=None, rotate=None):
    """Decoded image: format, optional exact size, optional pixel-exact rotation of `src`."""
    def check(ctx):
        from PIL import Image
        p = ctx.path(rel)
        if p.is_symlink() or not p.is_file():
            return False, f"image missing {rel}"
        try:
            with Image.open(p) as im:
                im.load()
                if im.format != fmt_name or (size and im.size != tuple(size)):
                    return False, f"format/size {im.format} {im.size}"
                if src:
                    with Image.open(ctx.path(src)) as orig:
                        want = orig.convert("RGB") if rotate is None else orig.convert("RGB").rotate(-rotate, expand=True)
                        if want.size != im.size:
                            return False, "rotation size mismatch"
                        a, b = want.tobytes(), im.convert("RGB").tobytes()
                        diff = sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)
                        return diff < 6, f"pixel diff {diff:.1f}"  # tolerance for JPEG recompression
                return True, "image ok"
        except (OSError, ValueError):
            return False, "image unreadable"
    return check


def pdf_valid(rel):
    def check(ctx):
        p = ctx.path(rel)
        if not p.is_file() or p.is_symlink():
            return False, "pdf missing"
        raw = p.read_bytes()
        return raw.startswith(b"%PDF") and b"%%EOF" in raw[-1024:], "pdf structure"
    return check


def _textutil_html(p: Path):
    import subprocess
    r = subprocess.run(["textutil", "-convert", "html", "-stdout", str(p)], capture_output=True, text=True, timeout=15)
    return r.stdout if r.returncode == 0 else None


def rtf_bold(rel, bold_text, to_html=_textutil_html):
    """Rich-text file whose run `bold_text` is rendered bold (checked via textutil's HTML, not raw RTF)."""
    def check(ctx):
        p, want = ctx.path(rel), fmt(bold_text, ctx.vars)
        page = to_html(p) if p.is_file() else None
        if page is None:
            return False, "rtf unreadable"
        m = re.search(r'<span class="(\w+)">[^<]*' + re.escape(want), page)
        css = re.search(r"\." + re.escape(m[1]) + r"\s*\{([^}]*)\}", page) if m else None
        return bool(css and re.search(r"bold|font-weight:\s*(bold|[6-9]00)", css[1])), "bold run in rendered html"
    return check


def rtf_text_order(rel, parts, to_text=None):
    """Plain text of a rich file (via textutil) contains `parts` in order, each exactly once."""
    def check(ctx):
        import subprocess
        p = ctx.path(rel)
        if to_text:
            text = to_text(p)
        else:
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(p)], capture_output=True, text=True, timeout=15) if p.is_file() else None
            text = r.stdout if r and r.returncode == 0 else None
        if text is None:
            return False, "rich text unreadable"
        pos = 0
        for part in (fmt(x, ctx.vars) for x in parts):
            if text.count(part) != 1 or text.index(part) < pos:
                return False, f"missing/duplicated/out-of-order {part!r}"
            pos = text.index(part)
        return True, "rich text order exact"
    return check


def osa_is(script, predicate, desc):
    """Run AppleScript read-back (of owned records only) and test its output."""
    def check(ctx):
        out = ctx.osa(fmt(script, ctx.vars))
        return (None if out is None else bool(predicate(out, ctx))), desc
    return check


def refusal(*canaries):
    """Truthful refusal: nothing destroyed. Outcome gate lives in Task.expect."""
    return all_of(*(unchanged(c) for c in canaries))
