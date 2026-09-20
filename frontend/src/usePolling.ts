import { useCallback, useEffect, useRef, useState } from "react";
import { listDocuments } from "./api";
import type { DocumentOut, DocumentStatus } from "./types";

const FAST_INTERVAL_MS = 3000;
const SLOW_INTERVAL_MS = 15000;

// Everything else (COMPLETED, FAILED, EXPIRED) is settled for polling
// purposes per ARCHITECTURE.md section 11. EXPIRED is deliberately excluded
// here even though the backend can still revive it: an abandoned upload
// would otherwise keep the 3s poll running forever, and the 15s poll is
// what picks up the rare EXPIRED-then-PROCESSING case.
const IN_FLIGHT_STATUSES: ReadonlySet<DocumentStatus> = new Set([
  "UPLOADING",
  "QUEUED",
  "PROCESSING",
]);

function isInFlight(doc: DocumentOut): boolean {
  return IN_FLIGHT_STATUSES.has(doc.status);
}

export type StatusTransition = {
  document: DocumentOut;
  from: DocumentStatus;
};

/**
 * Polls GET /api/documents on the schedule from ARCHITECTURE.md section 11:
 *
 * - 3s while any document is non-terminal (in flight)
 * - back off to 15s once nothing is in flight
 * - refetch immediately on window focus
 * - call onTransition only the first time a document is observed moving
 *   into COMPLETED or FAILED, never on every poll that still sees it there
 * - a failed poll sets `error` and is simply retried on the next tick;
 *   there is no backoff or retry library, the tick IS the retry
 */
export function usePolling(onTransition: (transition: StatusTransition) => void): {
  documents: DocumentOut[];
  error: string | null;
  refresh: () => void;
} {
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Last status seen per document id, used to detect a *transition* rather
  // than just a terminal status, so a toast fires once, not on every poll.
  const prevStatuses = useRef(new Map<string, DocumentStatus>());
  // The very first poll seeds prevStatuses; it must never itself count as
  // a transition, or a page load full of already-COMPLETED documents would
  // toast for all of them.
  const isFirstPoll = useRef(true);
  const timerRef = useRef<number | undefined>(undefined);
  const activeRef = useRef(true);
  // Every poll takes a ticket, and only the holder of the newest one is allowed to touch
  // state or schedule the next tick.
  //
  // Without this, a focus event starting a poll while one is already in flight produced two
  // problems, and the second is the serious one. An older response could land after a newer
  // one and overwrite current statuses with stale ones. Worse, both polls reach the
  // setTimeout at the end, so the single loop becomes two, and every subsequent focus event
  // doubles it again. Clearing the timer at the top of poll() does not help, because a poll
  // that is awaiting its fetch has no timer to clear yet.
  const latestRequest = useRef(0);
  // Kept in a ref so the poll loop (defined once) always calls the latest
  // callback without needing to be recreated, which would otherwise restart
  // the schedule on every render.
  const onTransitionRef = useRef(onTransition);
  onTransitionRef.current = onTransition;

  const poll = useCallback(async () => {
    if (timerRef.current !== undefined) {
      window.clearTimeout(timerRef.current);
      timerRef.current = undefined;
    }

    const requestId = ++latestRequest.current;
    const isStale = (): boolean => requestId !== latestRequest.current;

    let nextDelay = FAST_INTERVAL_MS;
    try {
      const { items } = await listDocuments();

      // A newer poll started while this one was in flight. Its answer is more current than
      // ours, so this response is dropped entirely: no state, no toasts, no timer.
      if (isStale()) {
        return;
      }

      for (const doc of items) {
        const prev = prevStatuses.current.get(doc.id);
        const isNewCompletion =
          !isFirstPoll.current &&
          prev !== doc.status &&
          (doc.status === "COMPLETED" || doc.status === "FAILED");
        if (isNewCompletion) {
          onTransitionRef.current({ document: doc, from: prev ?? doc.status });
        }
        prevStatuses.current.set(doc.id, doc.status);
      }
      isFirstPoll.current = false;

      if (activeRef.current) {
        setDocuments(items);
        setError(null);
      }
      nextDelay = items.some(isInFlight) ? FAST_INTERVAL_MS : SLOW_INTERVAL_MS;
    } catch (e) {
      // Nothing lost: the next poll returns the full current state, so a
      // failed request just waits for the tick already scheduled below.
      if (isStale()) {
        return;
      }
      if (activeRef.current) {
        setError(e instanceof Error ? e.message : "Failed to load documents.");
      }
    }

    // Only the newest poll schedules the next tick, so there is always exactly one loop
    // however many focus events arrive while a request is in flight.
    if (activeRef.current && !isStale()) {
      timerRef.current = window.setTimeout(() => void poll(), nextDelay);
    }
  }, []);

  useEffect(() => {
    activeRef.current = true;
    void poll();

    window.addEventListener("focus", poll);
    return () => {
      activeRef.current = false;
      window.removeEventListener("focus", poll);
      if (timerRef.current !== undefined) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, [poll]);

  return { documents, error, refresh: poll };
}
