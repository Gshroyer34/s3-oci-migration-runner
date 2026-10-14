"""Private cloud UI admission, dispatch and worker readiness."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006_cloud_ui'
down_revision = '0005_operator'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cloud_ui_runs',
        sa.Column('job_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('migrations.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('admitted_at', sa.DateTime(timezone=True)),
    )
    op.create_table('cloud_ui_dispatch',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('migrations.id')),
        sa.CheckConstraint('id=1', name='ck_cloud_ui_dispatch_singleton'))
    op.execute('INSERT INTO cloud_ui_dispatch (id) VALUES (1)')
    op.create_table('cloud_ui_workers',
        sa.Column('node_id', sa.String(64), primary_key=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('certificate_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('job_id', postgresql.UUID(as_uuid=True)),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('service_state', sa.String(16), nullable=False),
        sa.CheckConstraint("node_id IN ('worker-1','worker-2')", name='ck_cloud_ui_node'),
        sa.CheckConstraint("status IN ('idle','run','hold')", name='ck_cloud_ui_worker_status'))


def downgrade():
    op.drop_table('cloud_ui_workers')
    op.drop_table('cloud_ui_dispatch')
    op.drop_table('cloud_ui_runs')
