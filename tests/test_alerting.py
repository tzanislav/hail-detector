from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from hail_detector.alerting import AlertLifecycle
from hail_detector.archive import ArchiveStore


NOW = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)


def _assessment(level: str | None) -> dict[str, object]:
    return {
        "track_id": "track-000001",
        "candidate_level": level,
        "reasons": ["deterministic test candidate"],
    }


def test_lifecycle_deduplicates_escalates_and_expires_only_on_fresh_data(tmp_path: Path) -> None:
    archive = ArchiveStore(tmp_path)
    lifecycle = AlertLifecycle(archive, timedelta(minutes=20))

    watch = lifecycle.reconcile([_assessment("Hail Watch")], evaluated_at=NOW, data_is_fresh=True)
    duplicate = lifecycle.reconcile(
        [_assessment("Hail Watch")],
        evaluated_at=NOW + timedelta(minutes=4),
        data_is_fresh=True,
    )
    warning = lifecycle.reconcile(
        [_assessment("Hail Warning")],
        evaluated_at=NOW + timedelta(minutes=8),
        data_is_fresh=True,
    )
    stale = lifecycle.reconcile(
        [_assessment(None)],
        evaluated_at=NOW + timedelta(minutes=24),
        data_is_fresh=False,
    )
    expiry = lifecycle.reconcile(
        [_assessment(None)],
        evaluated_at=NOW + timedelta(minutes=28),
        data_is_fresh=True,
    )

    assert [event.kind for event in watch.transitions] == ["activated"]
    assert watch.outbox_events == 1
    assert duplicate.transitions == ()
    assert duplicate.active_alerts == 1
    assert [event.kind for event in warning.transitions] == ["escalated"]
    assert warning.transitions[0].previous_level == "Hail Watch"
    assert stale.deferred_for_stale_data is True
    assert stale.active_alerts == 1
    assert stale.transitions == ()
    assert [event.kind for event in expiry.transitions] == ["expired"]
    assert expiry.active_alerts == 0
    assert len(list(archive.notification_outbox_dir.glob("*.json"))) == 3
