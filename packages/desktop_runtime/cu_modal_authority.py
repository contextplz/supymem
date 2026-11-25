"""One execution's explicitly reviewed direct native-modal edges.

Only the owned transport wrapper calls refresh; actions/model JSON cannot mint
leases. The native daemon independently enforces the same reviewed parent grant.
"""
import time

from desktop_runtime.cu_reviewed_scope import check_reviewed_call, event_tools_names

_READS = {'list_apps', 'list_windows', 'get_window_state', 'check_permissions',
          'health_report', 'get_session_state', 'end_session'}


def identity(tool, args):
    target = args.get('target') if isinstance(args, dict) else None
    source = target if isinstance(target, dict) and target else args
    return source.get('pid'), source.get('window_id')


class ReviewedModalAuthority:
    def __init__(self, scope, *, stop=None):
        if not scope.modal_parents:
            raise ValueError('desktop_scope_unsupported')
        self.scope = scope
        self.stop = stop
        self.children = {}  # child -> exact explicitly reviewed parent, no sibling grant
        self.observed_at = 0.0
        self.armed = None

    def _check_live(self):
        self.scope.live()
        if self.stop is not None and self.stop.is_set():
            self.invalidate()
            raise ValueError('stopped_by_founder')

    @property
    def pairs(self):
        if time.monotonic() - self.observed_at >= 20:
            self.children.clear()
        return self.scope.pairs + tuple(self.children)

    def invalidate(self):
        self.children.clear()
        self.observed_at = 0.0
        self.armed = None

    def refresh(self, call, pid, inventory=None):
        self.invalidate()
        self._check_live()  # exact current process start and every reviewed root
        began = time.monotonic()
        if inventory is None:
            inventory = call('list_windows', {'pid': pid, 'on_screen_only': False})
        rows = inventory.get('windows') if isinstance(inventory, dict) else None
        if (not isinstance(rows, list) or len(rows) > 512
                or any(not isinstance(row, dict) or type(row.get('pid')) is not int
                       or type(row.get('window_id')) is not int for row in rows)):
            raise ValueError('desktop_modal_identity_unconfirmed')
        graph = {}
        for root_pid, root_window in self.scope.modal_parents:
            if root_pid != pid:
                continue
            if sum((row['pid'], row['window_id']) == (pid, root_window) for row in rows) != 1:
                raise ValueError('desktop_modal_identity_unconfirmed')
            state = call('get_window_state', {'pid': pid, 'window_id': root_window,
                         'max_dimension': 1280, 'max_elements': 400, 'max_depth': 20})
            if (not isinstance(state, dict) or state.get('modal_protocol') != 'supymem_exact_modal_v1'
                    or type(state.get('pid')) is not int or state['pid'] != pid
                    or type(state.get('window_id')) is not int or state['window_id'] != root_window
                    or not isinstance(state.get('snapshot_id'), str) or not state['snapshot_id']
                    or state.get('degraded') is True or state.get('status') == 'refused'):
                raise ValueError('desktop_modal_identity_unconfirmed')
            candidates = state.get('modal_windows')
            if not isinstance(candidates, list) or len(candidates) > 1:
                raise ValueError('desktop_modal_identity_unconfirmed')
            if not candidates:
                continue
            child = candidates[0]
            if (not isinstance(child, dict) or set(child) != {'pid', 'window_id', 'parent_window_id'}
                    or any(type(child.get(key)) is not int for key in child)
                    or child['pid'] != pid or child['parent_window_id'] != root_window
                    or child['window_id'] <= 0 or child['window_id'] == root_window
                    or (pid,child['window_id']) in self.scope.pairs
                    or (pid,child['window_id']) in graph
                    or sum((row['pid'],row['window_id']) == (pid,child['window_id']) for row in rows) != 1):
                raise ValueError('desktop_modal_identity_unconfirmed')
            graph[(pid,child['window_id'])] = (pid,root_window)
        self._check_live()
        if time.monotonic() - began >= 20:
            raise ValueError('desktop_modal_identity_unconfirmed')
        self.children = graph
        self.observed_at = time.monotonic()

    def arm(self, tool, args):
        check_reviewed_call(tool, args, self.pairs)
        pid, window = identity(tool,args)
        if (pid,window) in self.children and tool not in _READS:
            self.armed = (tool,pid,window,time.monotonic())

    def check_transport(self, tool, args):
        self._check_live()
        check_reviewed_call(tool,args,self.pairs)
        pid,window = identity(tool,args)
        if (pid,window) in self.children and tool not in _READS:
            armed, self.armed = self.armed, None
            if (armed is None or armed[:3] != (tool,pid,window)
                    or time.monotonic()-armed[3] >= 20):
                raise ValueError('desktop_modal_fresh_lease_required')

    def effectful(self, tool):
        return tool in event_tools_names() or tool == 'set_value'
