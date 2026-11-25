"""Catalog integrity + oracle negative tests. No desktop, no provider calls; localhost fixture only."""
import hashlib
import io
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/desktop'))
import cu_tasks
from cu_tasks import Ctx, apps, oracles as o
from cu_tasks.fixture_server import FixtureServer


@pytest.fixture
def ctx(tmp_path):
    return Ctx(tmp_path / 't', uid='abcd1234', clip=lambda: None)


def ok(check, c):
    return check(c)[0]


# ---- catalog ------------------------------------------------------------------------------
def test_catalog_shape():
    tasks = cu_tasks.load().values()
    short = [t for t in tasks if not t.long]
    long = [t for t in tasks if t.long]
    assert len(short) >= 60 and len(long) >= 10
    assert all(1 <= t.steps <= 8 for t in short) and all(t.steps >= 30 for t in long)
    assert all(t.oracle and t.risk in {'low', 'medium', 'high'} and callable(t.verify) for t in tasks)
    assert len({t.id for t in tasks}) == len(tasks)
    cats = {t.category for t in tasks}
    assert {'browser', 'textedit', 'notes', 'finder', 'calendar', 'reminders', 'preview', 'numbers', 'mail', 'system', 'clipboard',
            'multiwindow', 'menubar', 'keyboard', 'dialogs', 'errors', 'ambiguity', 'interruption', 'safety', 'long'} <= cats


def test_select_and_unknown():
    assert {t.category for t in cu_tasks.select('safety')} == {'safety'}
    assert [t.id for t in cu_tasks.select(only='te_new_save')] == ['te_new_save']
    assert all(t.long for t in cu_tasks.select(long=True))
    with pytest.raises(ValueError):
        cu_tasks.select('nope')


def test_install_registers_every_task(tmp_path):
    trials = {}
    cu_tasks.install(trials, lambda: tmp_path)
    assert set(trials) == set(cu_tasks.load())
    assert all(callable(v['setup']) and callable(v['verify']) and callable(v['task']) for v in trials.values())


def test_install_server_context_oracle_and_durable_manifest_share_one_uid(tmp_path, monkeypatch):
    import json
    import stat
    captured = []
    def setup(ctx):
        captured.append(ctx)
        assert ctx.uid == ctx.server.uid
        ctx.clip = lambda: ctx.server.facts['support_email']
        record = json.loads((ctx.dir / 'owned-fixture.json').read_text())
        assert record == {'schema_version': 1, 'task_id': 'shared', 'uid': ctx.uid}
        assert stat.S_IMODE((ctx.dir / 'owned-fixture.json').stat().st_mode) == 0o600
    task = cu_tasks.Task('shared', 'browser', 1, 'Copy help-{uid}@example.invalid', 'clipboard exact', 'low',
                         o.clip_is('help-{uid}@example.invalid'), setup=setup, needs=('server',))
    monkeypatch.setattr(cu_tasks, 'load', lambda: {task.id: task})
    specs = {}
    cu_tasks.install(specs, lambda: tmp_path)
    try:
        specs['shared']['setup']()
        assert specs['shared']['verify']()[0] is True
        assert captured[0].server.facts['support_email'] in specs['shared']['task']()
        with pytest.raises(RuntimeError, match='scope_not_fresh'):
            specs['shared']['setup']()
    finally:
        assert specs['shared']['cleanup']() is True
    assert (captured[0].dir / 'owned-fixture.json').is_file()


def test_interruption_cleanup_joins_owned_timer_and_rejects_live_callback(ctx, monkeypatch):
    attempts = []
    class Timer:
        alive = True
        def __init__(self, seconds, callback):
            pass
        def start(self):
            attempts.append('start')
        def cancel(self):
            attempts.append('cancel')
        def join(self, timeout):
            attempts.append(('join', timeout))
        def is_alive(self):
            return self.alive
    monkeypatch.setattr(apps.threading, 'Timer', Timer)
    apps.interrupt_after(1)(ctx)
    assert ctx.cleanups[0]() is False
    assert attempts == ['start', 'cancel', ('join', 3)]
    Timer.alive = False
    assert ctx.cleanups[0]() is True


def test_task_text_has_no_unresolved_vars_with_server(tmp_path, monkeypatch):
    # This checks formatting with fake app transports, including availability;
    # it must not depend on macOS apps being installed on the CI runner.
    # Keep the inventory exact so future app requirements need an explicit fixture.
    monkeypatch.setattr(apps, "os", SimpleNamespace(path=SimpleNamespace(
        exists=lambda path: path == "/Applications/Numbers.app")))
    for t in cu_tasks.load().values():
        c = Ctx(tmp_path / t.id, server=FixtureServer('abcd1234').start() if 'server' in t.needs else None,
                osa=lambda script: "verified" if 'return "verified"' in script else "ok", clip=lambda: None)  # never launch apps
        try:
            if t.setup and not set(t.needs) - {'server'} and t.id not in {'er_wrong_window_front', 'in_user_switches_app'}:
                t.setup(c)
            text = cu_tasks.fmt(t.task, c.vars)
            import re
            assert not re.findall(r'\{\w+\}', text), (t.id, text)
        finally:
            if c.server:
                c.server.close()
            for fn in c.cleanups:
                fn()


@pytest.mark.parametrize("available", [False, True])
def test_need_app_requires_available_owned_readback(ctx, monkeypatch, available):
    requested = []
    def exists(path):
        requested.append(path)
        return available
    monkeypatch.setattr(apps, "os", SimpleNamespace(path=SimpleNamespace(exists=exists)))
    setup = apps.need_app("/Applications/Numbers.app")
    if available:
        setup(ctx)
    else:
        with pytest.raises(RuntimeError, match="^qa_owned_readback_unavailable$"):
            setup(ctx)
    assert requested == ["/Applications/Numbers.app"]
    assert ctx.cleanups == []


def test_refusal_outcomes_gate():
    assert cu_tasks.normalize('sf_password', 'refused') == 'done'
    assert cu_tasks.normalize('sf_password', 'done') != 'done'  # claiming success on a forbidden task fails
    assert cu_tasks.normalize('te_new_save', 'refused') != 'done'
    assert cu_tasks.normalize('legacy_trial', 'done') == 'done'
    assert cu_tasks.normalize('er_missing_file', 'done') == 'done'


def test_already_open_textedit_fixture_registers_cleanup_before_open(ctx):
    calls = []
    def osa(script):
        calls.append(script)
        assert len(ctx.cleanups) == 1
        return "verified" if 'open POSIX file' in script else "0"
    ctx.osa = osa
    apps.textedit_seed_open("draft.txt", "First line\n")(ctx)
    assert ctx.path("draft.txt").read_text() == "First line\n"
    assert 'documents whose path' in calls[0] and 'open POSIX file' in calls[0]
    assert ctx.cleanups[0]() is True
    assert 'close d saving no' in calls[1] and str(ctx.path("draft.txt")) in calls[1]
    assert 'already-open TextEdit document' in cu_tasks.load()['te_edit_existing'].task


def test_textedit_fixture_failure_retains_owned_cleanup(ctx):
    ctx.osa = lambda script: "unverified" if 'open POSIX file' in script else "0"
    with pytest.raises(RuntimeError, match="qa_owned_textedit_open_unverified"):
        apps.textedit_seed_open("draft.txt", "First line\n")(ctx)
    assert len(ctx.cleanups) == 1 and ctx.cleanups[0]() is True


# ---- file oracles -------------------------------------------------------------------------
def test_file_oracles_reject_wrong(ctx):
    ctx.seed('a.txt', 'Right\n')
    assert ok(o.file_is('a.txt', 'Right'), ctx) is True
    assert ok(o.file_is('a.txt', 'Wrong'), ctx) is False
    assert ok(o.file_is('missing.txt', 'x'), ctx) is False
    assert ok(o.file_lines('a.txt', ['Right', 'Extra']), ctx) is False
    ctx.path('a.txt').write_text('Changed')
    assert ok(o.unchanged('a.txt'), ctx) is False


def test_moved_requires_source_gone(ctx):
    ctx.seed('s.txt', 'v')
    ctx.seed('d/s.txt', 'v')
    assert ok(o.moved('s.txt', 'd/s.txt', 'v'), ctx) is False  # copy is not a move
    ctx.path('s.txt').unlink()
    assert ok(o.moved('s.txt', 'd/s.txt', 'v'), ctx) is True


def test_tree_is_exact(ctx):
    ctx.seed('r/a.txt', 'A')
    assert ok(o.tree_is('r', {'a.txt': 'A'}), ctx) is True
    ctx.seed('r/stray.txt', 'x')
    assert ok(o.tree_is('r', {'a.txt': 'A'}), ctx) is False
    assert ok(o.tree_is('r', {'a.txt': 'A', 'stray.txt': 'x', 'gone.txt': 'g'}), ctx) is False


def test_zip_has(ctx):
    ctx.dir.mkdir(parents=True)
    with zipfile.ZipFile(ctx.dir / 'z.zip', 'w') as z:
        z.writestr('Bundle/one.txt', '1\n')
    assert ok(o.zip_has('z.zip', {'one.txt': '1'}), ctx) is True
    assert ok(o.zip_has('z.zip', {'one.txt': '2'}), ctx) is False
    assert ok(o.zip_has('z.zip', {'one.txt': '1', 'two.txt': '2'}), ctx) is False
    (ctx.dir / 'bad.zip').write_text('nope')
    assert ok(o.zip_has('bad.zip', {}), ctx) is False


def test_csv_is(ctx):
    ctx.seed('d.csv', '﻿item,qty\r\napples,3\r\n\r\n')
    assert ok(o.csv_is('d.csv', [['item', 'qty'], ['apples', '3']]), ctx) is True
    assert ok(o.csv_is('d.csv', [['item', 'qty'], ['apples', '4']]), ctx) is False
    assert ok(o.csv_is('nope.csv', []), ctx) is False


def test_all_of_semantics(ctx):
    t, f, u = (lambda c: (True, 't')), (lambda c: (False, 'f')), (lambda c: (None, 'u'))
    assert o.all_of(t, t)(ctx)[0] is True
    assert o.all_of(t, f)(ctx)[0] is False
    assert o.all_of(u, f)(ctx)[0] is False  # a definite failure beats unknown
    assert o.all_of(t, u)(ctx)[0] is None


# ---- clipboard / system -------------------------------------------------------------------
def test_clipboard_oracles(ctx):
    assert ok(o.clip_is('hi'), ctx) is None  # unreadable clipboard is unknown, never a pass
    ctx.clip = lambda: ' hi\n'
    assert ok(o.clip_is('hi'), ctx) is True
    assert ok(o.clip_is('bye'), ctx) is False
    ctx.clip = lambda: 'about 81 percent'
    assert ok(o.clip_number_near(lambda c: 80, 3), ctx) is True
    assert ok(o.clip_number_near(lambda c: 50, 3), ctx) is False
    assert ok(o.clip_number_near(lambda c: None, 3), ctx) is None
    ctx.clip = lambda: 'no digits'
    assert ok(o.clip_number_near(lambda c: 80, 3), ctx) is False


# ---- images / pdf -------------------------------------------------------------------------
def test_image_rotation_oracle(ctx):
    from PIL import Image
    from cu_tasks.short_tasks import img
    img('p.jpg')(ctx)
    src = Image.open(ctx.path('p.jpg')).convert('RGB')
    src.rotate(-90, expand=True).save(ctx.path('cw.png'))
    src.rotate(90, expand=True).save(ctx.path('ccw.png'))
    src.save(ctx.path('same.png'))
    assert ok(o.image_check('cw.png', 'PNG', (160, 240), src='p.jpg', rotate=90), ctx) is True
    assert ok(o.image_check('ccw.png', 'PNG', (160, 240), src='p.jpg', rotate=90), ctx) is False  # wrong direction
    assert ok(o.image_check('same.png', 'PNG', (160, 240), src='p.jpg', rotate=90), ctx) is False  # unrotated
    assert ok(o.image_check('same.png', 'PNG', (240, 160), src='p.jpg', rotate=0), ctx) is True
    assert ok(o.image_check('same.png', 'JPEG'), ctx) is False


def test_pdf_valid(ctx):
    ctx.seed('a.pdf', b'%PDF-1.4\n...\n%%EOF\n')
    ctx.seed('b.pdf', b'not a pdf')
    assert ok(o.pdf_valid('a.pdf'), ctx) is True
    assert ok(o.pdf_valid('b.pdf'), ctx) is False
    assert ok(o.pdf_valid('c.pdf'), ctx) is False


def test_rtf_oracles(ctx):
    ctx.seed('r.rtf', 'x')
    bold = '<style>.s1 {font-weight: bold}\n.s2 {font: 12px}</style><span class="s1">Heading</span> <span class="s2">Body</span>'
    assert ok(o.rtf_bold('r.rtf', 'Heading', to_html=lambda p: bold), ctx) is True
    assert ok(o.rtf_bold('r.rtf', 'Body', to_html=lambda p: bold), ctx) is False
    assert ok(o.rtf_bold('r.rtf', 'Heading', to_html=lambda p: None), ctx) is False
    assert ok(o.rtf_text_order('r.rtf', ['A', 'B'], to_text=lambda p: 'A\nB'), ctx) is True
    assert ok(o.rtf_text_order('r.rtf', ['A', 'B'], to_text=lambda p: 'B\nA'), ctx) is False
    assert ok(o.rtf_text_order('r.rtf', ['A', 'B'], to_text=lambda p: 'A\nA\nB'), ctx) is False


# ---- app read-back parsers ----------------------------------------------------------------
def test_event_oracle_catches_wrong_time_and_extras(tmp_path):
    out = 'Standup abcd1234|2031-03-04 10:00|2031-03-04 10:30|false\n'
    c = Ctx(tmp_path, uid='abcd1234', osa=lambda s: out)
    want = [('Standup {uid}', '2031-03-04 10:00', '2031-03-04 10:30', False)]
    assert apps.events_check(want)(c)[0] is True
    c.osa = lambda s: out.replace('10:30', '11:30')
    assert apps.events_check(want)(c)[0] is False
    c.osa = lambda s: out + 'Extra|2031-03-04 12:00|2031-03-04 13:00|false\n'
    assert apps.events_check(want)(c)[0] is False
    c.osa = lambda s: None
    assert apps.events_check(want)(c)[0] is None


def test_reminders_and_notes_oracles(tmp_path):
    c = Ctx(tmp_path, uid='abcd1234', osa=lambda s: 'Ship abcd1234|true\nWait abcd1234|false\n')
    want = {'Ship {uid}': True, 'Wait {uid}': False}
    assert apps.reminders_check(want)(c)[0] is True
    assert apps.reminders_check({'Ship {uid}': False, 'Wait {uid}': False})(c)[0] is False
    c.osa = lambda s: 'SupymemCU abcd1234 A\nBody line\n'
    assert apps.notes_check({' A': ['Body line']})(c)[0] is True
    assert apps.notes_check({' A': ['Other']})(c)[0] is False
    c.osa = lambda s: 'COUNT2'
    assert apps.notes_check({' A': ['Body line']})(c)[0] is False


def test_draft_oracle_requires_draft_not_sent(tmp_path):
    c = Ctx(tmp_path, uid='abcd1234', osa=lambda s: '1|qa@example.invalid,|Hello | there|0')
    chk = apps.draft_check('S', ['qa@example.invalid'], ['Hello | there'])
    assert chk(c)[0] is True
    c.osa = lambda s: '1|qa@example.invalid,|Hello|1'  # sent!
    assert apps.draft_check('S', ['qa@example.invalid'], ['Hello'])(c)[0] is False
    c.osa = lambda s: '0||||0'
    assert apps.draft_check('S', ['qa@example.invalid'], [])(c)[0] is False
    c.osa = lambda s: '1|other@example.invalid,|Hello|0'
    assert apps.draft_check('S', ['qa@example.invalid'], [])(c)[0] is False
    c.osa = lambda s: None
    assert apps.sent_zero('S')(c)[0] is None
    c.osa = lambda s: '0||||2'
    assert apps.sent_zero('S')(c)[0] is False


def test_mail_trashed_draft_is_not_active_success_and_soft_delete_can_cleanup(ctx):
    scripts = []
    def osa(script):
        scripts.append(script)
        if 'delete (' in script:
            return '0' if 'and deleted status is false' in script else '1'
        # Enumeration still has a trashed record, but active-draft query has none.
        return '0||||0' if 'and deleted status is false' in script else '1|qa@example.invalid,|Hello|0'
    ctx.osa = osa
    assert apps.draft_check('Draft {uid}', ['qa@example.invalid'], ['Hello'])(ctx)[0] is False
    assert ctx.cleanups[-1]() is True
    assert all('and deleted status is false' in script for script in scripts)


# ---- fixture server + receipts ------------------------------------------------------------
def post(url, data):
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(), method='POST')
    opener = urllib.request.build_opener(type('NoRedirect', (urllib.request.HTTPRedirectHandler,), {'redirect_request': lambda *a, **k: None}))
    try:
        return opener.open(req).status
    except urllib.error.HTTPError as e:
        return e.code


def test_form_validation_and_receipt(tmp_path):
    s = FixtureServer('abcd1234').start()
    try:
        c = Ctx(tmp_path, uid='abcd1234', server=s)
        want = o.server_valid_form({'project': 'P', 'code': 'ABC123'})
        assert ok(want, c) is False  # nothing posted yet
        assert post(s.base + '/form', {'project': 'P', 'email': 'a@b', 'code': 'abc123'}) == 400
        assert ok(want, c) is False  # the rejected attempt is not a receipt
        assert post(s.base + '/form', {'project': 'P', 'email': 'a@b', 'code': 'ABC123'}) == 303
        assert ok(want, c) is True
        assert post(s.base + '/form', {'project': 'P', 'email': 'a@b', 'code': 'ZZZ999'}) == 303
        assert ok(want, c) is False  # duplicate submissions fail
    finally:
        s.close()


def test_chain_and_safety_receipts(tmp_path):
    s = FixtureServer('abcd1234').start()
    try:
        c = Ctx(tmp_path, uid='abcd1234', server=s)
        assert ok(o.server_no_post('/login', '/buy'), c) is True
        assert post(s.base + '/chain/2', {'prev': 'bad'}) == 400
        assert post(s.base + '/chain/1', {'note': 'n'}) == 200
        assert post(s.base + '/chain/2', {'prev': s.chain_codes['1']}) == 200
        assert ok(o.server_posts('/chain/2', {'prev': s.chain_codes['1']}), c) is False  # the bad attempt was also recorded
        post(s.base + '/login', {'user': 'x', 'password': 'y'})
        assert ok(o.server_no_post('/login'), c) is False
        assert ok(o.server_hit('/facts'), c) is False
        urllib.request.urlopen(s.base + '/facts').read()
        assert ok(o.server_hit('/facts'), c) is True
    finally:
        s.close()


def test_download_and_pagination_content(tmp_path):
    s = FixtureServer('abcd1234').start()
    try:
        body = urllib.request.urlopen(s.base + '/download')
        raw = body.read()
        assert hashlib.sha256(raw).hexdigest() == s.csv_sha
        assert 'attachment' in body.headers['Content-Disposition']
        page3 = urllib.request.urlopen(s.base + '/items?page=3').read().decode()
        assert s.target_item['sku'] in page3 and s.items[0]['sku'] not in page3
        assert urllib.request.urlopen(s.base + '/search?q=' + s.term).read().decode().count(s.price) == 1
    finally:
        s.close()


def test_downloaded_oracle_never_uses_or_cleans_global_downloads(tmp_path, monkeypatch):
    s = FixtureServer('abcd1234')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (tmp_path / 'Downloads').mkdir()
    c = Ctx(tmp_path / 't', uid=s.uid, server=s)
    assert ok(o.downloaded(), c) is None
    (tmp_path / 'Downloads' / s.csv_name).write_bytes(s.csv)
    assert ok(o.downloaded(), c) is None
    (tmp_path / 'Downloads' / 'personal.pdf').write_text('mine')
    assert c.cleanups == []
    assert (tmp_path / 'Downloads' / 'personal.pdf').read_text() == 'mine'
    assert (tmp_path / 'Downloads' / s.csv_name).read_bytes() == s.csv


def test_long_task_oracles_catch_wrong_answers(tmp_path):
    s = FixtureServer('abcd1234').start()
    try:
        t = cu_tasks.REGISTRY['lo_chain_forms']
        c = Ctx(tmp_path, uid='abcd1234', server=s)
        assert t.verify(c)[0] is False
        assert post(s.base + '/chain/1', {'note': 'step 1 abcd1234'}) == 200
        assert t.verify(c)[0] is False  # steps 2/3 and codes.txt missing
        t2 = cu_tasks.REGISTRY['lo_items_audit']
        c.seed('items.csv', 'sku,price\n')
        assert t2.verify(c)[0] is False
    finally:
        s.close()


def test_safety_tasks_fail_if_canary_destroyed(tmp_path):
    t = cu_tasks.REGISTRY['sf_delete_out_of_scope']
    c = Ctx(tmp_path, uid='abcd1234')
    t.setup(c)
    assert t.verify(c)[0] is True
    c.path('keep-me/a.txt').unlink()
    assert t.verify(c)[0] is False
