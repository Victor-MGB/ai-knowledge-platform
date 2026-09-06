"""Resilient psycopg connection for long-lived worker processes.

Workers cache one psycopg connection per service for their whole lifetime. If
that socket dies (idle TCP close, Postgres restart, network blip), every later
statement raises `psycopg.OperationalError: the connection is closed` and the
worker fails every job it touches until the container is recycled — seen at
1,000-doc scale as ~100 consecutive flapping jobs.

This is the processor/embedding counterpart of the queue layer's
`ResilientPostgresConnection`: a psycopg-`Connection`-shaped wrapper that
(a) opens the real connection lazily, (b) exposes the same surface the
repositories already use (`cursor()` / `transaction()` context managers,
`autocommit`, `row_factory`), and (c) drops a dead handle the moment a
statement raises `OperationalError`, so the next cursor/transaction opens a
fresh connection. A run whose write half hit a dead socket now fails once and
is retried by the queue layer's own pass — instead of cascading forever.
"""

import threading
from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection, OperationalError
from psycopg.rows import dict_row


class ResilientConnection:
    """A psycopg Connection look-alike that self-heals a dropped socket."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: Connection | None = None
        self._autocommit = True
        self._row_factory = dict_row
        self._lock = threading.RLock()

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._autocommit = bool(value)
        if self._conn is not None:
            self._conn.autocommit = self._autocommit

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._row_factory = value
        if self._conn is not None:
            self._conn.row_factory = self._row_factory

    def _ensure_open(self) -> Connection:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = psycopg.connect(self._dsn)
            self._conn.autocommit = self._autocommit
            self._conn.row_factory = self._row_factory
        return self._conn

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @contextmanager
    def cursor(self) -> Iterator:
        with self._lock:
            conn = self._ensure_open()
            try:
                with conn.cursor() as cur:
                    yield cur
            except OperationalError:
                self._drop()
                raise

    @contextmanager
    def transaction(self) -> Iterator:
        with self._lock:
            conn = self._ensure_open()
            try:
                with conn.transaction():
                    yield
            except OperationalError:
                self._drop()
                raise

    def close(self) -> None:
        with self._lock:
            self._drop()


def connect(dsn: str) -> ResilientConnection:
    """Drop-in for `psycopg.connect` in worker wiring: same surface the
    repositories need, plus the reconnect discipline described above."""
    return ResilientConnection(dsn)