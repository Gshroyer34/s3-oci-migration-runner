"""Read-only dispatch query plus authenticated worker readiness heartbeat."""
import argparse
from datetime import datetime
import json
from pathlib import Path


def main():
    from migration_runner.cloud_dispatch import poll
    from migration_runner.cloud_cli import require_job_scope
    from migration_runner.config import Settings
    from migration_runner.installation.deployment import installed_profile
    from migration_runner.installation.host_plan import HostPlan
    from migration_runner.ledger.database import make_engine
    parser = argparse.ArgumentParser()
    parser.add_argument('--node',required=True)
    parser.add_argument('--state',choices=['active','inactive'],required=True)
    args = parser.parse_args()
    engine = None
    try:
        plan = HostPlan.model_validate_json(Path('/etc/migration/host-plan.json').read_bytes())
        if not plan.ui or installed_profile()!=plan.deployment:
            raise ValueError('Portable UI deployment required')
        settings = Settings()
        if settings.environment!='cloud-worker': raise ValueError('Worker identity required')
        expiry = datetime.fromisoformat(json.loads(Path('/run/migration/expiry.json').read_bytes())['expires_at'])
        engine = make_engine(settings)
        result = poll(engine,args.node,expiry,settings.worker_token.get_secret_value(),args.state,
            allowed_nodes=[node.node_id for node in plan.nodes if node.node_id!='coordinator'],
            scope_check=lambda job: require_job_scope(engine,job))
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'dispatch_unavailable':True}))
        return 1
    finally:
        if engine is not None: engine.dispose()


if __name__=='__main__':
    raise SystemExit(main())
