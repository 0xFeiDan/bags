import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
if settings.is_production and not settings.master_encryption_key:
    raise RuntimeError("MASTER_ENCRYPTION_KEY is required in production")
if settings.auth_allow_additional_registration:
    raise RuntimeError("Bags Security V1 supports one administrator only; AUTH_ALLOW_ADDITIONAL_REGISTRATION must remain false")

app = FastAPI(
    title="Bags API",
    version="0.7.0",
    openapi_url=None if settings.is_production else f"{settings.api_v1_prefix}/openapi.json",
    docs_url=None if settings.is_production else "/docs",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 1024 * 1024:
        return JSONResponse(status_code=413, content={"detail": "请求体过大"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path == "/docs":
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src https://cdn.jsdelivr.net; style-src https://cdn.jsdelivr.net; "
            "img-src data: https://fastapi.tiangolo.com; frame-ancestors 'none'; base-uri 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, error: Exception):
    logger.exception("Unhandled API error", exc_info=error)
    return JSONResponse(status_code=500, content={"detail": "服务器暂时无法处理请求"})


app.include_router(router, prefix=settings.api_v1_prefix)
