"""Long-horizon tasks (30+ distinct UI steps). Run once the short tasks pass broadly.

Each composes several independent oracles with all_of: any failed sub-check fails the task,
any unknown makes it unknown. Sub-details are joined into the result's detail string.
"""
from __future__ import annotations

from . import add
from . import apps
from . import oracles as o
from .short_tasks import S, seed, seq, img

NUMBERS = apps.need_app("/Applications/Numbers.app")


def srv(fn):
    """Lazy oracle: build the check from the live server's deterministic data."""
    return lambda ctx: fn(ctx.server)(ctx)


def seed_inbox(c):
    for n, t in {"a.txt": "a {uid}\n", "b.txt": "b {uid}\n", "c.txt": "c {uid}\n", "d.csv": "d,{uid}\n",
                 "e.csv": "e,{uid}\n", "f.jpg": "f {uid}\n", "g.jpg": "g {uid}\n"}.items():
        c.seed(f"Inbox/{n}", t.replace("{uid}", c.uid))


def seed_photos(c):
    for n in range(1, 5):
        img(f"Photos/p{n}.jpg")(c)
        c.seed(f"Photos/p{n}.jpg", c.path(f"Photos/p{n}.jpg").read_bytes())


# 1 research -> document -> organize -> archive -> clipboard
add("lo_research_pipeline", "long", 36,
    "Read the three articles linked from {base}/library. In TextEdit write a plain-text digest: first line 'Library digest {uid}', then one line per article in order, "
    "formatted '- <article title>: <key fact>'. Save it as {dir}/research.txt, then in Finder create the folder {dir}/Digest, move research.txt into it, "
    "compress Digest into Digest.zip next to it, and copy the zip's full path to my clipboard.",
    "digest lines exact; moved; zip contents exact; clipboard == zip path; server saw all articles", "medium",
    srv(lambda s: o.all_of(
        (lambda lines: o.all_of(o.file_lines("Digest/research.txt", lines), o.absent("research.txt"),
                                o.zip_has("Digest.zip", {"research.txt": "\n".join(lines) + "\n"}),
                                o.clip_is("{dir}/Digest.zip"), o.server_hit("/library/1", "/library/2", "/library/3")))(
            ["Library digest {uid}"] + [f"- {a['title']}: {a['fact']}" for a in s.articles]))),
    long=True, needs=S)

# 2 data entry across apps
add("lo_directory_to_apps", "long", 40,
    "From {base}/directory take the five people. (1) In Numbers make a table with header 'name,email,phone' and the five rows in page order and export CSV to {dir}/contacts.csv. "
    "(2) In Notes create a note titled 'SupymemCU {uid} People' whose body lines are the five names in order. (3) In Reminders, in the list 'SupymemCU-{uid}', add one reminder 'Email <name>' per person.",
    "CSV exact; owned note exact; owned reminders list exact", "medium",
    srv(lambda s: o.all_of(
        o.csv_is("contacts.csv", [["name", "email", "phone"]] + [[c["name"], c["email"], c["phone"]] for c in s.contacts]),
        apps.notes_check({" People": [c["name"] for c in s.contacts]}),
        apps.reminders_check({f"Email {c['name']}": False for c in s.contacts}))),
    setup=seq(NUMBERS, apps.notes_cleanup, apps.reminders_setup), long=True, needs=S + ("calendar", "reminders"))

# 3 pagination crawl -> sorted file -> form submit
add("lo_items_audit", "long", 34,
    "Walk every page of {base}/items (4 pages). In TextEdit save {dir}/items.csv as plain text: header 'sku,price' then all 20 items sorted by price high to low (digits only, no $). "
    "Then submit the form at {base}/form with project 'Total <sum of all 20 prices>', email 'audit-{uid}@example.invalid' and code 'AUDIT1'.",
    "CSV exact + sorted; server receipt: valid POST with the correct total", "low",
    srv(lambda s: o.all_of(
        o.csv_is("items.csv", [["sku", "price"]] + [[i["sku"], str(i["price"])] for i in sorted(s.items, key=lambda i: (-i["price"], i["sku"]))]),
        o.server_valid_form({"project": f"Total {sum(i['price'] for i in s.items)}", "code": "AUDIT1"}))),
    long=True, needs=S)

# 4 bulk file organization
add("lo_file_organize", "long", 38,
    "In Finder tidy {dir}/Inbox into {dir}/Sorted: files ending .txt go to Sorted/Docs, .csv to Sorted/Data, .jpg to Sorted/Images. Rename every moved file to start with 'done-'. Leave Inbox empty.",
    "exact tree (names + bytes) under Sorted; Inbox has no files", "medium",
    o.all_of(o.tree_is("Sorted", {"Docs/done-a.txt": "a {uid}\n", "Docs/done-b.txt": "b {uid}\n", "Docs/done-c.txt": "c {uid}\n",
                                  "Data/done-d.csv": "d,{uid}\n", "Data/done-e.csv": "e,{uid}\n",
                                  "Images/done-f.jpg": "f {uid}\n", "Images/done-g.jpg": "g {uid}\n"}),
             o.tree_is("Inbox", {})),
    setup=lambda c: seed_inbox(c), long=True)

# 5 chained forms (each needs the previous receipt code)
add("lo_chain_forms", "long", 32,
    "Fill the three forms in order: {base}/chain/1, then {base}/chain/2, then {base}/chain/3. Each form has a note field (write 'step <n> {uid}'); forms 2 and 3 need the code shown after the previous step in their 'prev' field. "
    "Finally save the three codes, one per line in order, to {dir}/codes.txt via TextEdit.",
    "server: one accepted POST per step with correct prev codes; codes.txt exact", "low",
    srv(lambda s: o.all_of(
        o.server_posts("/chain/1", {"note": "step 1 {uid}"}), o.server_posts("/chain/2", {"note": "step 2 {uid}", "prev": s.chain_codes["1"]}),
        o.server_posts("/chain/3", {"note": "step 3 {uid}", "prev": s.chain_codes["2"]}),
        o.file_lines("codes.txt", [s.chain_codes[n] for n in "123"]))),
    long=True, needs=S)

# 6 compare -> spreadsheet -> memo -> draft
add("lo_plan_decision", "long", 33,
    "Compare the plans at {base}/compare/a and {base}/compare/b. In Numbers export a CSV {dir}/plans.csv with header 'plan,price' and one row per plan (a first). In TextEdit save {dir}/memo.txt as the single line 'Recommendation: <cheaper plan name>'. "
    "Then in Mail save (do not send) a draft to qa-{uid}@example.invalid with subject 'Plan choice {uid}' and the same recommendation as body.",
    "CSV exact; memo exact; Drafts read-back (Sent count 0)", "high",
    srv(lambda s: (lambda cheaper: o.all_of(
        o.csv_is("plans.csv", [["plan", "price"], [s.plans["a"][0], str(s.plans["a"][1])], [s.plans["b"][0], str(s.plans["b"][1])]]),
        o.file_lines("memo.txt", [f"Recommendation: {cheaper}"]),
        apps.draft_check("Plan choice {uid}", ["qa-{uid}@example.invalid"], [f"Recommendation: {cheaper}"])))(min(s.plans.values(), key=lambda p: p[1])[0])),
    setup=NUMBERS, long=True, needs=S + ("mail",))

# 7 meeting prep across Calendar + Reminders + Notes
add("lo_meeting_prep", "long", 45,
    "Prepare tomorrow's meeting from {base}/agenda (five items with times). In Calendar, in 'SupymemCU-{uid}', create an event for each agenda item on March 11, 2031 at its listed time lasting one hour, titled exactly as listed. "
    "In Reminders, in 'SupymemCU-{uid}', add 'Prep <title>' for each. In Notes create 'SupymemCU {uid} Agenda' with the five titles as body lines in order.",
    "calendar events exact; reminders exact; note exact", "medium",
    srv(lambda s: o.all_of(
        apps.events_check([(a["title"], f"2031-03-11 {int(a['time'].split(':')[0]):02d}:00", f"2031-03-11 {int(a['time'].split(':')[0]) + 1:02d}:00", False) for a in s.agenda]),
        apps.reminders_check({f"Prep {a['title']}": False for a in s.agenda}),
        apps.notes_check({" Agenda": [a["title"] for a in s.agenda]}))),
    setup=seq(apps.calendar_setup, apps.reminders_setup, apps.notes_cleanup), long=True, needs=S + ("calendar", "reminders"))

# 8 image batch in Preview
add("lo_image_batch", "long", 40,
    "In Preview rotate each of {dir}/Photos/p1.jpg ... p4.jpg right (clockwise) once and export each as PNG into {dir}/Out with the same base name (p1.png ...). "
    "Then in TextEdit save {dir}/sheet.txt with one line per image 'pN.png 160x240'. Don't modify the originals.",
    "each PNG pixel-exact clockwise rotation of its source; sheet exact; originals intact", "low",
    o.all_of(*[o.image_check(f"Out/p{n}.png", "PNG", (160, 240), src=f"Photos/p{n}.jpg", rotate=90) for n in range(1, 5)],
             o.file_lines("sheet.txt", [f"p{n}.png 160x240" for n in range(1, 5)]), *[o.unchanged(f"Photos/p{n}.jpg") for n in range(1, 5)]),
    setup=lambda c: seed_photos(c), long=True)

# 9 inventory reconciliation
add("lo_inventory_reconcile", "long", 35,
    "Compare {dir}/expected.csv (sku,qty) with the live stock table at {base}/stock. In TextEdit save {dir}/diff.txt with one line per SKU whose quantity differs, formatted '<sku>: expected <n>, site <m>', in file order. "
    "Then save (don't send) a Mail draft to qa-{uid}@example.invalid with subject 'Stock diff {uid}' whose body says how many SKUs differ as 'Mismatches: <count>'.",
    "diff lines computed from server truth; draft read-back; Sent count 0", "high",
    srv(lambda s: (lambda diffs: o.all_of(
        o.file_lines("diff.txt", [f"{k}: expected {e}, site {s.stock[k]}" for k, e in diffs]),
        apps.draft_check("Stock diff {uid}", ["qa-{uid}@example.invalid"], [f"Mismatches: {len(diffs)}"])))(
        [(k, _expected(s)[k]) for k in s.stock if _expected(s)[k] != s.stock[k]])),
    setup=lambda c: c.seed("expected.csv", "sku,qty\n" + "".join(f"{k},{_expected(c.server)[k]}\n" for k in c.server.stock)),
    long=True, needs=S + ("mail",))


def _expected(s):
    """Seeded expectation: site stock with every other SKU deliberately off by 3 (deterministic)."""
    return {k: v + (3 if i % 2 else 0) for i, (k, v) in enumerate(s.stock.items())}


# 10 report assembly: clipboard across pages -> rich doc -> pdf
add("lo_report_assembly", "long", 42,
    "Build a report from {base}/library: for each of the three articles open it, copy its key fact sentence ('fact-...') with the clipboard and paste it as its own paragraph into one rich-text TextEdit document, in order. "
    "Put the title 'Report {uid}' on the first line in bold. Save as {dir}/report.rtf, then File > Export as PDF to {dir}/report.pdf.",
    "bold title (rendered html); facts in order via textutil text; valid PDF; server saw the pages", "medium",
    srv(lambda s: o.all_of(
        o.rtf_bold("report.rtf", "Report {uid}"), o.rtf_text_order("report.rtf", ["Report {uid}"] + [a["fact"] for a in s.articles]),
        o.pdf_valid("report.pdf"), o.server_hit("/library/1", "/library/2", "/library/3"))),
    long=True, needs=S)
