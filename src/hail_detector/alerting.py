from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .archive import ArchiveStore


LEVEL_RANK = {"Hail Watch": 1, "Hail Warning": 2}


@dataclass
class AlertRecord:
    track_id: str
    level: str
    status: str
    first_triggered_at: datetime
    last_candidate_at: datetime
    last_evaluated_at: datetime

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in ("first_triggered_at", "last_candidate_at", "last_evaluated_at"):
            data[name] = data[name].isoformat()
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AlertRecord":
        return cls(
            track_id=value["track_id"],
            level=value["level"],
            status=value["status"],
            first_triggered_at=datetime.fromisoformat(value["first_triggered_at"]),
            last_candidate_at=datetime.fromisoformat(value["last_candidate_at"]),
            last_evaluated_at=datetime.fromisoformat(value["last_evaluated_at"]),
        )


@dataclass(frozen=True)
class AlertEvent:
    event_id: str
    kind: str
    track_id: str
    level: str
    previous_level: str | None
    occurred_at: datetime
    reason: str

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data


@dataclass(frozen=True)
class AlertReconciliationResult:
    active_alerts: int
    transitions: tuple[AlertEvent, ...]
    outbox_events: int
    deferred_for_stale_data: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "active_alerts": self.active_alerts,
            "transitions": [transition.as_dict() for transition in self.transitions],
            "outbox_events": self.outbox_events,
            "deferred_for_stale_data": self.deferred_for_stale_data,
        }


class AlertLifecycle:
    """Deduplicate/evolve alert candidates and create dry-run delivery events."""

    def __init__(self, archive: ArchiveStore, expiry: timedelta):
        self.archive = archive
        self.expiry = expiry

    @staticmethod
    def _event(
        kind: str,
        track_id: str,
        level: str,
        previous_level: str | None,
        occurred_at: datetime,
        reason: str,
    ) -> AlertEvent:
        event_id = "-".join(
            (
                occurred_at.strftime("%Y%m%dT%H%M%S%fZ"),
                track_id,
                kind,
            )
        )
        return AlertEvent(
            event_id=event_id,
            kind=kind,
            track_id=track_id,
            level=level,
            previous_level=previous_level,
            occurred_at=occurred_at,
            reason=reason,
        )

    def reconcile(
        self,
        assessments: Iterable[dict[str, Any]],
        *,
        evaluated_at: datetime,
        data_is_fresh: bool,
    ) -> AlertReconciliationResult:
        if evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        current_state = self.archive.read_alert_state() or {"records": []}
        records = {
            value["track_id"]: AlertRecord.from_dict(value)
            for value in current_state.get("records", [])
        }
        assessment_list = list(assessments)
        if not data_is_fresh:
            result = AlertReconciliationResult(
                active_alerts=sum(record.status == "active" for record in records.values()),
                transitions=(),
                outbox_events=0,
                deferred_for_stale_data=True,
            )
            self.archive.write_alert_audit(
                evaluated_at,
                {
                    "evaluated_at": evaluated_at.isoformat(),
                    "data_is_fresh": False,
                    "reason": "alert state was not mutated because the radar source is stale or incomplete",
                    "assessments": assessment_list,
                    "result": result.as_dict(),
                },
            )
            return result

        transitions: list[AlertEvent] = []
        candidates: dict[str, dict[str, Any]] = {}
        for assessment in assessment_list:
            level = assessment.get("candidate_level")
            if level in LEVEL_RANK:
                candidates[assessment["track_id"]] = assessment

        for track_id, assessment in candidates.items():
            level = assessment["candidate_level"]
            record = records.get(track_id)
            reason = "; ".join(assessment.get("reasons", ()))
            if record is None or record.status == "expired":
                records[track_id] = AlertRecord(
                    track_id=track_id,
                    level=level,
                    status="active",
                    first_triggered_at=evaluated_at,
                    last_candidate_at=evaluated_at,
                    last_evaluated_at=evaluated_at,
                )
                transitions.append(
                    self._event("activated", track_id, level, None, evaluated_at, reason)
                )
                continue

            record.last_candidate_at = evaluated_at
            record.last_evaluated_at = evaluated_at
            if LEVEL_RANK[level] > LEVEL_RANK[record.level]:
                previous_level = record.level
                record.level = level
                transitions.append(
                    self._event(
                        "escalated", track_id, level, previous_level, evaluated_at, reason
                    )
                )
            elif LEVEL_RANK[level] < LEVEL_RANK[record.level]:
                previous_level = record.level
                record.level = level
                transitions.append(
                    self._event(
                        "deescalated", track_id, level, previous_level, evaluated_at, reason
                    )
                )

        for track_id, record in records.items():
            if record.status != "active":
                continue
            if track_id in candidates:
                continue
            record.last_evaluated_at = evaluated_at
            if evaluated_at - record.last_candidate_at >= self.expiry:
                record.status = "expired"
                transitions.append(
                    self._event(
                        "expired",
                        track_id,
                        record.level,
                        record.level,
                        evaluated_at,
                        "no fresh qualifying candidate was observed before the expiry interval",
                    )
                )

        state = {
            "updated_at": evaluated_at.isoformat(),
            "records": [record.as_dict() for record in records.values()],
        }
        self.archive.write_alert_state(state)
        for transition in transitions:
            self.archive.write_notification_outbox(transition.as_dict())
        result = AlertReconciliationResult(
            active_alerts=sum(record.status == "active" for record in records.values()),
            transitions=tuple(transitions),
            outbox_events=len(transitions),
            deferred_for_stale_data=False,
        )
        self.archive.write_alert_audit(
            evaluated_at,
            {
                "evaluated_at": evaluated_at.isoformat(),
                "data_is_fresh": True,
                "assessments": assessment_list,
                "result": result.as_dict(),
            },
        )
        return result
