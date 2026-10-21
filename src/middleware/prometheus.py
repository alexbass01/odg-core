import asyncio
import datetime
import logging

import aiohttp.typedefs
import aiohttp.web
import aiohttp.web_exceptions
import prometheus_client

import middleware.auth


logger = logging.getLogger(__name__)

# Metric names — these appear in Prometheus exposition; changing them is a breaking change.
METRIC_REQUEST_CONCURRENCY_STARTED_SECONDS = 'request_concurrency_started_seconds'
METRIC_REQUEST_LATENCY_SECONDS = 'request_latency_seconds'
METRIC_REQUESTS_CONCURRENCY = 'requests_concurrency'
METRIC_REQUESTS_TOTAL = 'requests_total'

# aiohttp app storage keys
APP_REQUEST_CONCURRENCY_STARTED_SECONDS: aiohttp.web.AppKey[prometheus_client.Gauge] = (
    aiohttp.web.AppKey('request_concurrency_started_seconds', prometheus_client.Gauge)
)
APP_REQUEST_LATENCY_SECONDS: aiohttp.web.AppKey[prometheus_client.Histogram] = aiohttp.web.AppKey(
    'request_latency_seconds', prometheus_client.Histogram,
)
APP_REQUESTS_CONCURRENCY: aiohttp.web.AppKey[prometheus_client.Gauge] = aiohttp.web.AppKey(
    'requests_concurrency',
    prometheus_client.Gauge,
)
APP_REQUESTS_TOTAL: aiohttp.web.AppKey[prometheus_client.Counter] = aiohttp.web.AppKey(
    'requests_total',
    prometheus_client.Counter,
)
SLOW_REQUEST_THRESHOLD_SECONDS = 30.0


@middleware.auth.noauth
class Metrics(aiohttp.web.View):
    async def get(self):
        """
        ---
        tags:
        - Metrics
        responses:
          "200":
            description: Successful operation.
            content:
              text/plain:
                schema:
                  type: string
        """
        return aiohttp.web.Response(
            body=prometheus_client.generate_latest(),
            content_type='text/plain',
        )


def add_prometheus_middleware(
    app: aiohttp.web.Application,
    registry: prometheus_client.CollectorRegistry | None = None,
) -> aiohttp.typedefs.Middleware:

    @aiohttp.web.middleware
    async def middleware(
        request: aiohttp.web.Request,
        handler: aiohttp.typedefs.Handler,
    ) -> aiohttp.web.StreamResponse:
        start_time = datetime.datetime.now()
        start_timestamp = start_time.timestamp()
        request.app[APP_REQUESTS_CONCURRENCY].labels(request.path, request.method).inc()
        request.app[APP_REQUEST_CONCURRENCY_STARTED_SECONDS].labels(
            request.path,
            request.method,
        ).set(start_timestamp)

        status = None
        response = None
        try:
            response = await handler(request)
            # handler may return None if the client disconnected mid-flight
            status = response.status if response is not None else 'disconnected'
        except aiohttp.web_exceptions.HTTPException as e:
            status = e.status
            raise
        except asyncio.CancelledError, GeneratorExit:
            status = 'cancelled'
            raise
        except BaseException:
            status = 'error'
            raise
        finally:
            latency_seconds = (datetime.datetime.now() - start_time).total_seconds()

            request.app[APP_REQUESTS_CONCURRENCY].labels(request.path, request.method).dec()
            request.app[APP_REQUEST_CONCURRENCY_STARTED_SECONDS].labels(
                request.path,
                request.method,
            ).set(0)

            request.app[APP_REQUEST_LATENCY_SECONDS].labels(
                request.path,
                request.method,
            ).observe(latency_seconds)

            if status is not None:
                request.app[APP_REQUESTS_TOTAL].labels(
                    request.path,
                    request.method,
                    status,
                ).inc()

            if latency_seconds > SLOW_REQUEST_THRESHOLD_SECONDS:
                logger.warning(
                    f'slow request: {request.method} {request.path} took '
                    f'{latency_seconds:.1f}s ({status=})',
                )

        return response

    registry_kwargs = {'registry': registry} if registry is not None else {}

    app[APP_REQUEST_LATENCY_SECONDS] = prometheus_client.Histogram(
        name=METRIC_REQUEST_LATENCY_SECONDS,
        documentation='Request latency (seconds)',
        labelnames=['endpoint', 'method'],
        buckets=(
            0.005,
            0.025,
            0.1,
            0.5,
            2.5,
            10.0,
            30.0,
            60.0,
            120.0,
            300.0,
            900.0,
            float('inf'),
        ),
        **registry_kwargs,
    )
    app[APP_REQUESTS_CONCURRENCY] = prometheus_client.Gauge(
        name=METRIC_REQUESTS_CONCURRENCY,
        documentation='Requests currently in progress',
        labelnames=['endpoint', 'method'],
        **registry_kwargs,
    )
    app[APP_REQUEST_CONCURRENCY_STARTED_SECONDS] = prometheus_client.Gauge(
        name=METRIC_REQUEST_CONCURRENCY_STARTED_SECONDS,
        documentation='Unix timestamp when the currently in-progress request started (0=none)',
        labelnames=['endpoint', 'method'],
        **registry_kwargs,
    )
    app[APP_REQUESTS_TOTAL] = prometheus_client.Counter(
        name=METRIC_REQUESTS_TOTAL,
        documentation='Requests total',
        labelnames=['endpoint', 'method', 'status'],
        **registry_kwargs,
    )

    app.middlewares.insert(0, middleware)

    return app
