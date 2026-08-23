from app.core.config import Settings


def test_cors_origins_accepts_comma_separated_environment_value(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:4173, http://localhost:4173",
    )

    settings = Settings(_env_file=None)

    assert settings.cors_origins == [
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ]
