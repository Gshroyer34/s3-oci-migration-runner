from sqlalchemy import create_engine

from migration_runner.config import Settings


def make_engine(settings: Settings):
    return create_engine(
        settings.database_url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
        hide_parameters=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
    )
