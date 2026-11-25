from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.campaign import create_campaign


def test_missing_hypothesis_rejected():
    with pytest.raises(ValueError):
        create_campaign("ven", hypothesis="", metric="signups", threshold=10, window_days=7, channels=["landing"])
    with pytest.raises(ValueError):
        create_campaign("ven", hypothesis="x", metric="signups", threshold=None, window_days=7, channels=["landing"])
    c = create_campaign("ven", hypothesis="landing converts >=2%", metric="cvr", threshold=0.02, window_days=14, channels=["landing"])
    assert c.id
