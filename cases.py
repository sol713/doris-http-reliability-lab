# SPDX-License-Identifier: Apache-2.0
# Generated-by: OpenAI Codex (GPT-6)
"""Six experiments: real HTTP, synthetic DNS, and observable cleanup."""

from __future__ import annotations

import asyncio
import math
import socket
import threading
import time
from contextlib import contextmanager, suppress
from unittest.mock import patch


def require_loopback(address) -> None:
    if not isinstance(address, tuple) or address[0] != "127.0.0.1":
        raise OSError("lab rejected a non-127.0.0.1 connection")


@contextmanager
def network_guard():
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(self, address):
        require_loopback(address)
        return connect(self, address)

    def guarded_ex(self, address):
        require_loopback(address)
        return connect_ex(self, address)

    def no_dns(*args, **kwargs):
        raise OSError("lab rejected an unexpected OS DNS lookup")

    with (
        patch.object(socket.socket, "connect", guarded),
        patch.object(socket.socket, "connect_ex", guarded_ex),
        patch.object(socket, "getaddrinfo", no_dns),
    ):
        yield


@contextmanager
def resources(module):
    sessions, connectors = [], []
    original_session = module.aiohttp.ClientSession
    original_connector = module.aiohttp.TCPConnector

    def session(*args, **kwargs):
        assert all(value.closed for value in sessions)
        value = original_session(*args, **kwargs)
        sessions.append(value)
        return value

    def connector(*args, **kwargs):
        value = original_connector(*args, **kwargs)
        connectors.append(value)
        return value

    with (
        patch.object(module.aiohttp, "ClientSession", session),
        patch.object(module.aiohttp, "TCPConnector", connector),
    ):
        yield sessions, connectors
        assert all(value.closed for value in sessions)
        assert all(value.closed for value in connectors)


class Loopback:
    """One listener with Host-based plans, representing logical FE attempts."""

    def __init__(self, plans):
        self.plans = plans  # host -> (HTTP status; zero means stall, response delay)
        self.requests = []
        self.closed_events = []
        self.tasks = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        children = []
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            host = next(
                line.split(b":", 1)[1].strip().decode().split(":")[0]
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"host:")
            )
            status, delay = self.plans[host]
            row = {"host": host, "peer_eof": False}
            self.requests.append(row)
            closed = asyncio.Event()
            self.closed_events.append(closed)
            eof = asyncio.create_task(reader.read())
            children.append(eof)
            if status:
                waiting = asyncio.create_task(asyncio.sleep(delay))
                children.append(waiting)
                done, _ = await asyncio.wait(
                    [waiting, eof], return_when=asyncio.FIRST_COMPLETED
                )
                if eof not in done:
                    writer.write(
                        f"HTTP/1.1 {status} Lab\r\nContent-Length: 2\r\n"
                        "Connection: keep-alive\r\n\r\nok".encode()
                    )
                    await writer.drain()
            await asyncio.wait_for(eof, 1)
            row["peer_eof"] = True
            closed.set()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for child in children:
                if not child.done():
                    child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def observed_eof(self):
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in self.closed_events)), 1
        )
        assert all(row["peer_eof"] for row in self.requests)


def make_client(module, server, hosts=("127.0.0.1",), total=0.05, connect=0.3):
    return module.DorisHTTPClient(
        user="lab_user",
        password="",
        allowed_endpoints={"fe": {(host, server.port) for host in hosts}},
        total_timeout_seconds=total,
        connect_timeout_seconds=connect,
        read_timeout_seconds=0.3,
    )


async def get(client, server, host="127.0.0.1"):
    return await asyncio.wait_for(
        client.get(role="fe", host=host, port=server.port, path="/metrics"), 1
    )


def answer(port, addresses=("127.0.0.1",)):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
        for ip in addresses
    ]


def measured(name, category, started, tracked, **details):
    sessions, connectors = tracked
    assert all(value.closed for value in sessions + connectors)
    return dict(
        case=name,
        classification=category,
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        sessions=len(sessions),
        connectors=len(connectors),
        resources_closed=True,
        **details,
    )


async def nan_case(base, fixed):
    # Same acceleration used by the existing regression: production fallback is 30s.
    base.DEFAULT_TOTAL_TIMEOUT_SECONDS = fixed.DEFAULT_TOTAL_TIMEOUT_SECONDS = 0.04
    async with Loopback({"127.0.0.1": (200, 0.12)}) as server:
        started = time.monotonic()
        with resources(base) as tracked:
            client = make_client(base, server, total=math.nan)
            assert not math.isfinite(client.total_timeout_seconds)  # Expected RED.
            assert (await get(client, server)).status == 200
            baseline_ms = (time.monotonic() - started) * 1000
            repaired = make_client(fixed, server, total=math.nan)
            assert repaired.total_timeout_seconds == 0.04  # GREEN.
            repair_start = time.monotonic()
            try:
                await get(repaired, server)
            except fixed.DorisHTTPRequestError as exc:
                assert str(exc) == "Doris HTTP request timed out"
            else:
                raise AssertionError("repaired NaN request did not time out")
            repair_ms = (time.monotonic() - repair_start) * 1000
            await server.observed_eof()
            return measured(
                "nan_red_green",
                "confirmed defect; existing PR #239",
                started,
                tracked,
                baseline_deadline_check="RED: NaN retained",
                patched_deadline_check="GREEN: finite fallback",
                baseline_ms=round(baseline_ms, 3),
                patched_ms=round(repair_ms, 3),
                lab_fallback_seconds=0.04,
                production_fallback_seconds=30,
                tcp_requests=len(server.requests),
            )


async def failover_case(base):
    hosts = ("fe-a.invalid", "fe-b.invalid")
    async with Loopback(
        dict(zip(hosts, [(503, 0.08), (200, 0.08)], strict=True))
    ) as server:

        async def resolve(host, port, **kwargs):
            assert host in hosts and port == server.port
            return answer(port)

        with (
            patch.object(asyncio.get_running_loop(), "getaddrinfo", resolve),
            resources(base) as tracked,
        ):
            started = time.monotonic()
            client = make_client(base, server, hosts, total=0.12)
            response = await asyncio.wait_for(
                client.get_first_available(
                    role="fe", hosts=hosts, port=server.port, path="/metrics"
                ),
                1,
            )
            assert response.status == 200
            assert time.monotonic() - started > 0.15
            assert [row["host"] for row in server.requests] == list(hosts)
            await server.observed_eof()
            return measured(
                "per_fe_budget",
                "expected per-request retry behavior",
                started,
                tracked,
                total_per_attempt_seconds=0.12,
                attempts=2,
            )


async def dns_budget_case(base):
    host, calls = "dns-lab.invalid", []
    async with Loopback({host: (200, 0.03)}) as server:

        async def resolve(requested, port, **kwargs):
            assert (requested, port) == (host, server.port)
            calls.append(requested)
            await asyncio.sleep(0.09)
            # A second answer would be prohibited data, never a real network target.
            return answer(
                port, ("127.0.0.1",) if len(calls) == 1 else ("169.254.169.254",)
            )

        with (
            patch.object(asyncio.get_running_loop(), "getaddrinfo", resolve),
            resources(base) as tracked,
        ):
            started = time.monotonic()
            assert (
                await get(make_client(base, server, (host,)), server, host)
            ).status == 200
            assert time.monotonic() - started > 0.11
            assert len(calls) == len(server.requests) == 1
            await server.observed_eof()
            return measured(
                "dns_plus_http",
                "budget-scope design question",
                started,
                tracked,
                dns_delay_seconds=0.09,
                http_delay_seconds=0.03,
                http_total_seconds=0.05,
                dns_lookups=1,
                address_pinned=True,
            )


async def dns_timeout_case(base):
    host, cancelled = "dns-lab.invalid", asyncio.Event()
    async with Loopback({host: (200, 0)}) as server:

        async def resolve(requested, port, **kwargs):
            assert (requested, port) == (host, server.port)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(asyncio.get_running_loop(), "getaddrinfo", resolve),
            resources(base) as tracked,
        ):
            started = time.monotonic()
            try:
                await get(
                    make_client(base, server, (host,), total=0.03, connect=0.06),
                    server,
                    host,
                )
            except base.DorisHTTPRequestError as exc:
                assert str(exc) == "Doris HTTP hostname resolution failed"
            else:
                raise AssertionError("stalled DNS did not time out")
            assert cancelled.is_set() and not server.requests and not tracked[0]
            return measured(
                "dns_connect_bound",
                "infinite-caller-wait hypothesis disproved",
                started,
                tracked,
                connect_seconds=0.06,
                resolver_cancelled=True,
            )


async def executor_cancel_case(base):
    host, loop = "dns-lab.invalid", asyncio.get_running_loop()
    entered, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
    async with Loopback({host: (200, 0)}) as server:

        def blocking(requested, port, family=0, type=0, proto=0, flags=0):
            assert (requested, port) == (host, server.port)
            assert (family, type) == (socket.AF_UNSPEC, socket.SOCK_STREAM)
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(1), "private resolver safety bound expired"
                return answer(port)
            finally:
                loop.call_soon_threadsafe(finished.set)

        with patch.object(socket, "getaddrinfo", blocking), resources(base) as tracked:
            task = asyncio.create_task(
                get(make_client(base, server, (host,)), server, host)
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                started = time.monotonic()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancellation did not propagate")
                cancel_ms = (time.monotonic() - started) * 1000
                worker_remained = not finished.is_set()
                assert worker_remained and not server.requests and not tracked[0]
            finally:
                release.set()
                await asyncio.wait_for(finished.wait(), 1)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            return measured(
                "native_dns_cancel",
                "bounded caller; resolver worker can continue",
                started,
                tracked,
                cancellation_ms=round(cancel_ms, 3),
                worker_running_at_cancel_return=worker_remained,
                worker_released_and_finished=True,
            )


async def ssrf_case(base):
    host = "dns-lab.invalid"
    async with Loopback({host: (200, 0)}) as server:

        async def resolve(requested, port, **kwargs):
            assert (requested, port) == (host, server.port)
            return answer(port, ("127.0.0.1", "169.254.169.254"))

        with (
            patch.object(asyncio.get_running_loop(), "getaddrinfo", resolve),
            resources(base) as tracked,
        ):
            started = time.monotonic()
            try:
                await get(make_client(base, server, (host,)), server, host)
            except base.DorisHTTPPolicyError as exc:
                assert "prohibited address" in str(exc)
            else:
                raise AssertionError("mixed prohibited DNS answer was accepted")
            assert not server.requests and not tracked[0] and not tracked[1]
            return measured(
                "mixed_dns_rejected",
                "SSRF boundary preserved",
                started,
                tracked,
                tcp_requests=0,
                metadata_address_is_synthetic=True,
            )


async def run_cases(base, fixed):
    with network_guard():
        return [
            await nan_case(base, fixed),
            await failover_case(base),
            await dns_budget_case(base),
            await dns_timeout_case(base),
            await executor_cancel_case(base),
            await ssrf_case(base),
        ]
