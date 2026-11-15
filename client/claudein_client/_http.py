"""Just enough HTTP/1.1 to talk to ClaudeIn on 127.0.0.1, using only Python's own parts (no add-ons to install)."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

CONNECT_TIMEOUT_S = 5.0


class Response:
    def __init__(self, status: int, headers: dict[str, str], reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.status = status
        self.headers = headers
        self._reader = reader
        self._writer = writer

    async def chunks(self) -> AsyncIterator[bytes]:
        """The body as it arrives (chunked, sized, or until the connection closes)."""
        r = self._reader
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = await r.readline()
                if not size_line:
                    return
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    await r.readline()
                    return
                data = await r.readexactly(size)
                await r.readline()  # the CRLF after each chunk
                yield data
        elif "content-length" in self.headers:
            left = int(self.headers["content-length"])
            while left > 0:
                data = await r.read(min(65536, left))
                if not data:
                    return
                left -= len(data)
                yield data
        else:
            while True:
                data = await r.read(65536)
                if not data:
                    return
                yield data

    async def lines(self) -> AsyncIterator[str]:
        buf = b""
        async for chunk in self.chunks():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", errors="replace")
        if buf:
            yield buf.decode("utf-8", errors="replace")

    async def json(self) -> dict:
        body = b"".join([c async for c in self.chunks()])
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def request(port: int, method: str, path: str, token: str, body: dict | None = None,
                  host: str = "127.0.0.1") -> Response:
    """Sends one request and reads the status and headers. Raises OSError when nothing is listening."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT_S)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
    head = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}", f"Authorization: Bearer {token}",
            "Connection: close", "Accept: application/json, text/event-stream"]
    if body is not None:
        head += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + payload)
    await writer.drain()
    status_line = await reader.readline()
    if not status_line:
        writer.close()
        raise ConnectionResetError("ClaudeIn closed the connection without answering.")
    parts = status_line.decode("latin-1").split(" ", 2)
    status = int(parts[1])
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return Response(status, headers, reader, writer)
