# 测试覆盖率改进计划

当前总体覆盖率: **64%**

---

## 0% 模块（需要新建测试文件）

### `agent/triggers/async_event_queue.py` (100 行, 0%)

**问题**: 纯 asyncio 代码，没有测试。

**需要测试的场景**:
- `put()` 非阻塞加入队列（队列未满/已满）
- `put_blocking()` 阻塞超时
- `start()` / `stop()` 生命周期
- 多 worker 并发消费
- `qsize()` 近似队列大小
- `is_started` 属性状态

**Mock 方式**: mock `asyncio.Queue`, `asyncio.create_task`, `threading.Thread`

```python
# tests/test_async_event_queue.py (需要新建)
import asyncio
from unittest.mock import MagicMock, patch
from agent.triggers.async_event_queue import AsyncEventQueue

def test_put_returns_false_when_not_started():
    queue = AsyncEventQueue()
    assert queue.put("item") is False

def test_put_returns_true_when_queue_available():
    ...

def test_put_returns_false_when_queue_full():
    ...

def test_start_idempotent():
    ...

def test_stop_cancels_workers():
    ...
```

---

### `agent/triggers/scheduler.py` (24 行, 0%)

**问题**: 调度器入口，依赖 `AsyncEventQueue`。

**需要测试的场景**:
- `Scheduler.start()` / `stop()`
- 调度周期触发

---

### `agent/triggers/webhook.py` (18 行, 0%)

**问题**: Webhook 接收端，依赖外部请求。

**需要测试的场景**:
- Payload 解析
- 验证签名逻辑（如果有）

---

### `agent/app.py` (38 行, 0%)

**问题**: 应用入口，FastAPI/Starlette app 实例化。

**需要测试的场景**:
- `create_app()` 返回有效 app
- 健康检查端点

---

## 低覆盖率模块

### `agent/k8s_client/executor.py` (14% → 可提升到 60%+)

**问题**: `FakeKubernetesClient` 直接实现所有 20 个方法，不经过 `ToolExecutor`。

**根因**: `test_agent_service.py` 的 `FakeKubernetesClient` 是 standalone mock，不是 `ToolExecutor` 的 mock。

**提升方法**:
```python
# 在 test_agent_service.py 或新建 test_executor.py
from agent.k8s_client.executor import ToolExecutor

def test_get_workload_status_calls_core(mocker):
    mock_core = mocker.patch.object(ToolExecutor, '__init__', lambda self, **kw: None)
    # 或者用 MagicMock 注入

# 更简单的方式: 直接 mock kubernetes.client
```

**建议**: 新建 `tests/test_executor.py`，用 `unittest.mock` mock kubernetes API 调用，测试 ToolExecutor 的 20 个方法的错误路径（API 抛异常时返回 error dict）。

---

### `agent/k8s_client/runtime.py` (23%)

**问题**: facade 方法未通过 executor 测试覆盖。

**同上，通过 test_executor.py 间接覆盖。**

---

### `agent/ui/http_server.py` (34%)

**问题**: 只测试了 `AlertTaskManager`，其他 HTTP handler 未覆盖。

**需要测试的场景**:
- `/reports` GET handler
- `/reports/<name>` GET handler
- 404 路径
- Query parameter 过滤

**Mock 方式**: mock `self.service` 和 `request`

---

### `agent/reporting/diagnosis_reporter.py` (61% → 可提升到 80%+)

**问题**: `KubernetesDiagnosisReportWriter.upsert_report` 未覆盖：
- `ApiException(409)` 路径（create 冲突）
- `ApiException(其他)` 路径
- patch_namespaced_custom_object_status 异常

**提升方法**:
```python
from unittest.mock import MagicMock, patch

def test_upsert_report_create_on_409(mocker):
    mock_custom = MagicMock()
    exc_409 = MagicMock()
    exc_409.status = 409
    mock_custom.create_namespaced_custom_object.side_effect = exc_409
    # 验证 patch 被调用
```

---

### `agent/transformers/event_storm_deduper.py` (56%)

**问题**: 一些边界分支未覆盖：
- `next_state` 的 `aggregated=True` 路径
- `mark_aggregated` 的并发调用
- 时间窗口边界条件

**Mock 方式**: 纯单元测试，mock `datetime`

---

### `agent/triggers/event_watcher.py` (65%)

**问题**: 一些事件类型分支未覆盖。

**Mock 方式**: mock Kubernetes Watch stream

---

## 高优先级排序

| 优先级 | 模块 | 覆盖率 | 努力度 | 价值 |
|--------|------|--------|--------|------|
| P1 | `async_event_queue.py` | 0% | 中 | 高 — 核心异步基础设施 |
| P1 | `diagnosis_reporter.py` | 61% | 低 | 高 — 刚修的竞态条件需要测试覆盖 |
| P2 | `executor.py` | 14% | 高 | 中 — 20 个方法需要覆盖 |
| P2 | `event_storm_deduper.py` | 56% | 中 | 中 — 核心去重逻辑 |
| P3 | `scheduler.py` | 0% | 低 | 低 — 入口代码 |
| P3 | `webhook.py` | 0% | 低 | 低 — 入口代码 |
| P3 | `app.py` | 0% | 低 | 低 — 入口代码 |
| P3 | `http_server.py` | 34% | 中 | 中 — 部分端点未覆盖 |

---

## 执行建议

1. **先补 P1** — `async_event_queue.py` 和 `diagnosis_reporter.py` 是核心逻辑
2. **用 pytest-mock** — `pip install pytest-mock`，简化 mock 代码
3. **fake_kubernetes_client 改进** — 改造 `FakeKubernetesClient` 为真正的 mock，让它调用 `ToolExecutor`（或反过来，让 ToolExecutor 的测试用 MagicMock 注入 API client）
