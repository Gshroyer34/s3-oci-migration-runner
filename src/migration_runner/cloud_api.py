"""Separate cloud operator surface, restricted to loopback through an authenticated SSH tunnel."""
import json
import os
from pathlib import Path
import secrets
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from migration_runner.cloud_ui_service import CloudUIService, UIConflict
from migration_runner.worker.rclone import WorkerError
from migration_runner.autoscaling.contracts import CapacityPreviewRequest
from migration_runner.installation.web import routes as installation_routes


class StartBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workers: int = Field(default=2, strict=True, ge=1, le=2)


class PortableStartBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workers: int = Field(default=1, strict=True, ge=1, le=4)


def create_app(service=None, operator_token=None, *, portable=False):
    if operator_token is None:
        path = Path(os.environ.get('MIGRATION_UI_TOKEN_FILE', '/run/migration-ui/operator-token'))
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError('Private operator token file required')
        operator_token = path.read_text().strip()
    if len(operator_token) < 40:
        raise ValueError('Strong operator credential required')
    if service is None:
        from migration_runner.config import Settings
        from migration_runner.ledger.database import make_engine
        settings = Settings()
        if settings.environment != 'cloud-cli' or secrets.compare_digest(operator_token, settings.worker_token.get_secret_value()):
            raise ValueError('Separate cloud coordinator and operator credentials required')
        config = json.loads(Path('/etc/migration/ui-lab.json').read_text())
        from migration_runner.autoscaling.monitor import CapacityMonitor, MonitorConfig
        from migration_runner.autoscaling.oci_network import WorkerNetworkConfig, WorkerNetworkReader
        worker_network = WorkerNetworkReader(WorkerNetworkConfig.model_validate(config['worker_network_monitor'])) if config.get('worker_network_monitor') else None
        monitor_config = MonitorConfig.model_validate(config['capacity_monitor']) if config.get('capacity_monitor') else None
        service = CloudUIService(make_engine(settings), settings, config['reference_path'], config['reference_sha256'],
                                 capacity_monitor=CapacityMonitor(monitor_config,worker_network_reader=worker_network))
    app = FastAPI(title='Migration Runner private deployment' if portable else 'Migration Runner private lab', docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
            return JSONResponse({'detail': 'Use the private local tunnel.'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Same-origin requests only.'}, status_code=403)
        if request.method == 'POST' and not request.headers.get('content-type', '').startswith('application/json'):
            return JSONResponse({'detail': 'JSON required.'}, status_code=415)
        # All mutation payloads are tiny; enforce the actual streamed size, not just Content-Length.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            limit = 16384 if request.url.path in ('/api/installation/validate', '/api/installation/package', '/api/installation/test-connections') else 4096
            if len(body) > limit:
                return JSONResponse({'detail': 'Request too large.'}, status_code=413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def authenticate(request: Request):
        header = request.headers.get('authorization', '')
        supplied = header[7:] if header.startswith('Bearer ') else ''
        if not secrets.compare_digest(supplied.encode(), operator_token.encode()):
            raise HTTPException(401, 'Sign in with your private operator token.')
    protected = [Depends(authenticate)]

    @app.exception_handler(UIConflict)
    async def conflict(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        if request.url.path in ('/api/installation/validate', '/api/installation/package', '/api/installation/test-connections'):
            # Do not echo submitted values: a user may accidentally paste a credential.
            allowed = {'schema_version', 'deployment_name', 'source', 'destination', 'network', 'compute'}
            fields = []
            for item in error.errors():
                path = item['loc'][1:]
                if path and path[0] in allowed:
                    fields.append(str(path[0]))
            return JSONResponse({'detail': 'Invalid installation settings.', 'fields': sorted(set(fields))}, status_code=422)
        return JSONResponse({'detail': 'Invalid request fields.'}, status_code=422)

    async def unavailable(request, error):
        return JSONResponse({'detail': 'Operation could not complete. Check worker readiness, certificate expiry and deployment scope.'}, status_code=503)
    for kind in (SQLAlchemyError, WorkerError, OSError, ValueError):
        app.add_exception_handler(kind, unavailable)

    @app.get('/health/live')
    def live():
        return {'status': 'ok', 'mode': 'private-cloud-ui'}

    @app.get('/api/session', dependencies=protected)
    def session():
        return service.ready()

    installation_routes(app, protected)

    @app.get('/api/jobs', dependencies=protected)
    def jobs():
        return service.jobs()

    @app.post('/api/jobs', dependencies=protected)
    def create():
        return service.create()

    @app.get('/api/jobs/{job}', dependencies=protected)
    def detail(job: UUID):
        return service.detail(job)

    if portable:
        @app.post('/api/jobs/{job}/start', dependencies=protected)
        def start_portable(job: UUID, body: PortableStartBody):
            return service.start(job, body.workers)
        @app.get('/api/deployment', dependencies=protected)
        def deployment():
            return service.configuration()
    else:
        @app.post('/api/jobs/{job}/start', dependencies=protected)
        def start(job: UUID, body: StartBody):
            return service.start(job, body.workers)

    @app.post('/api/jobs/{job}/capacity/preview', dependencies=protected)
    def capacity_preview(job: UUID, body: CapacityPreviewRequest):
        if portable:
            raise HTTPException(409, 'Capacity telemetry is not configured for this installation.')
        return service.capacity_preview(job, body)

    @app.get('/api/jobs/{job}/capacity', dependencies=protected)
    def capacity_status(job: UUID):
        if portable:
            raise HTTPException(409, 'Capacity telemetry is not configured for this installation.')
        return service.capacity_status(job)

    @app.post('/api/jobs/{job}/{action}', dependencies=protected)
    def control(job: UUID, action: str):
        return service.control(job, action)

    @app.get('/api/jobs/{job}/export', dependencies=protected)
    def export(job: UUID):
        return JSONResponse(jsonable_encoder(service.detail(job)), headers={'Content-Disposition': 'attachment; filename=migration-report.json'})

    @app.get('/api/jobs/{job}/manifest', dependencies=protected)
    def manifest(job: UUID):
        output = service.manifest(job)
        def chunks():
            try:
                while data := output.read(65536):
                    yield data
            finally:
                output.close()
        return StreamingResponse(chunks(), media_type='application/zip',
            headers={'Content-Disposition': f'attachment; filename=migration-manifest-{job}.zip'},
            background=BackgroundTask(output.close))

    static = Path(__file__).parent / 'cloud_static'
    app.mount('/static', StaticFiles(directory=static), name='static')
    @app.get('/', include_in_schema=False)
    def home():
        return FileResponse(static / ('portable.html' if portable else 'index.html'))
    return app
