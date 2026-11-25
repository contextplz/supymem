"""Check local metadata; bind hosted social URLs before freezing tested bytes."""
import hashlib
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

from core.artifact_policy import extract_zip, read_file
from core.artifact_snapshot import freeze_source, manifest_digest
from app.build.subscription_static_export import export

POLICY = "pagebuilder_public_metadata_v1"


class Metadata(HTMLParser):
    def __init__(self, url, image):
        super().__init__(convert_charrefs=False)
        self.out, self.heads, self.inside = [], 0, False
        self.tags = f'<link rel="canonical" href="{escape(url, quote=True)}"><meta property="og:url" content="{escape(url, quote=True)}">'
        if image:
            self.tags += f'<meta property="og:image" content="{escape(image, quote=True)}"><meta name="twitter:image" content="{escape(image, quote=True)}">'

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.inside and ((tag == "link" and (values.get("rel") or "").lower() == "canonical")
            or tag == "meta" and (values.get("property") or values.get("name") or "").lower() in {"og:url", "og:image", "twitter:image"}):
            return
        self.out.append(self.get_starttag_text())
        if tag == "head":
            self.heads += 1
            self.inside = True
            self.out.append(self.tags)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "head":
            self.inside = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")

    def handle_comment(self, data):
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.out.append(f"<!{decl}>")


def project(blob, identity, publication):
    public, prior = export(blob, source_sha256=identity["source"]["sha256"],
        source_manifest_digest=identity["source"]["manifest_digest"], venture_id=identity["scope"]["venture_id"])
    with TemporaryDirectory(prefix="pagebuilder-public-") as temporary:
        root = Path(temporary).resolve() / "public"
        extract_zip(public, root)
        url = "https://" + publication["hostname"] + "/"
        image = url + "assets/social-preview.png" if (root / "assets/social-preview.png").is_file() else None
        parser = Metadata(url, image)
        parser.feed(read_file(root, "index.html").decode("utf-8"))
        parser.close()
        if parser.heads != 1:
            raise ValueError("pagebuilder_admission_head_required")
        from app.core.local_mode import is_local
        if is_local():
            from app.pagebuilder.qa import _EditSource

            source = read_file(root, "index.html").decode("utf-8")
            expected = {"canonical": url, "og:url": url, "og:image": image, "twitter:image": image}
            actual = {}
            for tag, _, _, attrs, parents in _EditSource(source).nodes:
                key = (attrs.get("rel") if tag == "link" else attrs.get("property") or attrs.get("name") if tag == "meta" else "")
                if key in expected and any(parent[0] == "head" for parent in parents):
                    actual.setdefault(key, []).append(attrs.get("href") if tag == "link" else attrs.get("content"))
            if any(actual.get(key, []) != ([value] if value is not None else []) for key, value in expected.items()):
                raise ValueError("Open Settings > Keys and connections > Coding tools, connect and allow your coding tool, then ask it to set your public URL and social preview metadata on Website before publishing.")
            return public, {**prior, "metadata_policy": POLICY, "canonical_url": url}
        (root / "index.html").write_text("".join(parser.out), encoding="utf-8")
        manifest, frozen = freeze_source(root)
    return frozen, {**prior, "metadata_policy": POLICY, "canonical_url": url,
        "manifest": manifest, "manifest_digest": manifest_digest(manifest),
        "archive_sha256": hashlib.sha256(frozen).hexdigest(), "size_bytes": len(frozen)}
