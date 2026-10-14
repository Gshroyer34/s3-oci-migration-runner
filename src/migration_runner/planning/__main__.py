"""Local-only planning CLI. No live AWS command is exposed in Step 2."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import UUID

from pydantic import ValidationError
from psycopg import Error as PsycopgError
from sqlalchemy.exc import SQLAlchemyError

from migration_runner.config import Settings
from migration_runner.ledger.database import make_engine
from .contracts import Mapping, PlanningError
from .service import export_objects, plan, report
from .sources import inventory_manifest, jsonl_objects


def main():
    parser = argparse.ArgumentParser(description='Offline inventory planner; no object transfers')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('plan')
    create.add_argument('--mapping', type=Path, required=True)
    create.add_argument('--inventory', type=Path, required=True)
    create.add_argument('--format', choices=['jsonl', 's3-inventory-csv'], default='jsonl')
    create.add_argument('--data-root', type=Path)
    create.add_argument('--observed-at', help='Required timezone-qualified source observation time for JSONL')
    view = commands.add_parser('report')
    view.add_argument('--job', type=UUID, required=True)
    export = commands.add_parser('export')
    export.add_argument('--job', type=UUID, required=True)
    export.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    engine = None
    try:
        settings = Settings()
        # CLI cannot accidentally become an unreviewed cloud operation.
        if settings.db_host not in ('localhost', '127.0.0.1', 'db'):
            raise PlanningError('Step 2 CLI requires the configured local lab database')
        engine = make_engine(settings)
        if arguments.command == 'plan':
            mapping = Mapping.model_validate_json(arguments.mapping.read_text(encoding='utf-8'))
            if arguments.format == 'jsonl':
                if not arguments.observed_at:
                    raise PlanningError('--observed-at is required for JSONL')
                provenance = {'kind': 'jsonl', 'observed_at': arguments.observed_at}
                records = jsonl_objects(arguments.inventory)
            else:
                if arguments.data_root is None:
                    raise PlanningError('--data-root is required for downloaded S3 Inventory')
                provenance, records = inventory_manifest(arguments.inventory, arguments.data_root)
            job = plan(engine, mapping, records, provenance)
            print(json.dumps(report(engine, job), indent=2, ensure_ascii=True))
        elif arguments.command == 'report':
            print(json.dumps(report(engine, arguments.job), indent=2, ensure_ascii=True))
        else:
            export_objects(engine, arguments.job, arguments.output)
            print(json.dumps({'exported': True, 'job_id': str(arguments.job)}))
        return 0
    except PlanningError as error:
        print(f'Planning failed: {error}', file=sys.stderr)
    except ValidationError:
        print('Invalid mapping or inventory record. Check field types, current-version scope, and key limits.', file=sys.stderr)
    except (SQLAlchemyError, PsycopgError):
        print('Planning database operation failed. Check migrations, duplicate source keys, and database availability. No partial import was committed.', file=sys.stderr)
    except (ValueError, OSError, KeyError) as error:
        # Validation errors originate here; never dump database parameters or record bodies.
        print(f'Planning failed: {type(error).__name__}. Check inputs, scope overlap, and the documented format.', file=sys.stderr)
    finally:
        if engine is not None:
            engine.dispose()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())


