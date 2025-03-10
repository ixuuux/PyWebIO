import asyncio
import logging
from functools import partial

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route, WebSocketRoute, Mount

from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from .tornado import open_webbrowser_on_server_started
from .utils import make_applications, render_page, cdn_validation, OriginChecker
from ..session import CoroutineBasedSession, ThreadBasedSession, register_session_implement_for_target, Session
from ..session.base import get_session_info_from_headers
from ..utils import get_free_port, STATIC_PATH, iscoroutinefunction, isgeneratorfunction, strip_space

logger = logging.getLogger(__name__)


def _webio_routes(applications, cdn, check_origin_func):
    """
    :param dict applications: dict of `name -> task function`
    :param bool/str cdn: Whether to load front-end static resources from CDN
    :param callable check_origin_func: check_origin_func(origin, host) -> bool
    """

    async def http_endpoint(request: Request):
        origin = request.headers.get('origin')
        if origin and not check_origin_func(origin=origin, host=request.headers.get('host')):
            return HTMLResponse(status_code=403, content="Cross origin websockets not allowed")

        # Backward compatible
        if request.query_params.get('test'):
            return HTMLResponse(content="")

        app_name = request.query_params.get('app', 'index')
        app = applications.get(app_name) or applications['index']
        html = render_page(app, protocol='ws', cdn=cdn)
        return HTMLResponse(content=html)

    async def websocket_endpoint(websocket: WebSocket):
        ioloop = asyncio.get_event_loop()
        await websocket.accept()

        close_from_session_tag = False  # session close causes websocket close

        def send_msg_to_client(session: Session):
            for msg in session.get_task_commands():
                ioloop.create_task(websocket.send_json(msg))

        def close_from_session():
            nonlocal close_from_session_tag
            close_from_session_tag = True
            ioloop.create_task(websocket.close())
            logger.debug("WebSocket closed from session")

        session_info = get_session_info_from_headers(websocket.headers)
        session_info['user_ip'] = websocket.client.host or ''
        session_info['request'] = websocket
        session_info['backend'] = 'starlette'
        session_info['protocol'] = 'websocket'

        app_name = websocket.query_params.get('app', 'index')
        application = applications.get(app_name) or applications['index']

        if iscoroutinefunction(application) or isgeneratorfunction(application):
            session = CoroutineBasedSession(application, session_info=session_info,
                                            on_task_command=send_msg_to_client,
                                            on_session_close=close_from_session)
        else:
            session = ThreadBasedSession(application, session_info=session_info,
                                         on_task_command=send_msg_to_client,
                                         on_session_close=close_from_session, loop=ioloop)

        while True:
            try:
                msg = await websocket.receive_json()
            except WebSocketDisconnect:
                if not close_from_session_tag:
                    # close session because client disconnected to server
                    session.close(nonblock=True)
                    logger.debug("WebSocket closed from client")
                break

            if msg is not None:
                session.send_client_event(msg)

    return [
        Route("/", http_endpoint),
        WebSocketRoute("/", websocket_endpoint)
    ]


def webio_routes(applications, cdn=True, allowed_origins=None, check_origin=None):
    """Get the FastAPI/Starlette routes for running PyWebIO applications.

    The API communicates with the browser using WebSocket protocol.

    The arguments of ``webio_routes()`` have the same meaning as for :func:`pywebio.platform.fastapi.start_server`

    :return: FastAPI/Starlette routes
    """
    try:
        import websockets
    except Exception:
        raise RuntimeError(strip_space("""
        Missing dependency package `websockets` for websocket support.
        You can install it with the following command:
            pip install websocket
        """.strip(), n=8))

    applications = make_applications(applications)
    for target in applications.values():
        register_session_implement_for_target(target)

    cdn = cdn_validation(cdn, 'error')

    if check_origin is None:
        check_origin_func = partial(OriginChecker.check_origin, allowed_origins=allowed_origins or [])
    else:
        check_origin_func = lambda origin, host: OriginChecker.is_same_site(origin, host) or check_origin(origin)

    return _webio_routes(applications=applications, cdn=cdn, check_origin_func=check_origin_func)


def start_server(applications, port=0, host='',
                 cdn=True, static_dir=None, debug=False,
                 allowed_origins=None, check_origin=None,
                 auto_open_webbrowser=False,
                 **uvicorn_settings):
    """Start a FastAPI/Starlette server using uvicorn to provide the PyWebIO application as a web service.

    :param bool debug: Boolean indicating if debug tracebacks should be returned on errors.
    :param uvicorn_settings: Additional keyword arguments passed to ``uvicorn.run()``.
       For details, please refer: https://www.uvicorn.org/settings/

    The rest arguments of ``start_server()`` have the same meaning as for :func:`pywebio.platform.tornado.start_server`
    """
    kwargs = locals()

    try:
        from starlette.staticfiles import StaticFiles
    except Exception:
        raise RuntimeError(strip_space("""
        Missing dependency package `aiofiles` for static file serving.
        You can install it with the following command:
            pip install aiofiles
        """.strip(), n=8))

    if not host:
        host = '0.0.0.0'

    if port == 0:
        port = get_free_port()

    cdn = cdn_validation(cdn, 'warn')
    if cdn is False:
        cdn = '/pywebio_static'

    routes = webio_routes(applications, cdn=cdn, allowed_origins=allowed_origins, check_origin=check_origin)
    routes.append(Mount('/static', app=StaticFiles(directory=static_dir), name="static"))
    routes.append(Mount('/pywebio_static', app=StaticFiles(directory=STATIC_PATH), name="pywebio_static"))

    app = Starlette(routes=routes, debug=debug)

    if auto_open_webbrowser:
        asyncio.get_event_loop().create_task(open_webbrowser_on_server_started('localhost', port))

    uvicorn.run(app, host=host, port=port)
