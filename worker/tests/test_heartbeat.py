"""The order in which one beat extends its two clocks.

The lease and the visibility timeout are pushed to the same number of seconds, so that they
can never disagree about who owns a document. They are still two calls against two different
clocks, so whichever runs second lands a few milliseconds further out, and that skew is not
free: it decides what happens to a message the worker hands back.

The skew has to fall on the visibility timeout. If it falls on the lease instead, a returned
message becomes visible fractionally before its own lease dies, so the redelivery finds the
document held, refuses the claim, and burns one of three attempts achieving nothing.
"""

from __future__ import annotations

import threading
import uuid

from consumer import heartbeat as heartbeat_module


class FakeConn:
    def close(self) -> None:
        pass


def test_the_lease_is_extended_before_the_visibility_timeout(monkeypatch):
    calls: list[str] = []
    beaten = threading.Event()

    def fake_extend_lease(*_: object, **__: object) -> bool:
        calls.append("lease")
        return True

    def fake_extend_visibility(_: int) -> None:
        calls.append("visibility")
        beaten.set()

    monkeypatch.setattr(heartbeat_module, "extend_lease", fake_extend_lease)

    with heartbeat_module.Heartbeat(
        FakeConn,
        fake_extend_visibility,
        uuid.uuid4(),
        lease_seconds=120,
        interval_seconds=0.01,
        attempt_count=1,
    ):
        assert beaten.wait(timeout=5), "the heartbeat thread never beat"

    assert calls[:2] == ["lease", "visibility"]
