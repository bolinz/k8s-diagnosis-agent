from agent.analyzers.rules import RuleEngine


def test_rule_engine_filters_by_supported_symptom_and_duration():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    findings = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Deployment",
                "symptom": "CrashLoopBackOff",
                "observed_for_seconds": 700,
            },
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Deployment",
                "symptom": "Unknown",
                "observed_for_seconds": 700,
            },
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Deployment",
                "symptom": "Pending",
                "observed_for_seconds": 120,
            },
        ]
    )
    assert len(findings) == 1
    assert findings[0].trigger.symptom == "CrashLoopBackOff"


def test_fallback_diagnosis_has_specific_image_pull_guidance():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = findings = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Deployment",
                "symptom": "ImagePullBackOff",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert diagnosis.severity == "critical"
    assert "imagePullSecrets" in diagnosis.recommendations[0]


def test_rule_engine_accepts_new_workload_symptoms():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    findings = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "CreateContainerConfigError",
                "observed_for_seconds": 1000,
            },
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Deployment",
                "symptom": "ProgressDeadlineExceeded",
                "observed_for_seconds": 1000,
            },
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Deployment",
                "symptom": "ReplicaMismatch",
                "observed_for_seconds": 1000,
            },
        ]
    )
    assert [item.trigger.symptom for item in findings] == [
        "CreateContainerConfigError",
        "ProgressDeadlineExceeded",
        "ReplicaMismatch",
    ]


def test_fallback_diagnosis_has_specific_mount_guidance():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "FailedMount",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert diagnosis.severity == "critical"
    assert "PVC" in diagnosis.recommendations[0]


def test_fallback_diagnosis_refines_pending_for_resource_shortage():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "Pending",
                "observed_for_seconds": 1000,
                "reason": "FailedScheduling",
                "message": "0/3 nodes are available: 3 Insufficient cpu.",
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "insufficient" in diagnosis.probable_causes[0].lower()
    assert "capacity" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_refines_failed_mount_for_missing_secret():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "FailedMount",
                "observed_for_seconds": 1000,
                "reason": "FailedMount",
                "message": "MountVolume.SetUp failed for volume \"app-secret\": secret \"app-secret\" not found",
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "secret" in diagnosis.probable_causes[0].lower()
    assert "configmap" in diagnosis.recommendations[0].lower() or "secret" in diagnosis.recommendations[0].lower()
