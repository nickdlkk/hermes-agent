# Task Plan: Hindsight on_session_end() 实现

## Goal

为 `HindsightMemoryProvider` 实现 `on_session_end()` hook，解决 session 正常结束时 buffered turns 未达 N 轮阈值而丢失的问题。

## Current Phase

Phase 2

## Background

**问题根因：**
- `sync_turn()` 累积 turns 到 `_session_turns` buffer，达到 `_retain_every_n_turns` 才 flush
- `on_session_end()` 不存在，session 正常结束时 buffer 从未 flush
- `shutdown()` 只关闭 writer + client，不管 buffered turns
- `on_session_switch()` 有 flush 逻辑，但 session end ≠ session switch

**Gateway 调用顺序（run_agent.py L4685-4701）：**
1. `memory_manager.on_session_end(messages)` → provider 的 `on_session_end()` hook
2. `memory_manager.shutdown_all()` → provider 的 `shutdown()`

**`on_session_end()` signature（ABC L153）：**
```python
def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
```

## Phases

### Phase 1: Requirements & Discovery
- [x] 分析 `sync_turn()` retain 逻辑（buffer + threshold）
- [x] 分析 `shutdown()` 当前行为（只关 writer/client，不管 buffer）
- [x] 分析 `on_session_switch()` flush 实现（作为参考）
- [x] 确认 MemoryProvider ABC 中 `on_session_end()` signature
- **Status:** complete

### Phase 2: Implementation
- [ ] 实现 `_flush_session_turns()` 抽取方法
- [ ] 实现 `on_session_end()` hook，调用 `_flush_session_turns()`
- [ ] 修改 `shutdown()`：在关闭前检查并 flush 剩余 buffer（兜底）
- **Status:** pending

### Phase 3: Code Review & Verification
- [ ] 对比 `on_session_switch()` flush vs `on_session_end()` flush 的差异
- [ ] 确认 `_shutting_down` guard 正确
- [ ] 确认 writer queue 的 sentinel 逻辑不受影响
- [ ] 确认 `shutdown()` 的 buffer flush 与 `on_session_end()` 不重复（两者都会被调用，但时机不同）
- **Status:** pending

### Phase 4: Delivery
- [ ] 将 patch 内容写入文件
- [ ] 向 Nick 报告，等待批准后执行
- **Status:** pending

## Key Questions Answered

1. **调用顺序**：gateway 中 `on_session_end()` 先于 `shutdown()` 触发
2. **`on_session_end()` signature**：`messages: List[Dict[str, Any]]`（full conversation history）
3. **`messages` 参数用途**：Hindsight 不用它，用 `_session_turns` buffer 已足够
4. **兜底策略**：`shutdown()` 中也 flush buffer，防止 CLI 路径或边缘情况漏掉

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| `_flush_session_turns()` 抽取为独立方法 | `on_session_switch()` 和 `on_session_end()` + `shutdown()` 兜底都需要 flush |
| `shutdown()` 中也做 buffer flush 兜底 | CLI 直接调用 `shutdown()` 而不走 gateway 路径；双保险 |
| flush 通过 `_retain_queue.put()` 串行化 | 与 `on_session_switch()` 一致，writer loop 保证 FIFO |
| `on_session_end()` 中不 clear buffer | 让 `shutdown()` 兜底时也能检测到剩余 buffer |

## Implementation Detail

### `_flush_session_turns()` 抽取
从 `on_session_switch()` L1602-1661 的 flush 逻辑抽取核心代码：
```python
def _flush_session_turns(self) -> bool:
    """Flush buffered session turns if any. Returns True if flush was enqueued."""
    if not self._session_turns:
        return False
    # snapshot old state...
    # build _flush() job...
    # _retain_queue.put(_flush)...
    return True
```

### `on_session_end()` 实现
```python
def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
    # ignore messages param, use our own _session_turns buffer
    if self._shutting_down.is_set():
        return
    self._flush_session_turns()
```

### `shutdown()` 兜底
在 `shutdown()` 中，`_shutting_down.set()` 之后、join writer 之前，检查剩余 buffer：
```python
# In shutdown(), after self._shutting_down.set()
if self._session_turns:
    self._flush_session_turns()
```

但此时 writer 可能已经在退出，且 `_shutting_down.set()` 会让 `sync_turn()` 直接 return...
需要更仔细设计 shutdown 兜底逻辑。

**关键**：`_flush_session_turns()` 中需要检查 `not self._shutting_down.is_set()` 才能 enqueue。
`shutdown()` 中 flush 时需要传 `allow_shutdown=True` 参数。

## Errors Encountered

| Error | Attempt | Resolution |
|-------|---------|------------|
| 无 | - | - |

## Notes

- 改动仅限于 `plugins/memory/hindsight/__init__.py`
- 参考 `on_session_switch()` L1602-1661 的 flush 实现
- 预计新增约 50 行代码
