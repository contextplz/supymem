from __future__ import annotations

from desktop_runtime.paths import data_root, os_conventional_app_data_dir


def test_data_root_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "cos"))
    root = data_root(create=True)
    assert root == (tmp_path / "cos").resolve()
    assert (root / "ventures").is_dir()


def test_os_conventional_dir_is_absolute():
    p = os_conventional_app_data_dir()
    assert p.is_absolute()
    assert p.name in {"Supymem", "CompanyOS", "supymem", "companyos"}
