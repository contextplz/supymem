import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.peer_review import review


def test_no_self_stamp():
    with pytest.raises(PermissionError):
        review("p", author_id="a", reviewer_id="a")
