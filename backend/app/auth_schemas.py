from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=1024)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    remember_me: bool = False


class TotpLoginRequest(BaseModel):
    challenge: str = Field(min_length=32, max_length=256)
    code: str = Field(pattern=r"^\d{6}$")


class SensitiveVerifyRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class TotpCodeRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class PasswordChangeRequest(BaseModel):
    new_password: str = Field(min_length=10, max_length=1024)


class EmailChangeRequest(BaseModel):
    new_email: EmailStr


class UserRead(BaseModel):
    id: UUID
    email: EmailStr
    two_factor_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class SessionRead(BaseModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    current: bool


class LoginResponse(BaseModel):
    authenticated: bool
    totp_required: bool = False
    challenge: str | None = None
    user: UserRead | None = None


class TotpSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class MessageResponse(BaseModel):
    message: str


class BootstrapStatusResponse(BaseModel):
    registration_available: bool


class EmailValue(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize(cls, value: EmailStr) -> EmailStr:
        return EmailStr(str(value).strip().lower())
