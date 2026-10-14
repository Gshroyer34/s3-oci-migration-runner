from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine


def expected_heads() -> set[str]:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    return set(ScriptDirectory.from_config(config).get_heads())


def ledger_ready(settings: Settings) -> bool:
    """Read-only check: no schema auto-creation and no connection details returned."""
    engine = None
    try:
        engine = make_engine(settings)
        with engine.connect() as connection:
            actual = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
            marker = connection.execute(text("SELECT id FROM ledger_metadata WHERE id = 1")).scalar()
        return actual == expected_heads() and marker == 1
    except (SQLAlchemyError, ValueError):
        return False
    finally:
        if engine is not None:
            engine.dispose()
