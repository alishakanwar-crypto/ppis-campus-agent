"""Local, audit-only CP Plus face-capture feasibility pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import face_db
import gate_counter
from gate_counter import (
    CPPLUS_CAMERAS,
    capture_cpplus_frame,
    load_dvr_passwords,
    open_cpplus_stream,
)

cv2 = face_db.cv2
face_recognition = face_db.face_recognition

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "face_audit_results"
DEFAULT_CROSSING_AUDIT_DIR = Path(
    os.environ.get(
        "CPPLUS_CROSSING_AUDIT_DIR",
        str(DEFAULT_OUTPUT_DIR),
    )
)


class LatestFrameReader:
    def __init__(self, capture):
        self.capture = capture
        self.frames_read = 0
        self.failed = False
        self.failure_reason: str | None = None
        self._condition = threading.Condition()
        self._latest: tuple[int, datetime, np.ndarray] | None = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> None:
        while not self._stopped.is_set():
            try:
                ok, frame = self.capture.read()
            except cv2.error as exc:
                with self._condition:
                    self.failed = True
                    self.failure_reason = str(exc) or "OpenCV stream read failed"
                    self._condition.notify_all()
                return
            if not ok or frame is None:
                with self._condition:
                    self.failed = True
                    self.failure_reason = "Camera stream returned no frame"
                    self._condition.notify_all()
                return
            captured_at = datetime.now(IST)
            with self._condition:
                self.frames_read += 1
                self._latest = (self.frames_read, captured_at, frame)
                self._condition.notify_all()

    def next_frame(
        self,
        after_sequence: int,
        timeout: float,
    ) -> tuple[int, datetime, np.ndarray] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: (
                    self.failed
                    or self._stopped.is_set()
                    or (
                        self._latest is not None
                        and self._latest[0] > after_sequence
                    )
                ),
                timeout=timeout,
            )
            if self._latest is None or self._latest[0] <= after_sequence:
                return None
            return self._latest

    def close(self) -> None:
        self._stopped.set()
        self.capture.release()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=2)


def _category(role: str) -> str:
    normalized = role.strip().lower()
    if "student" in normalized:
        return "student"
    if "teacher" in normalized:
        return "teacher"
    if "vendor" in normalized:
        return "vendor"
    if "visitor" in normalized:
        return "visitor"
    if normalized:
        return "staff"
    return "unknown"


class UnknownTracker:
    def __init__(self, distance_threshold: float = 0.45):
        self.distance_threshold = distance_threshold
        self._profiles: list[tuple[str, np.ndarray]] = []

    @property
    def count(self) -> int:
        return len(self._profiles)

    def assign(self, encoding: np.ndarray) -> tuple[str, bool]:
        if self._profiles:
            distances = [
                float(np.linalg.norm(known_encoding - encoding))
                for _, known_encoding in self._profiles
            ]
            best_index = int(np.argmin(distances))
            if distances[best_index] <= self.distance_threshold:
                return self._profiles[best_index][0], True

        temp_id = f"Unknown-{len(self._profiles) + 1:03d}"
        self._profiles.append((temp_id, encoding.copy()))
        return temp_id, False


class FaceAuditAnalyzer:
    def __init__(
        self,
        known_faces: dict | None = None,
        candidate_distance: float = 0.45,
        review_distance: float = 0.55,
        minimum_margin: float = 0.05,
        minimum_face_width: int = 25,
        minimum_sharpness: float = 30.0,
        max_width: int = 960,
        include_students: bool = False,
        log_identities: bool = False,
        detector: str = "hog",
        multi_frame_window_seconds: float = 10.0,
    ):
        self.candidate_distance = candidate_distance
        self.review_distance = review_distance
        self.minimum_margin = minimum_margin
        self.minimum_face_width = minimum_face_width
        self.minimum_sharpness = minimum_sharpness
        self.max_width = max_width
        self.log_identities = log_identities
        self.detector = detector
        self.multi_frame_window_seconds = multi_frame_window_seconds
        self.unknowns = UnknownTracker()
        self._haar = None
        if detector == "haar":
            if cv2 is None:
                raise RuntimeError("OpenCV is required for Haar face detection")
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._haar = cv2.CascadeClassifier(cascade_path)
            if self._haar.empty():
                raise RuntimeError("OpenCV frontal-face detector could not be loaded")
        elif detector != "hog":
            raise ValueError(f"Unsupported face detector: {detector}")
        self._token_salt = os.urandom(16)
        loaded_faces = known_faces if known_faces is not None else face_db.load_known_faces()
        self._known_people = {
            person_id: person
            for person_id, person in loaded_faces.items()
            if include_students or _category(person.get("role", "")) != "student"
        }
        self._encodings: list[np.ndarray] = []
        self._encoding_people: list[str] = []
        self._last_seen: dict[str, float] = {}
        self._review_evidence: dict[
            tuple[str, str],
            list[tuple[float, float]],
        ] = {}
        self._best_review_distance: dict[tuple[str, str], float] = {}
        self._multi_frame_review_candidates: set[tuple[str, str]] = set()

        for person_id, person in self._known_people.items():
            for encoding in person.get("encodings", []):
                self._encodings.append(encoding)
                self._encoding_people.append(person_id)

    @property
    def enrolled_people(self) -> int:
        return len(self._known_people)

    @property
    def enrollment_images(self) -> int:
        return len(self._encodings)

    @property
    def multi_frame_review_candidates(self) -> int:
        return len(self._multi_frame_review_candidates)

    def _match(self, encoding: np.ndarray) -> tuple[str | None, float | None, float | None]:
        if not self._encodings:
            return None, None, None

        distances = face_recognition.face_distance(self._encodings, encoding)
        person_distances: dict[str, float] = {}
        for index, distance_value in enumerate(distances):
            person_id = self._encoding_people[index]
            distance = float(distance_value)
            previous = person_distances.get(person_id)
            if previous is None or distance < previous:
                person_distances[person_id] = distance

        ordered = sorted(person_distances.items(), key=lambda item: item[1])
        person_id, best_distance = ordered[0]
        second_distance = ordered[1][1] if len(ordered) > 1 else None
        return person_id, best_distance, second_distance

    def _identity_fields(self, person_id: str) -> dict:
        person = self._known_people[person_id]
        fields = {
            "candidate_token": hashlib.sha256(
                self._token_salt + person_id.encode("utf-8")
            ).hexdigest()[:12],
            "candidate_category": _category(person.get("role", "")),
        }
        if self.log_identities:
            fields.update({
                "candidate_person_id": person_id,
                "candidate_name": person.get("name", ""),
            })
        return fields

    def analyze(self, frame: np.ndarray, captured_at: datetime) -> list[dict]:
        if cv2 is None or face_recognition is None:
            raise RuntimeError("OpenCV and face_recognition are required")

        original_height, original_width = frame.shape[:2]
        scale = min(1.0, self.max_width / original_width)
        working = frame
        if scale < 1.0:
            working = cv2.resize(
                frame,
                (int(original_width * scale), int(original_height * scale)),
            )

        rgb = cv2.cvtColor(working, cv2.COLOR_BGR2RGB)
        if self._haar is not None:
            gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
            boxes = self._haar.detectMultiScale(
                gray,
                scaleFactor=1.08,
                minNeighbors=5,
                minSize=(20, 20),
            )
            working_height, working_width = working.shape[:2]
            locations = []
            for x, y, width, height in boxes:
                horizontal_padding = int(round(width * 0.2))
                vertical_padding = int(round(height * 0.2))
                locations.append((
                    max(0, int(y - vertical_padding)),
                    min(working_width, int(x + width + horizontal_padding)),
                    min(working_height, int(y + height + vertical_padding)),
                    max(0, int(x - horizontal_padding)),
                ))
        else:
            locations = face_recognition.face_locations(
                rgb,
                number_of_times_to_upsample=1,
                model="hog",
            )
        encoding_locations = [
            (
                max(0, min(original_height, int(round(top / scale)))),
                max(0, min(original_width, int(round(right / scale)))),
                max(0, min(original_height, int(round(bottom / scale)))),
                max(0, min(original_width, int(round(left / scale)))),
            )
            for top, right, bottom, left in locations
        ]
        encoding_rgb = (
            cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if scale < 1.0 else rgb
        )
        encodings = face_recognition.face_encodings(
            encoding_rgb,
            encoding_locations,
        )
        observations: list[dict] = []

        for location, encoding in zip(encoding_locations, encodings):
            top, right, bottom, left = location
            face_width = max(0, right - left)
            face_crop = frame[
                max(0, top):max(0, bottom),
                max(0, left):max(0, right),
            ]
            if face_crop.size:
                gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
                sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                brightness = float(gray.mean())
            else:
                sharpness = 0.0
                brightness = 0.0

            person_id, distance, second_distance = self._match(encoding)
            margin = (
                second_distance - distance
                if distance is not None and second_distance is not None
                else None
            )
            quality_ok = (
                face_width >= self.minimum_face_width
                and sharpness >= self.minimum_sharpness
            )
            candidate_ok = (
                quality_ok
                and person_id is not None
                and distance is not None
                and distance <= self.candidate_distance
                and (margin is None or margin >= self.minimum_margin)
            )
            review_match = (
                quality_ok
                and person_id is not None
                and distance is not None
                and distance <= self.review_distance
            )

            observation = {
                "captured_at": captured_at.strftime("%d-%m-%Y %H:%M:%S IST"),
                "face_width_px": face_width,
                "sharpness": round(sharpness, 2),
                "brightness": round(brightness, 2),
                "quality_ok": quality_ok,
                "match_distance": round(distance, 4) if distance is not None else None,
                "second_distance": (
                    round(second_distance, 4) if second_distance is not None else None
                ),
                "match_margin": round(margin, 4) if margin is not None else None,
                "candidate_support_frames": 0,
                "multi_frame_review_candidate": False,
                "best_evidence_so_far": False,
                "review_state": "pending",
            }

            if candidate_ok:
                last_seen = self._last_seen.get(person_id)
                now_ts = captured_at.timestamp()
                observation.update({
                    "status": "candidate_known",
                    "continuous_duplicate": (
                        last_seen is not None and now_ts - last_seen < 60
                    ),
                })
                observation.update(self._identity_fields(person_id))
                self._last_seen[person_id] = now_ts
            else:
                temp_id, repeated = self.unknowns.assign(encoding)
                observation.update({
                    "status": (
                        "unknown_needs_verification" if review_match else "unknown"
                    ),
                    "candidate_category": "unknown",
                    "temporary_id": temp_id,
                    "continuous_duplicate": repeated,
                })
                if review_match and person_id is not None:
                    evidence_key = (temp_id, person_id)
                    best_distance = self._best_review_distance.get(evidence_key)
                    observation["best_evidence_so_far"] = (
                        best_distance is None or distance < best_distance
                    )
                    if observation["best_evidence_so_far"]:
                        self._best_review_distance[evidence_key] = distance
                    if margin is None or margin >= self.minimum_margin:
                        now_ts = captured_at.timestamp()
                        evidence = [
                            item
                            for item in self._review_evidence.get(evidence_key, [])
                            if now_ts - item[0] <= self.multi_frame_window_seconds
                        ]
                        evidence.append((now_ts, distance))
                        self._review_evidence[evidence_key] = evidence
                        observation["candidate_support_frames"] = len(evidence)
                        if len(evidence) >= 3:
                            observation["multi_frame_review_candidate"] = True
                            self._multi_frame_review_candidates.add(evidence_key)
                    observation.update(self._identity_fields(person_id))
            observations.append(observation)

        return observations


def summarize(records: list[dict], analyzer: FaceAuditAnalyzer) -> dict:
    observations = [
        observation
        for record in records
        for observation in record.get("observations", [])
    ]
    face_widths = [item["face_width_px"] for item in observations]
    sharpness_values = [item["sharpness"] for item in observations]
    latencies = [record["processing_seconds"] for record in records if record.get("frame_ok")]
    successful_frames = sum(1 for record in records if record.get("frame_ok"))
    frames_with_faces = sum(
        1 for record in records if record.get("observations")
    )

    return {
        "mode": "audit_only_non_additive",
        "frames_attempted": len(records),
        "frames_captured": successful_frames,
        "rtsp_frames_analyzed": sum(
            1
            for record in records
            if record.get("frame_ok")
            and record.get("capture_source") == "rtsp_continuous"
        ),
        "http_frames_analyzed": sum(
            1
            for record in records
            if record.get("frame_ok")
            and record.get("capture_source") == "http_snapshot"
        ),
        "frames_with_faces": frames_with_faces,
        "face_frame_rate_pct": (
            round(100 * frames_with_faces / successful_frames, 2)
            if successful_frames
            else 0.0
        ),
        "faces_detected": len(observations),
        "quality_faces": sum(1 for item in observations if item["quality_ok"]),
        "candidate_known": sum(
            1 for item in observations if item["status"] == "candidate_known"
        ),
        "needs_verification": sum(
            1
            for item in observations
            if item["status"] == "unknown_needs_verification"
        ),
        "unknown": sum(1 for item in observations if item["status"] == "unknown"),
        "temporary_unknown_profiles": analyzer.unknowns.count,
        "multi_frame_review_candidates": analyzer.multi_frame_review_candidates,
        "enrolled_people": analyzer.enrolled_people,
        "enrollment_images": analyzer.enrollment_images,
        "median_face_width_px": (
            round(float(statistics.median(face_widths)), 2) if face_widths else 0.0
        ),
        "median_sharpness": (
            round(float(statistics.median(sharpness_values)), 2)
            if sharpness_values
            else 0.0
        ),
        "average_processing_seconds": (
            round(float(statistics.mean(latencies)), 3) if latencies else 0.0
        ),
        "official_headcount_changed": False,
        "attendance_changed": False,
        "cloud_data_sent": False,
    }


def _secure_append(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    if is_new:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _cleanup_old_reports(output_dir: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 24 * 60 * 60
    if not output_dir.exists():
        return
    for path in output_dir.glob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _parse_audit_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d-%m-%Y %H:%M:%S IST").replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None


def _load_crossings(
    started_at: datetime,
    completed_at: datetime,
    audit_dir: Path = DEFAULT_CROSSING_AUDIT_DIR,
) -> list[dict]:
    crossings: list[dict] = []
    if not audit_dir.exists():
        return crossings
    for path in audit_dir.glob("c1_crossings_*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                crossing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(crossing, dict):
                continue
            observed_at = _parse_audit_timestamp(crossing.get("timestamp"))
            if observed_at is not None and started_at <= observed_at <= completed_at:
                crossings.append(crossing)
    return sorted(crossings, key=lambda item: item["timestamp"])


def _load_tracker_traces(
    started_at: datetime,
    completed_at: datetime,
    audit_dir: Path = DEFAULT_CROSSING_AUDIT_DIR,
) -> list[dict]:
    traces: list[dict] = []
    if not audit_dir.exists():
        return traces
    for path in audit_dir.glob("c1_tracker_trace_*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(sample, dict):
                continue
            observed_at = _parse_audit_timestamp(sample.get("timestamp"))
            if observed_at is not None and started_at <= observed_at <= completed_at:
                traces.append(sample)
    return traces


def summarize_tracker_traces(traces: list[dict]) -> dict:
    sides_by_tracker: dict[int, set[int]] = {}
    nearest_line_distance: float | None = None
    tracks_reaching_line_band: set[int] = set()
    line_axes: set[str] = set()
    for sample in traces:
        tracker_id = sample.get("tracker_id")
        line_axis = sample.get("line_axis")
        if not isinstance(tracker_id, int) or line_axis not in {
            "horizontal",
            "vertical",
        }:
            continue
        line_axes.add(line_axis)
        side = sample.get("line_side")
        if side in {-1, 0, 1}:
            sides_by_tracker.setdefault(tracker_id, set()).add(side)
            if side == 0:
                tracks_reaching_line_band.add(tracker_id)
        anchor_key = "anchor_x_ratio" if line_axis == "vertical" else "anchor_y_ratio"
        try:
            distance = abs(
                float(sample[anchor_key]) - float(sample["line_position_ratio"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        nearest_line_distance = (
            distance
            if nearest_line_distance is None
            else min(nearest_line_distance, distance)
        )

    return {
        "tracker_trace_samples": len(traces),
        "tracker_tracks_observed": len(sides_by_tracker),
        "tracker_tracks_reaching_line_band": len(tracks_reaching_line_band),
        "tracker_tracks_observed_on_both_sides": sum(
            1 for sides in sides_by_tracker.values() if {-1, 1}.issubset(sides)
        ),
        "tracker_nearest_line_distance_ratio": (
            round(nearest_line_distance, 4)
            if nearest_line_distance is not None
            else None
        ),
        "tracker_line_axes": sorted(line_axes),
        "tracker_trace_only_non_additive": True,
    }


def correlate_crossing_evidence(
    records: list[dict],
    crossings: list[dict],
    window_seconds: float = 5.0,
) -> list[dict]:
    track_episodes: dict[str, list[list[tuple[datetime, dict]]]] = {}
    for record in records:
        for observation in record.get("observations", []):
            if not observation.get("quality_ok"):
                continue
            observed_at = _parse_audit_timestamp(observation.get("captured_at"))
            base_track_key = observation.get("candidate_token") or observation.get(
                "temporary_id"
            )
            if observed_at is None or not base_track_key:
                continue
            episodes = track_episodes.setdefault(base_track_key, [])
            if (
                not episodes
                or (observed_at - episodes[-1][-1][0]).total_seconds()
                > window_seconds
            ):
                episodes.append([])
            episodes[-1].append((observed_at, observation))

    tracks = {
        f"{base_track_key}:{index}": episode
        for base_track_key, episodes in track_episodes.items()
        for index, episode in enumerate(episodes)
    }

    profiles: list[dict] = []
    used_tracks: set[str] = set()
    status_rank = {
        "candidate_known": 3,
        "unknown_needs_verification": 2,
        "unknown": 1,
    }
    for crossing in crossings:
        crossing_at = _parse_audit_timestamp(crossing.get("timestamp"))
        if crossing_at is None:
            continue
        candidates: list[tuple[tuple[float, float, float], str, dict]] = []
        for track_key, observations in tracks.items():
            if track_key in used_tracks:
                continue
            nearby = [
                (abs((observed_at - crossing_at).total_seconds()), observation)
                for observed_at, observation in observations
                if abs((observed_at - crossing_at).total_seconds()) <= window_seconds
            ]
            if not nearby:
                continue
            seconds_apart, observation = min(nearby, key=lambda item: item[0])
            score = (
                float(status_rank.get(observation.get("status"), 0)),
                float(observation.get("face_width_px", 0)),
                -seconds_apart,
            )
            candidates.append((score, track_key, observation))
        if not candidates:
            continue
        _, track_key, observation = max(candidates, key=lambda item: item[0])
        used_tracks.add(track_key)
        profile = {
            "crossing_event_id": crossing.get("event_id"),
            "crossing_at": crossing.get("timestamp"),
            "direction": crossing.get("direction"),
            "attire_color": crossing.get("attire_color"),
            "bbox_height_ratio": crossing.get("bbox_height_ratio"),
            "bbox_width_ratio": crossing.get("bbox_width_ratio"),
            "face_evidence_status": observation.get("status"),
            "face_width_px": observation.get("face_width_px"),
            "match_distance": observation.get("match_distance"),
            "match_margin": observation.get("match_margin"),
            "review_state": "pending",
        }
        for key in (
            "candidate_token",
            "candidate_category",
            "temporary_id",
            "candidate_support_frames",
            "multi_frame_review_candidate",
        ):
            if key in observation:
                profile[key] = observation[key]
        profiles.append(profile)
    return profiles


def _activate_crossing_audit(marker_path: Path, duration_minutes: float) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({"expires_at_epoch": time.time() + duration_minutes * 60 + 60}),
        encoding="utf-8",
    )
    try:
        marker_path.chmod(0o600)
    except OSError:
        pass


def run_audit(
    duration_minutes: float,
    interval_seconds: float,
    output_dir: Path,
) -> tuple[Path, dict]:
    if not CPPLUS_CAMERAS:
        raise RuntimeError("CP Plus camera is disabled")

    load_dvr_passwords()
    max_width = int(os.environ.get("CPPLUS_FACE_AUDIT_MAX_WIDTH", "960"))
    detector = os.environ.get("CPPLUS_FACE_AUDIT_DETECTOR", "haar").strip().lower()
    analyzer = FaceAuditAnalyzer(
        max_width=max_width,
        detector=detector,
        include_students=os.environ.get(
            "CPPLUS_FACE_AUDIT_INCLUDE_STUDENTS", "0"
        ).lower() in {"1", "true", "yes"},
        log_identities=os.environ.get(
            "CPPLUS_FACE_AUDIT_LOG_IDENTITIES", "0"
        ).lower() in {"1", "true", "yes"},
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_reports(output_dir, retention_days=2)
    started_at = datetime.now(IST)
    output_path = output_dir / f"face_audit_{started_at.strftime('%Y%m%dT%H%M%S')}.jsonl"
    records: list[dict] = []
    started_monotonic = time.monotonic()
    deadline = started_monotonic + duration_minutes * 60
    capture = open_cpplus_stream(CPPLUS_CAMERAS[0])
    reader = LatestFrameReader(capture) if capture is not None else None
    stream_frames_read = 0
    stream_failed = False
    stream_failure_reason = None
    last_sequence = 0
    used_rtsp = reader is not None
    used_http = reader is None
    if reader is not None:
        reader.start()

    marker_path = gate_counter.CPPLUS_CROSSING_AUDIT_MARKER
    crossing_correlation_enabled = True
    try:
        _activate_crossing_audit(marker_path, duration_minutes)
    except OSError:
        crossing_correlation_enabled = False
    try:
        while time.monotonic() < deadline and gate_counter.running:
            processing_started = time.monotonic()
            capture_source = "http_snapshot"
            if reader is not None:
                capture_source = "rtsp_continuous"
                wait_seconds = min(2.0, max(0.01, deadline - time.monotonic()))
                latest = reader.next_frame(last_sequence, wait_seconds)
                if latest is None:
                    if reader.failed:
                        stream_frames_read = reader.frames_read
                        stream_failed = True
                        stream_failure_reason = reader.failure_reason
                        reader.close()
                        reader = None
                        used_http = True
                    continue
                last_sequence, captured_at, frame = latest
            else:
                captured_at = datetime.now(IST)
                frame = capture_cpplus_frame(CPPLUS_CAMERAS[0])

            if frame is None:
                record = {
                    "captured_at": captured_at.strftime("%d-%m-%Y %H:%M:%S IST"),
                    "camera": CPPLUS_CAMERAS[0]["name"],
                    "capture_source": capture_source,
                    "frame_ok": False,
                    "processing_seconds": round(
                        time.monotonic() - processing_started, 3
                    ),
                    "observations": [],
                }
            else:
                observations = analyzer.analyze(frame, captured_at)
                record = {
                    "captured_at": captured_at.strftime("%d-%m-%Y %H:%M:%S IST"),
                    "camera": CPPLUS_CAMERAS[0]["name"],
                    "capture_source": capture_source,
                    "frame_ok": True,
                    "frame_width": int(frame.shape[1]),
                    "frame_height": int(frame.shape[0]),
                    "processing_seconds": round(
                        time.monotonic() - processing_started, 3
                    ),
                    "observations": observations,
                }
            records.append(record)
            _secure_append(output_path, record)
            remaining = interval_seconds - (time.monotonic() - processing_started)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        if reader is not None:
            stream_frames_read = reader.frames_read
            stream_failed = reader.failed
            stream_failure_reason = reader.failure_reason
            reader.close()
        try:
            marker_path.unlink(missing_ok=True)
        except OSError:
            pass

    if used_rtsp and used_http:
        capture_source = "rtsp_continuous_then_http_snapshot"
    elif used_rtsp:
        capture_source = "rtsp_continuous"
    else:
        capture_source = "http_snapshot"

    runtime_seconds = time.monotonic() - started_monotonic
    completed_at = datetime.now(IST)
    crossings = _load_crossings(started_at, completed_at)
    tracker_traces = _load_tracker_traces(started_at, completed_at)
    evidence_profiles = correlate_crossing_evidence(records, crossings)
    for profile in evidence_profiles:
        _secure_append(output_path, {"crossing_evidence_profile": profile})

    summary = summarize(records, analyzer)
    summary.update(summarize_tracker_traces(tracker_traces))
    summary.update({
        "started_at": started_at.strftime("%d-%m-%Y %H:%M:%S IST"),
        "completed_at": completed_at.strftime("%d-%m-%Y %H:%M:%S IST"),
        "camera": CPPLUS_CAMERAS[0]["name"],
        "capture_source": capture_source,
        "stream_frames_read": stream_frames_read,
        "stream_failed": stream_failed,
        "stream_failure_reason": stream_failure_reason,
        "stopped_early": not gate_counter.running,
        "official_crossings_observed": len(crossings),
        "crossings_with_face_evidence": len(evidence_profiles),
        "crossings_with_candidate_identity": sum(
            1
            for profile in evidence_profiles
            if profile["face_evidence_status"] == "candidate_known"
        ),
        "crossing_correlation_enabled": crossing_correlation_enabled,
        "crossing_correlation_only_non_additive": True,
        "analysis_max_width": max_width,
        "full_resolution_encoding": True,
        "face_detector": detector,
        "analysis_frame_rate_fps": (
            round(len(records) / runtime_seconds, 2) if runtime_seconds else 0.0
        ),
    })
    _secure_append(output_path, {"summary": summary})
    return output_path, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-minutes", type=float, default=10.0)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    enabled = os.environ.get("CPPLUS_FACE_AUDIT_ENABLED", "0").lower()
    if enabled not in {"1", "true", "yes"}:
        print("Face audit is disabled. Set CPPLUS_FACE_AUDIT_ENABLED=1 to run it.")
        return 2
    if args.duration_minutes <= 0 or args.interval_seconds <= 0:
        print("Duration and interval must be positive.")
        return 2

    try:
        output_path, summary = run_audit(
            args.duration_minutes,
            args.interval_seconds,
            args.output_dir,
        )
    except Exception as exc:
        print(f"Face audit failed: {exc}")
        return 1

    print(json.dumps(summary, indent=2))
    print(f"Audit report: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
