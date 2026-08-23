from fastapi import APIRouter, Depends

from app.api.dependencies import require_authenticated_request
from app.api.routes import accounts, assets, auth, binance, connections, cost_basis, dashboard, evm, exchanges, health, ledger, perp_dex, portfolios, raw_events, transfers

router = APIRouter(dependencies=[Depends(require_authenticated_request)])
router.include_router(health.router, tags=["system"])
router.include_router(auth.router, prefix="/auth", tags=["authentication"])
router.include_router(portfolios.router, prefix="/portfolios", tags=["portfolios"])
router.include_router(assets.router, prefix="/assets", tags=["assets"])
router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
router.include_router(connections.router, prefix="/connections", tags=["connections"])
router.include_router(raw_events.router, prefix="/raw-events", tags=["raw events"])
router.include_router(ledger.router, prefix="/ledger", tags=["ledger"])
router.include_router(binance.router, prefix="/binance", tags=["binance"])
router.include_router(exchanges.router, prefix="/exchanges", tags=["exchanges"])
router.include_router(perp_dex.router, prefix="/perp-dex", tags=["perp dex"])
router.include_router(evm.router, prefix="/evm", tags=["evm wallets"])
router.include_router(transfers.router, prefix="/transfers", tags=["transfer matching"])
router.include_router(cost_basis.router, prefix="/cost-basis", tags=["cost basis"])
router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
