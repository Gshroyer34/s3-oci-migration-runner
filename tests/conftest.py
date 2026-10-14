import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text

from migration_runner.config import Settings


@pytest.fixture
def db_engine():
    if os.environ.get('MIGRATION_TEST_POSTGRES') != '1':
        pytest.skip('Opt in with the local Podman PostgreSQL database')
    settings = Settings()
    if settings.db_host not in ('127.0.0.1', 'localhost', 'db') or settings.db_name != 'migration':
        pytest.fail('Integration fixtures require the local migration lab database')
    admin = create_engine(settings.database_url())
    schema = 'test_' + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(settings.database_url(), connect_args={'options': f'-c search_path={schema} -c statement_timeout=120000'})
    try:
        with engine.connect() as connection:
            config = Config('alembic.ini')
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')
            connection.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
