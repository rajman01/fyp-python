"""Job progress reporting (Task 12).

A large plan is generated as a background job: the API queues it, a worker
drives it, and the browser polls for progress. The slowest steps are here in
the drawing engine, so this is where the progress that matters comes from --
reading a million points, triangulating them, tracing contours, writing the
DXF and converting it.

The engine writes into the *same* Redis job record the API created, keyed by a
job id that travels with the payload. Reporting back up the HTTP call was the
alternative, and it does not work: the API is blocked waiting for that very
response, so nothing it learned could reach the browser until the work had
already finished.

Reporting is always best-effort. A plan that draws correctly but could not
update a progress bar has succeeded, so every failure here is swallowed and
logged rather than raised.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

#: Share of the progress bar the API has already used for its own steps
#: (preparing the plan and exporting the points) before the engine starts.
ENGINE_START_PERCENT = 40
ENGINE_END_PERCENT = 99

_client = None
_unavailable = False


def _redis():
    """The shared Redis client, or None when it cannot be reached.

    Looked up lazily so an engine running without Redis -- generating plans
    synchronously, or in tests -- costs nothing and logs once.
    """
    global _client, _unavailable
    if _client is not None or _unavailable:
        return _client

    url = os.getenv("REDIS_URI") or os.getenv("REDIS_URL")
    if not url:
        _unavailable = True
        return None

    try:
        import redis  # imported lazily: only needed for background jobs

        _client = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        _client.ping()
        logger.info("progress reporting enabled")
    except Exception as exc:
        logger.warning("progress reporting unavailable: %s", exc)
        _unavailable = True
        _client = None

    return _client


class JobProgress:
    """Reports one job's progress, or does nothing when there is no job.

    A null instance -- no job id, or no Redis -- is the normal case for a plan
    generated inline, so callers never have to check.
    """

    def __init__(self, job_id: Optional[str]):
        self.job_id = job_id
        self._key = f"plan:job:{job_id}" if job_id else None
        self._last_percent = -1

    @property
    def active(self) -> bool:
        return bool(self._key) and _redis() is not None

    def stage(self, stage: str, fraction: float = 0.0,
              processed: int = 0, total: int = 0) -> None:
        """Record the current step.

        ``fraction`` is progress through the engine's share of the work, 0..1,
        which is mapped onto the slice of the bar the API left for it.
        """
        if not self.active:
            return

        percent = int(
            ENGINE_START_PERCENT
            + max(0.0, min(1.0, fraction)) * (ENGINE_END_PERCENT - ENGINE_START_PERCENT)
        )

        # Redis writes are cheap but not free, and a per-row update would make
        # the ingest slower than the work it is measuring.
        if percent == self._last_percent and processed == 0:
            return
        self._last_percent = percent

        fields = {
            "status": "running",
            "stage": stage,
            "percent": str(percent),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if total:
            fields["processed"] = str(processed)
            fields["total"] = str(total)

        try:
            client = _redis()
            client.hset(self._key, mapping=fields)
        except Exception as exc:  # never fail a drawing over a progress update
            logger.debug("could not report progress: %s", exc)

    def counter(self, stage: str, every: int = 50_000):
        """A callback for reporting counts during a long read.

        Used by the point stream, which knows how many rows it has consumed but
        not how many are coming, so progress is reported as a count rather than
        a fraction.
        """
        def report(processed: int) -> None:
            if processed % every:
                return
            self.stage(stage, fraction=0.0, processed=processed, total=0)

        return report if self.active else (lambda processed: None)
