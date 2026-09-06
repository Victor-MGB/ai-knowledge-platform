"""Day 33 (regression): the queue/repo connection self-heals.

The API caches ONE long-lived Postgres connection for the job ledger. If that
socket dies (idle TCP close, postgres restart) every enqueue started returning
503 "the connection is closed" until the container itself was restarted. This
reproduces the failure by killing the underlying connection and asserts the
next statement transparently reconnects.
"""

from __future__ import annotations

import psycopg

from app.core.config import get_settings
from app.queue.repository import PostgresFailSink, PostgresJobRepository, ResilientPostgresConnection


def test_connection_survives_a_closed_socket():
    conn = ResilientPostgresConnection(get_settings().database_url)
    try:
        assert conn.execute("SELECT 1 AS n", one=True)["n"] == 1
        # simulate the socket dropping (server goes away, TCP closes, ...)
        conn._conn.close()
        assert conn._conn.closed
        assert conn.execute("SELECT 1 AS n", one=True)["n"] == 1
    finally:
        conn.close()


class _DyingConnection:
    """Behavioural stand-in: healthy once, then raises the exact psycopg
    OperationalError text seen in production ("the connection is closed")."""

    def __init__(self):
        self.closed = False
        self._backing = None

    def cursor(self):
        raise psycopg.OperationalError("the connection is closed")


def test_in_flight_operational_error_retries_once():
    conn = ResilientPostgresConnection(get_settings().database_url)
    try:
        conn._conn = _DyingConnection()
        # execute hits the alive path, the fake raises, the wrapper reconnects
        assert conn.execute("SELECT 1 AS n", one=True)["n"] == 1
    finally:
        conn.close()


def test_repository_and_sink_route_through_the_resilient_connection():
    conn = ResilientPostgresConnection(get_settings().database_url)
    repo = PostgresJobRepository(conn)
    sink = PostgresFailSink(conn)
    try:
        assert repo.list_for_document("00000000-0000-0000-0000-000000000000") == []
        assert repo.document_status("00000000-0000-0000-0000-000000000000") is None
        assert sink is not None
    finally:
        conn.close()