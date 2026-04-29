"""Focused tests for Feishu tool-progress card rotation and dedup."""

import importlib
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class FeishuProgressCaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.FEISHU)
        self.sent = []
        self.edits = []
        self.typing = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        message_id = f"progress-{len(self.sent) + 1}"
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
                "message_id": message_id,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def stop_typing(self, chat_id) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": {"stopped": True}})

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class RotatingProgressAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        for preview in ("first command", "second command", "third command"):
            self.tool_progress_callback("tool.started", "terminal", preview, {})
            time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


class DedupProgressAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        for _ in range(3):
            self.tool_progress_callback("tool.started", "terminal", "repeat me", {})
            time.sleep(0.35)
        return {
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def _install_fake_agent(monkeypatch, agent_cls):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401


@pytest.mark.asyncio
async def test_feishu_progress_rotates_to_new_card_when_limit_exceeded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    _install_fake_agent(monkeypatch, RotatingProgressAgent)

    adapter = FeishuProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("display:\n  tool_progress: all\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(gateway_run, "_FEISHU_PROGRESS_CARD_MAX_LINES", 2)
    monkeypatch.setattr(gateway_run, "_FEISHU_PROGRESS_CARD_MAX_CHARS", 10_000)
    monkeypatch.setattr(gateway_run, "_FEISHU_PROGRESS_CARD_MAX_EDITS", 40)
    monkeypatch.setattr(gateway_run, "_PROGRESS_EDIT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(gateway_run, "_PROGRESS_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(gateway_run, "_PROGRESS_TYPING_RESTORE_DELAY_SECONDS", 0.0)

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-feishu-progress-rotate",
        session_key="agent:main:feishu:group:oc_chat",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 2
    assert adapter.sent[0]["message_id"] == "progress-1"
    assert adapter.sent[0]["metadata"]["message_kind"] == "tool_progress"
    assert adapter.sent[0]["metadata"]["progress_page_no"] == 1
    assert adapter.sent[1]["message_id"] == "progress-2"
    assert adapter.sent[1]["metadata"]["message_kind"] == "tool_progress"
    assert adapter.sent[1]["metadata"]["progress_page_no"] == 2
    assert any(edit["message_id"] == "progress-1" for edit in adapter.edits)
    assert "third command" in adapter.sent[1]["content"]


@pytest.mark.asyncio
async def test_feishu_progress_dedup_updates_last_line_without_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    _install_fake_agent(monkeypatch, DedupProgressAgent)

    adapter = FeishuProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("display:\n  tool_progress: all\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(gateway_run, "_FEISHU_PROGRESS_CARD_MAX_LINES", 2)
    monkeypatch.setattr(gateway_run, "_FEISHU_PROGRESS_CARD_MAX_CHARS", 10_000)
    monkeypatch.setattr(gateway_run, "_FEISHU_PROGRESS_CARD_MAX_EDITS", 40)
    monkeypatch.setattr(gateway_run, "_PROGRESS_EDIT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(gateway_run, "_PROGRESS_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(gateway_run, "_PROGRESS_TYPING_RESTORE_DELAY_SECONDS", 0.0)

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-feishu-progress-dedup",
        session_key="agent:main:feishu:group:oc_chat",
    )

    assert result["final_response"] == "done"
    assert len(adapter.sent) == 1
    combined = " ".join([call["content"] for call in adapter.sent] + [call["content"] for call in adapter.edits])
    assert 'terminal: "repeat me" (×3)' in combined
    assert not any(call["metadata"].get("progress_page_no") == 2 for call in adapter.sent if call["metadata"])
