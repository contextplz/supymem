"""Shared, uncached working state with explicit source scope and atomic transforms."""
import json
import re
from collections.abc import MutableMapping

from sqlalchemy import select
from app.db.models import MemoryWorkingEntry, Venture
from app.db.session import serialize_writes
from app.tenancy import current_or_none, ScopeRequired
from core.clock import utcnow


class PostgresWorkingMemory(MutableMapping):
    def _scope(self, session, key=None):
        scope = current_or_none()
        if scope is None:
            raise ScopeRequired("Hosted working memory requires an explicit workspace/venture scope")
        if key and key.startswith("engagement:crew:"):
            from app.db.models import Engagement
            engagement = session.get(Engagement, key.removeprefix("engagement:crew:"))
            if not engagement or engagement.workspace_id != scope.workspace_id:
                raise PermissionError("working_memory_scope_mismatch")
            return "workspace:" + engagement.workspace_id, engagement.workspace_id, None
        vid = re.search(r"(?:^|:)ven_[A-Za-z0-9_]+(?=:|$)", key or "")
        if vid:
            venture_id = vid.group().lstrip(":")
            row = session.get(Venture, venture_id)
            if row is None or scope.venture_id != venture_id:
                raise PermissionError("working_memory_scope_mismatch")
            if scope.workspace_id != row.workspace_id:
                raise PermissionError("working_memory_scope_mismatch")
            # Private pending state cannot be recovered via an opaque foreign chat id.
            if key and ":cnv_" in key:
                from app.chat.conversations import resolve
                resolve(venture_id, key.rsplit(":", 1)[-1])
            return "venture:" + venture_id, row.workspace_id, venture_id
        if scope and scope.venture_id:
            row = session.get(Venture, scope.venture_id)
            if row is None or scope.workspace_id != row.workspace_id:
                raise PermissionError("working_memory_scope_mismatch")
            return "venture:" + row.id, row.workspace_id, row.id
        if scope and scope.workspace_id != "none":
            return "workspace:" + scope.workspace_id, scope.workspace_id, None
        raise ScopeRequired("Hosted working memory requires an explicit workspace/venture scope")

    def __getitem__(self, key):
        from app.state import db
        with db()() as session:
            scope_key, wid, vid = self._scope(session, key)
            if vid and key == f"charter:{vid}":
                # Canonical commit precedes cache publication. An active
                # pointer therefore outranks even an older working entry.
                venture = session.get(Venture, vid)
                if venture.active_charter_id:
                    from app.charter.current import current_locked_charter
                    charter = current_locked_charter(session, vid, workspace_id=wid)
                    if charter is None:
                        raise KeyError(key)
                    return charter
            row = session.get(MemoryWorkingEntry, (scope_key, key))
            if row is None:
                raise KeyError(key)
            return json.loads(row.payload_json)

    def __setitem__(self, key, value):
        self.update_value(key, lambda _: value)

    def update_value(self, key, transform, default=None):
        """Run read/transform/write under the same shared transaction lock.

        The transform receives a detached JSON value. Exceptions roll back;
        callbacks must be local and provider-free, never network work.
        """
        from app.state import db
        with db()() as session:
            # Resolve before acquiring the separate working-key lock.
            scope_key, wid, vid = self._scope(session, key)
            if session.get_bind().dialect.name == "sqlite":
                session.rollback()
            serialize_writes(session, key="working-memory:" + scope_key + ":" + key)
            row = session.get(MemoryWorkingEntry, (scope_key, key))
            current = json.loads(row.payload_json) if row else default
            value = transform(current)
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 1_000_000:
                raise ValueError("working_memory_value_too_large")
            if row:
                row.payload_json, row.updated_at = encoded, utcnow()
                row.version += 1
            else:
                session.add(MemoryWorkingEntry(scope_key=scope_key, key=key, workspace_id=wid,
                    venture_id=vid, payload_json=encoded, version=1, updated_at=utcnow()))
            session.commit()
            return value

    def versioned(self, key):
        from app.state import db
        with db()() as session:
            scope_key, _, _ = self._scope(session, key)
            row = session.get(MemoryWorkingEntry, (scope_key, key))
            return (json.loads(row.payload_json), row.version) if row else (None, 0)

    def compare_and_set(self, key, expected_version, value):
        from app.state import db
        with db()() as session:
            scope_key, wid, vid = self._scope(session, key)
            if session.get_bind().dialect.name == "sqlite":
                session.rollback()
            serialize_writes(session, key="working-memory:" + scope_key + ":" + key)
            row = session.get(MemoryWorkingEntry, (scope_key, key))
            if (row.version if row else 0) != expected_version:
                return False
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 1_000_000:
                raise ValueError("working_memory_value_too_large")
            if row:
                row.payload_json, row.updated_at = encoded, utcnow()
                row.version += 1
            else:
                session.add(MemoryWorkingEntry(scope_key=scope_key, key=key, workspace_id=wid,
                    venture_id=vid, payload_json=encoded, version=1, updated_at=utcnow()))
            session.commit()
            return True

    def __delitem__(self, key):
        marker = object()
        if self.pop(key, marker) is marker:
            raise KeyError(key)

    def pop(self, key, default=None):
        from app.state import db
        with db()() as session:
            scope_key, _, _ = self._scope(session, key)
            if session.get_bind().dialect.name == "sqlite":
                session.rollback()
            serialize_writes(session, key="working-memory:" + scope_key + ":" + key)
            row = session.get(MemoryWorkingEntry, (scope_key, key))
            if row is None:
                return default
            value = json.loads(row.payload_json)
            session.delete(row)
            session.commit()
            return value

    def __iter__(self):
        from app.state import db
        with db()() as session:
            scope_key, _, _ = self._scope(session)
            return iter(session.scalars(select(MemoryWorkingEntry.key).where(MemoryWorkingEntry.scope_key == scope_key)).all())

    def __len__(self):
        return sum(1 for _ in self)

    def clear(self):
        # Never erase another scope through the dict-compatible API.
        for key in list(self):
            self.pop(key)

    def close(self):
        pass
