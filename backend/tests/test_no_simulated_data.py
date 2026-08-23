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
