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


def test_fallback_diagnosis_refines_image_pull_for_auth_failure():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "ErrImagePull",
                "observed_for_seconds": 1000,
                "message": "failed to pull image \"priv-registry/app:1.2\": unauthorized: authentication required",
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "authentication" in diagnosis.probable_causes[0].lower()
    assert "credentials" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_refines_image_pull_for_not_found():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "ImagePullBackOff",
                "observed_for_seconds": 1000,
                "message": "failed to pull image \"repo/app:missing\": manifest unknown: manifest unknown",
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "does not exist" in diagnosis.probable_causes[0].lower() or "invalid" in diagnosis.probable_causes[0].lower()
    assert "tag" in diagnosis.recommendations[0].lower() or "digest" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_refines_image_pull_for_network_failure():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "payments",
                "name": "checkout",
                "kind": "Pod",
                "symptom": "ImagePullBackOff",
                "observed_for_seconds": 1000,
                "message": "failed to do request: Head \"https://index.docker.io/v2/\": dial tcp: lookup index.docker.io: no such host",
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "dns" in diagnosis.probable_causes[0].lower() or "network" in diagnosis.probable_causes[0].lower()
    assert "dns" in diagnosis.recommendations[0].lower() or "egress" in diagnosis.recommendations[0].lower()


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


def test_rule_engine_accepts_new_symptom_types():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    findings = engine.findings_from_snapshot(
        [
            {
                "namespace": "storage",
                "name": "data-pvc",
                "kind": "Pod",
                "symptom": "PVCPending",
                "observed_for_seconds": 1000,
            },
            {
                "namespace": "app",
                "name": "hpa-web",
                "kind": "HorizontalPodAutoscaler",
                "symptom": "HPAMaxReplicas",
                "observed_for_seconds": 1000,
            },
            {
                "namespace": "default",
                "name": "web-pdb",
                "kind": "PodDisruptionBudget",
                "symptom": "PDBDisruptionBlocked",
                "observed_for_seconds": 1000,
            },
        ]
    )
    assert [item.trigger.symptom for item in findings] == [
        "PVCPending",
        "HPAMaxReplicas",
        "PDBDisruptionBlocked",
    ]


def test_fallback_diagnosis_pvc_pending():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "storage",
                "name": "data-pod",
                "kind": "Pod",
                "symptom": "PVCPending",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "pvc" in diagnosis.probable_causes[0].lower() or "volume" in diagnosis.probable_causes[0].lower()
    assert "phase" in diagnosis.recommendations[0].lower() or "pvc" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_hpa_max_replicas():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "app",
                "name": "web-hpa",
                "kind": "HorizontalPodAutoscaler",
                "symptom": "HPAMaxReplicas",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "maxreplicas" in diagnosis.probable_causes[0].lower() or "ceiling" in diagnosis.probable_causes[0].lower()
    assert "maxreplicas" in diagnosis.recommendations[0].lower() or "limit" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_pdb_disruption_blocked():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "default",
                "name": "web-pdb",
                "kind": "PodDisruptionBudget",
                "symptom": "PDBDisruptionBlocked",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "pdb" in diagnosis.probable_causes[0].lower() or "disruption" in diagnosis.probable_causes[0].lower()
    assert "minavailable" in diagnosis.recommendations[0].lower() or "pdb" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_volume_resize_failure():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "storage",
                "name": "data-pvc",
                "kind": "Pod",
                "symptom": "VolumeResizeFailure",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "resize" in diagnosis.probable_causes[0].lower() or "expand" in diagnosis.probable_causes[0].lower()
    assert "storage class" in diagnosis.recommendations[0].lower() or "expansion" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_network_policy_blocking():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "secure",
                "name": "api-pod",
                "kind": "Pod",
                "symptom": "NetworkPolicyBlocking",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "networkpolicy" in diagnosis.probable_causes[0].lower() or "blocking" in diagnosis.probable_causes[0].lower()
    assert "networkpolicy" in diagnosis.recommendations[0].lower() or "ingress" in diagnosis.recommendations[0].lower()


def test_fallback_diagnosis_resource_quota_exceeded():
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    trigger = engine.findings_from_snapshot(
        [
            {
                "namespace": "team-a",
                "name": "team-a",
                "kind": "Namespace",
                "symptom": "ResourceQuotaExceeded",
                "observed_for_seconds": 1000,
            }
        ]
    )[0].trigger
    diagnosis = engine.fallback_diagnosis(trigger)
    assert "quota" in diagnosis.probable_causes[0].lower() or "exceeded" in diagnosis.probable_causes[0].lower()
    assert "describe resourcequota" in diagnosis.recommendations[0].lower()
