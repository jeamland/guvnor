import asyncio
from asyncio import selectors, events
from collections import namedtuple
import socket
import os
import wsgiref.validate

import pytest

from gunicorn.config import Config

from guvnor.asyncio_worker import AsyncioWorker


def ensure_loop_stopped():
    loop = asyncio.get_event_loop()
    try:
        loop.stop()
        loop.close()
    except:
        pass

def setup_function(function):
    asyncio.set_event_loop(asyncio.new_event_loop())

def teardown_function(function):
    ensure_loop_stopped()


class StubWSGI(object):
    def __init__(self, status=None, headers=None, body=None, exc_info=None,
                 use_write=False):
        self.status = status
        self.headers = headers or []
        self.body = body
        self.exc_info = exc_info
        self.use_write = use_write

        self.environ = None
        self.start_response = None
        self.called = False

        if self.status is None:
            if self.body:
                self.status = '200 OK'
            else:
                self.status = '204 No Content'

    def __call__(self, environ, start_response):
        self.called = True
        self.environ = environ
        self.start_response = start_response

        writer = start_response(self.status, self.headers, self.exc_info)
        if self.use_write and self.body is not None:
            if self.body is not None:
                for chunk in self.body:
                    writer(chunk)
        else:
            return self.body or b''


class StubApplication(object):
    def __init__(self, callable):
        self.app = callable

    def wsgi(self):
        return self.app


class StubWriter(object):
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    @asyncio.coroutine
    def drain(self):
        pass

    def close(self):
        pass


def make_stub_application(status=None, headers=None, body=None, exc_info=None):
    wsgi = StubWSGI(status, headers, body, exc_info)
    return wsgi, StubApplication(wsgiref.validate.validator(wsgi))


def run_worker(worker):
    asyncio.get_event_loop().call_later(0.2, ensure_loop_stopped)
    worker.run()
    worker.cleanup()


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


class WSGITestRunner:
    def __init__(self, request, response_headers=None,
                 response_body=None):
        self.request = request
        self.response_headers = response_headers
        self.response_body = response_body

        self.wsgi = None
        self.writer = StubWriter()

    def run(self):
        loop = asyncio.get_event_loop()

        age = None
        ppid = os.getpid()
        sockets = []
        self.wsgi, app = make_stub_application(headers=self.response_headers,
                                               body=self.response_body)
        timeout = None
        cfg = Config()
        log = None
        sockname = ('127.0.0.1', '80')

        reader = asyncio.StreamReader()

        def feeder():
            reader.feed_data(self.request)
            reader.feed_eof()

        worker = AsyncioWorker(age, ppid, sockets, app, timeout, cfg, log)
        loop.create_task(worker.connection_task(sockname, reader, self.writer))
        loop.call_soon(feeder)
        run_worker(worker)


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
