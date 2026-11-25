"""Default-empty native CU pilot; browser and coding reviews are separate lanes."""
import json
import os
import re

from app.db.supymem_models import DesktopDevice

ALLOWLIST_ENV = "DEVICE_DESKTOP_EXECUTION_ALLOWLIST"
MIN_APP_VERSION = (0, 1, 84)  # First release with the official 0.33.3 packaging pin.
# Official 0.33.3 rejects modal_parents. The patched 0.29.1 artifact has no
# reviewed native-build ledger, so an environment flag cannot admit it.
REVIEWED_MODAL_PROTOCOL_SUPPORTED = False


def pair_allowed(actor, device):
    """JSON pairs of canonical person/device ids, never subjects or wildcards."""
    raw = os.environ.get(ALLOWLIST_ENV, "[]")
    if len(raw) > 32_768:
        return False
    try:
        pairs = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(pairs, list) or len(pairs) > 100:
        return False
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2
                or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                       for value in pair)):
            return False
    return [actor, device] in pairs


def available(session, actor, device, manifest=None):
    from app.build import device_orders as orders
    if not orders.desktop_execution_enabled() or not pair_allowed(actor, device):
        return False
    if ((manifest or {}).get("desktop_verification") or {}).get("modal_policy"):
        return False
    row = session.get(DesktopDevice, device)
    return bool(row and row.person_id == actor and row.revoked_at is None and row.platform == "macos"
        and re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", row.app_version or "")
        and tuple(map(int, row.app_version.split("."))) >= MIN_APP_VERSION)


def assert_available(session, actor, device, manifest):
    from app.build import device_orders as orders
    if not orders.native_desktop_manifest(manifest):
        return
    orders.assert_native_desktop_available(manifest)
    if not available(session, actor, device, manifest):
        raise ValueError(orders.DESKTOP_EXECUTION_UNAVAILABLE)
