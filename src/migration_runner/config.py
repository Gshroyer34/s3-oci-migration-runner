"""Local configuration. Never stringify connection URLs into logs."""

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MIGRATION_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    # Cloud deployment must add authentication/TLS before enabling another mode.
    environment: Literal["local", "cloud-worker", "cloud-cli"] = "local"
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=55432, ge=1, le=65535)
    db_name: str = "migration"
    db_user: str = "migration"
    db_password: SecretStr = SecretStr("")
    rclone_path: str = "rclone"
    rclone_version: Literal["1.75.1"] = "1.75.1"

    db_sslrootcert: str = ''
    db_sslcert: str = ''
    db_sslkey: str = ''
    aws_config_file: str = ''

    operator_enabled: bool = False
    operator_token: SecretStr = SecretStr('')
    worker_token: SecretStr = SecretStr('')
    lab_root: str = '/tmp/migration-lab'

    @model_validator(mode='after')
    def validate_local_auth(self):
        if self.environment != 'local':
            from pathlib import Path
            if self.operator_enabled or self.operator_token.get_secret_value():
                raise ValueError('Cloud HTTP operator activation is not implemented')
            expected_user = 'migration_worker' if self.environment == 'cloud-worker' else 'migration_coordinator'
            if self.db_user != expected_user or len(self.worker_token.get_secret_value()) < 32:
                raise ValueError('Cloud mode requires the scoped database role and enrolled worker credential')
            if self.db_password.get_secret_value():
                raise ValueError('Cloud mode uses certificate authentication only')
            for value in (self.db_sslrootcert, self.db_sslcert, self.db_sslkey):
                if not Path(value).is_absolute() or not Path(value).is_file():
                    raise ValueError('Cloud database TLS files are required')
        if self.operator_enabled:
            a, b = self.operator_token.get_secret_value(), self.worker_token.get_secret_value()
            if len(a) < 32 or len(b) < 32 or a == b:
                raise ValueError('Local operator mode requires distinct strong operator and worker credentials')
        return self

    def database_url(self) -> URL:
        if self.environment != 'local':
            return URL.create('postgresql+psycopg', username=self.db_user,
                host=self.db_host, port=self.db_port, database=self.db_name,
                query={'sslmode': 'verify-full', 'sslrootcert': self.db_sslrootcert,
                       'sslcert': self.db_sslcert, 'sslkey': self.db_sslkey})
        if not self.db_password.get_secret_value():
            raise ValueError("Database password is not configured; initialize local configuration.")
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
