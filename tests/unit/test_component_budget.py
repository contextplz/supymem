from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.build.workorder import check_component_budget, create_workorder


def test_25_components_rejected_with_decomposition():
    comps = [f"c{i}" for i in range(25)]
    res = check_component_budget(comps)
    assert res.ok is False
    assert res.decomposition
    with pytest.raises(ValueError):
        create_workorder("ven", title="big", components=comps, files_expected=["a.py"])
