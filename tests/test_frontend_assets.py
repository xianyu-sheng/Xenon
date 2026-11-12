"""The wheel must carry the front-end, and the launcher must find it there.

A wheel without ``xenon/frontend/assets/xenon-ui.js`` installs a package whose
only UI cannot start; these tests pin the staging contract and the lookup order.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from xenon import main as xenon_main

ROOT = Path(__file__).resolve().parents[1]


def _staging_module():
    spec = importlib.util.spec_from_file_location(
        "build_frontend_assets", ROOT / "scripts" / "build_frontend_assets.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_staging_copies_only_the_bundle(monkeypatch, tmp_path):
    module = _staging_module()
    dist = tmp_path / "dist"
    assets = tmp_path / "assets"
    dist.mkdir()
    (dist / "xenon-ui.js").write_text("bundle", encoding="utf-8")
    (dist / "cli.js").write_text("tsc output", encoding="utf-8")
    monkeypatch.setattr(module, "DIST", dist)
    monkeypatch.setattr(module, "ASSETS", assets)

    copied = module._stage()

    assert copied == ["xenon-ui.js"]
    assert sorted(p.name for p in assets.iterdir()) == ["xenon-ui.js"]


def test_staging_refuses_a_missing_bundle(monkeypatch, tmp_path):
    module = _staging_module()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "cli.js").write_text("tsc output", encoding="utf-8")
    monkeypatch.setattr(module, "DIST", dist)
    monkeypatch.setattr(module, "ASSETS", tmp_path / "assets")

    try:
        module._stage()
    except SystemExit as exc:
        assert "xenon-ui.js" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("staging a wheel without the bundle must fail")


def test_the_packaged_bundle_is_preferred(monkeypatch, tmp_path):
    package_dir = tmp_path / "site-packages" / "xenon"
    assets = package_dir / "frontend" / "assets"
    assets.mkdir(parents=True)
    bundle = assets / "xenon-ui.js"
    bundle.write_text("bundle", encoding="utf-8")
    monkeypatch.setattr(xenon_main, "__file__", str(package_dir / "main.py"))

    assert xenon_main._ts_frontend_script() == bundle


def test_a_checkout_falls_back_to_the_tsc_output(monkeypatch, tmp_path):
    package_dir = tmp_path / "repo" / "xenon"
    package_dir.mkdir(parents=True)
    dist = tmp_path / "repo" / "frontend" / "dist"
    dist.mkdir(parents=True)
    compiled = dist / "cli.js"
    compiled.write_text("tsc output", encoding="utf-8")
    monkeypatch.setattr(xenon_main, "__file__", str(package_dir / "main.py"))

    assert xenon_main._ts_frontend_script() == compiled


def test_pyproject_ships_the_assets() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "[tool.setuptools.package-data]" in text
    assert '"xenon.frontend" = ["assets/*.js"]' in text
