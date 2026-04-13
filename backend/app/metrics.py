import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# API metrics
REQUEST_COUNT = Counter(
    "spatiumddi_api_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status_code"],
)
REQUEST_DURATION = Histogram(
    "spatiumddi_api_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path_template"],
)
ACTIVE_REQUESTS = Gauge(
    "spatiumddi_api_active_requests",
    "Number of currently active HTTP requests",
)

# Auth metrics
AUTH_LOGIN_COUNT = Counter(
    "spatiumddi_auth_login_total",
    "Total login attempts",
    ["method", "result"],
)
AUTH_TOKEN_USAGE = Counter(
    "spatiumddi_auth_token_usage_total",
    "API token usage count",
    ["scope"],
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: object) -> Response:
        # Skip metrics endpoint itself to avoid recursion
        if request.url.path == "/metrics":
            return await call_next(request)  # type: ignore[arg-type]

        path_template = request.scope.get("route", {})
        if hasattr(path_template, "path"):
            path_label = path_template.path
        else:
            path_label = request.url.path

        method = request.method
        ACTIVE_REQUESTS.inc()
        start = time.perf_counter()
        status_code = 500  # fallback if call_next raises

        try:
            response: Response = await call_next(request)  # type: ignore[arg-type]
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - start
            ACTIVE_REQUESTS.dec()
            REQUEST_COUNT.labels(
                method=method,
                path_template=path_label,
                status_code=status_code,
            ).inc()
            REQUEST_DURATION.labels(method=method, path_template=path_label).observe(duration)


async def metrics_endpoint(request: Request) -> Response:
    """Prometheus scrape endpoint at /metrics."""
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
