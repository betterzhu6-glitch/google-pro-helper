"""Protect the loopback console from requests initiated by other websites."""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class LocalConsoleSecurity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        host = hosts[0].lower() if len(hosts) == 1 else ""
        try:
            parsed = urlsplit("//" + host)
            valid_host = (
                parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
            authority = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
            valid_host = valid_host and host in {authority, f"{authority}:{parsed.port}"}
        except ValueError:
            valid_host = False

        # A fixed loopback Host allowlist also prevents DNS rebinding.
        error = None
        if not valid_host:
            error = (400, "请使用 localhost 或 127.0.0.1 打开本地控制台")
        elif headers.get("sec-fetch-site", "").lower() == "cross-site":
            error = (403, "不允许跨站访问本地控制台")
        elif headers.get("origin") is not None and headers["origin"] != f"{scope['scheme']}://{host}":
            error = (403, "不允许跨来源访问本地控制台")
        elif scope["method"] not in {"GET", "HEAD", "OPTIONS"} and headers.get("x-console-request") != "1":
            # Cross-origin pages cannot add this header without a CORS preflight.
            # The console intentionally does not enable CORS.
            error = (403, "缺少本地控制台请求标记，请刷新页面后重试")

        async def secure_send(message):
            if message["type"] == "http.response.start":
                policy = {
                    b"cache-control": b"no-store",
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"content-security-policy": b"frame-ancestors 'none'",
                    b"referrer-policy": b"no-referrer",
                }
                message = dict(message)
                message["headers"] = [
                    (name, value) for name, value in message.get("headers", [])
                    if name.lower() not in policy
                ] + list(policy.items())
            await send(message)

        if error:
            response = JSONResponse({"detail": error[1]}, status_code=error[0])
            return await response(scope, receive, secure_send)
        await self.app(scope, receive, secure_send)
