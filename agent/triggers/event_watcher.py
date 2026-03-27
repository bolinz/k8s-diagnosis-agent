from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from agent.models import TriggerContext, WorkloadRef
from agent.runtime_logging import get_logger, log_event


IGNORED_REASONS = {
    "Pulled",
    "Pulling",
    "Created",
    "Started",
    "Scheduled",
    "SuccessfulCreate",
    "ScalingReplicaSet",
}


LOGGER = get_logger("event_watcher")


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
    name = involved.get("name") or ""

    if not name:
        return None

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
    elif (
        "errimagepull" in message
        or reason == "ErrImagePull"
        or (reason == "Failed" and "failed to pull image" in message)
    ):
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
        report_namespace: str,
        workload_name: str,
        on_trigger: Callable[[TriggerContext], None],
    ) -> None:
        super().__init__(daemon=True)
        self.stream_factory = stream_factory
        self.cluster_name = cluster_name
        self.report_namespace = report_namespace
        self.workload_name = workload_name
        self.on_trigger = on_trigger
        self._stop_event = threading.Event()

    def _should_ignore_event(self, event: dict) -> bool:
        involved = event.get("involvedObject") or {}
        namespace = involved.get("namespace") or ""
        kind = involved.get("kind") or ""
        name = involved.get("name") or ""
        if not name:
            return True
        if namespace != self.report_namespace:
            return False
        if name == self.workload_name or name.startswith(f"{self.workload_name}-"):
            return kind in {"Deployment", "ReplicaSet", "Pod"}
        return False

    def run(self) -> None:
        while not self._stop_event.is_set():
            stream = self.stream_factory()
            try:
                for item in stream:
                    if self._stop_event.is_set():
                        break
                    obj = item.get("object", item)
                    event_dict = obj.to_dict() if hasattr(obj, "to_dict") else obj
                    if self._should_ignore_event(event_dict):
                        log_event(LOGGER, logging.DEBUG, "event_skipped", "skipping ignored event")
                        continue
                    trigger = map_event_to_trigger(self.cluster_name, event_dict)
                    if trigger is not None:
                        log_event(
                            LOGGER,
                            logging.INFO,
                            "event_trigger",
                            "accepted event trigger",
                            namespace=trigger.workload.namespace,
                            workload_kind=trigger.workload.kind,
                            workload_name=trigger.workload.name,
                            symptom=trigger.symptom,
                        )
                        self.on_trigger(trigger)
            except Exception:
                log_event(
                    LOGGER,
                    logging.ERROR,
                    "watch_failed",
                    "event watch stream failed and will be retried",
                )
                if self._stop_event.wait(5):
                    break
            finally:
                close_fn = getattr(stream, "close", None)
                if callable(close_fn):
                    close_fn()

    def stop(self) -> None:
        self._stop_event.set()
