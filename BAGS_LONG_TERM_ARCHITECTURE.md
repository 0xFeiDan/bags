Codex 提示词：Bags 长期可扩展架构重构方案

请对当前 Bags 项目进行一次面向长期扩展的架构重构设计与实施规划。

本次目标不是盲目拆微服务，也不是推倒重写，而是在：

* 保持现有功能可运行
* 保证历史数据不丢失
* 保证金融计算结果可验证
* 保证每个阶段可以独立回滚
* 保证未来可以继续增加交易所、钱包、股票、MT5、DeFi、银行、AI 分析、提醒等功能

的前提下，把现有 Bags 升级成：

模块化单体 + 异步任务系统 + 可插拔 Connector + 统一 Ledger + 可重算 Accounting/Valuation + 可逐步升级前端

⸻

一、当前项目背景

当前技术栈：

Frontend:
静态 HTML + 原生 JavaScript
Backend:
FastAPI
ORM / Migration:
SQLAlchemy
Alembic
Database:
PostgreSQL
Infrastructure:
Redis
Docker Compose

当前已有能力：

1. 单管理员登录
2. Cookie Session
3. CSRF
4. TOTP
5. 敏感操作二次验证
6. Binance 只读同步
7. Hyperliquid 公开钱包地址同步
8. EVM 多链钱包同步
9. RawEvent 原始事件
10. LedgerEvent / LedgerEntry 标准账本
11. 内部转账匹配
12. Average Cost
13. FIFO
14. LIFO
15. Realized PnL
16. Unrealized PnL
17. Dashboard
18. NAV
19. Risk
20. Cash Flow
21. Snapshot

⸻

二、当前主要问题

目前存在以下架构问题：

1. Sync / Backfill / Cost Calculation
   等重任务仍由 API Request 同步执行
2. Redis 已部署，但没有承担：
   - Queue
   - Job coordination
   - Cache
   - Task status
3. Binance / Hyperliquid / EVM
   存在重复的：
   - fetch
   - retry
   - cursor
   - normalize
   - persist
   逻辑
4. 前端大型 HTML / JS 文件中：
   - 页面
   - State
   - API
   - DOM
   - Business formatting
   高度混合
5. models.py / routes / services
   将继续快速膨胀
6. 后续可能加入大量不同功能，
   当前没有清晰的领域边界
7. 同步、账本、成本、估值、Dashboard
   之间仍存在较强耦合

⸻

三、本次重构的核心原则

必须遵守：

1. 不拆微服务

当前阶段继续使用：

FastAPI
+
PostgreSQL
+
Redis

保持一个主要 Backend Deployment。

采用：

Modular Monolith

而不是：

Microservices

只有未来出现明确性能或组织边界后，才考虑拆服务。

⸻

2. 前后端分离

明确：

Frontend = Presentation Layer

Backend = Source of Truth

前端禁止承担：

Cost Basis
Realized PnL
NAV
核心资产计算
权限判断
敏感数据处理

这些必须由 Backend 完成。

⸻

四、必须长期保持的金融原则

这些属于 Bags Architecture Invariants。

任何重构不得破坏：

1. RawEvent 原始数据不可被业务代码覆盖
2. RawEvent 必须可以追溯：
   source
   external_id
   timestamp
   raw_payload
3. Ledger 必须独立于具体 Exchange
4. Internal Transfer 不能产生 Realized PnL
5. Spot Cost Basis 与 Futures PnL 分离
6. Deposit / Withdrawal 不等于 Buy / Sell
7. Transfer 必须继承 Cost Basis
8. 所有 Exchange / Wallet Integration 必须只读
9. 禁止：
   - Trade
   - Withdrawal
   - Signing
   - Private Key
   - Seed Phrase
10. Hyperliquid 只读监控只保存：
    public wallet address
11. Exchange API Secret 必须继续加密存储
12. TOTP Secret 必须继续加密存储
13. Existing Database / History
    不得丢失
14. 不允许 Silent Rewrite 历史金融数据

⸻

五、目标顶级模块

将 Backend 最终演进为：

backend/app/
core/
identity/
portfolio/
ingestion/
ledger/
accounting/
valuation/
reporting/
jobs/
api/

⸻

六、各模块职责

core

负责与业务领域无关的基础设施：

config
database
logging
encryption
exceptions
time
decimal
common types

禁止放业务逻辑。

⸻

identity

负责：

Login
Logout
Session
Cookie
CSRF
TOTP
Sensitive Auth
Authorization Context

⸻

portfolio

负责：

Portfolio
Account
Asset
Connection Metadata
Portfolio Settings

注意：

Ledger 不要放进 portfolio。

⸻

ingestion

负责外部数据采集。

包括：

Binance
Hyperliquid
EVM
未来：
Bybit
Bitget
OKX
MT5
Stocks
Bank
DeFi

其职责只包括：

连接
鉴权检查
API请求
分页
Cursor
Rate Limit
Retry
RawEvent生成
数据质量告警

⸻

七、Connector 必须拆成 Collector + Normalizer

不要创建一个巨大的 Connector 类处理全部事情。

采用：

External Source
      ↓
Connector / Collector
      ↓
RawEvent
      ↓
Normalizer
      ↓
Ledger

例如：

BinanceCollector

负责：

fetch_balances
fetch_trades
fetch_income
fetch_deposits
fetch_withdrawals
fetch_positions
cursor
rate_limit
retry

然后：

BinanceNormalizer

负责：

Raw Binance Event
        ↓
Canonical LedgerEvent
        ↓
LedgerEntry

⸻

八、统一 Connector Interface

设计明确的基础接口。

例如概念上：

Connector

具备：

validate_connection()
validate_permissions()
collect()
get_cursor()
update_cursor()
health_check()

Collector 返回统一：

CollectionResult

至少包含：

raw_events
next_cursor
has_more
warnings
data_quality
rate_limit_info

不要要求所有交易所实现完全相同的业务 API。

可以通过 Capability 表示：

BALANCE
SPOT_TRADES
FUTURES_TRADES
POSITIONS
FUNDING
DEPOSITS
WITHDRAWALS
CHAIN_TRANSFERS

⸻

九、Ledger 独立成一级模块

目标数据流：

ingestion
    ↓
RawEvent
    ↓
Normalizer
    ↓
ledger
    ↓
accounting
    ↓
valuation
    ↓
reporting

Ledger 是整个系统的：

Canonical Financial Event Layer

不能依赖：

Dashboard
Frontend
Binance-specific logic

⸻

十、Ledger Event 类型

统一事件模型应能表达：

BUY
SELL
DEPOSIT
WITHDRAWAL
TRANSFER
FEE
FUNDING
REWARD
INTEREST
DIVIDEND
OPEN_POSITION
CLOSE_POSITION
MANUAL_ADJUSTMENT

具体是否全部立即实现，根据当前业务决定。

不要为了未来功能提前过度建模。

⸻

十一、数据分成三层

必须在架构文档中明确：

A. Source of Truth

RawEvent

⸻

B. Canonical Accounting Data

LedgerEvent
LedgerEntry

⸻

C. Derived Data

Cost Basis
PnL
NAV
Risk
Snapshot
Dashboard

Derived Data 应尽可能：

可重新计算。

⸻

十二、确定性要求

增加系统原则：

Same Input + Same Configuration + Same Algorithm Version = Same Output

对于：

Normalizer
Transfer Matching
Cost Basis
PnL
Valuation

尽可能保证确定性。

⸻

十三、算法版本

设计时考虑：

normalizer_version
accounting_version
cost_basis_version
valuation_version

不要求第一阶段立刻全部数据库化。

但必须设计出版本策略。

未来修复算法 Bug 后，要能够判断：

哪些结果由旧版本生成
哪些结果已重新计算

⸻

十四、Job System

这是本轮重构的最高优先级之一。

新增：

Persistent Job Table
+
Redis Queue
+
Worker

原则：

PostgreSQL 是 Job 状态事实来源。

Redis 只是：

Queue
Coordination
Wakeup
Lock

Redis 不可以成为唯一任务事实来源。

⸻

十五、Job 基础模型

设计类似：

jobs
id
parent_job_id
job_type
status
progress
payload
result
attempt
max_attempts
idempotency_key
error_code
error_message
created_at
queued_at
started_at
finished_at
heartbeat_at

状态至少考虑：

PENDING
QUEUED
RUNNING
SUCCEEDED
FAILED
RETRYING
CANCELLED

最终状态命名由现有项目风格决定。

⸻

十六、Job 必须支持 Parent / Child

例如：

FULL_PORTFOLIO_SYNC

可以产生：

├ Binance
├ Hyperliquid
├ Ethereum
├ Arbitrum
└ Base

UI 未来可以显示：

Full Sync 62%
Binance       Completed
Hyperliquid   Completed
Ethereum      Running 40%
Base          Pending

⸻

十七、Pipeline Job

不要做一个巨大：

SYNC_EVERYTHING

把所有工作塞在一个函数里。

拆成可重跑阶段：

COLLECT_RAW
NORMALIZE
MATCH_TRANSFERS
RECALCULATE_ACCOUNTING
REFRESH_VALUATION
GENERATE_SNAPSHOT

完整流程：

Collect
  ↓
RawEvent
  ↓
Normalize
  ↓
Ledger
  ↓
Transfer Matching
  ↓
Accounting
  ↓
Valuation
  ↓
Snapshot

⸻

十八、Pipeline 可以局部重跑

例如发现：

Cost Basis 算法 Bug

修复后不应该重新请求：

Binance
Ethereum RPC
Hyperliquid

应该能够：

Existing Ledger
      ↓
Recalculate Accounting
      ↓
Refresh Valuation

这必须成为设计目标。

⸻

十九、幂等性

必须重点设计。

所有可能重试的 Job 应支持：

idempotency_key

RawEvent 尽量根据外部稳定 ID 去重。

例如：

binance:
account:
trade_id

链上：

chain_id:
tx_hash:
log_index

禁止仅依赖：

timestamp + amount

生成唯一标识。

⸻

二十、Cursor

Cursor 必须：

成功持久化 RawEvent
+
成功完成当前阶段要求
        ↓
才能推进

禁止：

部分请求失败
↓
仍然推进Cursor

不同类型数据需要考虑独立 Cursor。

例如：

trade_cursor
funding_cursor
deposit_cursor
withdrawal_cursor
erc20_cursor
native_transfer_cursor

不要错误共用一个游标。

⸻

二十一、Accounting

独立模块：

accounting/

负责：

Transfer Matching
Cost Basis
Realized PnL
Accounting State

未来：

Average Cost
FIFO
LIFO

作为策略实现。

⸻

二十二、Spot 与 Futures 必须明确分离

Spot：

inventory
cost basis
realized pnl

Futures：

position
entry
mark price
exchange realized pnl
funding
fees

不得为了代码复用强行使用一套计算模型。

⸻

二十三、Valuation

独立模块：

valuation/

负责：

Price
FX
NAV
Unrealized PnL
Risk
Snapshot

⸻

二十四、PriceProvider

增加统一价格接口概念：

PriceProvider

未来可以实现：

BinancePriceProvider
CoinGeckoProvider
ChainlinkProvider
StockPriceProvider
ManualPriceProvider

Price Service 决定：

Primary
Fallback
Staleness
Confidence

避免业务代码直接到处调用某个行情来源。

⸻

二十五、Reporting

负责：

Dashboard API
Charts
Aggregation
Export
Alerts Presentation

Reporting 不应该：

直接访问 Binance
重新计算成本
解析 Raw API

应该消费：

Accounting
Valuation
Portfolio

的标准结果。

⸻

二十六、API Versioning

所有新接口统一：

/api/v1/

例如：

/api/v1/auth
/api/v1/portfolios
/api/v1/accounts
/api/v1/connections
/api/v1/jobs
/api/v1/dashboard
/api/v1/ledger

API Version 只属于：

HTTP Contract

内部 service 不要出现：

portfolio_v1_service.py

这种无意义版本命名。

⸻

二十七、模块依赖规则

请明确依赖方向。

建议：

identity
ingestion
    ↓
ledger
    ↓
accounting
    ↓
valuation
    ↓
reporting

portfolio 提供领域实体和上下文。

禁止出现：

valuation → BinanceConnector
reporting → Raw Binance API
Dashboard Route → 自己算 Cost Basis
accounting → frontend

⸻

二十八、防止循环依赖

请在设计阶段：

生成现有模块依赖图

并指出：

circular dependencies
cross-domain imports
shared god modules

目标是逐步减少。

⸻

二十九、RawEvent 保留策略

当前原则继续：

业务逻辑不得修改 RawEvent。

但不要把：

RawEvent 永远全部留在 PostgreSQL 热表

写死。

架构需要允许未来：

PostgreSQL Hot Data
      ↓
Archive
      ↓
Object Storage

如果未来归档：

数据库至少继续保留：

event id
hash
source
external id
timestamp
archive location

本次不需要立即实现归档。

⸻

三十、数据完整性

所有重要外部事件尽量保存：

source
source_account
external_id
timestamp
raw_payload
payload_hash
ingested_at

未来可以判断：

上游数据改变
重复数据
Normalizer版本变化

⸻

三十一、前端第一阶段不要推倒重写

当前：

大型原生 HTML / JavaScript

不要第一阶段全部改 React。

先抽离：

API Client
Auth State
Portfolio State
Job State
Shared Formatting
Page Modules

目标：

frontend/
  api/
  state/
  pages/
  components/
  utils/

在保持原前端正常工作的情况下逐步拆。

⸻

三十二、现代前端评估

请评估：

Vite + React + TypeScript

作为未来迁移目标。

重点说明：

迁移收益
开发成本
风险
需要重写多少代码
是否值得现在做

因为这是登录后的资产 Dashboard：

SEO不是核心需求

所以不要因为流行默认选择 Next.js。

如认为：

React + Vite + TypeScript

更合适，请明确说明。

⸻

三十三、前端禁止重复金融计算

前端只能做：

formatting
sorting
filtering
display-only aggregation

禁止把 Backend 返回的金融结果重新按另一套逻辑计算：

Cost Basis
Realized PnL
NAV
Portfolio Return

Backend 必须是唯一可信源。

⸻

三十四、未来新增功能分类

以后新增功能之前必须先判断：

类型 A

与 Bags 核心资产账本强相关：

新交易所
新链
股票
MT5
银行账户
DeFi
Staking

如果需要成为统一资产和 Ledger 的一部分：

作为 Bags Module。

⸻

类型 B

只消费 Bags 数据：

例如：

AI分析
策略分析
资产健康评分
外部提醒服务
自动报告

优先：

独立消费者
↓
调用稳定 Bags API

不要直接侵入 Ledger / Accounting。

⸻

类型 C

完全无关 Bags 核心：

独立 Repository

避免 Bags 成为万能项目。

⸻

三十五、未来交易功能原则

当前 Bags 是：

Read Only Portfolio System

任何未来：

Trade
Order
Transfer
Withdrawal
Signing

都不能偷偷加入现有 Connector。

如果未来真的加入交易能力：

必须：

独立设计
独立权限模型
独立安全审查

当前架构必须继续默认：

READ ONLY

⸻

三十六、数据库迁移原则

任何 schema 修改必须：

Alembic Migration

并且说明：

Existing Data Compatibility
Backfill
Rollback
Index Impact
Lock Risk

禁止：

drop old table
recreate everything

这种破坏式迁移。

⸻

三十七、禁止静默重算历史数据

如果重构涉及：

Ledger
Cost Basis
PnL
NAV

历史结果发生变化：

必须首先：

Compare
Report
Explain

不能自动覆盖后不告诉我。

⸻

三十八、Golden Dataset

请设计一套：

Golden Dataset

用于长期重构验证。

包含匿名化的：

Binance RawEvent
Hyperliquid RawEvent
EVM RawEvent

覆盖：

Buy
Sell
Fee
Deposit
Withdrawal
Transfer
Funding
Zero Balance
Partial Sell
Full Close
Reopen

⸻

三十九、Golden Output

对于固定输入，保存预期：

Ledger
Balances
Positions
Cost Basis
Realized PnL
Unrealized PnL
NAV

重构前后必须比较。

如果不是有意业务改变：

结果必须一致。

⸻

四十、每阶段必须结果等价

核心原则：

Structural Refactor ≠ Behavioral Change

例如：

重构前：

BTC Balance = X
BTC Cost = Y
Realized PnL = Z
NAV = N

重构后：

必须完全一致

除非修复了已确认 Bug。

如果结果改变：

必须明确：

Before
After
Reason
Expected?

⸻

四十一、测试策略

至少包括：

Unit Tests
Integration Tests
Golden Dataset Tests
Migration Tests
Job Retry Tests
Idempotency Tests

重点验证：

重复执行不会重复数据
任务失败后可恢复
Cursor 不会错误推进
Worker 重启不会破坏数据
同一 RawEvent 不会重复生成 Ledger
Accounting 可独立重算
Dashboard 重构前后结果一致

⸻

四十二、Job Failure 测试

测试：

Collect完成
Normalize失败

重试后：

不能重新产生重复 RawEvent。

测试：

Ledger完成
Accounting失败

必须能够从 Accounting 继续。

⸻

四十三、Worker 崩溃恢复

设计：

heartbeat
lease / lock
stale running job recovery

避免 Worker 崩溃后：

Job 永远 RUNNING

但不要过度设计分布式系统。

⸻

四十四、Redis 使用原则

Redis 可以用于：

Job Queue
Distributed Lock
Short-lived Cache
Rate Limit State

但禁止作为唯一来源保存：

Financial Data
Job Final State
Accounting Result
Cursor

这些必须持久化到 PostgreSQL。

⸻

四十五、并发原则

重点考虑：

同一 Connection

不能同时执行两个危险的：

full sync
backfill

导致数据冲突。

设计合理：

lock scope

例如：

connection_id
portfolio_id
job type

不要全局大锁。

⸻

四十六、Observability

不要做复杂监控平台。

但至少设计：

structured logging
job id
connection id
portfolio id
source
duration
records fetched
records normalized
warnings

方便未来排查：

为什么少了一笔交易？
为什么同步卡住？
为什么PnL变化？

⸻

四十七、Data Quality

Connector / Normalizer 应允许产生：

DataQualityWarning

例如：

MISSING_PRICE
PARTIAL_PAGE
CURSOR_GAP
UNKNOWN_ASSET
UNKNOWN_STABLECOIN
STALE_PRICE
UNSUPPORTED_EVENT
RATE_LIMIT_PARTIAL

不要所有异常都：

raise Exception

或者全部忽略。

⸻

四十八、Architecture Decision Records

建议新增：

docs/architecture/

并创建 ADR。

例如：

ADR-001 modular monolith
ADR-002 persistent jobs + Redis queue
ADR-003 RawEvent as immutable source evidence
ADR-004 canonical Ledger
ADR-005 read-only connectors

第一阶段可以先输出建议，不一定立即创建全部文档。

⸻

四十九、Phase 0：只做架构盘点

第一阶段：

不修改代码。

请输出：

1. Current Architecture
2. Current Directory Structure
3. Current Module Dependency Graph
4. Database Core Relationship
5. Current Data Flow
6. Sync Flow
7. Accounting Flow
8. Dashboard Flow
9. Current Architectural Risks
10. Target Architecture
11. Migration Risks

必须结合当前真实代码。

不要只根据这份提示词想象。

⸻

五十、现状盘点必须指出 God Files

找出：

过大 models
过大 routes
过大 services
大型 dashboard JS

以及：

responsibility mixing

但不要因为文件大就机械认定错误。

需要说明为什么需要拆。

⸻

五十一、Phase 1：领域目录边界

第一轮实际重构优先：

core
identity
portfolio
ingestion
ledger
accounting
valuation
reporting
jobs

目标：

主要是代码组织和依赖整理。

尽可能：

不改变数据库
不改变HTTP行为
不改变金融计算

所有测试继续通过。

⸻

五十二、Phase 2：Job System

新增：

Job Model
Worker
Redis Queue
/api/v1/jobs

第一批只迁一个风险较低任务，例如：

Generate Snapshot

验证 Job Framework 稳定后，再迁同步。

⸻

五十三、Phase 3：异步 Sync

逐个迁移：

Hyperliquid
Binance
EVM

建议先 Hyperliquid。

因为：

公开地址
逻辑较简单
风险较低

每迁一个 Connector：

必须验证数据结果完全一致。

⸻

五十四、Phase 4：Connector 抽象

建立：

Connector
Collector
Normalizer
Cursor
Capability
CollectionResult
DataQualityWarning

不要强行一次迁完。

⸻

五十五、Phase 5：Accounting Pipeline

把：

Transfer Matching
Cost Basis
PnL

形成独立可重跑流程。

支持：

从已有Ledger重算

而不需要重新请求外部 API。

⸻

五十六、Phase 6：Valuation

建立统一：

PriceService
PriceProvider
NAV
Risk
Snapshot

并清理 Dashboard 中重复计算。

⸻

五十七、Phase 7：Frontend Boundary

先在原生前端中抽：

api client
auth state
portfolio state
job state
shared components

然后再提交：

React + TypeScript migration proposal

不要直接重写。

⸻

五十八、Phase 8：可选 React Migration

只有在前面模块化完成后，再评估。

如果迁移：

优先考虑：

Vite
React
TypeScript

采用逐页迁移策略。

不要一次性 Rewrite。

⸻

五十九、每个 Phase 必须输出

每一阶段开始前告诉我：

目标
为什么现在做
涉及目录
涉及数据库
涉及API
预计风险

⸻

完成后告诉我：

修改文件
Migration
Tests
Before / After
Known Risks
Rollback

⸻

六十、每阶段必须有 Rollback

例如：

Code Refactor

git revert

Schema Migration

Alembic downgrade

New Job System

需要能够临时恢复：

legacy synchronous path

直到新系统验证通过。

不要第一天就删除旧实现。

⸻

六十一、Legacy Path

迁移期间允许：

feature flag

例如：

ASYNC_SYNC_ENABLED=false

验证新 Pipeline 后再切换。

不要永久保留双实现。

⸻

六十二、废弃代码流程

任何旧代码不要立即删除。

顺序：

新实现
↓
Parallel Validation
↓
Switch
↓
Observation
↓
Remove Legacy

删除前必须确认：

No Callers
Tests Pass
Production Behavior Equivalent

⸻

六十三、性能原则

目前不要：

Kubernetes
Kafka
Event Sourcing Platform
Distributed Microservices
Service Mesh

除非当前代码实际证明需要。

优先：

PostgreSQL
Redis
Worker
FastAPI

够用。

⸻

六十四、未来可拆点

架构需要让未来以下模块具备独立拆分可能：

Worker
Pricing
Realtime WebSocket
Reporting

但现在不要拆 Deployment。

⸻

六十五、实时能力

现在不要求全面实现 WebSocket。

但架构不要阻止未来：

/ws/jobs
/ws/portfolio
/ws/prices

用于：

同步进度
实时PnL
价格
提醒

⸻

六十六、安全要求必须保持

重构不能降低已有：

Cookie Session
HttpOnly
Secure
CSRF
TOTP
Sensitive Re-auth
Argon2
Encrypted Exchange Secret

安全能力。

⸻

六十七、不得扩大 API 权限

Connector 抽象过程中：

禁止为了统一接口增加：

place_order()
withdraw()
sign()
transfer()

当前 Connector Base Interface：

必须明确是：

READ ONLY

⸻

六十八、数据库模型不要过度抽象

不要创建：

UniversalEverythingEntity
GenericDataModel

这种难以理解的抽象。

资产领域需要保持：

明确模型
明确关系
明确约束

⸻

六十九、Repository Pattern

不要为了“架构标准”强制所有 CRUD 都写 Repository。

只有在：

跨模块数据访问
复杂查询
需要测试隔离

明显有价值的地方使用。

避免无意义包装 SQLAlchemy。

⸻

七十、Service Layer

Service 应表达：

业务动作

例如：

sync_connection
normalize_raw_events
calculate_cost_basis
generate_snapshot

不要做：

UserService.get_user_by_id()

这种纯 CRUD 包装，除非确实有领域价值。

⸻

七十一、优先简单

每个新 abstraction 都回答：

它解决当前哪个实际问题？

如果回答只是：

未来可能有用

但当前无明确需要：

不要加入。

⸻

七十二、最终第一阶段交付

现在先不要修改任何代码。

请阅读当前 Bags 整个项目后，输出：

A. Current State

现有目录
模块职责
关键类和函数
数据库核心表
API入口
同步入口
Accounting入口
Dashboard入口

B. Current Architecture Diagram

使用清晰文本图。

C. Current Data Flow

例如：

Binance
↓
...
↓
Dashboard

分别说明：

Binance
Hyperliquid
EVM

D. Current Dependency Problems

列出：

跨模块调用
重复逻辑
God Files
耦合
循环依赖
难以测试部分

E. Target Architecture

给出建议目录结构。

F. Target Data Flow

External
↓
Collector
↓
RawEvent
↓
Normalizer
↓
Ledger
↓
Accounting
↓
Valuation
↓
Reporting

G. Job Architecture

详细解释：

API
Job DB
Redis
Worker
Pipeline
Retry
Idempotency
Progress
Failure Recovery

H. Migration Plan

至少：

Phase 0
Phase 1
Phase 2
...

每个 Phase 包含：

Goal
Files
DB Changes
API Changes
Compatibility
Tests
Rollback
Risk

I. Frontend Migration Plan

比较：

继续原生JS模块化
vs
React + TypeScript

明确推荐方案。

J. Keep / Migrate / Deprecate

生成：

保留什么
迁移什么
未来废弃什么

K. Risks

指出重构最可能破坏：

历史数据
成本
PnL
Transfer
Cursor
Sync
Dashboard

的区域。

L. Confirmation Gate

最后停止。

不要修改代码。

明确告诉我：

第一阶段设计已经完成，等待确认后再进入 Phase 1。

⸻

七十三、非常重要

整个重构过程中：

不以“代码更漂亮”为成功标准。

成功标准是：

更容易增加数据源
更容易增加新资产类别
重任务不会阻塞API
同步可恢复
数据可追溯
Accounting可重算
结果可验证
历史兼容
模块边界清晰

⸻

七十四、最终架构原则总结

请将以下原则作为 Bags 的长期 Architecture Rules：

1. RawEvent is immutable source evidence.
2. Ledger is canonical financial truth.
3. Derived data is rebuildable.
4. Same input + same algorithm version
   = same output.
5. Frontend is presentation.
6. Backend is source of truth.
7. Connectors are read-only.
8. Collection and normalization are separate.
9. Jobs are persistent.
10. Redis is coordination, not truth.
11. Structural refactor should preserve behavior.
12. Architecture grows by modules before services.
