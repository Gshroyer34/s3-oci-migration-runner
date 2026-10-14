"""Ledger foundation and immutable inventory planning schemas."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LedgerMetadata(Base):
    __tablename__ = "ledger_metadata"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ledger_metadata_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

# Step 2: immutable planning ledger. Execution/lease schemas follow later.
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID


class Migration(Base):
    __tablename__ = 'migrations'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    control: Mapped[str] = mapped_column(String(20), server_default='active', nullable=False)
    execution: Mapped[dict | None] = mapped_column(JSONB)
    run_limits: Mapped[dict | None] = mapped_column(JSONB)
    scheduled_bytes: Mapped[int] = mapped_column(BigInteger, server_default='0', nullable=False)
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    destination_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    destination_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    destination_region: Mapped[str] = mapped_column(Text, nullable=False)
    destination_prefix: Mapped[str] = mapped_column(Text(collation='C'), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("state IN ('planning', 'planned', 'needs_review', 'archived')", name='ck_migration_state'),
        CheckConstraint("control IN ('active', 'paused', 'draining', 'cancelled')", name='ck_migration_control'),
        Index('ix_migration_scope', 'destination_namespace', 'destination_bucket', 'destination_region'),
    )


class Batch(Base):
    __tablename__ = 'batches'
    migration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('migrations.id', ondelete='CASCADE'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    planned_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    claim_count: Mapped[int] = mapped_column(Integer, server_default='0', nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oversized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(20), server_default='planned', nullable=False)
    __table_args__ = (
        CheckConstraint('ordinal > 0 AND planned_bytes >= 0 AND object_count > 0', name='ck_batch_totals'),
        CheckConstraint("state IN ('planned', 'running', 'verified', 'failed', 'retry_wait', 'cancelled', 'source_changed')", name='ck_batch_execution_state'),
        CheckConstraint("claim_count >= 0 AND ((state = 'running' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND current_attempt_id IS NOT NULL) OR (state <> 'running' AND lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL))", name='ck_batch_lease'),
        Index('ix_batch_queue', 'migration_id', 'state', 'available_at'),
    )


class ObjectRecord(Base):
    __tablename__ = 'inventory_objects'
    migration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('migrations.id', ondelete='CASCADE'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(Text(collation='C'), nullable=False)
    destination_key: Mapped[str | None] = mapped_column(Text(collation='C'))
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_identity: Mapped[dict] = mapped_column(JSONB, nullable=False)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(80))
    batch_ordinal: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint('migration_id', 'source_key', name='uq_inventory_source_key'),
        CheckConstraint('ordinal > 0 AND size >= 0', name='ck_inventory_size'),
        CheckConstraint("disposition IN ('eligible', 'excluded', 'blocked')", name='ck_inventory_disposition'),
        CheckConstraint("(disposition = 'eligible' AND reason IS NULL) OR (disposition <> 'eligible' AND reason IS NOT NULL AND batch_ordinal IS NULL)", name='ck_inventory_reason'),
        ForeignKeyConstraint(['migration_id', 'batch_ordinal'], ['batches.migration_id', 'batches.ordinal'], name='fk_object_batch', deferrable=True, initially='DEFERRED'),
        Index('ix_inventory_batch', 'migration_id', 'batch_ordinal'),
    )



class Attempt(Base):
    __tablename__ = 'worker_attempts'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    migration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    batch_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['migration_id', 'batch_ordinal'], ['batches.migration_id', 'batches.ordinal'], ondelete='CASCADE'),
        CheckConstraint("state IN ('running', 'verified', 'failed', 'abandoned', 'cancelled', 'source_changed')", name='ck_attempt_state'),
        Index('ix_attempt_batch', 'migration_id', 'batch_ordinal', 'created_at'),
    )


class ObjectEvidence(Base):
    __tablename__ = 'object_evidence'
    attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('worker_attempts.id', ondelete='CASCADE'), primary_key=True)
    object_ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    migration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(['migration_id', 'object_ordinal'], ['inventory_objects.migration_id', 'inventory_objects.ordinal'], ondelete='CASCADE'),
    )


class WorkerHeartbeat(Base):
    __tablename__ = 'worker_heartbeats'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class QueueEvent(Base):
    __tablename__ = 'queue_events'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    migration_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('migrations.id', ondelete='CASCADE'), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RuntimeSecurity(Base):
    __tablename__ = 'runtime_security'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (CheckConstraint('id = 1', name='ck_runtime_security_singleton'),)


class AuditRecord(Base):
    __tablename__ = 'audit_records'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CloudUIRun(Base):
    __tablename__ = 'cloud_ui_runs'
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('migrations.id', ondelete='CASCADE'), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CloudUIDispatch(Base):
    __tablename__ = 'cloud_ui_dispatch'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey('migrations.id'))
    __table_args__ = (CheckConstraint('id=1', name='ck_cloud_ui_dispatch_singleton'),)


class CloudUIWorker(Base):
    __tablename__ = 'cloud_ui_workers'
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    certificate_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    service_state: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (CheckConstraint("node_id IN ('worker-1','worker-2')", name='ck_cloud_ui_node'),
                      CheckConstraint("status IN ('idle','run','hold')", name='ck_cloud_ui_worker_status'))
