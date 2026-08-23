from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_UI_FILES = (
    "index.html",
    "dashboard.js",
    "connections.html",
    "connections.js",
    "login.html",
    "security.html",
    "auth.js",
    "auth-pages.css",
    "preview-server.js",
)


def test_fresh_database_does_not_create_sample_portfolios(client):
    response = client.get("/api/v1/portfolios")

    assert response.status_code == 200
    assert response.json() == []


def test_production_ui_contains_no_known_sample_records_or_prefilled_names():
    shipped_ui = "\n".join(
        (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in PRODUCTION_UI_FILES
    )
    forbidden_markers = (
        "$1.285M",
        "$825,173",
        "$359,789",
        "$438,262",
        "$283,642",
        "$203,269",
        "3.5200",
        "71.4800",
        "$286,730",
        "$406,515",
        "$119,785",
        "TRF_000184",
        "LOT_BTC_",
        "所有数值均为静态示例",
        "UI 原型",
        'value="Personal"',
        'value="Binance Main"',
        'value="Binance Read-only"',
        'value="Bybit Main"',
        'value="Bybit Read-only"',
        'value="Bitget Main"',
        'value="Bitget Read-only"',
        'value="Main Wallet"',
    )

    assert not [marker for marker in forbidden_markers if marker in shipped_ui]


def test_sync_and_first_dashboard_load_build_real_accounting_snapshots():
    connections_script = (PROJECT_ROOT / "connections.js").read_text(encoding="utf-8")
    dashboard_script = (PROJECT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "await refreshPortfolioSnapshot(portfolioId, run);" in connections_script
    assert "await refreshPortfolioSnapshot(account.portfolio_id, run);" in connections_script
    assert "async function loadPortfolioSummary(portfolioId)" in dashboard_script
    assert "`/dashboard/portfolios/${portfolioId}/snapshots`" in connections_script
    assert "`/dashboard/portfolios/${portfolioId}/snapshots`" in dashboard_script


def test_evm_connection_wizard_supports_one_address_on_multiple_chains():
    connections_markup = (PROJECT_ROOT / "connections.html").read_text(encoding="utf-8")
    connections_script = (PROJECT_ROOT / "connections.js").read_text(encoding="utf-8")

    assert 'type="checkbox" name="evmChain"' in connections_script
    assert "function selectedEvmChains()" in connections_script
    assert "async function createEvmAccountsAndSync(portfolioId)" in connections_script
    assert "for (const chain of chains)" in connections_script
    assert "chain_id: chain.key" in connections_script
    assert "网络（可多选）" in connections_markup
