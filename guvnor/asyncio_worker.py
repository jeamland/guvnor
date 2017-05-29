import asyncio

from gunicorn.http.wsgi import base_environ
from gunicorn.workers.base import Worker

import h11


def environ_from_request(cfg, req, sockname):
    environ = base_environ(cfg)
    environ.update({
        'REQUEST_METHOD': req.method,
        'SERVER_NAME': sockname[0],
        'SERVER_PORT': str(sockname[1]),
        'SERVER_PROTOCOL': b'HTTP/%s' % req.http_version,
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

    async def connection_task(self, sockname, reader, writer):
        print(repr(sockname))

        conn = h11.Connection(h11.SERVER)
        event = h11.NEED_DATA

        while event is h11.NEED_DATA:
            data = await reader.read()
            conn.receive_data(data)

            event = conn.next_event()
            if event is h11.NEED_DATA:
                if conn.they_are_waiting_for_100_continue:
                    go_ahead = h11.InformationalResponse(
                                    status_code=100,
                                    headers=self.basic_headers())
                    data = conn.send(go_head)
                    writer.write(data)
                    await writer.drain()

        print(repr(event))

        if isinstance(event, h11.Request):
            environ = environ_from_request(self.cfg, event, sockname)
            app = self.app.wsgi()

            def write(stuff):
                pass
            wsgi_response = StartResponse(write)

            result = app(environ, wsgi_response)

            writer.write(conn.send(h11.Response(
                status_code=wsgi_response.status,
                reason=wsgi_response.reason,
                headers=wsgi_response.headers,
            )))
            writer.write(conn.send(h11.Data(data=result)))
            writer.write(conn.send(h11.EndOfMessage()))

            await writer.drain()
