"""Exact v1 reviewed desktop surface; task prose never expands native authority."""
from dataclasses import dataclass
import os

from desktop_runtime.peekaboo_driver import APP_ID, PeekabooDriver


def event_tools_names():
    return {'click', 'double_click', 'right_click', 'drag', 'type_text',
            'hotkey', 'press_key', 'scroll', 'move_cursor'}


def check_reviewed_call(tool, args, surface):
    """Admission also runs at the actual MCP transport before future dispatch."""
    surfaces = (surface,) if len(surface) == 2 and all(type(v) is int for v in surface) else surface
    pids = {row[0] for row in surfaces}
    if tool in {'launch_app', 'invoke_menu'}:
        raise ValueError('desktop_scope_expansion_required')
    args = args if isinstance(args, dict) else {}
    if tool in event_tools_names() and any(key in args for key in ('pid','window_id')):
        raise ValueError('desktop_scope_expansion_required')
    if tool not in event_tools_names() and 'target' in args:
        raise ValueError('desktop_scope_expansion_required')
    target = args.get('target') or {}
    if not isinstance(target, dict) or (target and target.get('kind') != 'window'):
        raise ValueError('desktop_scope_expansion_required')
    event_tools = event_tools_names()
    if tool in event_tools and (not target or args.get('delivery_mode') != 'background'):
        raise ValueError('desktop_scope_expansion_required')
    pid = target.get('pid') if target else args.get('pid')
    window = target.get('window_id') if target else args.get('window_id')
    if tool not in {'list_apps', 'check_permissions', 'health_report', 'get_session_state', 'end_session'}:
        if type(pid) is not int or pid not in pids:
            raise ValueError('desktop_scope_expansion_required')
        if tool != 'list_windows' and (type(window) is not int or (pid, window) not in surfaces):
            raise ValueError('desktop_scope_expansion_required')


@dataclass(frozen=True)
class ReviewedDesktopScope:
    bundle: str
    pid: int
    start: str
    window: int
    extra: tuple = ()
    max_steps: int = 48
    modal_parents: tuple = ()

    @property
    def surfaces(self):
        return (self,) + self.extra

    @property
    def pairs(self):
        return tuple((scope.pid, scope.window) for scope in self.surfaces)

    @classmethod
    def from_manifest(cls, manifest):
        spec = manifest.get('desktop_verification') or {}
        policy = spec.get('modal_policy')
        if policy is not None and os.environ.get('SUPYMEM_REVIEWED_MODAL_CHILDREN') != '1':
            raise ValueError('desktop_scope_unsupported')
        reviewed_steps = spec.get('max_steps')
        if type(reviewed_steps) is not int or not 1 <= reviewed_steps <= 48:
            raise ValueError('desktop_scope_unsupported')
        if spec.get('operation') == 'desktop_goal_v2':
            if os.environ.get('SUPYMEM_REVIEWED_DESKTOP_V2') != '1':
                raise ValueError('desktop_scope_unsupported')
            rows = spec.get('reviewed_surfaces')
            keys = {'application_bundle', 'process_id', 'process_start_identity', 'window_id', 'window_title'}
            if (not isinstance(rows, list) or not 1 <= len(rows) <= 8
                    or any(not isinstance(row, dict) or set(row) != keys
                           or type(row.get('window_title')) is not str or len(row['window_title']) > 200 for row in rows)
                    or any(spec.get(key) != rows[0][key] for key in keys)):
                raise ValueError('desktop_scope_unsupported')
            bundles = list(dict.fromkeys(row['application_bundle'] for row in rows))
            if manifest.get('applications') != bundles:
                raise ValueError('desktop_scope_unsupported')
            scopes = []
            for row in rows:
                projected = {**manifest, 'applications': [row['application_bundle']],
                    'desktop_verification': {**{k:v for k,v in spec.items() if k not in {'reviewed_surfaces','modal_policy'}}, **row, 'operation': 'desktop_goal_v1'}}
                scopes.append(cls.from_manifest(projected))
            if len({(row.pid, row.window) for row in scopes}) != len(scopes):
                raise ValueError('desktop_scope_unsupported')
            for row in scopes:
                if any((other.pid == row.pid or other.bundle == row.bundle)
                        and (other.pid, other.bundle, other.start) != (row.pid, row.bundle, row.start)
                        for other in scopes):
                    raise ValueError('desktop_scope_unsupported')
            first = scopes[0]
            scope = cls(first.bundle, first.pid, first.start, first.window, tuple(scopes[1:]), reviewed_steps)
            return scope.with_modal_policy(policy)
        bundle, pid, start, window = (spec.get(k) for k in
            ('application_bundle', 'process_id', 'process_start_identity', 'window_id'))
        if (spec.get('reviewed_surfaces') is not None or spec.get('operation') != 'desktop_goal_v1'
                or spec.get('delivery_mode') != 'background'
                or type(bundle) is not str or not APP_ID.fullmatch(bundle)
                or type(pid) is not int or pid <= 0
                or type(start) is not str or not 1 <= len(start) <= 24 or not start.isascii() or not start.isdigit()
                or type(window) is not int or window <= 0
                or manifest.get('applications') != [bundle]
                or manifest.get('actions') != ['desktop.read', 'desktop.interact']
                or manifest.get('origins') != []):
            raise ValueError('desktop_scope_unsupported')
        return cls(bundle, pid, start, window, max_steps=reviewed_steps).with_modal_policy(policy)

    def with_modal_policy(self, policy):
        if policy is None:
            return self
        keys = {'application_bundle','process_id','process_start_identity','window_id'}
        if (type(policy) is not dict or set(policy) != {'operation','parents'}
                or policy['operation'] != 'exact_parent_children_v1'
                or type(policy['parents']) is not list or not 1 <= len(policy['parents']) <= 8
                or any(type(row) is not dict or set(row) != keys for row in policy['parents'])):
            raise ValueError('desktop_scope_unsupported')
        roots = {(scope.bundle,scope.pid,scope.start,scope.window) for scope in self.surfaces}
        parents = []
        for row in policy['parents']:
            identity = (row['application_bundle'],row['process_id'],row['process_start_identity'],row['window_id'])
            if (type(row['application_bundle']) is not str or type(row['process_id']) is not int or type(row['window_id']) is not int
                    or type(row['process_start_identity']) is not str or identity not in roots
                    or (row['process_id'],row['window_id']) in parents):
                raise ValueError('desktop_scope_unsupported')
            parents.append((row['process_id'],row['window_id']))
        from dataclasses import replace
        return replace(self,modal_parents=tuple(parents))

    def live(self):
        for scope in self.extra:
            scope.live()
        matches = [app for app in PeekabooDriver.running_apps() if app.bundle_id == self.bundle]
        if (len(matches) != 1 or matches[0].pid != self.pid
                or matches[0].process_start_identity != self.start):
            raise ValueError('desktop_app_identity_changed')
        app = matches[0]
        windows = PeekabooDriver.running_windows(app)
        if sum(row.get('window_id') == self.window for row in windows) != 1:
            raise ValueError('desktop_window_identity_changed')
        return app

    def admit(self, action, _observation):
        # These are separate reviewed work-order kinds. A desktop manifest does
        # not grant an unrelated owned browser or direct filesystem executor.
        if action.op in {'fs', 'script', 'template', 'navigate', 'open_path', 'new_tab', 'switch_tab'}:
            return 'desktop_scope_expansion_required'
        if action.op == 'key':
            from desktop_runtime.cu_ai_cua import KEY_ALIASES
            parts = [KEY_ALIASES.get(part.lower(), part.lower()) for part in action.chord.split('+') if part]
            if parts and parts[-1] == 'n' and {'cmd', 'ctrl'}.intersection(parts[:-1]):
                return 'desktop_scope_expansion_required'
        self.live()  # Before every action, reject PID reuse/closed reviewed window.
        return None

    def caller(self, call, *, stop=None, modal_authority=None):
        def scoped(tool, arguments):
            if modal_authority is not None:
                try:
                    self.live()
                    from desktop_runtime.cu_modal_authority import identity
                    pid,window = identity(tool,arguments)
                    if (pid,window) in modal_authority.children:
                        modal_authority.refresh(call,pid)
                    modal_authority.arm(tool,arguments)
                except Exception:
                    # Only read-only preflight has run. This is not an unknown
                    # input outcome and Continue cannot widen the reviewed graph.
                    modal_authority.invalidate()
                    from desktop_runtime.cu_ai_engine import NeedsHuman
                    raise NeedsHuman('other','This reviewed modal is not admitted. A fresh exact parent/child observation or a new review is required.',
                                     dispatch_state='not_dispatched') from None
            else:
                self.live()
                check_reviewed_call(tool, arguments, self.pairs)
            if stop is not None and stop.is_set():
                raise ValueError('stopped_by_founder')
            try:
                result = call(tool, arguments)
            finally:
                if modal_authority is not None and modal_authority.effectful(tool):
                    modal_authority.invalidate()
            if tool == 'list_apps':
                return {**result, 'apps': [row for row in result.get('apps', [])
                    if any(row.get('bundle_id') == scope.bundle and row.get('pid') == scope.pid for scope in self.surfaces) and row.get('running') is True]}
            if tool == 'list_windows':
                if modal_authority is not None:
                    modal_authority.refresh(call,arguments['pid'],result)
                pairs = modal_authority.pairs if modal_authority is not None else self.pairs
                return {**result, 'windows': [row for row in result.get('windows', [])
                    if (row.get('pid'), row.get('window_id')) in pairs]}
            return result
        def close():
            if modal_authority is not None:
                modal_authority.invalidate()
            getattr(call,'close',lambda:None)()
        scoped.close = close
        scoped.is_reviewed_child = lambda pid,wid: modal_authority is not None and (pid,wid) in modal_authority.children
        return scoped
