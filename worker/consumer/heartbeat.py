"""One heartbeat, extending two things at once.

The database lease and the SQS visibility timeout answer the same question: who owns this
document right now. If they are extended separately they can disagree, and every way they
disagree is a bug. A visibility timeout that outlasts the lease lets a second worker claim a
document nobody will redeliver. A lease that outlasts the visibility timeout lets SQS hand
the message to a second worker who then cannot claim it, so it spins until the redrive limit.

So there is one timer, pushing both to the same point, and if the lease is gone the loop
stops rather than carrying on with work it is no longer allowed to record.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

import psycopg

from consumer.claim import heartbeat as extend_lease

log = logging.getLogger("worker.heartbeat")


class Heartbeat:
    """Extends the lease and the visibility timeout on a background thread.

    A thread rather than an async task because the graph is synchronous and CPU bound in
    places, such as rendering PDF pages, and an event loop would be blocked exactly when the
    heartbeat matters most.
    """

    def __init__(
        self,
        conn_factory: Callable[[], psycopg.Connection],
        extend_visibility: Callable[[int], None],
        document_id: uuid.UUID,
        lease_seconds: int,
        interval_seconds: int,
        attempt_count: int,
    ) -> None:
        self._conn_factory = conn_factory
        self._extend_visibility = extend_visibility
        self._document_id = document_id
        self._lease_seconds = lease_seconds
        # The token this worker's own claim returned. A reclaim by anyone else bumps it,
        # which is how a beat from a superseded worker is refused.
        self._attempt_count = attempt_count
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._current_step: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def lease_lost(self) -> bool:
        """True once the lease could not be extended. The caller must abandon the document."""
        return self._lost.is_set()

    def set_step(self, node: str) -> None:
        """Record which graph node is running, written on the next beat.

        Written by the heartbeat rather than by the graph, because the graph performs no
        database calls at all and that boundary is what makes it testable.
        """
        self._current_step = node

    def __enter__(self) -> Heartbeat:
        # A separate connection, because the main one is in use by the run and a heartbeat
        # that has to wait for it is not a heartbeat.
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        conn = self._conn_factory()
        try:
            while not self._stop.wait(self._interval):
                try:
                    # The lease is extended first, and only then the visibility timeout.
                    # Both are pushed to the same number of seconds, but whichever is set
                    # last lands a few milliseconds further out, and that skew has to fall
                    # on the visibility timeout. The other way round the message becomes
                    # visible fractionally before the lease dies, so a worker that returns a
                    # message finds its own lease still live on the redelivery and burns an
                    # attempt doing nothing.
                    held = extend_lease(
                        conn,
                        self._document_id,
                        self._lease_seconds,
                        self._attempt_count,
                        self._current_step,
                    )
                    self._extend_visibility(self._lease_seconds)
                except Exception:
                    # A failed beat is not fatal on its own: the next one may succeed well
                    # before the lease runs out. Losing the lease is what is fatal, and that
                    # is decided by the database, not by this exception.
                    log.warning("heartbeat failed, will retry on the next beat", exc_info=True)
                    continue

                if not held:
                    log.warning("this attempt is no longer current, abandoning the document")
                    self._lost.set()
                    return
        finally:
            conn.close()
