import asyncio
import os
import urllib.parse

from gunicorn.config import Config

from guvnor.asyncio_worker import AsyncioWorker

from .support import (WSGITestRunner, run_worker, setup_function,
                      teardown_function)


def encode_path(path):
    path = path.encode('utf8')
    path = urllib.parse.quote_from_bytes(path)
    return path.encode('ascii')


def path_test(path):
    req = b'GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'

    runner = WSGITestRunner(req % path)
    runner.run()

    assert runner.wsgi.called
    return runner


def test_basic_paths():
    runner = path_test(encode_path('/'))

    environ = runner.wsgi.environ
    assert environ['PATH_INFO'] == '/'
    assert environ['QUERY_STRING'] == ''


def test_simple_utf8_path():
    expected_path = '/frobnitz™'
    runner = path_test(encode_path(expected_path))

    environ = runner.wsgi.environ
    assert environ['PATH_INFO'] == expected_path
    assert environ['QUERY_STRING'] == ''


def test_basic_query():
    query = 'foo=bar&baz=qux'
    runner = path_test(('/?' + query).encode('utf8'))

    environ = runner.wsgi.environ
    assert environ['QUERY_STRING'] == query


def test_basic_query():
    query = 'foo=bar&baz=qux'
    runner = path_test(encode_path('/?' + query))

    environ = runner.wsgi.environ
    assert environ['QUERY_STRING'] == query


def test_utf8_query():
    query = 'foo=bar&baz=qux&utf8=✔'
    runner = path_test(encode_path('/?' + query))

    environ = runner.wsgi.environ
    assert environ['QUERY_STRING'] == query

