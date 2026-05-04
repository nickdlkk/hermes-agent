# Plan: Response Footer — Model + Usage Display

## Goal
在 AI 回复末尾追加显示 model + usage 信息（与 `/usage` 命令格式一致），用于判断是否需要开新会话。

## Footer 格式
```
Model: MiniMax-M2.7-highspeed · API calls: 69 · Context: 37,157 / 204,800 (18%) · Compressions: 1
```

## 输出类型处理
- **Card（含表格）** → 飞书 Card native `footer` 元素（视觉分割）
- **post / text** → footer 文字 append 到 content 末尾

---

## 信息流

```
run.py (_handle_message_with_agent)
    ↓ event._hermes_response_meta
base.py (_process_message_background)
    ↓ metadata["_hermes_response_meta"]
feishu.py (send)
    ↓ 读取 metadata
    ↓ 渲染 footer（Card: native element / post/text: append 文字）
```

---

## Phase 1: gateway/run.py — 注入 response meta
**状态:** pending

**目标:** 在 `_handle_message_with_agent` 的 return 前（~line 10623），从 `_agent` 提取 model/api_calls/context_pct/compressions，注入 `event._hermes_response_meta`

**改动:** 在 return dict 前插入：

```python
# 构建 response footer metadata
_resolved_model = getattr(_agent, "model", None) if _agent else None
_api_calls = getattr(_agent, "session_api_calls", 0) if _agent else 0
_ctx = _agent.context_compressor if _agent and hasattr(_agent, "context_compressor") else None
_ctx_tokens = getattr(_ctx, "last_prompt_tokens", 0) if _ctx else 0
_ctx_len = getattr(_ctx, "context_length", 0) if _ctx else 0
_ctx_pct = min(100, int(_ctx_tokens / _ctx_len * 100)) if _ctx_len else 0
_compressions = getattr(_ctx, "compression_count", 0) if _ctx else 0

event._hermes_response_meta = {
    "model": _resolved_model,
    "api_calls": _api_calls,
    "context_tokens": _ctx_tokens,
    "context_limit": _ctx_len,
    "context_pct": _ctx_pct,
    "compressions": _compressions,
}
```

**验证:** 确认 `event` 对象在 `_handle_message_with_agent` 中可用（是函数参数）

---

## Phase 2: gateway/platforms/base.py — 传递 response meta
**状态:** pending

**目标:** 在 `_process_message_background` 的 `await self.send()` 调用前（~line 2472），从 `event._hermes_response_meta` 取值，塞入 `_thread_metadata`（传给 send 的 metadata）

**改动:** 在 `if text_content:` 前插入：

```python
# 传递 response footer metadata 给 send()
_response_meta = getattr(event, "_hermes_response_meta", None) if event else None
if _response_meta:
    _thread_metadata = _thread_metadata or {}
    _thread_metadata["_hermes_response_meta"] = _response_meta
```

---

## Phase 3: gateway/platforms/feishu.py — 渲染 footer
**状态:** pending

**目标:** 在 `send()` 方法里（~line 1835），读取 `metadata.get("_hermes_response_meta")`，根据消息类型渲染 footer

### 3a. Card footer — native 元素
**位置:** `_build_interactive_payload` 调用后（~line 1852-1873 表格分支）

在 `payload_chunks` 循环内，每次 `_feishu_send_with_retry` 调用前，从 `metadata["_hermes_response_meta"]` 构建 footer element，注入到 card JSON 的 `element` 末尾。

Card footer 结构：
```python
footer_element = {
    "tag": "note",
    "elements": [
        {"tag": "plain_text", "text": f"Model: {meta['model']} · API calls: {meta['api_calls']} · Context: {meta['context_tokens']:,} / {meta['context_limit']:,} ({meta['context_pct']}%) · Compressions: {meta['compressions']}"}
    ]
}
```

飞书 Card 支持在 `elements` 末尾加 `note` 类型的 element 作为 footer。

**改动:** 
- `_feishu_send_with_retry` 调用前，parse payload JSON，append footer element，再 dumps

### 3b. post / text footer — append 文字
**位置:** ~line 1874-1911（无表格分支）

在 `chunks` 循环内，每次 `msg_type, payload` 确定后，从 `metadata["_hermes_response_meta"]` 构建文字 footer，append 到 chunk 末尾：

```python
footer_text = f"\n\nModel: {meta['model']} · API calls: {meta['api_calls']} · Context: {meta['context_tokens']:,} / {meta['context_limit']:,} ({meta['context_pct']}%) · Compressions: {meta['compressions']}"
```

- **post 类型**: footer 加在 `content` 末尾（post payload 的 `content` 是个 list of content blocks）
- **text 类型**: footer 加在 `{"text": content}` 的 text 末尾

---

## Phase 4: 验证
**状态:** pending

- [ ] 重启 hermes-gateway
- [ ] 发送任意消息，确认 footer 出现
- [ ] 发送带表格消息，确认 Card footer 正确渲染
- [ ] 发送普通文字消息，确认 post/text footer 正确
- [ ] 确认 `/usage` 命令格式与 footer 一致

---

## 涉及文件

| 文件 | 改动类型 |
|------|---------|
| `gateway/run.py` | 注入 metadata |
| `gateway/platforms/base.py` | 传递 metadata |
| `gateway/platforms/feishu.py` | 渲染 footer |
