from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException


class UploadTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "请求体超过允许大小（52MB）")


class RequestSizeLimit:
    """Bound multipart bytes before Starlette spools uploaded files to disk."""
    def __init__(self, app, max_bytes=52 * 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        try:
            too_large = length is not None and int(length) > self.max_bytes
        except ValueError:
            too_large = True
        rejection = JSONResponse({"detail": "请求体超过允许大小（52MB）"}, status_code=413)
        if too_large:
            return await rejection(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                raise UploadTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except UploadTooLarge:
            await rejection(scope, receive, send)
