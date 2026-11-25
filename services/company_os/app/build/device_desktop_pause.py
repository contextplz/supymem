"""Founder-controlled desktop-only pause for one authenticated actor/device."""
import hashlib
import json

from app.db.models import VentureDocument, Workspace

KIND = 'device_desktop_pause_v1'
MESSAGE = ('Mac app control is paused on this device. Ask Supymem for manual app steps. '
           'Reviewed coding, browser and terminal tasks remain available.')


def _id(workspace_id, actor, device):
    return 'desktop-pause:' + hashlib.sha256(json.dumps([workspace_id, actor, device]).encode()).hexdigest()


def _saved(session, workspace_id, actor, device):
    row = session.get(VentureDocument, _id(workspace_id, actor, device))
    if row is None:
        return None, False
    try:
        value = json.loads(row.provenance_json or '{}')
    except (ValueError, TypeError) as error:
        raise ValueError('device_desktop_pause_invalid') from error
    if not isinstance(value, dict):
        raise ValueError('device_desktop_pause_invalid')
    if (row.kind != KIND or (value.get('workspace_id'), value.get('actor_id'), value.get('device_id'))
            != (workspace_id, actor, device) or type(value.get('paused')) is not bool):
        raise ValueError('device_desktop_pause_invalid')
    return row, value['paused']


def assert_available(session, workspace_id, actor, device, manifest):
    if manifest.get('desktop_verification'):
        _lock_workspace(session, workspace_id)
        if _saved(session, workspace_id, actor, device)[1]:
            raise ValueError('device_desktop_paused')


def status(venture_id, caller):
    from app.build import device_orders as orders
    wid, actor, device = orders._scope(venture_id, caller)
    with orders.db()() as session:
        orders.authorize(session, workspace_id=wid, venture_id=venture_id, actor=actor)
        _, paused = _saved(session, wid, actor, device)
    return {'paused': paused, 'message': MESSAGE if paused else None}


def _lock_workspace(session, workspace_id):
    if session.get(Workspace, workspace_id, with_for_update=True) is None:
        raise KeyError('device_workspace_not_found')


def set_pause(venture_id, caller, paused):
    if type(paused) is not bool:
        raise ValueError('device_desktop_pause_invalid')
    from app.build import device_orders as orders
    wid, actor, device = orders._scope(venture_id, caller)
    with orders.db()() as session:
        _lock_workspace(session, wid)
        orders.authorize(session, workspace_id=wid, venture_id=venture_id, actor=actor, mutate=True)
        row, _ = _saved(session, wid, actor, device)
        now = orders.utcnow()
        if row is None:
            row = VentureDocument(id=_id(wid, actor, device), venture_id=venture_id, kind=KIND,
                title='Mac app control pause', created_at=now, updated_at=now)
            session.add(row)
        row.provenance_json = json.dumps({'workspace_id': wid, 'actor_id': actor, 'device_id': device,
                                         'paused': paused})
        row.updated_at = now
        session.commit()
    return {'paused': paused, 'message': MESSAGE if paused else None}
