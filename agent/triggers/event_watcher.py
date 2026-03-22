from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

from agent.models import TriggerContext, WorkloadRef


IGNORED_REASONS = {
    "Pulled",
    "Pulling",
    "Created",
    "Started",
    "Scheduled",
    "SuccessfulCreate",
    "ScalingReplicaSet",
}


def _parse_event_time(event: dict) -> datetime:
    for key in ("eventTime", "lastTimestamp", "firstTimestamp"):
        value = event.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def map_event_to_trigger(cluster: str, event: dict) -> TriggerContext | None:
    event_type = event.get("type", "")
    reason = event.get("reason", "")
    raw_message = event.get("message", "")
    message = raw_message.lower()
    involved = event.get("involvedObject") or {}
    namespace = involved.get("namespace") or "default"
    kind = involved.get("kind") or "Pod"
    name = involved.get("name") or "unknown"

    if reason in IGNORED_REASONS:
        return None
    if event_type != "Warning" and reason not in {"BackOff", "FailedScheduling"}:
        return None

    symptom = None
    if reason == "BackOff":
        symptom = "CrashLoopBackOff"
    elif reason == "FailedScheduling":
        symptom = "Pending"
    elif reason == "Failed" and "probe" in message:
        symptom = "ProbeFailure"
    elif reason == "FailedMount":
        symptom = "FailedMount"
    elif reason in {"FailedCreatePodSandBox", "FailedCreatePodSandbox"}:
        symptom = "FailedCreatePodSandbox"
    elif reason == "ProgressDeadlineExceeded":
        symptom = "ProgressDeadlineExceeded"
    elif reason == "Evicted":
        symptom = "Evicted"
    elif reason == "ContainerCannotRun" or (
        reason == "Failed" and "container cannot run" in message
    ):
        symptom = "ContainerCannotRun"
    elif reason == "CreateContainerError":
        symptom = "CreateContainerError"
    elif reason == "CreateContainerConfigError" or (
        reason == "Failed" and any(token in message for token in {"configmap", "secret", "not found"})
    ):
        symptom = "CreateContainerConfigError"
    elif "imagepullbackoff" in message or reason == "ImagePullBackOff":
        symptom = "ImagePullBackOff"
    elif "errimagepull" in message or reason == "ErrImagePull":
        symptom = "ErrImagePull"
    elif "oomkilled" in message:
        symptom = "OOMKilled"

    if symptom is None:
        return None

    return TriggerContext(
        source="event",
        cluster=cluster,
        workload=WorkloadRef(kind=kind, namespace=namespace, name=name),
        symptom=symptom,
        observed_for_seconds=0,
        trigger_at=_parse_event_time(event),
        raw_signal={
            "eventType": event_type,
            "reason": reason,
            "message": raw_message,
            "involvedObject": involved,
            "timestamp": _parse_event_time(event).astimezone(timezone.utc).isoformat(),
        },
    )


class EventWatcher(threading.Thread):
    def __init__(
        self,
        stream_factory: Callable[[], object],
        cluster_name: str,
        on_trigger: Callable[[TriggerContext], None],
    ) -> None:
        super().__init__(daemon=True)
        self.stream_factory = stream_factory
        self.cluster_name = cluster_name
        self.on_trigger = on_trigger
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            stream = self.stream_factory()
            try:
                for item in stream:
                    if self._stop_event.is_set():
                        break
                    obj = item.get("object", item)
                    event_dict = obj.to_dict() if hasattr(obj, "to_dict") else obj
                    trigger = map_event_to_trigger(self.cluster_name, event_dict)
                    if trigger is not None:
                        self.on_trigger(trigger)
            except Exception:
                if self._stop_event.wait(5):
                    break
            finally:
                close_fn = getattr(stream, "close", None)
                if callable(close_fn):
                    close_fn()

    def stop(self) -> None:
        self._stop_event.set()
