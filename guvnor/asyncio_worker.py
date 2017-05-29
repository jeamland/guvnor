import asyncio
import io
from itertools import count
from wsgiref.handlers import format_date_time

from gunicorn.http.wsgi import base_environ
from gunicorn.workers.base import Worker

import h11


def environ_from_request(cfg, req, sockname, body):
    environ = base_environ(cfg)
    environ.update({
        'REQUEST_METHOD': req.method,
        'SERVER_NAME': sockname[0],
        'SERVER_PORT': str(sockname[1]),
        'SERVER_PROTOCOL': b'HTTP/%s' % req.http_version,

        'wsgi.input': io.BytesIO(body),
    })

    for k, v in req.headers:
        print(repr(k), repr(v))
        if k == b'host':
            environ['HOST'] = v

        key = 'HTTP_' + k.decode('ascii').replace('-', '_')
        if key in environ:
            v = "%s,%s" % (environ[key], v)
        environ[key] = v

    return environ


class StartResponse(object):
    def __init__(self, writer):
        self.writer = writer

        self.status = None
        self.reason = None
        self.headers = []
        self.exc_info = None

    def __call__(self, status, headers, exc_info=None):
        status, reason = status.split(' ', 1)
        self.status = int(status)
        self.reason = reason
        self.exc_info = exc_info

        return self.writer


class HTTPConnection:
    _next_id = count()

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.conn = h11.Connection(h11.SERVER)
        # Our Server: header
        self.ident = " ".join([
            "h11-example-curio-server/{}".format(h11.__version__),
            h11.PRODUCT_ID,
        ]).encode("ascii")
        # A unique id for this connection, to include in debugging output
        # (useful for understanding what's going on if there are multiple
        # simultaneous clients).
        self._obj_id = next(HTTPConnection._next_id)

    async def send(self, event):
        # The code below doesn't send ConnectionClosed, so we don't bother
        # handling it here either -- it would require that we do something
        # appropriate when 'data' is None.
        assert type(event) is not h11.ConnectionClosed
        data = self.conn.send(event)
        self.writer.write(data)
        await self.writer.drain()

    async def _read_from_peer(self):
        if self.conn.they_are_waiting_for_100_continue:
            self.info("Sending 100 Continue")
            go_ahead = h11.InformationalResponse(
                status_code=100,
                headers=self.basic_headers())
            await self.send(go_ahead)
        try:
            data = await self.reader.read()
        except ConnectionError:
            # They've stopped listening. Not much we can do about it here.
            data = b""
        self.conn.receive_data(data)

    async def next_event(self):
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                await self._read_from_peer()
                continue
            return event

    def basic_headers(self):
        # HTTP requires these headers in all responses (client would do
        # something different here)
        return [
            ("Date", format_date_time(None).encode("ascii")),
            ("Server", self.ident),
        ]

    def info(self, *args):
        # Little debugging method
        print("{}:".format(self._obj_id), *args)


class AsyncioWorker(Worker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.servers = []
        self.sockname = None

    def run(self):
        loop = asyncio.get_event_loop()
        loop.create_task(self.create_servers())
        loop.run_forever()

    def cleanup(self):
        self.tmp.close()

    async def create_servers(self):
        for sock in self.sockets:
            print(repr(sock))

            def task_factory(sock):
                sockname = sock.getsockname()

                async def _inner(reader, writer):
                    await self.connection_task(sockname, reader, writer)

                return _inner

            server = await asyncio.start_server(task_factory(sock), sock=sock)
            self.servers.append(server)

    async def maybe_send_error_response(self, http, exc):
        # If we can't send an error, oh well, nothing to be done
        http.info("trying to send error response...")
        if http.conn.our_state not in {h11.IDLE, h11.SEND_RESPONSE}:
            http.info("...but I can't, because our state is",
                         http.conn.our_state)
            return
        try:
            if isinstance(exc, h11.RemoteProtocolError):
                status_code = exc.error_status_hint
            else:
                status_code = 500
            body = str(exc).encode("utf-8")
            http.info("Sending", status_code,
                         "response with", len(body), "bytes")
            headers = http.basic_headers()
            headers.append(("Content-Type", "text/plain; charset=utf-8"))
            headers.append(("Content-Length", str(len(body))))
            res = h11.Response(status_code=status_code, headers=headers)
            await http.send(res)
            await http.send(h11.Data(data=body))
            await http.send(h11.EndOfMessage())
        except Exception as exc:
            http.info("error while sending error response:", exc)

    async def respond_to_request(self, http, req, sockname):
        http.info("Preparing echo response")
        body = b''

        while True:
            event = await http.next_event()
            if type(event) is h11.EndOfMessage:
                break
            assert type(event) is h11.Data
            body += event.data

        environ = environ_from_request(self.cfg, req, sockname, body)
        app = self.app.wsgi()

        def write(stuff):
            pass
        wsgi_response = StartResponse(write)

        result = app(environ, wsgi_response)
        res = h11.Response(
            status_code=wsgi_response.status,
            reason=wsgi_response.reason,
            headers=wsgi_response.headers,
        )

        await http.send(res)
        await http.send(h11.Data(data=body))
        await http.send(h11.EndOfMessage())

    async def connection_task(self, sockname, reader, writer):
        print(repr(sockname))

        http = HTTPConnection(reader, writer)

        while True:
            assert http.conn.states == {
                h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}

            try:
                http.info("Server main loop waiting for request")
                event = await http.next_event()
                http.info("Server main loop got event:", event)
                if type(event) is h11.Request:
                    await self.respond_to_request(http, event, sockname)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                http.info("Error during response handler:", exc)
                await self.maybe_send_error_response(http, exc)

            if http.conn.our_state is h11.MUST_CLOSE:
                http.info("connection is not reusable, so shutting down")
                await writer.drain()
                writer.close()
                return
            else:
                try:
                    http.info("trying to re-use connection")
                    http.conn.start_next_cycle()
                except h11.ProtocolError:
                    states = http.conn.states
                    http.info("unexpected state", states, "-- bailing out")
                    await self.maybe_send_error_response(
                        http,
                        RuntimeError("unexpected state {}".format(states)))
                    await writer.drain()
                    writer.close()
                    return
