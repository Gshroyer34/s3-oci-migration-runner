"""Explicitly enabled localhost operator surface. Cloud identity is a later step."""
import csv
import io
import secrets
from pathlib import Path
from uuid import UUID
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from migration_runner import security, operations, reports
from migration_runner.ledger.database import make_engine
from migration_runner.ledger.models import Migration, AuditRecord
from migration_runner.supervisor import LocalSupervisor
from migration_runner.worker import queue
from migration_runner.worker.rclone import WorkerError


def install(app, settings, engine=None, supervisor=None):
    # Lazy engine creation keeps liveness independent of secrets/database setup.
    database = [engine]
    supervisor = supervisor or LocalSupervisor()
    def db():
        if database[0] is None:
            database[0] = make_engine(settings)
        return database[0]

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        yield
        supervisor.close()
    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def boundaries(request, call_next):
        origin = request.headers.get('origin')
        host = request.url.hostname
        if host not in ('localhost','127.0.0.1','test') or (origin and origin != str(request.base_url).rstrip('/')):
            return JSONResponse({'detail':'Local same-origin requests only'},status_code=403)
        if request.headers.get('content-length','0').isdigit() and int(request.headers.get('content-length','0')) > 65536:
            return JSONResponse({'detail':'Request too large'},status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return JSONResponse({'detail':'Invalid request fields'}, status_code=422)

    @app.exception_handler(WorkerError)
    async def conflict(request, exc):
        security.audit(db(),'local-operator','operation',outcome='rejected')
        return JSONResponse({'detail':'Operation rejected; check job state, limits and local roots'}, status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse({'detail':'Ledger unavailable'}, status_code=503)

    def authenticate(request: Request):
        if not settings.operator_enabled:
            raise HTTPException(503, 'Local operator access is disabled')
        supplied = request.headers.get('authorization','').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied.encode(), settings.operator_token.get_secret_value().encode()):
            security.audit(db(),'anonymous','authentication',None, 'denied')
            raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Bearer'})
        with db().connect() as c:
            try:
                security.worker(c,settings.worker_token.get_secret_value(),required=True)
            except WorkerError:
                raise HTTPException(503,'Initialize local worker authentication before operator access')
        security.audit(db(),'local-operator',request.method+':'+request.scope['route'].path, outcome='authorized')

    protected = [Depends(authenticate)]
    @app.get('/api/session',dependencies=protected)
    def session():
        return {'actor':'local-operator','mode':'local-only'}

    @app.get('/api/jobs',dependencies=protected)
    def jobs():
        with db().connect() as c:
            return [dict(r) for r in c.execute(select(Migration.id,Migration.state,Migration.control,Migration.created_at).order_by(Migration.created_at.desc()).limit(100)).mappings()]

    @app.get('/api/jobs/{job}',dependencies=protected)
    def preview(job: UUID):
        return reports.report(db(),job)

    @app.post('/api/jobs/{job}/start',dependencies=protected)
    def start(job: UUID, request: operations.StartRequest):
        result=operations.start(db(),job,request,settings.lab_root)
        result['local_workers']=supervisor.launch(job,request.limits.max_workers)
        security.audit(db(),'local-operator','start',job)
        return result

    @app.post('/api/jobs/{job}/retry/{batch}',dependencies=protected)
    def retry(job: UUID, batch: int):
        result=operations.retry(db(),job,batch)
        security.audit(db(),'local-operator','retry',job)
        return result

    @app.post('/api/jobs/{job}/{action}',dependencies=protected)
    def control(job: UUID, action: str):
        if action not in ('pause','drain','resume','cancel'):
            raise HTTPException(404,'Unknown action')
        result=queue.control(db(),job,action)
        if action=='resume' and result['run_limits']:
            supervisor.launch(job,result['run_limits']['max_workers'])
        security.audit(db(),'local-operator',action,job)
        return result

    @app.get('/api/jobs/{job}/export',dependencies=protected)
    def export(job: UUID, format: str='json'):
        result=reports.report(db(),job)
        if format=='json':
            return JSONResponse(jsonable_encoder(result),headers={'Content-Disposition':'attachment; filename=report.json'})
        if format!='csv':
            raise HTTPException(422,'Use json or csv')
        stream=io.StringIO(newline='')
        writer=csv.writer(stream)
        writer.writerow(['metric','value'])
        for k,v in result.items():
            if v is None or isinstance(v,(str,int,float,bool)):
                value='unknown' if v is None else str(v)
                if value.startswith(('=','+','-','@','\t','\r','\n')):
                    value="'"+value
                writer.writerow([k,value])
        return Response(stream.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=report.csv'})

    @app.get('/api/audit',dependencies=protected)
    def audit():
        with db().connect() as c:
            return [dict(r) for r in c.execute(select(AuditRecord.__table__).order_by(AuditRecord.created_at.desc()).limit(100)).mappings()]

    @app.get('/api/worker/check')
    def worker_check(request: Request):
        if not settings.operator_enabled:
            raise HTTPException(503,'Local access disabled')
        try:
            with db().connect() as c:
                security.worker(c,request.headers.get('authorization','').removeprefix('Bearer '),required=True)
        except WorkerError:
            raise HTTPException(401,'Worker authentication required')
        return {'authenticated':True,'transport':'trusted local PostgreSQL'}

    static = Path(__file__).parent/'static'
    app.mount('/static',StaticFiles(directory=static),name='static')
    @app.get('/',include_in_schema=False)
    def home():
        return FileResponse(static/'index.html')
