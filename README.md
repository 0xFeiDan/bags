# Bags backend — Security V1

The backend provides the immutable raw-event store, normalized multi-account ledger,
canonical asset identity, account/connection registry, balance snapshots, and a
Docker runtime. Phase 2 adds a GET-only Binance connector for Spot, USD-M and COIN-M.
Phase 3 adds Hyperliquid through its public, read-only information API.
Security V1 adds a single-administrator login boundary for the shared personal ledger.
Phase 8 adds Bybit V5 and Bitget V2 read-only synchronization plus the browser-based
account connection and credential-rotation workbench at `/connections.html`.

## Run locally

1. Copy `.env.example` to `.env` and set a strong `POSTGRES_PASSWORD`.
2. Generate a 32-byte `MASTER_ENCRYPTION_KEY` and a separate random `AUTH_BOOTSTRAP_TOKEN`.
3. Run `docker compose up --build`; the API binds to `127.0.0.1` by default.
4. Start the UI with `node preview-server.js`, open `http://127.0.0.1:4173`, and initialize the only administrator with the bootstrap token. Remove `AUTH_BOOTSTRAP_TOKEN` and restart the API immediately afterwards.

After signing in, verify the current password (and TOTP when enabled) on the Security page before manually creating portfolios, accounts, assets, raw events, ledger records, or balance snapshots.

The static UI remains at `http://127.0.0.1:4173` when run with `node preview-server.js`.
The dashboard, login page, and security page use cookie authentication against
`/api/v1`. API docs remain available in development at `http://127.0.0.1:8000/docs`.

## Ubuntu one-shot startup

On an Ubuntu host with Docker, Docker Compose v2, OpenSSL, and curl installed:

```bash
chmod +x deploy/ubuntu-bootstrap.sh
./deploy/ubuntu-bootstrap.sh
```

The script creates `.env` without overwriting existing secrets, generates the
database password, AES-256-GCM key, and first-admin bootstrap token when needed,
runs migrations, starts PostgreSQL/Redis/API, and builds a restricted frontend
container bound to localhost. Use the SSH tunnel printed by the script. After
creating the administrator, run the same script once more; it detects the user and
removes `AUTH_BOOTSTRAP_TOKEN` automatically.

For a public production domain, first point a DNS-only A record at the server and
open TCP 80/443 in the cloud security group. Then run as root:

```bash
./deploy/ubuntu-production.sh example.com 203.0.113.10
```

For every later production update, pull `main` and run the production script again:

```bash
cd /home/ubuntu/bags
git pull --ff-only origin main
sudo ./deploy/ubuntu-production.sh nmbags.org 43.156.30.192
```

Do not use `docker compose up -d --build` as the complete deployment command. The
Compose file rebuilds PostgreSQL, Redis, and the API, while `bags-ui` is a hardened
standalone container rebuilt and replaced by `ubuntu-bootstrap.sh`, which the
production script invokes automatically.

The production script keeps PostgreSQL, Redis, the API, and the UI on localhost,
switches authentication to secure same-origin cookies, installs the official Ubuntu
Caddy package, and enables automatic HTTPS. It binds Caddy to the primary cloud
interface instead of a Tailscale address, so an existing Tailscale HTTPS listener can
remain active. Keep Cloudflare in DNS-only mode during setup; if proxying is enabled
later, use Full (strict), never Flexible.

## Security V1

- The first registration requires `AUTH_BOOTSTRAP_TOKEN`, creates the only
  administrator, and permanently closes public registration. This release is a
  single-administrator personal ledger; the server refuses multi-user registration.
- Passwords use Argon2id. Session and login-challenge cookies contain 256-bit random
  values; only SHA-256 hashes are stored in PostgreSQL.
- The authentication cookie is `HttpOnly` and `SameSite=Lax`. Production forces
  `Secure`; deploy the API and UI through HTTPS on the same site.
- Every authenticated state-changing API request requires the matching
  `X-CSRF-Token` header. The supplied UI sends it automatically.
- Login failures are limited independently by normalized email and client IP. The
  sixth failure in a one-minute window waits 30 seconds, then uses exponential delay.
- Password, email, TOTP, API credential, and other-device operations require a recent
  password check. When TOTP is enabled, that check also requires a current code.
- TOTP secrets and exchange API credentials use AES-256-GCM with
  `MASTER_ENCRYPTION_KEY`. Never supply a wallet private key or mnemonic.
- Production disables OpenAPI docs and emits HSTS. Configure the reverse proxy to
  redirect all HTTP traffic to HTTPS before enabling production.

Security endpoints live under `/api/v1/auth`. Apply migrations with
`alembic upgrade head`; revision `20260823_0004` creates `users`, `sessions`,
`login_challenges`, `login_attempts`, and `security_events`.

## Binance Phase 2

Create a Binance connection through `POST /api/v1/connections`, then start a sync with:

`POST /api/v1/binance/connections/{connection_id}/sync`

The request can select `spot`, `usdm`, and `coinm`, plus the historical markets to scan:

```json
{
  "products": ["spot", "usdm", "coinm"],
  "spot_symbols": ["BTCUSDT", "ETHUSDT"],
  "usdm_symbols": ["BTCUSDT", "ETHUSDT"],
  "coinm_pairs": ["BTCUSD", "ETHUSD"],
  "history_start": "2026-05-01T00:00:00Z",
  "history_end": "2026-08-23T00:00:00Z"
}
```

Binance requires a market identifier for every Spot, USD-M, and COIN-M trade-history
query. For Spot, Bags now reads balances first, discovers supported markets from the
current non-zero holdings, and persists those markets as connection scopes. Persisted
scopes continue to sync after a holding reaches zero. Assets that were already closed
before discovery are intentionally not exhaustively scanned; add those markets
manually through the connection page or the Spot symbol endpoints below. If a futures
market list is omitted, Bags only derives markets from current positions and returns a
coverage warning. A first sync defaults to 90 days; later default syncs use the saved
per-market cursor with a five-minute overlap. Explicit dates perform a backfill.
Binance currently limits USD-M trades to six months and USD-M income to three months;
the sync result reports these source limitations.

Spot, USD-M, and COIN-M remain separate internal ledger scopes so their balances,
positions, cursors, and raw sources stay auditable, but they are exposed in the API
and dashboard as one Binance account. Use `GET /api/v1/accounts?include_internal=true`
only for diagnostics that need the internal product-ledger records.

Useful read endpoints:

- `GET /api/v1/binance/connections/{connection_id}/sync-runs`
- `GET /api/v1/binance/connections/{connection_id}/spot-symbols`
- `POST /api/v1/binance/connections/{connection_id}/spot-symbols`
- `PATCH /api/v1/binance/connections/{connection_id}/spot-symbols/{scope_id}`
- `GET /api/v1/binance/accounts/{account_id}/positions`
- `GET /api/v1/raw-events`
- `GET /api/v1/ledger/events`

The connector exposes HTTP GET only. It checks Binance API restrictions before a sync
and refuses to proceed when withdrawal permission is enabled.

## Hyperliquid Phase 3

Create a `perp_dex` account with provider `hyperliquid` and place the public wallet
address in both `external_account_id` and `address`. Then create a connection through
`POST /api/v1/connections`. Hyperliquid does not require an API key for these public
account reads, so `api_key` may be omitted for this provider. A wallet private key or
trading API wallet must never be supplied.

Start a synchronization with:

`POST /api/v1/perp-dex/connections/{connection_id}/sync`

```json
{
  "history_start": "2026-05-01T00:00:00Z",
  "history_end": "2026-08-23T00:00:00Z",
  "include_spot": true
}
```

The sync stores:

- Perpetual account equity, withdrawable balance, margin usage and total notional.
- Open positions with side, signed size, entry/mark/liquidation price, leverage and
  unrealized PnL.
- Hyperliquid Spot token balances.
- Historical fills with closed PnL and fees separated into ledger entries.
- Funding payments.
- Deposits, withdrawals and supported internal/sub-account transfers without turning
  transfers into realized PnL.
- Every source response as an immutable raw event before normalization.

Useful read endpoints:

- `GET /api/v1/perp-dex/connections/{connection_id}/sync-runs`
- `GET /api/v1/perp-dex/accounts/{account_id}/equity`
- `GET /api/v1/perp-dex/accounts/{account_id}/positions`

The Hyperliquid connector is deliberately restricted to `POST /info` query types. It
cannot call exchange actions, sign transactions, place orders or withdraw funds. The
generic `/perp-dex` route is ready for a later Lighter connector; Phase 3 follows the
project roadmap and implements Hyperliquid only.

## EVM Wallet Phase 4

Phase 4 supports public-address, read-only wallet synchronization for Ethereum,
Arbitrum, Base, BNB Chain, Optimism, and Polygon. Configure the matching
`EVM_*_RPC_URL` value, create a `wallet` account with provider `evm`, and start a sync:

`POST /api/v1/evm/accounts/{account_id}/sync`

```json
{
  "from_block": 42000000,
  "to_block": 42005000,
  "token_contracts": ["0x..."],
  "transaction_hashes": ["0x..."]
}
```

Omit the block range for incremental synchronization. The first automatic sync scans
the configured lookback and later runs resume from `last_synced_block`. ERC-20
`Transfer` logs are split into approximately 5,000-block ranges. Any failed range is
returned in `failed_ranges_json`, makes the run partial, and prevents the normal
cursor from advancing.

The same public address may be registered once per configured EVM chain. The
connection page groups those chain accounts into one wallet manager where a display
name, additional networks, active state, precise backfills, and persistent token
contracts can be maintained. Deactivation only stops future reads and never deletes
raw events or ledger history. Persistent contracts are included in ordinary later
syncs without having to submit `token_contracts` again.

Each relevant transaction stores the original transaction, receipt, and wallet-facing
transfer logs before normalization. Native value, ERC-20 movements, and receipt-
verified gas (`gasUsed * effectiveGasPrice`) become separate ledger entries. Balances
are read at the confirmed target block, and assets are identified by chain ID plus
contract address.

Standard EVM JSON-RPC cannot enumerate native transactions by wallet address. Bags
therefore scans native transactions block-by-block, bounded by
`EVM_NATIVE_SCAN_MAX_BLOCKS`; older native-only history must be backfilled in bounded
ranges or supplied through `transaction_hashes`. ERC-20 history can use the larger log
range. The API accepts public addresses only and its RPC client cannot sign or send a
transaction.

Useful endpoints:

- `POST /api/v1/evm/accounts/{account_id}/sync`
- `GET /api/v1/evm/accounts/{account_id}/sync-runs`
- `GET /api/v1/evm/accounts/{account_id}/tracked-contracts`
- `POST /api/v1/evm/accounts/{account_id}/tracked-contracts`
- `PATCH /api/v1/evm/accounts/{account_id}/tracked-contracts/{contract_id}`
- `PATCH /api/v1/accounts/{account_id}`
- `GET /api/v1/accounts/{account_id}/balance-snapshots`
- `GET /api/v1/raw-events?account_id={account_id}`
- `GET /api/v1/ledger/events`

## Transfer Matching Phase 5

Run matching after exchange and wallet synchronization:

`POST /api/v1/transfers/portfolios/{portfolio_id}/match`

The engine considers posted `withdraw` / `transfer_out` events as sources and
`deposit` / `transfer_in` events as destinations. Each candidate receives the
documented 0-100 score:

- Exact normalized transaction hash: 60 points.
- Canonical asset match: 20 points.
- Amount difference below 0.1%, including the reported same-asset fee case: 10 points.
- Time difference below 30 minutes: 10 points.

Scores from 90-100 are automatically grouped, 70-89 require review, and lower
plausible candidates remain unmatched for manual confirmation. A ledger event can
belong to only one active Transfer Group; competing candidates are never allowed to
reuse it automatically.

Transfer Groups preserve source/destination accounts, assets and quantities,
reported fee amount and fee asset, transaction hash, exchange identifiers,
timestamps, confidence, and matching method. Matching never deletes raw evidence or
rewrites the original ledger event type. Phase 6 consumes active Transfer Groups to
carry cost basis without treating internal movement as realized PnL.

Useful endpoints:

- `POST /api/v1/transfers/portfolios/{portfolio_id}/match`
- `GET /api/v1/transfers/portfolios/{portfolio_id}/runs`
- `GET /api/v1/transfers/portfolios/{portfolio_id}/candidates`
- `GET /api/v1/transfers/portfolios/{portfolio_id}/groups`
- `POST /api/v1/transfers/candidates/{candidate_id}/confirm`
- `POST /api/v1/transfers/manual`
- `POST /api/v1/transfers/groups/{group_id}/unmatch`
- `POST /api/v1/transfers/candidates/{candidate_id}/ignore`

Manual confirmation, unmatching, and ignoring are accounting-sensitive operations.
They require the recent password/TOTP authorization window from Security V1.

## 统一成本引擎 Phase 6

Phase 6 不读取交易所返回的汇总 PnL 作为最终结果，而是从不可变 Ledger
重新生成独立的 `CostBasisRun`。每次运行都会保留自己的 Cost Lots、Lot
消耗记录、已实现盈亏和账户持仓成本快照；旧运行不会被覆盖。默认口径为
`average_cost`，也可在运行请求中选择 `fifo` 或 `lifo`。Cost Lot 数据结构
已保留原始取得时间、来源事件和父 Lot，因此后续可增加 Specific
Identification，而不需要改写历史账本。

核心计算规则：

- 买入成本 = 支付对价 + 可估值的外部手续费；若手续费从买入资产扣除，
  Lot 数量使用净到账数量。
- 已实现盈亏 = 净处置收入 - 被消耗 Lot 成本。
- 未实现盈亏 = 当前价格 × 数量 - Effective Cost。
- Effective Cost 优先使用独立的 Manual Override，否则使用 Calculated Cost。
- 已确认/自动匹配的 Transfer Group 只搬移 Lot，不生成 Spot Realized PnL；
  部分转账按成本法拆分成本，转账手续费单独记为 Fee Expense。
- 合约事件写入 `derivative` PnL 分类，永远不混入 Spot Cost Lots。
- 无法确定成本或价格时返回 `partial` 和明确 warning，不会擅自按零成本制造盈利。

建议调用顺序：先完成交易所/钱包同步，再运行 Phase 5 转账匹配，然后写入
必要的历史价格或人工成本，最后重算成本：

```text
POST /api/v1/cost-basis/prices
POST /api/v1/cost-basis/overrides
POST /api/v1/cost-basis/portfolios/{portfolio_id}/calculate
```

计算请求示例：

```json
{"method":"average_cost","as_of":"2026-08-23T00:00:00Z"}
```

主要查询接口：

- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/runs`
- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/assets`
- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/positions`
- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/lots`
- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/consumptions`
- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/realized-pnl`
- `GET /api/v1/cost-basis/portfolios/{portfolio_id}/pnl-summary`
- `POST /api/v1/cost-basis/pnl-adjustments`

价格、成本覆盖和 PnL Adjustment 都是只追加的独立记录，创建操作需要
Security V1 的近期密码/TOTP 验证。Phase 6 只提供手工/外部系统写入的 USD
价格入口；自动行情源可在后续阶段继续扩展。

## Dashboard Phase 7

Phase 7 将最新 Cost Basis Run、账户权益快照、当前合约仓位和已匹配转账
聚合为真实 Dashboard 数据。总净值只在所有非零资产都能估值时返回；如果
缺少价格、成本运行或负债估值能力，接口会返回明确的质量告警，不会用零值
掩盖未知净值。

每日组合快照保存 Spot、Perp、DeFi、现金、负债、已实现/未实现盈亏、费用、
Funding 和外部资金流。区间投资盈亏遵循：

```text
Investment PnL = Ending NAV - Starting NAV - Net External Flow
```

已确认的内部 Transfer Group 不进入外部资金流，因此不会制造虚假的收益或
亏损。Spot 成本与合约 PnL 继续分离；风险页单独展示 Gross Long、Gross
Short、Net Exposure 和逐仓位详情。

建议工作流：完成同步和转账匹配，运行一次 Phase 6 成本计算，再创建当前
Dashboard 快照。首次部署也可按日回填历史快照：

```text
POST /api/v1/dashboard/portfolios/{portfolio_id}/snapshots
POST /api/v1/dashboard/portfolios/{portfolio_id}/snapshots/backfill
```

主要查询接口：

- `GET /api/v1/dashboard/portfolios/{portfolio_id}/summary`
- `GET /api/v1/dashboard/portfolios/{portfolio_id}/snapshots`
- `POST /api/v1/dashboard/portfolios/{portfolio_id}/snapshots`
- `POST /api/v1/dashboard/portfolios/{portfolio_id}/snapshots/backfill`

现有首页以及资产、账户、账本、盈亏、风险敞口和分析页已接入这些接口；加载、
空数据、部分估值和请求失败都有独立状态。静态演示数据不再作为 Dashboard
财务数字显示。

## Bybit / Bitget Phase 8

Open `/connections.html` after signing in to create a Portfolio, add an encrypted
read-only connection, verify the administrator password/TOTP, and immediately run
the first synchronization. The page supports Binance, Bybit, Bitget, Hyperliquid,
and configured EVM wallets; it never reads a stored secret back into the browser.
It also provides inline management for grouped EVM wallet networks/contracts and
Binance Spot symbol scopes. Every mutation requires a fresh sensitive-operation
verification, and disabling a scope preserves existing accounting history.

Bybit uses the V5 Unified API:

- `POST /api/v1/exchanges/bybit/connections/{connection_id}/sync`
- `GET /api/v1/exchanges/bybit/connections/{connection_id}/sync-runs`

It verifies `/v5/user/query-api` reports `readOnly=1`, then reads Unified balances,
Linear/Inverse positions, Spot executions, transaction logs, deposits, and
withdrawals. V5 history is bounded to two years and the result reports scope limits.

Bitget uses the V2 Classic API so ordinary Spot and Futures accounts remain
supported:

- `POST /api/v1/exchanges/bitget/connections/{connection_id}/sync`
- `GET /api/v1/exchanges/bitget/connections/{connection_id}/sync-runs`

It requires API key, secret, and passphrase, reads the account `authorities`, and
fails closed when the permission list is missing or contains any write, transfer,
withdrawal, or unknown authority. Spot and each Futures product are stored in
separate accounts so same-asset balances cannot overwrite each other. Bitget fills
and account bills normally cover the latest 90 days; older records require a later
import workflow.

Both clients expose HTTP GET only. Every response used by the normalizer is first
stored as a raw event; credentials are AES-256-GCM encrypted and never logged or
returned by the API.

## Safety invariants already enforced

- Raw source events only have read/create APIs; they cannot be updated or deleted.
- API secrets are AES-256-GCM encrypted before persistence, and never returned by the API.
- Connections declare read-only access only. Binance-reported withdrawal or trading
  permission blocks synchronization.
- Ledger rows use positive quantities with explicit debit/credit direction, avoiding
  ambiguous signed-balance interpretation.
- Normalized/manual data is separate from the raw payload, so later calculation rules
  can be rebuilt without destroying evidence.

## Test

From `backend`, install the declared dependencies and run `pytest`.
