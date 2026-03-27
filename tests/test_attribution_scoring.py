from agent.analyzers.attribution import score_root_cause_candidates


def test_score_root_cause_candidates_prefers_symptom_kind():
    scored = score_root_cause_candidates(
        symptom="ProgressDeadlineExceeded",
        items=[
            {
                "objectRef": {"kind": "Pod", "namespace": "payments", "name": "checkout-abc"},
                "reason": "Pod reports repeated probe failures.",
                "confidence": 0.9,
            },
            {
                "objectRef": {"kind": "Deployment", "namespace": "payments", "name": "checkout"},
                "reason": "Deployment condition ProgressDeadlineExceeded indicates rollout is blocked.",
                "confidence": 0.8,
            },
        ],
    )
    assert scored[0]["objectRef"]["kind"] == "Deployment"
    assert "symptom-kind-priority" in scored[0]["rankReasons"]
    assert scored[0]["score"] >= scored[1]["score"]


def test_score_root_cause_candidates_is_deterministic_for_ties():
    items = [
        {
            "objectRef": {"kind": "Node", "namespace": "", "name": "node-b"},
            "reason": "Node currently affects multiple pods on the same host.",
            "confidence": 0.7,
        },
        {
            "objectRef": {"kind": "Node", "namespace": "", "name": "node-a"},
            "reason": "Node currently affects multiple pods on the same host.",
            "confidence": 0.7,
        },
    ]
    first = score_root_cause_candidates(symptom="Pending", items=items)
    second = score_root_cause_candidates(symptom="Pending", items=list(reversed(items)))
    assert [item["objectRef"]["name"] for item in first] == [item["objectRef"]["name"] for item in second]
    assert first[0]["objectRef"]["name"] == "node-a"
