# Architecture

## Component Diagram

```mermaid
flowchart TB
    subgraph Ingress["入口层"]
        W[EventWatcher<br/>Kubernetes Watch]
        S[Scheduler<br/>Periodic Snapshot]
        H[HTTP Webhook<br/>POST /alert]
    end

    subgraph Queue["事件队列"]
        Q[AsyncEventQueue<br/>maxsize=100<br/>num_workers=4]
    end

    subgraph Dedup["事件去重"]
        D[EventStormDeduper<br/>时间窗口聚合<br/>count/first_seen/last_seen/aggregated]
    end

    subgraph Orchestration["诊断编排"]
        T[TriggerTransformer<br/>normalize · augment · correlate]
        Eng[DiagnosisEngine<br/>循环调用 DiagnosisAgent]
        A[DiagnosisAgent<br/>pure functional diagnose()<br/>returns tuple[Result, ToolCallRecords]]
        R[ToolRegistry<br/>20 Kubernetes tools]
    end

    subgraph K8sClient["K8s 客户端"]
        C[RuntimeKubernetesClient<br/>facade]
        X[ToolExecutor<br/>20 tool implementations]
        P[KubernetesReadClient<br/>Protocol]
    end

    subgraph Enrichment["结果增强"]
        N[DiagnosisEnricher<br/>qualityScore · evidenceAttribution<br/>uncertainties · evidenceTimeline]
        QL[QualityScorer<br/>YAML配置评分]
        FL[FallbackRuleLoader<br/>YAML fallback规则]
    end

    subgraph Reporting["报告持久化"]
        WRT[ReportWriter<br/>orchestrates]
        F[DiagnosisReportFormatter]
        KW[KubernetesDiagnosisReportWriter<br/>upsert_report<br/>atomic create→409→patch]
    end

    W & S & H --> Q
    Q --> D
    D --> T
    T --> Eng
    Eng --> A
    A --> R
    R --> C
    C --> X
    X -.-> P
    Eng --> N
    N --> QL
    N --> FL
    N --> KW
    WRT --> F
    WRT --> KW

    class W,S,H external
    class Q async
    class D dedup
    class Eng,A orch
    class C,X,P k8s
    class N,QL,FL enrich
    class WRT,F,KW report
```

## Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| `EventWatcher` | `agent/triggers/event_watcher.py` | Kubernetes Watch API → trigger events |
| `Scheduler` | `agent/triggers/scheduler.py` | Periodic `list_anomaly_snapshot` triggers |
| `HTTP Webhook` | `agent/triggers/webhook.py` | `POST /alert` → trigger |
| `AsyncEventQueue` | `agent/triggers/async_event_queue.py` | Async worker pool, bounded queue, backpressure |
| `EventStormDeduper` | `agent/transformers/event_storm_deduper.py` | Time-window aggregation, suppresses duplicate events |
| `TriggerTransformer` | `agent/transformers/trigger_transformer.py` | Normalize, augment, correlate triggers |
| `DiagnosisEngine` | `agent/diagnosis/diagnosis_engine.py` | Loop: call DiagnosisAgent until budget exhausted |
| `DiagnosisAgent` | `agent/orchestrator/diagnosis_agent.py` | Pure functional `diagnose(trigger, registry) → tuple[DiagnosisResult, list[ToolCallRecord]]` |
| `ToolRegistry` | `agent/tools/registry.py` | 20 K8s tool definitions, scope guard, execute |
| `RuntimeKubernetesClient` | `agent/k8s_client/runtime.py` | Facade, delegates `execute()` to ToolExecutor |
| `ToolExecutor` | `agent/k8s_client/executor.py` | All 20 tool implementations |
| `KubernetesReadClient` | `agent/k8s_client/base.py` | Protocol defining K8s client interface |
| `DiagnosisEnricher` | `agent/diagnosis/diagnosis_enricher.py` | Fill missing fields, quality score, evidence attribution |
| `QualityScorer` | `agent/config/quality_scorer.py` | Deterministic quality scoring from YAML config |
| `FallbackRuleLoader` | `agent/config/fallback_rule_loader.py` | Symptom fallback rules from YAML |
| `ReportWriter` | `agent/reporting/report_writer.py` | Orchestrates formatting and writing |
| `DiagnosisReportFormatter` | `agent/reporting/diagnosis_reporter.py` | Builds spec/status, dedupe_name |
| `KubernetesDiagnosisReportWriter` | `agent/reporting/diagnosis_reporter.py` | K8s CRD upsert, atomic create-first pattern |

## Key Design Decisions

1. **Pure functional DiagnosisAgent**: `diagnose()` returns `tuple[DiagnosisResult, list[ToolCallRecord]]` — no mutable instance state, `tool_history` lives only in the return value.

2. **AsyncEventQueue backpressure**: `put()` returns `False` when queue full (dropped event counted via metric). `put_blocking()` waits up to 5s.

3. **EventStormDeduper state machine**: Tracks `count`, `first_seen`, `last_seen`, `aggregated` per event key. `mark_aggregated()` called instead of setting `state["aggregated"] = True`.

4. **Atomic report upsert**: `create` first → catch `ApiException(409)` → `patch`. Replaces TOCTOU `get`→`patch`/`create` pattern.

5. **ToolExecutor extraction**: All 20 tool implementations live in `ToolExecutor`. `RuntimeKubernetesClient` is a pure facade — all tool methods delegate to `self._executor.<method>()`.

## Data Flow

```
K8s Event / Scheduled Snapshot
    → AsyncEventQueue (put)
    → EventStormDeduper (next_state / mark_aggregated)
    → TriggerTransformer (normalize / augment / correlate)
    → DiagnosisEngine (loop)
        → DiagnosisAgent (pure fn)
            → ToolRegistry.execute()
                → RuntimeKubernetesClient.execute()
                    → ToolExecutor.<tool>()
    → DiagnosisEnricher (quality / attribution / uncertainties)
    → ReportWriter (format + persist)
    → KubernetesDiagnosisReportWriter (upsert_report)
    → DiagnosisReport (CRD)
```
