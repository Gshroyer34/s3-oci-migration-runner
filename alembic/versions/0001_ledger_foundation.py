"""Create the ledger foundation; business tables follow in Step 2."""
from alembic import op
import sqlalchemy as sa

revision = "0001_ledger_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "ledger_metadata",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ledger_metadata_singleton"),
    )
    op.bulk_insert(table, [{"id": 1}])


def downgrade():
    op.drop_table("ledger_metadata")
