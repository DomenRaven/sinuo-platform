from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SINUO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: str = "development"
    database_url: str = "sqlite:///./var/sinuo.db"
    theory_root: str = ""
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    model_enabled: bool = False
    model_name: str = ""
    model_name_reasoning: str = "deepseek-v4-pro"
    deepseek_api_key: SecretStr | None = None
    max_specialists: int = Field(default=3, ge=1, le=3)
    max_parallel_tasks: int = Field(default=2, ge=1, le=3)
    run_timeout_seconds: float = Field(default=60, gt=0, le=300)
    public_url: str = "http://127.0.0.1:8000"
    allowed_origins: str = "http://127.0.0.1:8000"
    session_idle_seconds: int = Field(default=28800, ge=60)
    session_absolute_seconds: int = Field(default=86400, ge=60)
    argon2_time_cost: int = Field(default=3, ge=3)
    argon2_memory_kib: int = Field(default=65536, ge=19456)
    argon2_parallelism: int = Field(default=4, ge=1)
    argon2_hash_len: int = Field(default=32, ge=16)
    argon2_salt_len: int = Field(default=16, ge=16)
    login_rate_limit_attempts: int = Field(default=5, ge=1)
    login_rate_limit_window_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def validate_model_configuration(self):
        if self.model_enabled and (
            not self.model_name.strip()
            or self.deepseek_api_key is None
            or not self.deepseek_api_key.get_secret_value().strip()
        ):
            raise ValueError("Enabled model requires server-side model name and API key")
        return self

    @model_validator(mode="after")
    def validate_identity_configuration(self):
        parsed = urlparse(self.public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("public_url must be http or https with a host")
        if parsed.scheme == "http" and parsed.hostname != "127.0.0.1":
            raise ValueError("HTTP public_url host must be 127.0.0.1")
        if self.environment == "production" and parsed.scheme != "https":
            raise ValueError("production public_url must be https")
        origins = tuple(item.strip() for item in self.allowed_origins.split(","))
        if not origins or any(item in {"", "*"} for item in origins):
            raise ValueError("allowed origins reject blanks and wildcards")
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise ValueError("session idle seconds cannot exceed the absolute lifetime")
        return self

    def parsed_allowed_origins(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.allowed_origins.split(","))
