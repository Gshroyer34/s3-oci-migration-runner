"""Loopback setup assistant with optional, read-only local cloud checks."""
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .package import build_package
from .profile import InstallationProfile
from .connections import ConnectionSettings, ProbeError
from .discovery import DiscoverySettings, DiscoveryError


def routes(app, dependencies=(), connection_tester=None, discovery=None):
    def discovery_enabled():
        if discovery is None:
            raise HTTPException(503, 'Read-only discovery is disabled. Enable it in the local setup assistant with a selected AWS profile and private state directory.')
        return discovery

    @app.get('/api/discovery', dependencies=list(dependencies))
    def discovery_list():
        return discovery_enabled().recent()

    @app.post('/api/discovery', dependencies=list(dependencies))
    def discovery_create(body: DiscoverySettings):
        return discovery_enabled().create(body)

    @app.get('/api/discovery/{scan}', dependencies=list(dependencies))
    def discovery_report(scan: str):
        return discovery_enabled().report(scan)

    @app.post('/api/discovery/{scan}/advance', dependencies=list(dependencies))
    def discovery_advance(scan: str):
        return discovery_enabled().advance(scan)

    @app.post('/api/installation/test-connections', dependencies=list(dependencies))
    def test_connections(body: ConnectionSettings):
        if connection_tester is None:
            raise HTTPException(503, 'Live checks are disabled on this server. Start the local assistant with explicitly selected cloud profiles.')
        try:
            return connection_tester.run(body)
        except ProbeError as error:
            raise HTTPException(429, str(error)) from None

    @app.post('/api/installation/validate', dependencies=list(dependencies))
    def validate(body: InstallationProfile):
        return body.report()

    @app.post('/api/installation/package', dependencies=list(dependencies))
    def package(body: InstallationProfile):
        return Response(build_package(body), media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename={body.deployment_name}-installation.zip'})


def create_app(connection_tester=None, discovery=None):
    app = FastAPI(title='Migration installation assistant', docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
            return JSONResponse({'detail': 'Use the local setup address.'}, status_code=403)
        if request.headers.get('origin') not in (None, str(request.base_url).rstrip('/')):
            return JSONResponse({'detail': 'Same-origin requests only.'}, status_code=403)
        if request.method == 'POST' and not request.headers.get('content-type', '').startswith('application/json'):
            return JSONResponse({'detail': 'JSON required.'}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse({'detail': 'Request too large.'}, status_code=413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        # Keep the message independent of submitted keys and values.
        return JSONResponse({'detail': 'Invalid installation settings. Check the source, destination, network and compute fields.'}, status_code=422)

    @app.exception_handler(DiscoveryError)
    async def discovery_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.get('/api/installation/context')
    def context():
        return {'mode': 'local-stateless-assistant', 'operator_token_required': False,
                'cloud_access': connection_tester is not None,
                'connections': connection_tester.context() if connection_tester else {'enabled': False},
                'discovery': discovery.context() if discovery else {'enabled': False}}

    @app.get('/')
    def home():
        return RedirectResponse('/static/install.html')

    routes(app, connection_tester=connection_tester, discovery=discovery)
    app.mount('/static', StaticFiles(directory=Path(__file__).parents[1] / 'cloud_static'), name='static')
    return app
