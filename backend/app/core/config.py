from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Bags API"
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+psycopg://bags:bags@localhost:5432/bags"
    redis_url: str = "redis://localhost:6379/0"
    master_encryption_key: str | None = None
    auth_session_cookie_name: str = "bags_session"
    auth_csrf_cookie_name: str = "bags_csrf"
    auth_session_days: int = 7
    auth_remember_days: int = 30
    auth_login_challenge_minutes: int = 5
    auth_sensitive_minutes: int = 10
    auth_totp_issuer: str = "Bags"
    auth_cookie_secure: bool = False
    auth_allow_additional_registration: bool = False
    auth_bootstrap_token: str | None = None
    auth_trust_proxy_headers: bool = False
    binance_spot_base_url: str = "https://api.binance.com"
    binance_usdm_base_url: str = "https://fapi.binance.com"
    binance_coinm_base_url: str = "https://dapi.binance.com"
    binance_request_timeout_seconds: float = 15.0
    binance_max_retries: int = 3
    bybit_base_url: str = "https://api.bybit.com"
    bybit_request_timeout_seconds: float = 15.0
    bybit_max_retries: int = 3
    bitget_base_url: str = "https://api.bitget.com"
    bitget_request_timeout_seconds: float = 15.0
    bitget_max_retries: int = 3
    hyperliquid_base_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_request_timeout_seconds: float = 15.0
    hyperliquid_max_retries: int = 3
    evm_ethereum_rpc_url: str | None = None
    evm_arbitrum_rpc_url: str | None = None
    evm_base_rpc_url: str | None = None
    evm_bsc_rpc_url: str | None = None
    evm_optimism_rpc_url: str | None = None
    evm_polygon_rpc_url: str | None = None
    evm_request_timeout_seconds: float = 15.0
    evm_max_retries: int = 3
    evm_log_chunk_size: int = 5_000
    # Keep automatic scans within the native transaction scan guarantee. Older
    # history must be requested explicitly in bounded backfills.
    evm_default_lookback_blocks: int = 2_000
    evm_max_block_span: int = 500_000
    evm_native_scan_max_blocks: int = 2_000
    evm_max_token_contracts: int = 250
    evm_confirmations: int = 12
    # Zerion is deliberately disabled until an operator supplies a server-side
    # key. Defaults follow the lower quota advertised by the live API response;
    # provider response headers can tighten them further during a run.
    zerion_enabled: bool = False
    zerion_api_key: str | None = None
    zerion_base_url: str = "https://api.zerion.io"
    zerion_request_timeout_seconds: float = 20.0
    zerion_max_retries: int = 0
    zerion_requests_per_second_limit: int = 1
    zerion_daily_request_limit: int = 300
    zerion_max_requests_per_run: int = 3
    zerion_min_sync_interval_seconds: int = 900
    # Keep 10% of the observed 300-request daily quota as a diagnostic reserve.
    zerion_daily_request_budget: int = 270
    transfer_match_window_hours: int = 72
    transfer_exact_hash_window_days: int = 30
    transfer_amount_candidate_tolerance: float = 0.05
    transfer_amount_close_tolerance: float = 0.001
    transfer_time_close_minutes: int = 30
    transfer_auto_score: int = 90
    transfer_review_score: int = 70
    transfer_match_max_events: int = 10_000
    valuation_max_age_hours: int = 24
    # Accept an operator-friendly comma-separated env value. NoDecode is
    # required because pydantic-settings otherwise tries JSON decoding before
    # the field validator gets a chance to split the string.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
