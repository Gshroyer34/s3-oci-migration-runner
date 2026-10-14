"""Allow the four worker identities supported by portable deployment profiles."""
from alembic import op

revision = '0007_portable_workers'
down_revision = '0006_cloud_ui'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_cloud_ui_node', 'cloud_ui_workers', type_='check')
    op.create_check_constraint('ck_cloud_ui_node', 'cloud_ui_workers', "node_id IN ('worker-1','worker-2','worker-3','worker-4')")


def downgrade():
    # Do not silently discard worker observations on rollback.
    op.drop_constraint('ck_cloud_ui_node', 'cloud_ui_workers', type_='check')
    op.create_check_constraint('ck_cloud_ui_node', 'cloud_ui_workers', "node_id IN ('worker-1','worker-2')")
