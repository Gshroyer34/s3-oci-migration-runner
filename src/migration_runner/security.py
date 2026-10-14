"""Separate local operator/worker credentials; no plaintext tokens in the ledger."""
import hashlib
import secrets
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from migration_runner.ledger.models import AuditRecord, RuntimeSecurity
from migration_runner.worker.rclone import WorkerError


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def audit(engine, actor, action, job=None, outcome='ok'):
    with engine.begin() as c:
        c.execute(insert(AuditRecord).values(id=uuid4(), actor=actor, action=action, job_id=job, outcome=outcome))


def worker(c, token, required=False):
    expected = c.execute(select(RuntimeSecurity.worker_token_hash).where(RuntimeSecurity.id == 1)).scalar_one_or_none()
    if expected is None and not required:
        return  # Legacy isolated unit-test/admin ledger; the HTTP worker interface still fails closed.
    if not expected or not token or not secrets.compare_digest(expected, digest(token)):
        raise WorkerError('Worker authentication failed')


def initialize(engine, token):
    if len(token) < 32:
        raise WorkerError('A strong worker credential is required')
    with engine.begin() as c:
        statement = insert(RuntimeSecurity).values(id=1, worker_token_hash=digest(token))
        c.execute(statement.on_conflict_do_update(index_elements=['id'], set_={'worker_token_hash': digest(token)}))
