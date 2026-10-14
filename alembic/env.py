from alembic import context

from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from migration_runner.ledger.models import Base


def run_online(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(dialect_name='postgresql', target_metadata=Base.metadata,
                      literal_binds=True, dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = context.config.attributes.get('connection')
    if supplied is not None:
        run_online(supplied)
    else:
        engine = make_engine(Settings())
        try:
            with engine.connect() as connection:
                run_online(connection)
        finally:
            engine.dispose()
