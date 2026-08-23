import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_migration(filename: str):
    path = Path(__file__).parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase9_recreates_asset_identity_index(monkeypatch) -> None:
    migration = _load_migration("20260823_0009_security_integrity.py")
    statements: list[str] = []
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert statements == [
        "ALTER TABLE assets DROP CONSTRAINT IF EXISTS asset_identity",
        "DROP INDEX IF EXISTS ux_asset_identity",
        "CREATE UNIQUE INDEX ux_asset_identity "
        "ON assets (canonical_symbol, chain_id, contract_address) NULLS NOT DISTINCT",
    ]
