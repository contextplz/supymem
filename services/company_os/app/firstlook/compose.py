"""Plan a unique landing page, then code self-contained HTML.

Jinja templates stay for unit tests of the old renderer. Live generation uses
this composer so two ideas never share a layout skeleton.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from datetime import date
from typing import Any

from app.firstlook.brandkit import BrandKit, svg_mark_data_uri
from app.firstlook.lint import lint_copy
from app.firstlook.render import LOCAL_NOTE, PRIVACY_LINE, STAGE_LABELS

LAYOUTS = ("split", "stack", "rail", "mosaic", "ribbon", "diary", "booth", "night")
MOTIONS = ("rise", "orbit", "marquee", "drift", "bloom", "stagger", "wave", "ticker")

_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["']https?://|@import\s+|url\(\s*["']?https?://""",
    re.I,
)


def plan_page(
    idea_text: str,
    *,
    variant_index: int = 0,
    archetype: str | None = None,
) -> dict[str, Any]:
    """Deterministic layout spec from the idea — not a template name."""
    idea = re.sub(r"\s+", " ", (idea_text or "").strip())
    material = f"{idea}\0{variant_index}\0{archetype or ''}".encode()
    digest = hashlib.sha256(material).digest()
    layout = LAYOUTS[digest[0] % len(LAYOUTS)]
    motion = MOTIONS[digest[1] % len(MOTIONS)]
    words = [w for w in re.findall(r"[A-Za-z0-9]+", idea) if len(w) > 2][:8]
    hue_shift = digest[2]
    return {
        "layout": layout,
        "motion": motion,
        "words": words,
        "hue_shift": hue_shift,
        "variant_index": variant_index,
        "angle": 8 + (digest[3] % 28),
        "blob_x": 8 + (digest[4] % 40),
        "blob_y": 4 + (digest[5] % 30),
        "density": 3 + (digest[6] % 5),
        "idea": idea,
    }


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _brand_vars(brand: dict[str, Any]) -> str:
    colors = brand.get("colors") or {}
    type_pair = brand.get("type_pair") or {}
    return (
        f"--page:{_esc(colors.get('page') or '#f6f1e8')};"
        f"--surface:{_esc(colors.get('surface') or '#fff')};"
        f"--border:{_esc(colors.get('border') or '#d9d0c4')};"
        f"--ink:{_esc(colors.get('ink') or '#1c1916')};"
        f"--muted:{_esc(colors.get('muted') or '#6b635a')};"
        f"--accent:{_esc(colors.get('accent') or '#c45c26')};"
        f"--accent-ink:{_esc(colors.get('accent_ink') or '#fff')};"
        f"--display:{_esc(type_pair.get('display') or 'Georgia,serif')};"
        f"--body:{_esc(type_pair.get('body') or 'system-ui,sans-serif')};"
    )


def _points_html(copy: dict[str, Any]) -> str:
    cards = []
    for i, point in enumerate(copy.get("points") or [], start=1):
        if not isinstance(point, dict):
            continue
        cards.append(
            f'<article class="card c{i}">'
            f"<h3>{_esc(point.get('title'))}</h3>"
            f"<p>{_esc(point.get('body'))}</p>"
            "</article>"
        )
    return "".join(cards)


def _steps_html(copy: dict[str, Any]) -> str:
    items = []
    for step in copy.get("how_it_works") or []:
        if isinstance(step, dict):
            text = step.get("text") or ""
            n = step.get("step") or len(items) + 1
        else:
            text, n = str(step), len(items) + 1
        if text:
            items.append(f"<li><span>{_esc(n)}</span>{_esc(text)}</li>")
    if not items:
        return ""
    return f'<ol class="steps">{"".join(items)}</ol>'


def _interest_form(copy: dict[str, Any], *, action: str, version: int) -> str:
    label = _esc((copy.get("cta") or {}).get("label") or "Join the waitlist")
    return f"""
<section id="interest" class="capture reveal">
  <h2>Stay close</h2>
  <p>Leave an email. This stays on this machine.</p>
  <form id="interest-form" method="post" action="{_esc(action)}">
    <input type="hidden" name="site_version" value="{int(version)}">
    <input type="hidden" name="source" value="direct">
    <label for="email">Your email</label>
    <input id="email" name="email" type="email" required autocomplete="email" placeholder="you@example.com">
    <label for="note">Anything you want to add (optional)</label>
    <textarea id="note" name="note"></textarea>
    <p class="privacy">{_esc(PRIVACY_LINE)}</p>
    <button type="submit">{label}</button>
  </form>
  <p id="interest-thanks" hidden>Thanks, that's recorded.</p>
</section>
"""


def _public_waitlist(copy: dict[str, Any], *, url: str) -> str:
    """Published variant: the one call to action links to the hosted consent form; no form, no local-only wording."""
    label = _esc((copy.get("cta") or {}).get("label") or "Join the waitlist")
    return f"""
<section id="interest" class="capture reveal">
  <h2>Stay close</h2>
  <p>Join the waitlist to hear first. You will see exactly what you are agreeing to before you submit anything.</p>
  <a class="cta" href="{_esc(url)}" rel="noopener">{label}</a>
</section>
"""


def _footer(copy: dict[str, Any], *, idea: str, stage: str, version: int, generated_on: str, public: bool = False) -> str:
    assumptions = copy.get("assumptions") or []
    assumed = ""
    if assumptions:
        assumed = "<p>I assumed: " + _esc("; ".join(str(a) for a in assumptions)) + ".</p>"
    return f"""
<footer class="reveal">
  <p>{_esc(STAGE_LABELS.get(stage, STAGE_LABELS["first_look"]))}</p>
  {assumed}
  <p class="verbatim">Your words: “{_esc(idea)}”</p>
  {"" if public else f"<p>{_esc(LOCAL_NOTE)}</p>"}
  <p>Version {int(version)} · built {_esc(generated_on)}</p>
</footer>
"""


def _motion_css(plan: dict[str, Any]) -> str:
    motion = plan["motion"]
    angle = int(plan["angle"])
    bx, by = int(plan["blob_x"]), int(plan["blob_y"])
    bits = [
        f"""
@keyframes rise {{ from {{ opacity:0; transform:translateY(18px); }} to {{ opacity:1; transform:none; }} }}
@keyframes drift {{
  0% {{ transform: translate(0,0) rotate(0deg); }}
  50% {{ transform: translate({bx - 16}px,{by - 10}px) rotate({angle}deg); }}
  100% {{ transform: translate(0,0) rotate(0deg); }}
}}
@keyframes orbit {{
  from {{ transform: rotate(0deg); }}
  to {{ transform: rotate(360deg); }}
}}
@keyframes bloom {{
  0%,100% {{ box-shadow: 0 12px 40px color-mix(in srgb, var(--accent) 22%, transparent); }}
  50% {{ box-shadow: 0 22px 56px color-mix(in srgb, var(--accent) 42%, transparent); }}
}}
@keyframes marquee {{
  from {{ transform: translateX(0); }}
  to {{ transform: translateX(-50%); }}
}}
@keyframes wave {{
  0%,100% {{ clip-path: polygon(0 18%, 100% 0, 100% 100%, 0 100%); }}
  50% {{ clip-path: polygon(0 0, 100% 18%, 100% 100%, 0 100%); }}
}}
@keyframes underline {{
  from {{ transform: scaleX(0); }}
  to {{ transform: scaleX(1); }}
}}
.reveal {{ opacity:0; transform:translateY(14px); transition: opacity 520ms ease, transform 520ms ease; }}
.reveal.is-in {{ opacity:1; transform:none; }}
.anim {{ animation: rise 640ms ease both; }}
"""
    ]
    if motion == "orbit":
        bits.append(".art { animation: orbit 28s linear infinite; }")
    elif motion == "drift":
        bits.append(".art, .blob { animation: drift 9s ease-in-out infinite; }")
    elif motion == "bloom":
        bits.append(".cta { animation: bloom 3.2s ease-in-out infinite; }")
    elif motion in {"marquee", "ticker"}:
        bits.append(".ticker-track { animation: marquee 22s linear infinite; }")
    elif motion == "wave":
        bits.append(".hero-panel { animation: wave 8s ease-in-out infinite; }")
    elif motion == "stagger":
        bits.append(
            ".card.c1{animation:rise .7s .05s both}.card.c2{animation:rise .7s .18s both}"
            ".card.c3{animation:rise .7s .32s both}"
        )
    else:
        bits.append(".hero h1 .bar { animation: underline 900ms 200ms ease both; transform-origin: left; }")
    bits.append(
        """
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
  .reveal { opacity: 1; transform: none; }
}
"""
    )
    return "\n".join(bits)


def _layout_css(plan: dict[str, Any]) -> str:
    layout = plan["layout"]
    shared = """
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background:
    radial-gradient(900px 520px at var(--blob-x) var(--blob-y), color-mix(in srgb, var(--accent) 20%, transparent), transparent 62%),
    var(--page);
  color: var(--ink);
  font-family: var(--body);
  line-height: 1.5;
}
a { color: inherit; }
.cta {
  display: inline-block; background: var(--accent); color: var(--accent-ink);
  text-decoration: none; padding: .85rem 1.4rem; border-radius: .7rem; font-weight: 650;
}
.card { background: color-mix(in srgb, var(--surface) 92%, transparent); border: 1px solid var(--border); border-radius: 1rem; padding: 1.1rem; }
.card h3 { margin: 0 0 .4rem; font-family: var(--display); font-size: 1.05rem; }
.card p { margin: 0; color: var(--muted); }
form { display: grid; gap: .55rem; max-width: 26rem; }
input, textarea, button { font: inherit; padding: .7rem .85rem; border-radius: .55rem; border: 1px solid var(--border); background: var(--surface); color: var(--ink); }
button { background: var(--accent); color: var(--accent-ink); border: 0; font-weight: 650; cursor: pointer; }
.privacy, footer, .muted { color: var(--muted); font-size: .9rem; }
.steps { list-style: none; padding: 0; margin: 0; display: grid; gap: .7rem; }
.steps span { display: inline-flex; width: 1.5rem; height: 1.5rem; border-radius: 99px; background: var(--accent); color: var(--accent-ink); align-items: center; justify-content: center; margin-right: .5rem; font-size: .8rem; }
.mark { display: inline-flex; align-items: center; gap: .6rem; }
.logo { width: 2.2rem; height: 2.2rem; border-radius: .65rem; object-fit: cover; display: block; }
.mono { width: 2.2rem; height: 2.2rem; border-radius: .65rem; background: var(--accent); color: var(--accent-ink); display: inline-flex; align-items: center; justify-content: center; font-family: var(--display); }
.wordmark { font-family: var(--display); font-weight: 650; }
.ticker { overflow: hidden; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.ticker-track { display: flex; gap: 2.5rem; white-space: nowrap; width: max-content; padding: .7rem 0; color: var(--muted); letter-spacing: .08em; text-transform: uppercase; font-size: .78rem; }
.art { width: min(100%, 28rem); aspect-ratio: 1; position: relative; }
.art .mono { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 1; }
.art .dot { position: absolute; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
.art .d0 { top: 18%; left: 22%; }
.art .d1 { top: 28%; right: 18%; }
.art .d2 { bottom: 22%; left: 30%; }
.art .d3 { bottom: 16%; right: 24%; }
.art .d4 { top: 48%; left: 12%; }
.blob { position: absolute; inset: 12%; border-radius: 42% 58% 37% 63%; background: color-mix(in srgb, var(--accent) 55%, var(--surface)); }
.ring { position: absolute; inset: 6%; border: 2px dashed color-mix(in srgb, var(--accent) 55%, transparent); border-radius: 50%; }
h1 { font-family: var(--display); font-weight: 650; line-height: 1.08; margin: 0 0 1rem; font-size: clamp(2.1rem, 6vw, 4.1rem); }
h1 .bar { display: block; height: 4px; width: 4.5rem; background: var(--accent); margin-top: .7rem; }
.eyebrow { letter-spacing: .08em; text-transform: uppercase; font-size: .78rem; margin: 0 0 .75rem; }
.sub { font-size: 1.15rem; color: var(--muted); max-width: 36rem; }
"""
    layouts = {
        "split": """
.shell { display: grid; grid-template-columns: minmax(0,1.1fr) minmax(0,.9fr); min-height: 100vh; }
.left { padding: 2.2rem clamp(1.2rem,4vw,3.5rem) 3rem; }
.right { background: color-mix(in srgb, var(--accent) 14%, var(--page)); display: grid; place-items: center; padding: 2rem; }
.points { display: grid; gap: 1rem; margin: 2.5rem 0; }
@media (max-width: 860px) { .shell { grid-template-columns: 1fr; } .right { min-height: 16rem; } }
""",
        "stack": """
.shell { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.4rem 4rem; text-align: center; }
.hero { padding: 3rem 0 2rem; }
.points { display: grid; gap: 1rem; text-align: left; margin: 2rem 0; }
.art { margin: 0 auto 1.5rem; }
""",
        "rail": """
.shell { display: grid; grid-template-columns: 11rem minmax(0,1fr); min-height: 100vh; }
.rail { padding: 2rem 1rem; border-right: 1px solid var(--border); position: sticky; top: 0; height: 100vh; }
.main { padding: 2.4rem clamp(1.2rem,4vw,3rem) 4rem; }
.points { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 1rem; }
@media (max-width: 800px) { .shell { grid-template-columns: 1fr; } .rail { height: auto; position: static; } .points { grid-template-columns: 1fr; } }
""",
        "mosaic": """
.shell { max-width: 72rem; margin: 0 auto; padding: 1.8rem 1.3rem 4rem; }
.hero { display: grid; grid-template-columns: 1.3fr .7fr; gap: 1.5rem; align-items: end; }
.points { display: grid; grid-template-columns: 1.2fr .8fr .9fr; gap: 1rem; margin-top: 1.5rem; }
.card.c1 { grid-row: span 1; min-height: 12rem; }
@media (max-width: 800px) { .hero, .points { grid-template-columns: 1fr; } }
""",
        "ribbon": """
.ribbon { background: var(--accent); color: var(--accent-ink); padding: 2.8rem 8vw 7rem; }
.ribbon h1, .ribbon .sub { color: var(--accent-ink); }
.shell { max-width: 58rem; margin: -4.2rem auto 0; padding: 0 1.3rem 4rem; }
.points { display: grid; gap: 1rem; }
""",
        "diary": """
.shell { max-width: 38rem; margin: 0 auto; padding: 3rem 1.4rem 5rem; }
.hero h1 { font-size: clamp(2.4rem, 7vw, 3.6rem); }
.drop { float: left; font-size: 4.4rem; line-height: .8; padding: 0 .4rem 0 0; font-family: var(--display); color: var(--accent); }
.points { display: grid; gap: 1.4rem; margin: 2rem 0; }
""",
        "booth": """
.shell { max-width: 70rem; margin: 0 auto; padding: 1.6rem 1.2rem 4rem; }
.hero { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; align-items: center; }
.lane { display: grid; gap: .8rem; }
.points { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-top: 2rem; }
@media (max-width: 800px) { .hero, .points { grid-template-columns: 1fr; } }
""",
        "night": """
body { background: #0c0c0e; color: #f3efe6; }
.shell { padding: 0 0 4rem; }
.hero { padding: 3.2rem 8vw 2rem; }
.points { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 1px; background: var(--border); margin: 0; }
.card { border-radius: 0; background: #141416; }
@media (max-width: 800px) { .points { grid-template-columns: 1fr; } }
""",
    }
    return shared + layouts.get(layout, layouts["stack"])


def _ticker(plan: dict[str, Any]) -> str:
    words = plan.get("words") or ["first", "look"]
    line = " · ".join(words * 4)
    return f'<div class="ticker" aria-hidden="true"><div class="ticker-track"><span>{_esc(line)}</span><span>{_esc(line)}</span></div></div>'


def _art(plan: dict[str, Any], monogram: str) -> str:
    n = int(plan["density"])
    chips = "".join(f'<i class="dot d{i}"></i>' for i in range(n))
    return f'<div class="art" aria-hidden="true"><div class="ring"></div><div class="blob"></div><span class="mono">{_esc(monogram)}</span>{chips}</div>'


def _mark(brand: dict[str, Any], logo_src: str | None) -> str:
    src = (logo_src or "").replace('"', "")
    if src and not src.lower().startswith("http"):
        return (
            f'<span class="mark"><img class="logo" src="{src}" alt="{_esc(brand.get("wordmark"))} logo" '
            f'width="36" height="36"/><span class="wordmark">{_esc(brand.get("wordmark"))}</span></span>'
        )
    return (
        f'<span class="mark"><span class="mono" aria-hidden="true">{_esc(brand.get("monogram"))}</span>'
        f'<span class="wordmark">{_esc(brand.get("wordmark"))}</span></span>'
    )


def _body(plan: dict[str, Any], brand: dict[str, Any], copy: dict[str, Any], *, logo_src: str | None, action: str, version: int, stage: str, generated_on: str, public_waitlist_url: str | None = None) -> str:
    layout = plan["layout"]
    headline = _esc(copy.get("headline"))
    sub = _esc(copy.get("subhead"))
    cta = _esc((copy.get("cta") or {}).get("label") or "Join the waitlist")
    points = _points_html(copy)
    steps = _steps_html(copy)
    form = (_public_waitlist(copy, url=public_waitlist_url) if public_waitlist_url
            else _interest_form(copy, action=action, version=version))
    foot = _footer(copy, idea=plan["idea"], stage=stage, version=version, generated_on=generated_on,
                   public=bool(public_waitlist_url))
    mark = _mark(brand, logo_src)
    art = _art(plan, str(brand.get("monogram") or "N"))
    ticker = _ticker(plan)
    hero = f'<p class="eyebrow muted anim">First look · {_esc(layout)}</p><h1 class="anim">{headline}<span class="bar"></span></h1><p class="sub anim">{sub}</p><a class="cta" href="#interest">{cta}</a>'
    if layout == "split":
        return f'<div class="shell"><div class="left">{mark}<section class="hero">{hero}</section><div class="points">{points}</div>{steps}{form}{foot}</div><div class="right hero-panel">{art}</div></div>'
    if layout == "rail":
        return f'<div class="shell"><aside class="rail">{mark}{art}</aside><div class="main"><section class="hero">{hero}</section>{ticker}<div class="points">{points}</div>{steps}{form}{foot}</div></div>'
    if layout == "ribbon":
        return f'<div class="ribbon hero-panel">{mark}<section class="hero">{hero}</section></div><div class="shell"><div class="points">{points}</div>{steps}{form}{foot}</div>'
    if layout == "booth":
        return f'<div class="shell">{mark}<section class="hero"><div>{hero}</div><div class="lane">{art}{steps}</div></section><div class="points">{points}</div>{form}{foot}</div>'
    if layout == "night":
        return f'{ticker}<div class="shell"><section class="hero">{mark}{hero}</section><div class="points">{points}</div>{steps}{form}{foot}</div>'
    if layout == "mosaic":
        return f'<div class="shell">{mark}<section class="hero"><div>{hero}</div>{art}</section><div class="points">{points}</div>{steps}{form}{foot}</div>'
    if layout == "diary":
        first = (copy.get("subhead") or "A")[:1]
        rest = (copy.get("subhead") or "")[1:]
        return f'<div class="shell">{mark}<section class="hero"><h1>{headline}<span class="bar"></span></h1><p class="sub"><span class="drop">{_esc(first)}</span>{_esc(rest)}</p><a class="cta" href="#interest">{cta}</a></section><div class="points">{points}</div>{steps}{form}{foot}</div>'
    return f'<div class="shell">{mark}{art}<section class="hero">{hero}</section><div class="points">{points}</div>{steps}{form}{foot}</div>'


def _script() -> str:
    return """
<script>
(function () {
  var form = document.getElementById('interest-form');
  var thanks = document.getElementById('interest-thanks');
  if (form && thanks) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var button = form.querySelector('button');
      button.disabled = true;
      fetch(form.action, { method: 'POST', mode: 'cors', credentials: 'omit', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: form.email.value, note: (form.note && form.note.value) || '', source: 'direct', site_version: parseInt((form.site_version && form.site_version.value) || '1', 10) || 1 }) })
        .then(function (response) {
          if (!response.ok) { throw new Error('rejected'); }
          form.hidden = true;
          thanks.hidden = false;
        })
        .catch(function () {
          button.disabled = false;
          form.submit();
        });
    });
  }
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('is-in'); });
    return;
  }
  var nodes = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    nodes.forEach(function (el) { el.classList.add('is-in'); });
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-in');
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });
  nodes.forEach(function (el) { io.observe(el); });
})();
</script>
"""


def lint_html(html_text: str) -> list[str]:
    failures: list[str] = []
    if "<!DOCTYPE html>" not in html_text:
        failures.append("missing_doctype")
    if _EXTERNAL.search(html_text):
        failures.append("external_asset")
    if 'rel="stylesheet"' in html_text.lower():
        failures.append("stylesheet_link")
    if "@font-face" in html_text.lower():
        failures.append("webfont")
    if "prefers-reduced-motion" not in html_text:
        failures.append("missing_reduced_motion")
    if "I assumed" not in html_text and "I assumed:" not in html_text:
        failures.append("missing_assumptions")
    return failures


def compose_html(
    *,
    brand: BrandKit | dict[str, Any],
    copy: dict[str, Any],
    idea_text: str,
    version: int,
    stage: str = "first_look",
    interest_action: str = "",
    variant_index: int = 0,
    archetype: str | None = None,
    logo_url: str | None = None,
    generated_on: str | None = None,
    public_waitlist_url: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return (html, template_name, plan)."""
    brand_dict = brand.to_dict() if isinstance(brand, BrandKit) else dict(brand)
    plan = plan_page(idea_text, variant_index=variant_index, archetype=archetype)
    logo_src = logo_url or svg_mark_data_uri(brand_dict, size=96)
    if logo_src.lower().startswith("http"):
        logo_src = svg_mark_data_uri(brand_dict, size=96)
    generated = generated_on or date.today().isoformat()
    action = interest_action or "#"
    css = (
        f":root {{ {_brand_vars(brand_dict)} --blob-x:{int(plan['blob_x'])}%; --blob-y:{int(plan['blob_y'])}%; }}\n"
        + _layout_css(plan)
        + _motion_css(plan)
    )
    body = _body(
        plan,
        brand_dict,
        copy,
        logo_src=logo_src,
        action=action,
        version=version,
        stage=stage,
        generated_on=generated,
        public_waitlist_url=public_waitlist_url,
    )
    page = (
        "<!DOCTYPE html>\n"
        f'<html lang="en" data-layout="{_esc(plan["layout"])}" data-motion="{_esc(plan["motion"])}">\n'
        "<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        '<meta name="generator" content="Company OS first look">\n'
        f"<title>{_esc(copy.get('headline'))}{_esc(brand_dict.get('wordmark'))}</title>\n"
        f"<style>\n{css}\n</style>\n</head>\n<body>\n{body}\n{_script()}\n</body>\n</html>\n"
    )
    name = f"composed:{plan['layout']}"
    return page, name, plan


async def try_code_html(
    *,
    brand: BrandKit | dict[str, Any],
    copy: dict[str, Any],
    idea_text: str,
    version: int,
    stage: str,
    interest_action: str,
    variant_index: int,
    archetype: str | None,
    logo_url: str | None,
    workdir: str | None = None,
) -> tuple[str, str, dict[str, Any]] | dict[str, Any] | None:
    """Ask an explicitly selected coding engine to write HTML, if available."""
    from app.core.local_mode import is_local

    if is_local():
        from app.firstlook.service import FirstLookError, prepare_local_page
        from app.tenancy import current_or_none

        scope = current_or_none()
        if scope is None:
            raise FirstLookError("page_venture_required", "Open Website for your venture, then ask me to build your page with your connected coding tool.")
        return prepare_local_page(scope.workspace_id, scope.venture_id, {
            "idea": idea_text, "copy": copy, "brand": brand.to_dict() if isinstance(brand, BrandKit) else brand,
            "layout": plan_page(idea_text, variant_index=variant_index, archetype=archetype),
            "stage": stage, "version": version, "interest_action": interest_action, "logo_url": logo_url,
        })
    if os.environ.get("LLM_OFFLINE", "true").lower() in {"1", "true", "yes"}:
        return None
    mode = (os.environ.get("FIRSTLOOK_ENGINE") or "compose").strip().lower()
    if mode in {"compose", "offline", "fallback"}:
        return None
    try:
        from coding_agents.detect import detect
        from coding_agents.protocol import CodingRequest
        from coding_agents import load_adapter
    except Exception:
        return None
    if not any(agent.name == "opencode" for agent in detect()):
        return None
    from pathlib import Path

    plan = plan_page(idea_text, variant_index=variant_index, archetype=archetype)
    fallback, _, _ = compose_html(
        brand=brand,
        copy=copy,
        idea_text=idea_text,
        version=version,
        stage=stage,
        interest_action=interest_action,
        variant_index=variant_index,
        archetype=archetype,
        logo_url=logo_url,
    )
    root = Path(workdir) if workdir else Path(os.environ.get("COMPANY_OS_DATA_DIR") or ".") / "tmp-firstlook"
    root.mkdir(parents=True, exist_ok=True)
    (root / "PLAN.txt").write_text(
        f"Layout {plan['layout']} with {plan['motion']} motion.\nIdea: {idea_text}\n",
        encoding="utf-8",
    )
    prompt = (
        "Write a single self-contained index.html for this first-look landing page. "
        "No external CSS, fonts, images, or CDNs. CSS-only motion with prefers-reduced-motion. "
        "Do not invent testimonials, metrics, or social proof. This is a concept page: describe what is being built "
        "('We're building...'), never present-tense claims that the product already does something, and keep one CTA (waitlist). Include the waitlist form posting to "
        f"{interest_action or '#'} with fields email, note, site_version={version}, source=direct. "
        f"Put the founder's words in the footer. Layout must be '{plan['layout']}' and feel unique. "
        f"Copy JSON: {copy!r}"
    )
    try:
        adapter = load_adapter("opencode")
        result = await adapter.run(
            CodingRequest(prompt=prompt, workdir=root, allow_write=True, timeout_s=40)
        )
    except Exception:
        return None
    if not result.ok:
        return None
    target = root / "index.html"
    if not target.is_file():
        for path in root.rglob("*.html"):
            target = path
            break
        else:
            return None
    html_text = target.read_text(encoding="utf-8")
    if lint_copy(copy, idea_text=idea_text).ok is False:
        return None
    if lint_html(html_text):
        return None
    _ = fallback
    return html_text, f"coded:opencode:{plan['layout']}", plan


__all__ = [
    "LAYOUTS",
    "compose_html",
    "lint_html",
    "plan_page",
    "try_code_html",
]
