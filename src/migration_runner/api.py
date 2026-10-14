from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from migration_runner import __version__
from migration_runner.config import Settings
from migration_runner.ledger.readiness import ledger_ready


def create_app(readiness_probe: Callable[[], bool] | None = None, *, settings=None, engine=None, supervisor=None) -> FastAPI:
    settings = settings or Settings()
    if settings.environment != 'local':
        raise ValueError('Cloud HTTP service activation is pending integration')
    app = FastAPI(title="Migration Runner (local scaffold)", version=__version__)

    @app.get("/health/live")
    def live():
        return {"status": "ok", "version": __version__, "mode": "scaffold"}

    @app.get("/health/ready")
    def ready():
        ready_now = readiness_probe() if readiness_probe else ledger_ready(Settings())
        return JSONResponse(
            status_code=200 if ready_now else 503,
            content={"status": "ready" if ready_now else "not_ready", "ledger": "ready" if ready_now else "unavailable_or_unmigrated"},
        )

    from migration_runner.operator_api import install
    install(app, settings or Settings(), engine, supervisor)
    return app


app = create_app()
