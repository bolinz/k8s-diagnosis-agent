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
