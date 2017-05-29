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
        self.status = status or '200 OK'
        self.headers = headers or []
        self.body = body
        self.exc_info = exc_info
        self.use_write = use_write

        self.environ = None
        self.start_response = None
        self.called = False

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
            return self.body or []


class StubApplication(object):
    def __init__(self, callable):
        self.app = callable

    def wsgi(self):
        return self.app


class StubSocket(object):
    type = socket.SOCK_STREAM

    def __init__(self):
        self.listening = False
        self.backlog = None
        self.blocking = True

    def listen(self, backlog=None):
        self.listening = True
        self.backlog = backlog

    def setblocking(self, blocking):
        self.blocking = blocking

    def fileno(self):
        return 99


class StubWriter(object):
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    @asyncio.coroutine
    def drain(self):
        pass


class StubSelector(selectors.BaseSelector):
    key_type = namedtuple('key', ['fileobj', 'data'])

    def __init__(self):
        self.keys = {}
        self.ready = []

    def register(self, fileobj, events, data=None):
        key = selectors.SelectorKey(fileobj, 0, events, data)
        self.keys[fileobj] = key
        return key

    def unregister(self, fileobj):
        return self.keys.pop(fileobj)

    def select(self, timeout):
        ready = self.ready
        self.ready = []
        return ready

    def get_map(self):
        return self.keys

    def make_ready(self, fileobj, mask, data):
        self.ready.append((self.key_type(fileobj, data), mask))


def make_stub_application(status=None, headers=None, body=None, exc_info=None):
    wsgi = StubWSGI(status, headers, body, exc_info)
    return wsgi, StubApplication(wsgiref.validate.validator(wsgi))


def run_worker(worker):
    asyncio.get_event_loop().call_later(0.2, ensure_loop_stopped)
    worker.run()
    worker.cleanup()


def test_worker_creates_servers_for_sockets(monkeypatch):
    loop = asyncio.get_event_loop()
    calls = []

    sock = StubSocket()

    age = None
    ppid = os.getpid()
    sockets = [sock]
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
    loop = asyncio.get_event_loop()

    request = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'

    age = None
    ppid = os.getpid()
    sockets = []
    wsgi, app = make_stub_application()
    timeout = None
    cfg = Config()
    log = None
    sockname = ('127.0.0.1', '80')

    reader = asyncio.StreamReader()
    writer = StubWriter()

    def feeder():
        reader.feed_data(request)
        reader.feed_eof()

    worker = AsyncioWorker(age, ppid, sockets, app, timeout, cfg, log)
    loop.create_task(worker.connection_task(sockname, reader, writer))
    loop.call_soon(feeder)
    run_worker(worker)

    assert wsgi.called
    assert wsgi.environ['REQUEST_METHOD'] == b'GET'
    assert wsgi.environ['SERVER_PROTOCOL'] == b'HTTP/1.1'
    assert wsgi.environ['HOST'] == b'localhost'


def test_worker_returns_response_to_socket():
    loop = asyncio.get_event_loop()

    request = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
    response_body = b'hello'

    age = None
    ppid = os.getpid()
    sockets = []
    wsgi, app = make_stub_application(body=response_body)
    timeout = None
    cfg = Config()
    log = None
    sockname = ('127.0.0.1', '80')

    reader = asyncio.StreamReader()
    writer = StubWriter()

    def feeder():
        reader.feed_data(request)
        reader.feed_eof()

    worker = AsyncioWorker(age, ppid, sockets, app, timeout, cfg, log)
    loop.create_task(worker.connection_task(sockname, reader, writer))
    loop.call_soon(feeder)
    run_worker(worker)

    assert wsgi.called
    print(repr(writer.data))
    assert b'200' in writer.data
    assert response_body in writer.data
