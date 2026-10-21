import asyncio
import contextlib

import aiohttp
import aiohttp.web
import prometheus_client
import pytest
import pytest_asyncio

import middleware.prometheus


@pytest.fixture
def prometheus_registry():
    """Fresh CollectorRegistry per test so metric values are isolated."""
    return prometheus_client.CollectorRegistry(auto_describe=True)


@pytest.fixture
def app(prometheus_registry):
    """aiohttp Application with prometheus middleware bound to a fresh registry."""
    app = aiohttp.web.Application()
    return middleware.prometheus.add_prometheus_middleware(
        app=app,
        registry=prometheus_registry,
    )


@pytest_asyncio.fixture
async def serve(app, unused_tcp_port):
    """
    Returns an async-context-manager: `async with serve() as base_url: ...`.
    Starts the app server (must be called after routes are registered).
    """

    @contextlib.asynccontextmanager
    async def _serve():
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, '127.0.0.1', unused_tcp_port)
        await site.start()
        try:
            yield f'http://127.0.0.1:{unused_tcp_port}'
        finally:
            await runner.cleanup()

    return _serve


@pytest_asyncio.fixture
async def http_session():
    session = aiohttp.ClientSession()
    try:
        yield session
    finally:
        await session.close()


def get_sample(collector, name: str, **label_filters):
    for metric in collector.collect():
        for sample in metric.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(k) == v for k, v in label_filters.items()):
                return sample
    return None


@pytest.mark.asyncio
async def test_metrics_recorded_on_success(app, serve, http_session):
    async def handler(request):
        return aiohttp.web.Response(text='ok')

    app.router.add_get('/fast', handler)

    async with serve() as base_url:
        async with http_session.get(f'{base_url}/fast') as resp:
            assert resp.status == 200

    sample = get_sample(
        app[middleware.prometheus.APP_REQUEST_LATENCY_SECONDS],
        'request_latency_seconds_count',
        endpoint='/fast',
    )
    assert sample.value == 1

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_TOTAL],
        'requests_total',
        endpoint='/fast',
        status='200',
    )
    assert sample.value == 1

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_CONCURRENCY],
        'requests_concurrency',
        endpoint='/fast',
    )
    assert sample.value == 0


@pytest.mark.asyncio
async def test_metrics_recorded_on_4xx(app, serve, http_session):
    async def handler(request):
        raise aiohttp.web.HTTPBadRequest(text='boom')

    app.router.add_get('/error', handler)

    async with serve() as base_url:
        async with http_session.get(f'{base_url}/error') as resp:
            assert resp.status == 400

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_TOTAL],
        'requests_total',
        endpoint='/error',
        status='400',
    )
    assert sample.value == 1

    sample = get_sample(
        app[middleware.prometheus.APP_REQUEST_LATENCY_SECONDS],
        'request_latency_seconds_count',
        endpoint='/error',
    )
    assert sample.value == 1


@pytest.mark.asyncio
async def test_metrics_recorded_for_long_running_handler(app, serve, http_session):
    """
    Asserts metrics are recorded even when the handler takes a long time to complete
    (aiohttp does not cancel handlers on client disconnect by default).
    """

    async def handler(request):
        # simulate a long DB/CPU-bound payload (recent `PUT /artefacts/metadata` case)
        await asyncio.sleep(2)
        return aiohttp.web.Response(text='done')

    app.router.add_get('/slow', handler)

    async with serve() as base_url:
        async with http_session.get(f'{base_url}/slow') as resp:
            assert resp.status == 200

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_TOTAL],
        'requests_total',
        endpoint='/slow',
        status='200',
    )
    assert sample.value == 1

    sample = get_sample(
        app[middleware.prometheus.APP_REQUEST_LATENCY_SECONDS],
        'request_latency_seconds_count',
        endpoint='/slow',
    )
    assert sample.value == 1

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_CONCURRENCY],
        'requests_concurrency',
        endpoint='/slow',
    )
    assert sample.value == 0


@pytest.mark.asyncio
async def test_metrics_recorded_on_cancelled_handler(app, serve, http_session):
    """
    Asserts metrics are recorded when the handler raises CancelledError
    (e.g. via asyncio.wait_for's timeout or a dispatcher-level cancellation).
    """

    async def handler(request):
        # simulate the wait inside a `asyncio.wait_for` being cancelled
        raise asyncio.CancelledError

    app.router.add_get('/hang', handler)

    async with serve() as base_url:
        with contextlib.suppress(aiohttp.ServerDisconnectedError, aiohttp.ClientConnectionError):
            await http_session.get(f'{base_url}/hang')

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_TOTAL],
        'requests_total',
        endpoint='/hang',
    )
    assert sample is not None
    assert sample.labels['status'] == 'cancelled'

    sample = get_sample(
        app[middleware.prometheus.APP_REQUESTS_CONCURRENCY],
        'requests_concurrency',
        endpoint='/hang',
    )
    assert sample.value == 0


@pytest.mark.asyncio
async def test_concurrency_started_seconds_reset_after_request(app, serve, http_session):
    async def handler(request):
        return aiohttp.web.Response(text='ok')

    app.router.add_get('/ping', handler)

    async with serve() as base_url:
        async with http_session.get(f'{base_url}/ping'):
            pass

    sample = get_sample(
        app[middleware.prometheus.APP_REQUEST_CONCURRENCY_STARTED_SECONDS],
        'request_concurrency_started_seconds',
        endpoint='/ping',
    )
    assert sample.value == 0


def test_histogram_includes_long_tail_buckets(app):
    histogram = app[middleware.prometheus.APP_REQUEST_LATENCY_SECONDS]
    # materialise bucket sample series by creating a labelled child
    histogram.labels(endpoint='/x', method='GET')
    bucket_strs = {
        s.labels['le'] for s in histogram.collect()[0].samples if s.name.endswith('_bucket')
    }
    assert '300.0' in bucket_strs
    assert '900.0' in bucket_strs
    assert '+Inf' in bucket_strs
