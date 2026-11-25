"""Register vault values with the data-boundary detector (43 §5.6)."""

from __future__ import annotations


def _vault_values(workspace_id: str | None):
    from app.connectors.secrets import store

    vault = store()
    listed = getattr(vault, "values", None)
    if listed is None:
        return ()
    try:
        return tuple(listed(workspace_id))
    except TypeError:
        return tuple(listed())
    except Exception:
        return ()


def register_vault_boundary() -> None:
    from app.boundary.detectors.vault import register_value_provider

    register_value_provider(_vault_values)
