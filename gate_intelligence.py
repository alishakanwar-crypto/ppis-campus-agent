from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class GateIntelligenceConfig:
    congestion_people: int = 6
    congestion_seconds: float = 20.0
    loiter_seconds: float = 120.0
    vehicle_dwell_seconds: float = 180.0
    offline_seconds: float = 20.0
    frozen_seconds: float = 15.0
    blurred_seconds: float = 30.0
    blocked_seconds: float = 20.0
    view_changed_seconds: float = 60.0
    health_clear_seconds: float = 10.0
    expected_direction: str = ""


def _ist(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def _event_id(camera: str, event_type: str, key: str) -> str:
    raw = f"{camera}|{event_type}|{key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class GateIntelligenceMonitor:
    def __init__(self, camera: str, config: GateIntelligenceConfig) -> None:
        self.camera = camera
        self.config = config
        self.person_first_seen: dict[int, datetime] = {}
        self.vehicle_first_seen: dict[int, datetime] = {}
        self.loitering_alerted: set[int] = set()
        self.vehicle_dwell_alerted: set[int] = set()
        self.last_crossing: dict[int, tuple[str, datetime]] = {}
        self.congestion_started: datetime | None = None
        self.congestion_active = False
        self.capture_failure_started: datetime | None = None
        self.previous_thumbnail: np.ndarray | None = None
        self.reference_thumbnail: np.ndarray | None = None
        self.health_started: dict[str, datetime] = {}
        self.health_active: set[str] = set()
        self.health_clear_started: dict[str, datetime] = {}

    def _event(
        self,
        event_type: str,
        now: datetime,
        *,
        severity: str,
        key: str,
        metadata: dict,
    ) -> dict:
        current = _ist(now)
        return {
            "event_id": _event_id(
                self.camera,
                event_type,
                f"{current:%Y-%m-%d}|{key}",
            ),
            "timestamp": current.strftime("%Y-%m-%d %H:%M:%S"),
            "camera": self.camera,
            "event_type": event_type,
            "severity": severity,
            "verification_only": True,
            "metadata": metadata,
        }

    def observe_tracks(
        self,
        now: datetime,
        person_ids: set[int],
        vehicle_ids: set[int],
        *,
        visible_person_count: int | None = None,
    ) -> list[dict]:
        current = _ist(now)
        events: list[dict] = []

        for tracker_id in person_ids:
            first_seen = self.person_first_seen.setdefault(tracker_id, current)
            dwell = (current - first_seen).total_seconds()
            if (
                dwell >= self.config.loiter_seconds
                and tracker_id not in self.loitering_alerted
            ):
                self.loitering_alerted.add(tracker_id)
                events.append(
                    self._event(
                        "loitering",
                        current,
                        severity="warning",
                        key=f"person-{tracker_id}-{first_seen:%H%M%S}",
                        metadata={
                            "tracker_id": tracker_id,
                            "dwell_seconds": round(dwell),
                            "threshold_seconds": round(self.config.loiter_seconds),
                        },
                    )
                )

        for tracker_id in set(self.person_first_seen) - person_ids:
            self.person_first_seen.pop(tracker_id, None)
            self.loitering_alerted.discard(tracker_id)
            self.last_crossing.pop(tracker_id, None)

        for tracker_id in vehicle_ids:
            first_seen = self.vehicle_first_seen.setdefault(tracker_id, current)
            dwell = (current - first_seen).total_seconds()
            if (
                dwell >= self.config.vehicle_dwell_seconds
                and tracker_id not in self.vehicle_dwell_alerted
            ):
                self.vehicle_dwell_alerted.add(tracker_id)
                events.append(
                    self._event(
                        "vehicle_dwell",
                        current,
                        severity="warning",
                        key=f"vehicle-{tracker_id}-{first_seen:%H%M%S}",
                        metadata={
                            "tracker_id": tracker_id,
                            "dwell_seconds": round(dwell),
                            "threshold_seconds": round(
                                self.config.vehicle_dwell_seconds
                            ),
                        },
                    )
                )

        for tracker_id in set(self.vehicle_first_seen) - vehicle_ids:
            self.vehicle_first_seen.pop(tracker_id, None)
            self.vehicle_dwell_alerted.discard(tracker_id)

        occupancy = (
            len(person_ids) if visible_person_count is None else visible_person_count
        )
        if occupancy >= self.config.congestion_people:
            if self.congestion_started is None:
                self.congestion_started = current
            sustained = (current - self.congestion_started).total_seconds()
            if (
                sustained >= self.config.congestion_seconds
                and not self.congestion_active
            ):
                self.congestion_active = True
                events.append(
                    self._event(
                        "congestion_started",
                        current,
                        severity="warning",
                        key=f"started-{self.congestion_started:%H%M%S}",
                        metadata={
                            "people_in_gate_zone": occupancy,
                            "threshold_people": self.config.congestion_people,
                            "sustained_seconds": round(sustained),
                        },
                    )
                )
        else:
            self.congestion_started = None
            if self.congestion_active:
                self.congestion_active = False
                events.append(
                    self._event(
                        "congestion_cleared",
                        current,
                        severity="info",
                        key=f"cleared-{current:%H%M%S}",
                        metadata={"people_in_gate_zone": occupancy},
                    )
                )

        return events

    def observe_crossing(
        self,
        now: datetime,
        tracker_id: int,
        direction: str,
        *,
        official_hours: bool,
    ) -> list[dict]:
        current = _ist(now)
        normalized = direction.upper()
        events: list[dict] = []
        key = f"{tracker_id}-{current:%H%M%S}-{normalized}"

        if not official_hours:
            events.append(
                self._event(
                    "after_hours_movement",
                    current,
                    severity="critical",
                    key=key,
                    metadata={"direction": normalized, "tracker_id": tracker_id},
                )
            )

        expected = self.config.expected_direction.upper()
        if expected in {"IN", "OUT"} and normalized != expected:
            events.append(
                self._event(
                    "wrong_way",
                    current,
                    severity="warning",
                    key=key,
                    metadata={
                        "observed_direction": normalized,
                        "expected_direction": expected,
                        "tracker_id": tracker_id,
                    },
                )
            )

        previous = self.last_crossing.get(tracker_id)
        if previous is not None:
            previous_direction, previous_at = previous
            elapsed = (current - previous_at).total_seconds()
            if previous_direction != normalized and elapsed <= 60:
                events.append(
                    self._event(
                        "direction_reversal",
                        current,
                        severity="warning",
                        key=key,
                        metadata={
                            "previous_direction": previous_direction,
                            "observed_direction": normalized,
                            "seconds_since_previous_crossing": round(elapsed),
                            "tracker_id": tracker_id,
                        },
                    )
                )
        self.last_crossing[tracker_id] = (normalized, current)
        return events

    def vehicle_dwell_seconds(self, tracker_id: int, now: datetime) -> int:
        first_seen = self.vehicle_first_seen.get(tracker_id)
        if first_seen is None:
            return 0
        return max(0, round((_ist(now) - first_seen).total_seconds()))

    def observe_capture_failure(self, now: datetime) -> list[dict]:
        current = _ist(now)
        if self.capture_failure_started is None:
            self.capture_failure_started = current
            return []
        elapsed = (current - self.capture_failure_started).total_seconds()
        if elapsed < self.config.offline_seconds or "offline" in self.health_active:
            return []
        self.health_active.add("offline")
        return [
            self._event(
                "camera_health",
                current,
                severity="critical",
                key=f"offline-{self.capture_failure_started:%H%M%S}",
                metadata={"state": "offline", "duration_seconds": round(elapsed)},
            )
        ]

    def observe_frame(
        self,
        now: datetime,
        frame: np.ndarray,
        *,
        active_people: int,
    ) -> list[dict]:
        current = _ist(now)
        events: list[dict] = []
        if self.capture_failure_started is not None:
            self.capture_failure_started = None
            if "offline" in self.health_active:
                self.health_active.remove("offline")
                events.append(
                    self._event(
                        "camera_health",
                        current,
                        severity="info",
                        key=f"online-{current:%H%M%S}",
                        metadata={"state": "online", "recovered_from": "offline"},
                    )
                )

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thumbnail = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        mean = float(thumbnail.mean())
        stddev = float(thumbnail.std())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        frozen_diff = None
        if self.previous_thumbnail is not None:
            frozen_diff = float(
                cv2.absdiff(
                    thumbnail,
                    self.previous_thumbnail,
                ).mean()
            )
        self.previous_thumbnail = thumbnail

        if self.reference_thumbnail is None and active_people == 0 and stddev >= 10:
            self.reference_thumbnail = thumbnail.copy()
        view_diff = None
        if self.reference_thumbnail is not None and active_people == 0:
            view_diff = float(
                cv2.absdiff(
                    thumbnail,
                    self.reference_thumbnail,
                ).mean()
            )

        conditions = {
            "frozen": frozen_diff is not None and frozen_diff < 0.15,
            "blurred": sharpness < 20.0 and mean >= 15.0,
            "blocked": mean < 15.0 or stddev < 5.0,
            "view_changed": view_diff is not None and view_diff > 45.0,
        }
        durations = {
            "frozen": self.config.frozen_seconds,
            "blurred": self.config.blurred_seconds,
            "blocked": self.config.blocked_seconds,
            "view_changed": self.config.view_changed_seconds,
        }
        metrics = {
            "frozen": frozen_diff,
            "blurred": sharpness,
            "blocked": min(mean, stddev),
            "view_changed": view_diff,
        }

        for state, condition in conditions.items():
            if condition:
                self.health_clear_started.pop(state, None)
                started = self.health_started.setdefault(state, current)
                elapsed = (current - started).total_seconds()
                if elapsed >= durations[state] and state not in self.health_active:
                    self.health_active.add(state)
                    events.append(
                        self._event(
                            "camera_health",
                            current,
                            severity="warning",
                            key=f"{state}-{started:%H%M%S}",
                            metadata={
                                "state": state,
                                "duration_seconds": round(elapsed),
                                "metric": (
                                    round(float(metrics[state]), 2)
                                    if metrics[state] is not None
                                    else None
                                ),
                            },
                        )
                    )
                continue

            self.health_started.pop(state, None)
            if state not in self.health_active:
                self.health_clear_started.pop(state, None)
                continue
            clear_started = self.health_clear_started.setdefault(state, current)
            if (
                current - clear_started
            ).total_seconds() < self.config.health_clear_seconds:
                continue
            self.health_active.remove(state)
            self.health_clear_started.pop(state, None)
            if state == "view_changed" and active_people == 0:
                self.reference_thumbnail = thumbnail.copy()
            events.append(
                self._event(
                    "camera_health",
                    current,
                    severity="info",
                    key=f"{state}-cleared-{current:%H%M%S}",
                    metadata={"state": "healthy", "recovered_from": state},
                )
            )

        return events
