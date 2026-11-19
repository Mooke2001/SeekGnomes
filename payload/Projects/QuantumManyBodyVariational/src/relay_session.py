"""Optional synchronous owner of one persistent asynchronous HTTP session.

The event loop, client and connection pool live on one dedicated thread. A
caller must close the owner explicitly (normally through RelayClient's context
manager). Requests are serialized; no request is retried by this owner.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable, TypeVar

import httpx


T = TypeVar("T")


class PersistentSession:
    def __init__(self, transport=None):
        self._transport = transport
        self._loop = None
        self._client = None
        self._thread = None
        self._lock = threading.RLock()
        self._closed = False

    @property
    def in_session_thread(self):
        return threading.current_thread() is self._thread

    def _start(self):
        if self._thread is not None:
            return
        ready = threading.Event()
        # Create before starting the worker, so a local loop-creation failure
        # raises in the caller instead of leaving it waiting for readiness.
        loop = asyncio.new_event_loop()
        self._loop = loop

        def worker():
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor(timeout=1))
                loop.close()

        self._thread = threading.Thread(target=worker, name="relay-http-session", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            self._loop = None
            loop.close()
            raise
        ready.wait()

    async def _perform(self, operation):
        if self._client is None:
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=1,
                                  keepalive_expiry=None)
            transport = self._transport
            if transport is None:
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False, limits=limits)
            self._client = httpx.AsyncClient(
                transport=transport, follow_redirects=False, trust_env=False, limits=limits)
        # Persistent transport must not introduce cross-request cookie auth.
        self._client.cookies.clear()
        try:
            return await operation(self._client)
        finally:
            self._client.cookies.clear()

    def run(self, operation: Callable[[httpx.AsyncClient], Awaitable[T]]) -> T:
        if self.in_session_thread:
            raise RuntimeError("a synchronous session request cannot run on its own event loop")
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session is closed")
            self._start()
            return asyncio.run_coroutine_threadsafe(self._perform(operation), self._loop).result()

    async def _reset_connection(self):
        client, self._client = self._client, None
        if client is None:
            return False
        client.cookies.clear()
        await client.aclose()
        return True

    def reset_connection(self):
        """Explicitly discard the pool; never send or replay a request.

        Keep the owner event loop alive and create a fresh client lazily on the
        next host-initiated request. The request lock prevents an active request
        from being interrupted by a reset.
        """
        if self.in_session_thread:
            raise RuntimeError("a synchronous session cannot reset from its own event loop")
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session is closed")
            if self._loop is None or self._client is None:
                return False
            try:
                return asyncio.run_coroutine_threadsafe(
                    self._reset_connection(), self._loop).result()
            except Exception:
                # Closing a broken transport can raise protocol exceptions that
                # quote unapproved headers. Expose a fixed lifecycle error only.
                raise RuntimeError("HTTP session connection reset failed") from None

    async def _aclose(self):
        if self._client is not None:
            await self._client.aclose()

    def close(self):
        if self.in_session_thread:
            raise RuntimeError("a synchronous session cannot close from its own event loop")
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._loop is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(self._aclose(), self._loop).result()
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    raise RuntimeError("HTTP session event loop did not stop")
