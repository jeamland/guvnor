import asyncio
import os

from gunicorn.config import Config

from guvnor.asyncio_worker import AsyncioWorker

from .support import (WSGITestRunner, run_worker, setup_function,
                      teardown_function)


def test_worker_creates_servers_for_sockets(monkeypatch, mocker):
    loop = asyncio.get_event_loop()
    calls = []

    age = None
    ppid = os.getpid()
    sockets = [mocker.MagicMock(), mocker.MagicMock()]
    app = None
    timeout = None
    cfg = Config()
    log = None

    async def start_server(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == len(sockets):
            loop.stop()

    monkeypatch.setattr(asyncio, 'start_server', start_server)

    worker = AsyncioWorker(age, ppid, sockets, app, timeout, cfg, log)
    run_worker(worker)

    for call in calls:
        assert call[1]['sock'] in sockets


def test_worker_passes_request_to_app():
    request = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'

    runner = WSGITestRunner(request)
    runner.run()

    assert runner.wsgi.called
    assert runner.wsgi.environ['REQUEST_METHOD'] == 'GET'
    assert runner.wsgi.environ['SERVER_PROTOCOL'] == 'HTTP/1.1'
    assert runner.wsgi.environ['HOST'] == 'localhost'


def test_worker_returns_response_to_socket():
    request = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
    response_body = b'hello'

    runner = WSGITestRunner(request, response_headers=[
        ('Content-Type', 'text/plain'),
    ], response_body=[response_body])
    runner.run()

    assert runner.wsgi.called
    print(repr(runner.writer.data))
    assert b'200' in runner.writer.data
    assert response_body in runner.writer.data
