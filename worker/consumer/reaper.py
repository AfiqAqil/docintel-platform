"""Two sweeps for rows nobody will otherwise finish.

The normal failure paths all end with somebody writing a status. These cover the two cases
where nobody does.

Architecture section 15 records the trade-off: this runs inside the poll loop rather than on
its own schedule, which is one fewer moving part, and the cost is that a worker service that
is fully down leaves stuck rows unswept until it comes back. An idle worker still sweeps,
because the loop keeps polling.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

# Sweep one: a worker died on every delivery attempt, so the message went to the DLQ without
# anybody writing a status, and the row sits in PROCESSING forever.
#
# The lease must be expired by a clear margin, not merely expired. A lease that expired a
# second ago probably belongs to a worker whose heartbeat is briefly late, and the right
# answer there is redelivery reclaiming it, not the reaper declaring it dead. The margin has
# to cover the whole redrive window, so that by the time this fires, SQS has already given
# up on the message.
REAP_PROCESSING_SQL = """
UPDATE documents
   SET status        = 'FAILED',
       error_message = 'Processing did not complete. The worker holding this document '
                       'stopped responding and the message exhausted its delivery attempts.',
       current_step  = NULL,
       lease_expires_at = NULL,
       completed_at  = now(),
       updated_at    = now()
 WHERE status = 'PROCESSING'
   AND lease_expires_at < now() - make_interval(secs => %(grace_seconds)s)
RETURNING id
"""

# Sweep two: a user asked for an upload slot and never used it, so no object exists and no
# S3 event will ever arrive.
#
# This one is a guess, which is why it writes EXPIRED rather than FAILED. EXPIRED is not
# terminal: the claim statement accepts it, so if an event does turn up later, proving the
# object exists after all, the document processes normally. Writing FAILED here would close
# a door that is not actually shut.
REAP_UPLOADING_SQL = """
UPDATE documents
   SET status     = 'EXPIRED',
       updated_at = now()
 WHERE status = 'UPLOADING'
   AND created_at < now() - make_interval(secs => %(expiry_seconds)s)
RETURNING id
"""


@dataclass(frozen=True)
class ReapResult:
    stuck_processing: int
    abandoned_uploads: int

    @property
    def total(self) -> int:
        return self.stuck_processing + self.abandoned_uploads


def reap(
    conn: psycopg.Connection, processing_grace_seconds: int, upload_expiry_seconds: int
) -> ReapResult:
    """Run both sweeps. Safe to call from every worker on every poll.

    Concurrent reapers cannot double count: each statement is a single UPDATE, so a row can
    only be returned to whichever transaction commits first.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(REAP_PROCESSING_SQL, {"grace_seconds": processing_grace_seconds})
        stuck = len(cur.fetchall())

        cur.execute(REAP_UPLOADING_SQL, {"expiry_seconds": upload_expiry_seconds})
        abandoned = len(cur.fetchall())

    conn.commit()
    return ReapResult(stuck_processing=stuck, abandoned_uploads=abandoned)
