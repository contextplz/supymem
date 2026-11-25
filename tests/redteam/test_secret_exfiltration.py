from coding_agents.detect import FORBIDDEN_PATH_SUFFIXES


def test_forbidden_suffixes_defined():
    assert ".claude" in FORBIDDEN_PATH_SUFFIXES
    assert ".opencode" in FORBIDDEN_PATH_SUFFIXES
