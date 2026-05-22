"""
Lansenger (蓝信) platform adapter — Hermes Agent plugin version.

Uses Lansenger Smart Bot API for real-time message reception via WebSocket.
Responses are sent via Lansenger's HTTP API.

Requires:
    pip install websockets httpx
    LANSENGER_APP_ID and LANSENGER_APP_SECRET env vars

Configuration in config.yaml:
    platforms:
      lansenger:
        enabled: true
        extra:
          app_id: "your-app-id"        # or LANSENGER_APP_ID env var
          app_secret: "your-secret"    # or LANSENGER_APP_SECRET env var
          api_gateway_url: "https://open.e.lanxin.cn/open/apigw"  # optional

This is a PLUGIN adapter — registered via ctx.register_platform() in the
register(ctx) entry point.  No modifications to core Hermes code are needed.
"""

import asyncio
import logging
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

try:
    import websockets
    from websockets.asyncio.client import connect as _ws_connect
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]
    _ws_connect = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

# ── Lazy imports from Hermes core ──────────────────────────────────────────
# These live in the main repo; we import at module level because the gateway
# guarantees the package is on sys.path before the plugin is loaded.
from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_document_from_bytes,
)

logger = logging.getLogger(__name__)

# Constants
MAX_MESSAGE_LENGTH = 4000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEFAULT_API_GATEWAY_URL = "https://open.e.lanxin.cn/open/apigw"

# API Endpoints
API_ENDPOINTS = {
    "auth": {
        "tenant_access_token": "/auth/v3/tenant_access_token/internal",
    },
    "websocket": {
        "endpoint": "/v1/ws/endpoint/create",
    },
    "smart_bot": {
        "private_message": "/v1/bot/messages/create",
        "group_message": "/v1/messages/group/create",
    },
    "app": {
        "upload_media": "/v1/medias/create",
    },
    "message": {
        "revoke": "/v1/messages/revoke",
        "dynamic_update": "/v1/messages/dynamic/update",
    },
    "groups": {
        "fetch": "/v2/groups/fetch",
    },
}


# check_requirements is defined at the bottom of this file (near register()).


class LansengerAdapter(BasePlatformAdapter):
    """Lansenger chatbot adapter using WebSocket long-connection."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("lansenger"))

        extra = config.extra or {}
        self._app_id: str = extra.get("app_id") or os.getenv("LANSENGER_APP_ID", "")
        self._app_secret: str = extra.get("app_secret") or os.getenv("LANSENGER_APP_SECRET", "")
        self._api_gateway_url: str = extra.get("api_gateway_url") or os.getenv("LANSENGER_API_GATEWAY_URL", DEFAULT_API_GATEWAY_URL)

        # Home channel from PlatformConfig.home_channel (standard Hermes structure)
        self._home_channel_id: Optional[str] = None
        if config.home_channel:
            self._home_channel_id = config.home_channel.chat_id

        self._ws_client: Optional[Any] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._ws_url: Optional[str] = None

        # Message deduplication
        self._dedup = MessageDeduplicator(max_size=1000)

        # Chat type cache: maps chat_id → "group" or "dm"
        # Populated from inbound messages so outbound can route correctly
        self._chat_type_map: Dict[str, str] = {}
        self._chat_type_file = Path.home() / ".hermes" / "lansenger_chat_types.json"
        self._load_chat_type_map()

        # User language cache: maps chat_id → "zh" or "en"
        # Populated from inbound messages so approval cards use the right language
        self._user_lang_map: Dict[str, str] = {}

        # Token cache
        self._app_token: Optional[str] = None
        self._token_expiry: float = 0
        self._token_file = Path.home() / ".hermes" / "lansenger_token.json"

        # Owner ID (the user who bound the bot)
        self._owner_id: Optional[str] = None
        self._owner_id_file = Path.home() / ".hermes" / "lansenger_owner.json"
        self._load_owner_id()

        # Auto-sethome: first DM becomes home channel if none configured.
        # If an existing home is a group (group:xxx), the first DM overrides it.
        _existing_home = self._home_channel_id or ""
        self._auto_sethome_done: bool = bool(_existing_home) and not _existing_home.startswith("group:")

        # Pairing state
        self._pending_pairings: Dict[str, Dict[str, Any]] = {}

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self) -> bool:
        """Connect to Lansenger via WebSocket."""
        if not WEBSOCKETS_AVAILABLE or not HTTPX_AVAILABLE:
            return False
        if not self._app_id or not self._app_secret:
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0)

            ws_url = await self._get_websocket_url()
            if not ws_url:
                logger.error("[Lansenger] Failed to get WebSocket URL — check appId/secret and API gateway")
                return False

            self._running = True
            self._ws_task = asyncio.create_task(self._run_ws(ws_url))
            logger.info("[Lansenger] WebSocket task created")
            return True
        except Exception as e:
            logger.error("[Lansenger] Failed to connect: %s", e)
            return False

    async def _get_websocket_url(self) -> Optional[str]:
        """Get WebSocket URL from Lansenger API (includes ticket with expiresIn)."""
        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['websocket']['endpoint']}"
            logger.info("[Lansenger] Requesting WebSocket endpoint from %s", url)
            response = await self._http_client.post(
                url,
                json={"appId": self._app_id, "secret": self._app_secret}
            )
            logger.info("[Lansenger] WS endpoint response: status=%d, body=%s",
                        response.status_code, response.text[:200])
            response.raise_for_status()
            data = response.json()
            
            if data.get("errCode") == 0:
                ws_url = data.get("data", {}).get("wsEndpoint")
                expires_in = data.get("data", {}).get("expiresIn", 7200)
                ping_interval = data.get("data", {}).get("pingInterval", 50)
                logger.info("[Lansenger] Got WS endpoint: url=%s, expiresIn=%ds, pingInterval=%ds",
                            ws_url, expires_in, ping_interval)
                self._ws_url = ws_url
                return ws_url
            else:
                logger.error("[Lansenger] WebSocket endpoint error: errCode=%s, errMsg=%s",
                             data.get("errCode"), data.get("errMsg"))
                return None
        except httpx.HTTPStatusError as e:
            logger.error("[Lansenger] WS endpoint HTTP error: %s (response=%s)",
                         e, e.response.text[:200] if e.response else "n/a")
            return None
        except Exception as e:
            logger.error("[Lansenger] Error getting WebSocket URL: %s", e)
            return None

    async def _run_ws(self, ws_url: str) -> None:
        """Run WebSocket client with auto-reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                logger.info("[Lansenger] Connecting to WebSocket: %s", ws_url)
                async with _ws_connect(ws_url, ping_interval=30, ping_timeout=10, open_timeout=10) as ws:
                    self._ws_client = ws
                    backoff_idx = 0
                    self._mark_connected()
                    logger.info("[Lansenger] WebSocket connected (ping_interval=30s, ping_timeout=10s)")

                    async for message in ws:
                        await self._on_message(message)
            except asyncio.CancelledError:
                return
            except websockets.exceptions.InvalidStatus as e:
                if not self._running:
                    return
                resp = e.response
                logger.error("[Lansenger] WebSocket rejected: status_code=%d, headers=%s, body=%s",
                             resp.status_code, dict(resp.headers) if resp.headers else "n/a",
                             resp.body.decode(errors='replace')[:200] if resp.body else "n/a")
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[Lansenger] WebSocket error: %s (type=%s)", e, type(e).__name__)

            if not self._running:
                return

            self._ws_client = None
            self._mark_disconnected()
            logger.warning("[Lansenger] WebSocket disconnected, will reconnect")

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[Lansenger] Reconnecting in %ds (attempt %d)...", delay, backoff_idx + 1)
            await asyncio.sleep(delay)
            backoff_idx += 1

            new_url = await self._get_websocket_url()
            if new_url:
                ws_url = new_url
                logger.info("[Lansenger] Will reconnect with fresh ticket")
            else:
                logger.error("[Lansenger] Failed to get new ticket, cannot reconnect — retrying next cycle")

    async def disconnect(self) -> None:
        """Disconnect from Lansenger."""
        self._running = False
        self._mark_disconnected()

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._ws_client = None
        self._dedup.clear()
        logger.info("[Lansenger] Disconnected")

    # -- Inbound message processing -----------------------------------------

    def _load_owner_id(self) -> None:
        """Load owner ID from file."""
        try:
            if self._owner_id_file.exists():
                import json
                data = json.loads(self._owner_id_file.read_text())
                self._owner_id = data.get("owner_id")
                if self._owner_id:
                    logger.info("[Lansenger] Loaded owner ID: %s", self._owner_id[:20] if self._owner_id else None)
        except Exception as e:
            logger.warning("[Lansenger] Failed to load owner ID: %s", e)

    def _save_owner_id(self) -> None:
        """Save owner ID to file."""
        try:
            import json
            self._owner_id_file.parent.mkdir(parents=True, exist_ok=True)
            self._owner_id_file.write_text(json.dumps({"owner_id": self._owner_id}, indent=2))
            logger.info("[Lansenger] Saved owner ID: %s", self._owner_id[:20] if self._owner_id else None)
        except Exception as e:
            logger.error("[Lansenger] Failed to save owner ID: %s", e)

    def _load_chat_type_map(self) -> None:
        try:
            if self._chat_type_file.exists():
                import json
                data = json.loads(self._chat_type_file.read_text())
                if isinstance(data, dict):
                    self._chat_type_map.update(data)
                    logger.info("[Lansenger] Loaded %d chat type mappings from file", len(data))
        except Exception as e:
            logger.warning("[Lansenger] Failed to load chat type map: %s", e)

    def _persist_chat_type_map(self) -> None:
        try:
            import json
            self._chat_type_file.parent.mkdir(parents=True, exist_ok=True)
            self._chat_type_file.write_text(json.dumps(self._chat_type_map, indent=2))
            logger.debug("[Lansenger] Persisted %d chat type mappings", len(self._chat_type_map))
        except Exception as e:
            logger.error("[Lansenger] Failed to persist chat type map: %s", e)

    async def _auto_sethome(self, chat_id: str) -> None:
        """Auto-designate the first DM as Lansenger home channel.

        Triggers when no home_channel is configured, or when an existing
        group home is superseded by the first DM (DM > group upgrade).
        Silent: writes config.yaml + env, no user-facing message.
        """
        if self._auto_sethome_done:
            return

        # Check if we should set/upgrade
        _cur_home = self._home_channel_id or ""
        _should_set = (not _cur_home) or _cur_home.startswith("group:")

        # DM seen — no further upgrades needed after this
        self._auto_sethome_done = True

        if not _should_set:
            return

        try:
            from hermes_constants import get_hermes_home
            from utils import atomic_yaml_write
            import yaml

            _home = get_hermes_home()
            config_path = _home / "config.yaml"
            user_config: dict = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}

            # Write lansenger home_channel into config.yaml
            platforms = user_config.setdefault("platforms", {})
            lansenger = platforms.setdefault("lansenger", {})
            lansenger["home_channel"] = {"platform": "lansenger", "chat_id": chat_id, "name": "Lansenger Home"}
            atomic_yaml_write(config_path, user_config)

            # Update runtime state immediately
            self._home_channel_id = chat_id
            os.environ["LANSENGER_HOME_CHANNEL"] = str(chat_id)

            # Also update the adapter's config.home_channel for runtime
            if hasattr(self, "config") and hasattr(self.config, "home_channel"):
                try:
                    from gateway.config import HomeChannel, Platform
                    self.config.home_channel = HomeChannel(platform=Platform("lansenger"), chat_id=chat_id, name="Lansenger Home")
                except Exception:
                    pass

            logger.info(
                "[Lansenger] Auto-sethome: designated %s as Lansenger home channel",
                chat_id[:30],
            )
        except Exception as e:
            logger.warning("[Lansenger] Auto-sethome failed (non-critical): %s", e)

    async def _on_message(self, raw_message: str) -> None:
        """Process an incoming Lansenger message."""
        import json

        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("[Lansenger] Invalid JSON message")
            return

        events = data.get("events", [])
        for event_data in events:
            await self._process_event(event_data)

    async def _process_event(self, event_data: Dict[str, Any]) -> None:
        """Process a single event."""
        # Debug: log raw event structure
        msg_data = event_data.get("data", {})
        msg_type = msg_data.get("msgType", "text")
        logger.debug("[Lansenger] Raw event msgType=%s, data keys=%s", msg_type, list(msg_data.keys()) if msg_data else "None")
        
        msg_id = msg_data.get("messageId") or uuid.uuid4().hex

        if self._dedup.is_duplicate(msg_id):
            logger.debug("[Lansenger] Duplicate message %s, skipping", msg_id)
            return

        text = await self._extract_text(msg_data)
        if not text:
            logger.debug("[Lansenger] Empty message (msgType=%s), skipping", msg_type)
            return

        # Chat context
        chat_type = msg_data.get("chatType", "p2p")
        is_group = chat_type == "group"
        sender_id = msg_data.get("from", "")
        chat_id = msg_data.get("conversationId") or sender_id

        # Cache chat type for outbound routing
        self._chat_type_map[chat_id] = "group" if is_group else "dm"
        self._persist_chat_type_map()

        # Cache user language from message text (for appCard language selection)
        text_content = msg_data.get("text", {}).get("content", "")
        if text_content:
            self._user_lang_map[chat_id] = self._detect_lang(text_content)

        # Record owner ID on first p2p message
        if not is_group and not self._owner_id and sender_id:
            self._owner_id = sender_id
            self._save_owner_id()
            logger.info("[Lansenger] Recorded owner ID from first message: %s", sender_id)

        # Auto-sethome: designate the first DM as home channel.
        # If no home_channel is configured, or the existing one is a group,
        # the first DM overrides it (DM > group upgrade, same as Yuanbao).
        if not is_group:
            await self._auto_sethome(chat_id)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=msg_data.get("conversationTitle"),
            chat_type="group" if is_group else "dm",
            user_id=sender_id,
            user_name=msg_data.get("senderName", sender_id),
        )

        timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=msg_data,
            timestamp=timestamp,
        )

        logger.debug("[Lansenger] Message from %s in %s: %s",
                     source.user_name, chat_id[:20] if chat_id else "?", text[:50])
        await self.handle_message(event)

    async def _extract_text(self, msg_data: Dict[str, Any]) -> str:
        """Extract text from message, downloading media if needed.
        
        For image/video/file/voice: downloads first media and returns file path.
        """
        msg_type = msg_data.get("msgType", "text")
        msg_payload = msg_data.get("msgData", {})

        if msg_type == "text":
            return msg_payload.get("text", {}).get("content", "").strip()
        
        elif msg_type == "image":
            media_ids = msg_payload.get("image", {}).get("mediaIds", [])
            if media_ids:
                media_bytes = await self._download_media(media_ids[0])
                if media_bytes:
                    path = await self._save_media_temp(media_bytes, "image")
                    return path if path else "[Image download failed]"
            return "[Image]"
        
        elif msg_type == "video":
            media_ids = msg_payload.get("video", {}).get("mediaIds", [])
            if media_ids:
                media_bytes = await self._download_media(media_ids[0])
                if media_bytes:
                    path = await self._save_media_temp(media_bytes, "video")
                    return path if path else "[Video download failed]"
            return "[Video]"
        
        elif msg_type == "file":
            media_ids = msg_payload.get("file", {}).get("mediaIds", [])
            if media_ids:
                media_bytes = await self._download_media(media_ids[0])
                if media_bytes:
                    path = await self._save_media_temp(media_bytes, "file")
                    return path if path else "[File download failed]"
            return "[File]"
        
        elif msg_type == "voice":
            media_ids = msg_payload.get("voice", {}).get("mediaIds", [])
            if media_ids:
                media_bytes = await self._download_media(media_ids[0])
                if media_bytes:
                    path = await self._save_media_temp(media_bytes, "voice")
                    return path if path else "[Voice download failed]"
            return "[Voice]"
        
        elif msg_type == "position":
            pos = msg_payload.get("position", {})
            name = pos.get("name", "")
            address = pos.get("address", "")
            return f"[Location] {name} {address}" if name or address else "[Location]"
        
        elif msg_type == "card":
            staff_id = msg_payload.get("card", {}).get("staffId", "")
            return f"[Contact Card] {staff_id}" if staff_id else "[Contact Card]"
        
        elif msg_type == "sticker":
            sticker_id = msg_payload.get("sticker", {}).get("stickerId", "")
            return f"[Sticker] {sticker_id}" if sticker_id else "[Sticker]"

        return ""

    # -- Token management ---------------------------------------------------

    async def _get_app_token(self) -> Optional[str]:
        """Get or refresh app access token, with persistent caching."""

        if self._app_token and datetime.now().timestamp() < self._token_expiry:
            return self._app_token

        persisted = self._load_persisted_token()
        if persisted and datetime.now().timestamp() < persisted["expires_at"]:
            self._app_token = persisted["app_token"]
            self._token_expiry = persisted["expires_at"]
            logger.info("[Lansenger] Loaded persisted appToken (expires in %ds)",
                        int(persisted["expires_at"] - datetime.now().timestamp()))
            return self._app_token

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)

        try:
            url = f"{self._api_gateway_url}/v1/apptoken/create"
            params = {
                "grant_type": "client_credential",
                "appid": self._app_id,
                "secret": self._app_secret
            }
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                logger.error("[Lansenger] Token error: %s", data.get("errMsg"))
                return None

            self._app_token = data.get("data", {}).get("appToken")
            expires_in = data.get("data", {}).get("expiresIn", 7200)
            self._token_expiry = datetime.now().timestamp() + expires_in - 300

            self._persist_token(self._app_token, self._token_expiry + 300)

            logger.info("[Lansenger] Got new access token (expires in %ds)", expires_in)
            return self._app_token
        except Exception as e:
            logger.error("[Lansenger] Error getting token: %s", e)
            return None

    def _load_persisted_token(self) -> Optional[Dict[str, Any]]:
        """Load persisted token from ~/.hermes/lansenger_token.json."""
        try:
            if not self._token_file.exists():
                return None
            content = self._token_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if "app_token" in data and "expires_at" in data:
                return data
        except Exception as e:
            logger.debug("[Lansenger] Failed to load persisted token: %s", e)
        return None

    def _persist_token(self, app_token: str, expires_at: float) -> None:
        """Write token to ~/.hermes/lansenger_token.json for cross-process reuse."""
        try:
            data = {
                "app_token": app_token,
                "expires_at": expires_at,
            }
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(json.dumps(data), encoding="utf-8")
            logger.debug("[Lansenger] Persisted appToken to %s", self._token_file)
        except Exception as e:
            logger.debug("[Lansenger] Failed to persist token: %s", e)

    async def _download_media(self, media_id: str) -> Optional[bytes]:
        """Download media file by media ID. Returns raw file bytes or None."""
        token = await self._get_app_token()
        if not token:
            return None

        try:
            url = f"{self._api_gateway_url}/v1/medias/{media_id}/fetch"
            params = {"app_token": token}
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error("[Lansenger] Download media error: %s", e)
            return None

    async def _save_media_temp(self, media_bytes: bytes, media_type: str = "file") -> str:
        """Save media bytes to temp file, return file path."""
        import tempfile
        
        ext_map = {"image": ".jpg", "video": ".mp4", "file": ".dat", "voice": ".amr"}
        ext = ext_map.get(media_type, ".dat")
        
        # Detect image type from magic bytes
        if media_type == "image" and len(media_bytes) >= 8:
            if media_bytes[:2] == b'\xff\xd8': ext = ".jpg"
            elif media_bytes[:8] == b'\x89PNG\r\n\x1a\n': ext = ".png"
            elif media_bytes[:6] in (b'GIF87a', b'GIF89a'): ext = ".gif"
        
        fd, path = tempfile.mkstemp(suffix=ext, prefix=f"lansenger_{media_type}_")
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(media_bytes)
            logger.info("[Lansenger] Saved media to %s", path)
            return path
        except Exception as e:
            logger.error("[Lansenger] Save media error: %s", e)
            try: os.unlink(path)
            except: pass
            return ""

    # -- Outbound message sending -------------------------------------------

    async def send(self, chat_id: str, content: str, **kwargs) -> SendResult:
        """Send a message (alias for send_format_text)."""
        reminder = kwargs.get("reminder")
        return await self.send_format_text(chat_id, content, reminder=reminder)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Send typing indicator (not supported by Lansenger)."""
        pass  # Lansenger doesn't support typing indicators

    async def send_text(self, chat_id: str, content: str, reminder: dict = None) -> SendResult:
        """Send a plain text message, optionally with @mentions.
        
        Routes to /v1/messages/group/create for group chats,
        /v1/bot/messages/create for private chats.
        
        Args:
            chat_id: Recipient user ID or chat ID
            content: Text content. In group chat, recommended to include @姓名
                     when replying to someone (e.g. "@张三 请查收").
            reminder: Optional dict with 'all' (bool) and 'userIds' (list) for @mentions.
                      Private chat supports this but it is unnecessary (only one participant).
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            is_group = self._chat_type_map.get(chat_id) == "group"

            if is_group:
                # Group message: use /v1/messages/group/create
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
                text_data = {"content": content}
                if reminder:
                    text_data["reminder"] = reminder
                payload = {
                    "groupId": chat_id,
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }
            else:
                # Private message: use /v1/bot/messages/create
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
                text_data = {"content": content}
                if reminder:
                    text_data["reminder"] = reminder
                payload = {
                    "userIdList": [chat_id],
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Text message sent to %s (group=%s)", chat_id, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Send text error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_format_text(self, chat_id: str, content: str, reminder: dict = None) -> SendResult:
            """Send a formatted text message (Markdown support), optionally with @mentions.

            Routes to /v1/messages/group/create for group chats,
            /v1/bot/messages/create for private chats.

NOTE: formatText @mention (reminder) is a NEWER API capability.
        Older Lansenger API versions silently accept the reminder field without error
        but do NOT trigger client-side @mention notifications. On newer versions,
        reminder triggers the notification. In group chat, it is recommended to
        include @姓名 in the text content so people know who the reply is for.
        Private chat supports reminder but it is unnecessary (only one participant).

        Args:
            chat_id: Recipient user ID or chat ID
            content: Markdown-formatted text content. In group chat, recommended to
                     include @姓名 when replying to someone (e.g. "@张三 请查看报告").
            reminder: Optional dict with 'all' (bool), 'userIds' (list), and
                      'botIds' (list) for @mentions. Private chat supports this
                      but it is unnecessary. Old API silently accepts this field
                      without triggering notifications.
            """
            token = await self._get_app_token()
            if not token:
                return SendResult(success=False, error="No access token")

            try:
                is_group = self._chat_type_map.get(chat_id) == "group"

                format_text_data: Dict[str, Any] = {
                    "formatType": 1,
                    "text": content,
                }
                if reminder:
                    format_text_data["reminder"] = reminder

                if is_group:
                    url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
                    payload = {
                        "groupId": chat_id,
                        "msgType": "formatText",
                        "msgData": {"formatText": format_text_data},
                    }
                else:
                    url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
                    payload = {
                        "userIdList": [chat_id],
                        "msgType": "formatText",
                        "msgData": {"formatText": format_text_data},
                    }

                response = await self._http_client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()

                if data.get("errCode") != 0:
                    return SendResult(success=False, error=data.get("errMsg"))

                msg_id = data.get("data", {}).get("msgId")
                logger.info("[Lansenger] FormatText sent to %s (group=%s, reminder=%s)",
                            chat_id, is_group, bool(reminder))
                return SendResult(success=True, message_id=msg_id, raw_response=data)
            except Exception as e:
                logger.error("[Lansenger] Send formatText error: %s", e, exc_info=True)
                return SendResult(success=False, error=str(e), retryable=True)

    async def query_groups(self, page_offset: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """Query the bot's group ID list via GET /v2/groups/fetch.

        Args:
            page_offset: Page number (default 1)
            page_size: Per-page count (max 100, default 100)

        Returns:
            Dict with totalGroupIds (int) and groupIds (list of str)
        """
        token = await self._get_app_token()
        if not token:
            return {"totalGroupIds": 0, "groupIds": []}

        try:
            url = (
                f"{self._api_gateway_url}{API_ENDPOINTS['groups']['fetch']}"
                f"?app_token={token}&page_offset={page_offset}&page_size={page_size}"
            )

            response = await self._http_client.get(url)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                logger.error("[Lansenger] Query groups error: %s", data.get("errMsg"))
                return {"totalGroupIds": 0, "groupIds": []}

            result = data.get("data", {})
            logger.info("[Lansenger] Queried groups: total=%d", result.get("totalGroupIds", 0))
            return result

        except Exception as e:
            logger.error("[Lansenger] Query groups error: %s", e)
            return {"totalGroupIds": 0, "groupIds": []}

    async def send_app_articles(
        self,
        chat_id: str,
        articles: List[Dict[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an appArticles (图文卡片) message with multiple article entries.

        Routes to /v1/messages/group/create for group chats,
        /v1/bot/messages/create for private chats.

        Each article dict must contain:
            - imgUrl (required): Image URL
            - title (required): Article title
            - url (required): Content link URL
            Optional:
            - pcUrl: PC content link URL
            - summary: Article summary
            - attach: Mini-app redirect params (ignored by other apps)

        Args:
            chat_id: Recipient user ID or chat ID
            articles: List of article dicts (1+ entries)
            metadata: Optional metadata dict
        """
        if not articles:
            return SendResult(success=False, error="No articles provided")

        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            is_group = self._chat_type_map.get(chat_id) == "group"

            if is_group:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
                payload = {
                    "groupId": chat_id,
                    "msgType": "appArticles",
                    "msgData": {"appArticles": articles},
                }
            else:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
                payload = {
                    "userIdList": [chat_id],
                    "msgType": "appArticles",
                    "msgData": {"appArticles": articles},
                }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] appArticles sent to %s, msgId=%s (group=%s)", chat_id, msg_id, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send appArticles error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_app_card(
        self,
        chat_id: str,
        head_title: str = "",
        body_title: str = "",
        body_sub_title: str = "",
        body_content: str = "",
        signature: str = "",
        fields: Optional[List[Dict[str, str]]] = None,
        links: Optional[List[Dict[str, str]]] = None,
        card_link: str = "",
        pc_card_link: str = "",
        is_dynamic: bool = False,
        head_status_info: Optional[Dict[str, str]] = None,
        staff_id: str = "",
        head_icon_url: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an appCard (应用卡片) message with rich formatting support.

        NOTE: appCard and i18nAppCard are DIFFERENT message types:
        - appCard: supports isDynamic + headStatusInfo for in-place status
          updates, but uses a SINGLE language (no i18n fields).
        - i18nAppCard: supports 5 languages (zhHans/zhHant/zhHantHK/en/fr)
          but does NOT support dynamic updates or headStatusInfo.
          Reserved for future use (send_i18n_app_card stub below).

        appCard supports div-style HTML formatting (color, font-size, text-align, text-indent).
        font-size MUST use pt unit (e.g. 14pt) — px is rejected by the enterprise API.
        adapter provides _convert_px_to_pt() helper but it is not auto-applied;
        callers must ensure pt units or call the helper explicitly.
        Dynamic cards (is_dynamic=True) can be updated later via update_dynamic_card_status().

        Args:
            chat_id: Recipient user ID or chat ID
            head_title: Card header title
            body_title: Card body title (required, max 600 bytes). Supports div style tags.
            body_sub_title: Card body subtitle (max 1200 bytes). Supports div style tags.
            body_content: Card body content (max 3000 bytes). Supports div style tags.
            signature: Card signature (max 96 bytes). Supports color style.
            fields: List of key/value dicts (max 10 pairs). Supports color style.
            links: List of title/url dicts (max 3 pairs). Title supports color/position.
            card_link: Card click-through link
            pc_card_link: PC client click-through link
            is_dynamic: Enable dynamic card status updates (for approval workflows)
            head_status_info: Dynamic card status info dict with iconLink/description/colour
            staff_id: Staff ID for showing sender avatar
            head_icon_url: Header icon URL
            metadata: Optional metadata dict
        """
        if not body_title:
            return SendResult(success=False, error="body_title is required for appCard")

        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            is_group = self._chat_type_map.get(chat_id) == "group"

            if is_group:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
            else:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"

            app_card_data: Dict[str, Any] = {
                "headTitle": self._convert_font_px_to_pt(head_title),
                "headIconUrl": head_icon_url,
                "isDynamic": is_dynamic,
                "bodyTitle": self._convert_font_px_to_pt(body_title),
                "cardLink": card_link,
                "pcCardLink": pc_card_link,
            }

            if is_dynamic and not head_status_info:
                head_status_info = {
                    "description": "<div style=\"color:rgba(0,0,0,.47)\">Active</div>",
                    "colour": "rgba(0,0,0,.47)",
                }

            if is_dynamic and head_status_info:
                app_card_data["headStatusInfo"] = head_status_info

            if body_sub_title:
                app_card_data["bodySubTitle"] = self._convert_font_px_to_pt(body_sub_title)
            if body_content:
                app_card_data["bodyContent"] = self._convert_font_px_to_pt(body_content)
            if signature:
                app_card_data["signature"] = self._convert_font_px_to_pt(signature)
            if staff_id:
                app_card_data["staffId"] = staff_id
            if fields:
                app_card_data["fields"] = fields
            if links:
                app_card_data["links"] = links

            if is_group:
                payload = {
                    "groupId": chat_id,
                    "msgType": "appCard",
                    "msgData": {"appCard": app_card_data},
                }
            else:
                payload = {
                    "userIdList": [chat_id],
                    "msgType": "appCard",
                    "msgData": {"appCard": app_card_data},
                }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response for appCard — likely a payload format issue", retryable=True)

            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] appCard sent to %s, msgId=%s, dynamic=%s, group=%s", chat_id, msg_id, is_dynamic, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send appCard error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    # ------------------------------------------------------------------
    # i18nAppCard — RESERVED for future use
    # ------------------------------------------------------------------
    # i18nAppCard supports 5 languages (zhHans/zhHant/zhHantHK/en/fr) but
    # does NOT support isDynamic or headStatusInfo.  It cannot be updated
    # in-place after sending.  Currently the approval workflow uses appCard
    # with language detection instead.  When multi-language broadcast
    # (sending the SAME card to users of different languages simultaneously)
    # becomes necessary, implement send_i18n_app_card() here.

    async def send_i18n_app_card(
        self,
        chat_id: str,
        i18n_head_title: Optional[Dict[str, str]] = None,
        head_icon_url: str = "",
        i18n_body_title: Optional[Dict[str, str]] = None,
        i18n_body_sub_title: Optional[Dict[str, str]] = None,
        i18n_body_content: Optional[Dict[str, str]] = None,
        i18n_signature: Optional[Dict[str, str]] = None,
        staff_id: str = "",
        i18n_fields: Optional[List[Dict[str, Any]]] = None,
        i18n_links: Optional[List[Dict[str, Any]]] = None,
        card_link: str = "",
        pc_card_link: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an i18nAppCard (国际化应用卡片) — RESERVED for future use.

        i18nAppCard supports 5 languages but does NOT support dynamic
        updates (isDynamic) or headStatusInfo.  For approval workflows
        that need in-place status updates, use send_app_card() instead.
        """
        raise NotImplementedError(
            "i18nAppCard is reserved for future use. "
            "For approval cards with dynamic updates, use send_app_card() "
            "with is_dynamic=True and headStatusInfo."
        )

    async def update_dynamic_card_status(
        self,
        msg_id: str,
        head_status_info: Optional[Dict[str, str]] = None,
        links: Optional[List[Dict[str, str]]] = None,
        is_last_update: bool = False,
        chat_id: Optional[str] = None,
    ) -> SendResult:
        """Update a dynamic appCard's status (e.g. approval: pending → approved/rejected).

        The card must have been sent with is_dynamic=True. Uses POST /v1/messages/dynamic/update.

        Args:
            msg_id: The message ID returned from send_app_card (when is_dynamic=True)
            head_status_info: Updated status info dict with iconLink/description/colour
            links: Updated links list (max 3 pairs)
            is_last_update: True = final status update, card becomes static after this
            chat_id: Optional chat_id for private message updates (user_token needed)
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            # Build URL with optional user_token for private chat updates
            url_params = f"app_token={token}"
            # user_token needed for private chat dynamic updates
            # For bot messages in groups, user_token is not needed
            url = f"{self._api_gateway_url}{API_ENDPOINTS['message']['dynamic_update']}?{url_params}"

            app_card_update: Dict[str, Any] = {
                "isLastUpdate": is_last_update,
            }
            if head_status_info:
                app_card_update["headStatusInfo"] = head_status_info
            if links:
                app_card_update["links"] = links

            payload = {
                "msgId": msg_id,
                "msgType": "appCard",
                "msgData": {
                    "appCardUpdateMsg": app_card_update,
                },
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            logger.info("[Lansenger] Dynamic card %s updated, isLast=%s", msg_id, is_last_update)
            return SendResult(success=True, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Update dynamic card error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_text_with_media(self, chat_id: str, content: str, media_type: int, media_ids: List[str], reminder: dict = None) -> SendResult:
        """Send a text message with media attachment (file/image/video), optionally with @mentions.
        
        Args:
            chat_id: Recipient user ID or chat ID
            content: Text content (caption)
            media_type: 1=video, 2=image, 3=file
            media_ids: List of media IDs from upload_media_file()
            reminder: Optional dict with 'all' (bool) and 'userIds' (list) for @mentions.
                      Only works in group/staff chat; private chat does not support @mentions.
            
        Note: Uses msgType='text' (not formatText) because formatText doesn't support media.
              Markdown is NOT supported when sending media.
              For video (mediaType=1), the API requires 2 mediaIds: [videoId, coverImageId].
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        try:
            is_group = self._chat_type_map.get(chat_id) == "group"

            if is_group:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
            else:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"

            text_data = {
                "content": content,
                "mediaType": media_type,
                "mediaIds": media_ids
            }
            if reminder:
                text_data["reminder"] = reminder

            if is_group:
                payload = {
                    "groupId": chat_id,
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }
            else:
                payload = {
                    "userIdList": [chat_id],
                    "msgType": "text",
                    "msgData": {"text": text_data},
                }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Text+media message sent to %s (group=%s)", chat_id, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Send text+media error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def upload_media_file(self, file_path: str, media_type: int) -> Optional[str]:
        """Upload a media file to Lansenger and return mediaId.
        
        Args:
            file_path: Path to the local file
            media_type: 1=video, 2=image, 3=file
            
        Returns:
            mediaId string on success, None on failure
            
        Note: File size limits are determined by the organization's Lansenger configuration.
        """
        token = await self._get_app_token()
        if not token:
            logger.error("[Lansenger] No access token for media upload")
            return None
        
        try:
            url = f"{self._api_gateway_url}/v1/medias/create?type={media_type}&app_token={token}"
            
            # Read file and prepare multipart form data
            with open(file_path, 'rb') as f:
                file_content = f.read()
            
            # Get filename from path
            filename = os.path.basename(file_path)
            
            # Build multipart form data manually for httpx
            files = {'media': (filename, file_content)}
            
            response = await self._http_client.post(url, files=files)
            response.raise_for_status()
            data = response.json()
            
            if data.get("errCode") != 0:
                logger.error("[Lansenger] Upload media error: %s", data.get("errMsg"))
                return None
            
            media_id = data.get("data", {}).get("mediaId")
            logger.info("[Lansenger] Media uploaded: %s (%s)", filename, media_id)
            return media_id
        except Exception as e:
            logger.error("[Lansenger] Upload media error: %s", e)
            return None

    async def send_file(self, chat_id: str, file_path: str, caption: str = "", media_type: int = 3) -> SendResult:
        """Send a file/image/video message.
        
        Args:
            chat_id: Recipient user ID
            file_path: Path to the local file
            caption: Optional caption text (plain text, Markdown NOT supported with media)
            media_type: 1=video, 2=image, 3=file (default: 3)
            
        Returns:
            SendResult with success status
            
        Note: Uses msgType='text' which doesn't support Markdown. For Markdown, send separately.
        """
        # Graceful degradation: skip non-existent files instead of crashing
        # (base's extract_local_files can misidentify placeholder paths)
        if not os.path.isfile(file_path):
            logger.warning("[Lansenger] File not found: %s — skipping", file_path)
            return SendResult(success=False, error=f"File not found: {file_path}")

        # Upload file first
        media_id = await self.upload_media_file(file_path, media_type)
        if not media_id:
            return SendResult(success=False, error="Failed to upload file")
        
        # Send message with media attachment (uses msgType='text', not formatText)
        return await self.send_text_with_media(chat_id, caption, media_type=media_type, media_ids=[media_id])

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send a local image file.
        
        Args:
            chat_id: Recipient user ID
            image_path: Path to the local image file
            caption: Optional caption text (plain text, Markdown NOT supported with media)
            
        Returns:
            SendResult with success status
            
        Note: Uses media_type=2 (image) for upload and sending.
        """
        return await self.send_file(chat_id, image_path, caption or "", media_type=2)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send an image by URL.
        
        Note: Downloads image first, then uploads to Lansenger.
        """
        import tempfile
        import httpx
        
        try:
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.get(image_url, timeout=30)
                except httpx.ConnectError as e:
                    return SendResult(success=False, error=f"Image URL unreachable: {image_url} — network error: {e}")
                except httpx.TimeoutException:
                    return SendResult(success=False, error=f"Image URL timed out: {image_url}")
                
                if resp.status_code == 404:
                    return SendResult(success=False, error=f"Image URL not found (404): {image_url}")
                if resp.status_code >= 400:
                    return SendResult(success=False, error=f"Image URL returned HTTP {resp.status_code}: {image_url}")
                
                content_type = resp.headers.get("content-type", "")
                if content_type and not content_type.startswith("image/"):
                    return SendResult(success=False, error=f"URL returned non-image content ({content_type}): {image_url}")
                
                image_bytes = resp.content
            
            fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='lansenger_image_')
            os.write(fd, image_bytes)
            os.close(fd)
            
            media_type = 2
            
            return await self.send_file(chat_id, temp_path, caption or "", media_type=media_type)
        except Exception as e:
            logger.error("[Lansenger] Send image error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send a document file.
        
        Note: Falls back to send_file with media_type=3 (file).
        """
        return await self.send_file(chat_id, file_path, caption or "", media_type=3)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None, **kwargs) -> SendResult:
        """Send a video file natively via the platform API.

        Uses send_file with media_type=1 (video).
        """
        return await self.send_file(chat_id, video_path, caption or "", media_type=1)

    async def send_voice(self, chat_id: str, audio_path: str, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        """Send a voice/audio file.

        Uses send_file with media_type=3 (file) as voice messages
        need a specific format not guaranteed here.
        """
        return await self.send_file(chat_id, audio_path, "", media_type=3)

    async def revoke_message(
        self, 
        message_ids: List[str], 
        chat_type: str = "bot",
        sender_id: Optional[str] = None,
    ) -> SendResult:
        """Revoke previously sent messages.

        For a personal bot plugin, only 'bot' and 'group' chat types are relevant.
        - bot: bot-to-user private chat messages. sender_id not required.
        - group: group chat messages. sender_id is REQUIRED per API spec.

        Note: Lansenger displays a fixed system message after revocation.
              Custom sysMsg content/icon is NOT supported by the current API.
        """
        if chat_type not in ("bot", "group"):
            return SendResult(success=False, error=f"chat_type must be 'bot' or 'group', got '{chat_type}'")
        if chat_type == "group" and not sender_id:
            return SendResult(success=False, error="chat_type='group' requires sender_id")

        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="Failed to get token")
        
        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['message']['revoke']}?app_token={token}"
            payload = {"chatType": chat_type, "messageIds": message_ids}
            if sender_id:
                payload["senderId"] = sender_id
            
            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            
            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg", "Unknown error"))
            
            logger.info("[Lansenger] Message(s) revoked: %s (chatType=%s)", message_ids, chat_type)
            return SendResult(success=True, message_id=None, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Revoke error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_link_card(
        self,
        chat_id: str,
        title: str,
        link: str,
        description: Optional[str] = None,
        icon_link: Optional[str] = None,
        pc_link: Optional[str] = None,
        from_name: Optional[str] = None,
        from_icon_link: Optional[str] = None,
    ) -> SendResult:
        """Send a linkCard card message.

        Routes to /v1/messages/group/create for group chats,
        /v1/bot/messages/create for private chats.

        Args:
            chat_id: Recipient user ID
            title: Card title
            link: Card click-through link
            description: Card description
            icon_link: Card icon image link
            pc_link: PC-side redirect link
            from_name: Source name
            from_icon_link: Source icon image link
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="Failed to get token")

        try:
            is_group = self._chat_type_map.get(chat_id) == "group"

            if is_group:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['group_message']}?app_token={token}"
            else:
                url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"

            link_card_data = {
                "title": title,
                "link": link,
                "description": description or "",
                "iconLink": icon_link or "",
                "pcLink": pc_link or "",
                "fromName": from_name or "",
                "fromIconLink": from_icon_link or "",
            }

            if is_group:
                payload = {
                    "groupId": chat_id,
                    "msgType": "linkCard",
                    "msgData": {"linkCard": link_card_data},
                }
            else:
                payload = {
                    "userIdList": [chat_id],
                    "msgType": "linkCard",
                    "msgData": {"linkCard": link_card_data},
                }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", retryable=True)

            data = response.json()

            if data.get("errCode") != 0:
                logger.error("[Lansenger] LinkCard API error: errCode=%s, errMsg=%s",
                             data.get("errCode"), data.get("errMsg"))
                return SendResult(success=False, error=data.get("errMsg", "Unknown error"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] LinkCard sent to %s, msgId=%s (group=%s)", chat_id, msg_id, is_group)
            return SendResult(success=True, message_id=msg_id, raw_response=data)
        except Exception as e:
            logger.error("[Lansenger] Send linkCard error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_to_owner(self, content: str, format: str = "text") -> SendResult:
        """Send a text message to the bot owner (or home_channel if owner not set).
        
        Args:
            content: Message content
            format: 'text' for plain text, 'formatText' for Markdown
        """
        # Use home_channel as fallback if owner_id not set
        target_id = self._owner_id or self._home_channel_id
        if not target_id:
            return SendResult(success=False, error="Owner ID and home_channel not set")
        if format == "formatText":
            return await self.send_format_text(target_id, content)
        return await self.send_text(target_id, content)

    async def send_exec_approval(
        self, chat_id: str, command: str, session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a dynamic appCard approval card with isDynamic=True.

        NOTE: This uses appCard (not i18nAppCard).  appCard supports
        isDynamic + headStatusInfo for in-place status updates, but does
        NOT support multi-language (i18n).  i18nAppCard supports 5
        languages but cannot be dynamically updated and has no
        headStatusInfo — it is reserved for future use.

        Uses the user's cached language preference (from inbound messages)
        to select card content language.  Default: Chinese.

        After the user replies /approve, /approve session, /approve always,
        or /deny, the gateway intercepts those text replies and calls
        update_approval_status(), which uses the dynamic update API to
        change the card status in-place (待审批 → 已批准/已拒绝).

        Args:
            chat_id: Recipient user ID
            command: The command to approve
            session_key: Session identifier for approval resolution
            description: Reason for approval request
            metadata: Optional metadata (not used currently)

        Returns:
            SendResult with message_id for later status updates
        """
        logger.info("[Lansenger] send_exec_approval called for chat_id=%s", chat_id)
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        lang = self._get_lang(chat_id)
        cmd_preview = command[:300] + "..." if len(command) > 300 else command

        # --- Build appCard content in the user's language ---
        if lang == "zh":
            head_title = "⚠️ 危险命令审批"
            body_title = f"确认 {cmd_preview[:20]}"
            body_sub_title = description
            body_content = f"会话 ID: {session_key[:32]}\n命令:\n{cmd_preview}"
            status_desc = "待审批"
            signature = self._get_agent_signature("zh")
            fields = [
                {"key": "执行一次", "value": "/approve"},
                {"key": "本会话有效", "value": "/approve session"},
                {"key": "永久允许", "value": "/approve always"},
                {"key": "拒绝执行", "value": "/deny"},
            ]
        else:
            head_title = "⚠️ Command Approval"
            body_title = "Dangerous Command Approval Request"
            body_sub_title = description
            body_content = f"Session ID: {session_key[:32]}\nCommand:\n{cmd_preview}"
            status_desc = "Pending"
            signature = self._get_agent_signature("en")
            fields = [
                {"key": "Execute Once", "value": "/approve"},
                {"key": "This Session", "value": "/approve session"},
                {"key": "Always Allow", "value": "/approve always"},
                {"key": "Deny", "value": "/deny"},
            ]

        # Escape HTML in dynamic content to prevent accidental div parsing
        body_content = self._escape_html(body_content)
        body_sub_title = self._escape_html(body_sub_title)

        # Dynamic card: head status info shows "待审批" (amber)
        head_status_info = {
            "description": self._build_status_div(status_desc, "#FFB116"),
            "colour": "#FFB116",
        }

        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
            app_card_data = {
                "headTitle": head_title,
                "headIconUrl": "",
                "isDynamic": True,
                "headStatusInfo": head_status_info,
                "bodyTitle": f'<div style="color:#000;font-size:15pt;text-align:left">{body_title}</div>',
                "bodySubTitle": f'<div style="color:rgba(0,0,0,.47);font-size:13pt;text-align:left">{body_sub_title}</div>',
                "bodyContent": f'<div style="color:#000;font-size:13pt;text-align:left;text-indent:0em">{body_content}</div>',
                "signature": f'<div style="color:rgba(0,0,0,.47)">{signature}</div>',
                "fields": fields,
                "cardLink": "",
                "pcCardLink": "",
            }

            payload = {
                "userIdList": [chat_id],
                "msgType": "appCard",
                "msgData": {"appCard": app_card_data},
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", retryable=True)

            data = response.json()
            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] appCard approval sent to %s, msgId=%s, lang=%s", chat_id, msg_id, lang)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send appCard approval error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    # ------------------------------------------------------------------
    # Slash-command confirmation (gateway destructive_slash_confirm gate)
    # ------------------------------------------------------------------
    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a dynamic appCard slash-command confirmation card.

        NOTE: This uses appCard (not i18nAppCard).  See send_exec_approval
        docstring for the appCard vs i18nAppCard distinction.

        Uses the user's cached language preference to select content language.

        Used by the gateway's ``_maybe_confirm_destructive_slash`` gate for
        /new, /reset, /undo.  Lansenger does not support inline button
        callbacks like Telegram, so this card displays the confirmation
        request with fields showing the text-based reply options
        (/approve, /always, /cancel).

        The gateway's text intercept recognises /approve, /always, /cancel
        and routes them through ``slash_confirm.resolve()``.

        Returns SendResult(success=True) so the gateway skips the
        redundant text fallback.
        """
        logger.info("[Lansenger] send_slash_confirm: chat_id=%s, title=%s, confirm_id=%s", chat_id, title, confirm_id)
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        lang = self._get_lang(chat_id)
        command_name = title.strip() if title else "unknown"

        if lang == "zh":
            head_title = f"🔄 {command_name} 确认"
            body_title = "会话操作确认请求"
            body_content = self._escape_html(message or "此操作将修改当前会话。")
            status_desc = "待确认"
            signature = self._get_agent_signature("zh")
            fields = [
                {"key": "确认执行", "value": "/approve"},
                {"key": "本会话免确认", "value": "/always"},
                {"key": "取消", "value": "/cancel"},
            ]
        else:
            head_title = f"🔄 {command_name} Confirm"
            body_title = "Session Action Confirmation"
            body_content = self._escape_html(message or "This action will modify your current session.")
            status_desc = "Pending"
            signature = self._get_agent_signature("en")
            fields = [
                {"key": "Approve Once", "value": "/approve"},
                {"key": "Always This Session", "value": "/always"},
                {"key": "Cancel", "value": "/cancel"},
            ]

        head_status_info = {
            "description": self._build_status_div(status_desc, "#FFB116"),
            "colour": "#FFB116",
        }

        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
            app_card_data = {
                "headTitle": head_title,
                "headIconUrl": "",
                "isDynamic": True,
                "headStatusInfo": head_status_info,
                "bodyTitle": f'<div style="color:#000;font-size:15pt;text-align:left">{body_title}</div>',
                "bodyContent": f'<div style="color:#000;font-size:13pt;text-align:left;text-indent:0em">{body_content}</div>',
                "signature": f'<div style="color:rgba(0,0,0,.47)">{signature}</div>',
                "fields": fields,
                "cardLink": "",
                "pcCardLink": "",
            }

            payload = {
                "userIdList": [chat_id],
                "msgType": "appCard",
                "msgData": {"appCard": app_card_data},
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", retryable=True)

            data = response.json()
            if data.get("errCode") != 0:
                logger.error("[Lansenger] Slash confirm card API error: errCode=%s, errMsg=%s", data.get("errCode"), data.get("errMsg"))
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Slash confirm appCard sent to %s, msgId=%s", chat_id, msg_id)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send slash confirm appCard error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def update_approval_status(
        self, chat_id: str, message_id: str,
        status: str, user_name: str = ""
    ) -> SendResult:
        """Update a dynamic appCard message status in-place.

        NOTE: This uses the DynamicMsg appCard update API (appCardUpdateMsg),
        not i18nAppCard.  i18nAppCard does not support dynamic updates.

        Uses the user's cached language preference for status text.

        Args:
            chat_id: Recipient user ID (used to determine language)
            message_id: The message ID of the original appCard to update
            status: One of 'pending', 'approved', 'denied'
            user_name: Name of user who made the decision (shown in status)

        Returns:
            SendResult with success status
        """
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        lang = self._get_lang(chat_id)

        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['message']['dynamic_update']}?app_token={token}"

            # Status text and color based on language
            status_map = {
                "pending":  {"zh": "待审批",       "en": "Pending",   "color": "#FFB116"},
                "approved": {"zh": f"已批准 · {user_name}" if user_name else "已批准",
                             "en": f"Approved · {user_name}" if user_name else "Approved",
                             "color": "#198754"},
                "denied":   {"zh": f"已拒绝 · {user_name}" if user_name else "已拒绝",
                             "en": f"Denied · {user_name}" if user_name else "Denied",
                             "color": "#dc3545"},
            }

            cfg = status_map.get(status, status_map["pending"])
            text = cfg.get(lang, cfg["en"])

            # appCardUpdateMsg — dynamic update payload
            payload = {
                "msgId": message_id,
                "msgType": "appCard",
                "msgData": {
                    "appCardUpdateMsg": {
                        "isLastUpdate": (status != "pending"),  # Final state locks the card
                        "headStatusInfo": {
                            "description": self._build_status_div(text, cfg["color"]),
                            "colour": cfg["color"],
                        },
                    }
                }
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if data.get("errCode") != 0:
                return SendResult(success=False, error=data.get("errMsg"))

            logger.info("[Lansenger] appCard status updated to %s (lang=%s)", status, lang)
            return SendResult(success=True, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Update appCard status error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    # ------------------------------------------------------------------
    # Update prompt (gateway /update watcher)
    # ------------------------------------------------------------------
    async def send_update_prompt(
        self,
        chat_id: str,
        prompt: str,
        default: str = "",
        session_key: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a dynamic appCard update prompt with /approve /deny reply hints.

        NOTE: This uses appCard (not i18nAppCard).  See send_exec_approval
        docstring for the appCard vs i18nAppCard distinction.

        Uses the user's cached language preference to select content language.

        Used by the gateway's ``/update`` watcher when ``hermes update --gateway``
        needs user input (stash restore, config migration).  Lansenger does not
        support inline button callbacks like Telegram/Discord, so this card
        displays the prompt text with fields showing the text-based reply
        options (/approve → yes, /deny → no).

        The gateway's text intercept recognises /approve, /yes → "y" and
        /deny, /no → "n" and routes them through ``update_prompt.resolve()``.

        Returns SendResult(success=True) so the gateway skips the
        redundant text fallback.
        """
        logger.info(
            "[Lansenger] send_update_prompt: chat_id=%s, prompt=%s, default=%s",
            chat_id, prompt[:80], default,
        )
        token = await self._get_app_token()
        if not token:
            return SendResult(success=False, error="No access token")

        lang = self._get_lang(chat_id)
        prompt_text = prompt or "Update needs your input."
        default_hint = f" (default: {default})" if default else ""
        escaped_prompt = self._escape_html(prompt_text)

        if lang == "zh":
            head_title = "⚕ 更新确认"
            body_title = "更新需要您的输入"
            body_content = f"{escaped_prompt}{default_hint}"
            status_desc = "待确认"
            signature = self._get_agent_signature("zh")
            fields = [
                {"key": "确认执行", "value": "/approve"},
                {"key": "拒绝执行", "value": "/deny"},
            ]
        else:
            head_title = "⚕ Update Confirmation"
            body_title = "Update Needs Your Input"
            body_content = f"{escaped_prompt}{default_hint}"
            status_desc = "Pending"
            signature = self._get_agent_signature("en")
            fields = [
                {"key": "Approve (Yes)", "value": "/approve"},
                {"key": "Deny (No)", "value": "/deny"},
            ]

        head_status_info = {
            "description": self._build_status_div(status_desc, "#FFB116"),
            "colour": "#FFB116",
        }

        try:
            url = f"{self._api_gateway_url}{API_ENDPOINTS['smart_bot']['private_message']}?app_token={token}"
            app_card_data = {
                "headTitle": head_title,
                "headIconUrl": "",
                "isDynamic": True,
                "headStatusInfo": head_status_info,
                "bodyTitle": f'<div style="color:#000;font-size:15pt;text-align:left">{body_title}</div>',
"bodyContent": f'<div style="color:#000;font-size:13pt;text-align:left;text-indent:0em">{body_content}</div>',
            "signature": f'<div style="color:rgba(0,0,0,.47)">{signature}</div>',
            "fields": fields,
            "cardLink": "",
            "pcCardLink": "",
        }

            payload = {
                "userIdList": [chat_id],
                "msgType": "appCard",
                "msgData": {"appCard": app_card_data},
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            if not response.text or len(response.text.strip()) == 0:
                return SendResult(success=False, error="Empty API response", retryable=True)

            data = response.json()
            if data.get("errCode") != 0:
                logger.error(
                    "[Lansenger] Update prompt appCard API error: errCode=%s, errMsg=%s",
                    data.get("errCode"), data.get("errMsg"),
                )
                return SendResult(success=False, error=data.get("errMsg"))

            msg_id = data.get("data", {}).get("msgId")
            logger.info("[Lansenger] Update prompt appCard sent to %s, msgId=%s", chat_id, msg_id)
            return SendResult(success=True, message_id=msg_id, raw_response=data)

        except Exception as e:
            logger.error("[Lansenger] Send update prompt appCard error: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    def _build_agent_signature_i18n(self) -> Dict[str, str]:
        """Build the i18nSignature with the agent name from SOUL.md (dynamic).

        RESERVED for future i18nAppCard use.  Currently not called by
        any active flow — the approval workflow uses appCard with
        language detection instead.

        Falls back to "Hermes" if SOUL.md cannot be read.  The signature
        format is "{agent_name} 安全系统" / "{agent_name} Security System" etc.
        """
        agent_name = self._read_agent_name_from_soul()

        return self._build_i18n_obj_full(
            f"{agent_name} 安全系统",
            f"{agent_name} 安全系統",
            f"{agent_name} 安全系統",
            f"{agent_name} Security",
            f"{agent_name} Sécurité"
        )

    def _read_agent_name_from_soul(self) -> str:
        """Read the agent display name from SOUL.md.

        Looks for the **Name:** field in the YAML frontmatter or markdown
        body of ~/.hermes/SOUL.md.  Returns "Hermes" as fallback.
        """
        try:
            soul_path = Path.home() / ".hermes" / "SOUL.md"
            if not soul_path.exists():
                return "Hermes"

            content = soul_path.read_text(encoding="utf-8")

            # Try YAML frontmatter first (--- ... ---)
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    frontmatter = content[3:end]
                    # Look for "Name:" in frontmatter
                    for line in frontmatter.split("\n"):
                        line = line.strip()
                        if line.startswith("Name:") or line.startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                            if name:
                                return name

            # Try markdown body — look for **Name:** pattern
            import re
            match = re.search(r"\*?\*?Name:?\*?\*?\s*:?\s*(.+)", content, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Strip markdown bold/italic markers
                name = name.replace("*", "").strip()
                if name:
                    return name

            return "Hermes"
        except Exception:
            return "Hermes"

    def _build_i18n_obj_full(self, zh_hans: str, zh_hant: str, zh_hant_hk: str, en: str, fr: str) -> Dict[str, str]:
        """Build i18n object with all 5 supported languages.

        RESERVED for future i18nAppCard use.  Currently not called by
        any active flow — the approval workflow uses appCard with
        language detection (single-language per card) instead.
        
        Args:
            zh_hans: Simplified Chinese text
            zh_hant: Traditional Chinese text
            zh_hant_hk: Traditional Chinese (Hong Kong) text
            en: English text
            fr: French text
            
        Returns:
            Dict with language codes as keys
        """
        return {
            "zhHans": zh_hans,
            "zhHant": zh_hant,
            "zhHantHK": zh_hant_hk,
            "en": en,
            "fr": fr
        }
    
    def _escape_html(self, text: str) -> str:
        """Escape < and > to prevent HTML tag parsing.
        
        Client doesn't support HTML entities like &quot; or &amp;,
        but we need to escape < and > to prevent them from being
        parsed as HTML tags.
        """
        return text.replace("<", "&lt;").replace(">", "&gt;")

    def _convert_font_px_to_pt(self, text: str) -> str:
        """Convert font-size px values to pt in div-style HTML strings.

        Lansenger enterprise deployment rejects font-size with px unit.
        1px ≈ 0.75pt. Common sizes: 14px→10.5pt, 16px→12pt, 18px→13.5pt.
        Only converts numeric px values; pt values are left unchanged.
        """
        import re
        def _px_to_pt(m):
            px_val = float(m.group(1))
            pt_val = px_val * 0.75
            if pt_val == int(pt_val):
                return f"font-size:{int(pt_val)}pt"
            return f"font-size:{pt_val}pt"
        return re.sub(r'font-size:(\d+(?:\.\d+)?)px', _px_to_pt, text)

    def _detect_lang(self, text: str) -> str:
        """Detect language from user message text. Returns 'zh' or 'en'.

        Simple heuristic: if text contains any CJK Unicode range characters,
        treat as Chinese. Otherwise, English.
        """
        import unicodedata
        for ch in text:
            try:
                if unicodedata.category(ch).startswith("Lo"):  # Letter, Other = CJK
                    return "zh"
            except Exception:
                pass
        # Also check for common CJK ranges directly
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or \
               (0xF900 <= cp <= 0xFAFF) or (0x3000 <= cp <= 0x303F):
                return "zh"
        return "en"

    def _get_lang(self, chat_id: str) -> str:
        """Get cached user language for chat_id, defaulting to 'zh'."""
        return self._user_lang_map.get(chat_id, "zh")

    def _get_agent_signature(self, lang: str = "zh") -> str:
        """Build agent signature string in the given language.

        Reads agent name from SOUL.md and formats it for appCard signature field.
        """
        agent_name = self._read_agent_name_from_soul()
        if lang == "zh":
            return f"{agent_name} 安全系统"
        elif lang == "fr":
            return f"{agent_name} Sécurité"
        else:
            return f"{agent_name} Security"

    def _build_status_div(self, text: str, color: str) -> str:
        """Build div-style text for headStatusInfo.description.

        headStatusInfo = dot + text. 'description' is the text portion,
        supports single div-style color tag (must be <30 bytes).
        'colour' controls the dot color — they are independent.
        No nested divs — API rejects nested div structure.
        """
        return f'<div style="color:{color}">{text}</div>'

    @property
    def owner_id(self) -> Optional[str]:
        """Get the bot owner's user ID."""
        return self._owner_id

    # -- Helper methods -----------------------------------------------------

    def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get chat info (stub - Lansenger API doesn't provide this)."""
        return {"name": chat_id, "type": "unknown", "chat_id": chat_id}


def check_requirements() -> bool:
    """Check if Lansenger dependencies are available."""
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("[Lansenger] websockets not installed. Run: pip install websockets")
        return False
    if not HTTPX_AVAILABLE:
        logger.warning("[Lansenger] httpx not installed. Run: pip install httpx")
        return False
    return True


def validate_config(config) -> bool:
    """Check if Lansenger is properly configured (env vars or config.yaml extra)."""
    extra = getattr(config, "extra", None) or {}
    app_id = extra.get("app_id") or os.getenv("LANSENGER_APP_ID", "")
    app_secret = extra.get("app_secret") or os.getenv("LANSENGER_APP_SECRET", "")
    return bool(app_id and app_secret)


def is_connected(config) -> bool:
    """Check if Lansenger appears to be connected/enabled."""
    return bool(config and getattr(config, "enabled", False))


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars (for env-only setups).

    Called during _apply_env_overrides BEFORE the adapter is constructed,
    so ``hermes gateway status`` can reflect env-only configuration.
    """
    app_id = os.getenv("LANSENGER_APP_ID")
    app_secret = os.getenv("LANSENGER_APP_SECRET")
    if not app_id or not app_secret:
        return None

    extra = {
        "app_id": app_id,
        "app_secret": app_secret,
    }
    api_url = os.getenv("LANSENGER_API_GATEWAY_URL")
    if api_url:
        extra["api_gateway_url"] = api_url

    home_channel = os.getenv("LANSENGER_HOME_CHANNEL")
    if home_channel:
        return {"extra": extra, "home_channel": {"chat_id": home_channel}}
    return {"extra": extra}


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None,
                           media_files=None, force_document=False) -> dict:
    """Out-of-process delivery for cron jobs when the gateway is not running.

    Creates an ephemeral adapter, sends the message, and tears down.
    """
    if not check_requirements() or not validate_config(pconfig):
        return {"error": "Lansenger dependencies or config not available"}

    _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}

    media_files = media_files or []

    try:
        adapter = LansengerAdapter(pconfig)
        # Connect just enough to send (get token + HTTP client)
        adapter._http_client = httpx.AsyncClient(timeout=30.0)

        last_result = None
        # Send caption text as formatText (Markdown)
        if message.strip():
            last_result = await adapter.send(chat_id=chat_id, content=message)
            if not last_result.success:
                await adapter._http_client.aclose()
                return {"error": f"Lansenger send failed: {last_result.error}"}

        # Send each media file
        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                await adapter._http_client.aclose()
                return {"error": f"Media file not found: {media_path}"}

            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS:
                last_result = await adapter.send_file(chat_id, media_path, caption="", media_type=2)
            elif ext in _VIDEO_EXTS:
                last_result = await adapter.send_file(chat_id, media_path, caption="", media_type=1)
            else:
                last_result = await adapter.send_file(chat_id, media_path, caption="", media_type=3)

            if not last_result.success:
                await adapter._http_client.aclose()
                return {"error": f"Lansenger media send failed: {last_result.error}"}

        await adapter._http_client.aclose()

        if last_result is None:
            return {"error": "No deliverable text or media"}

        return {
            "success": True,
            "platform": "lansenger",
            "chat_id": chat_id,
            "message_id": last_result.message_id,
        }
    except Exception as e:
        return {"error": f"Lansenger standalone send failed: {e}"}


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="lansenger",
        label="Lansenger (蓝信)",
        adapter_factory=lambda cfg: LansengerAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["LANSENGER_APP_ID", "LANSENGER_APP_SECRET"],
        install_hint="pip install websockets httpx",
        setup_fn=_interactive_setup,
        # Env-driven auto-configuration
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support
        cron_deliver_env_var="LANSENGER_HOME_CHANNEL",
        # Out-of-process cron delivery
        standalone_sender_fn=_standalone_send,
        # Auth env vars
        allowed_users_env="LANSENGER_ALLOWED_USERS",
        allow_all_env="LANSENGER_ALLOW_ALL_USERS",
        # Message limit
        max_message_length=4000,
        # Display
        emoji="💠",
        # Lansenger uses opaque user IDs, not phone numbers
        pii_safe=True,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via Lansenger (蓝信), an enterprise messaging platform. "
            "You can send Markdown-formatted text using the "
            "formatText msgType, and send files/images/videos via send_file().  "
            "Messages have a ~4000 character limit.  Dynamic appCard is used for "
            "approval workflows (status updates in-place).  Keep responses concise and professional."
        ),
    )


def _interactive_setup():
    """Interactive setup wizard for Lansenger credentials.
    
    Called by `hermes setup gateway` when the user selects Lansenger.
    Prompts for APP_ID, APP_SECRET, and optional API_GATEWAY_URL,
    then writes them to ~/.hermes/.env (idempotent — won't duplicate).
    """
    from pathlib import Path
    
    hermes_home = Path.home() / ".hermes"
    env_file = hermes_home / ".env"
    
    # ANSI colors for terminal output
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    
    print()
    print(f"  {CYAN}─── 💠 Lansenger (蓝信) Setup ───{RESET}")
    print()
    print(f"  {YELLOW}Where to find your credentials:{RESET}")
    print(f"  Lansenger desktop → Contacts → Smart Bot → Personal Bot → ℹ️ icon")
    print(f"  (Mobile client does not support viewing credentials)")
    print()
    
    # Read existing .env
    existing_lines = []
    existing_values = {}
    if env_file.exists():
        with open(env_file) as f:
            existing_lines = f.readlines()
        for line in existing_lines:
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                existing_values[key.strip()] = value.strip()
    
    def _prompt_field(env_key: str, label: str, default: str = "", sensitive: bool = False) -> str | None:
        """Prompt for a single env var. Returns new value or None if unchanged."""
        current = existing_values.get(env_key, "")
        if current:
            # Mask sensitive values for display
            display = current if not sensitive else (current[:4] + "****" if len(current) > 4 else "****")
            print(f"  {DIM}Current {label}: {display}{RESET}")
            print(f"  {BOLD}New {label}{RESET} [press Enter to keep current]: ", end="", flush=True)
            new_value = input().strip()
            if not new_value:
                print(f"  {GREEN}✓ Keeping current value{RESET}")
                return None  # unchanged
            return new_value
        else:
            print(f"  {BOLD}{label}:{RESET} ", end="", flush=True)
            new_value = input().strip()
            if not new_value:
                if default:
                    return default
                print(f"  {YELLOW}Skipped — you can set it later in ~/.hermes/.env{RESET}")
                return None
            return new_value
    
    # Prompt for credentials
    app_id = _prompt_field("LANSENGER_APP_ID", "App ID")
    app_secret = _prompt_field("LANSENGER_APP_SECRET", "App Secret", sensitive=True)
    gateway_url = _prompt_field("LANSENGER_API_GATEWAY_URL", "API Gateway URL", default="https://open.e.lanxin.cn/open/apigw")
    
    # Build updated .env content — replace existing keys or append new ones
    changes = {}
    if app_id is not None:
        changes["LANSENGER_APP_ID"] = app_id
    if app_secret is not None:
        changes["LANSENGER_APP_SECRET"] = app_secret
    if gateway_url is not None:
        changes["LANSENGER_API_GATEWAY_URL"] = gateway_url
    
    if changes:
        # Rewrite .env: replace changed keys, keep others intact
        output_lines = []
        keys_replaced = set()
        for line in existing_lines:
            if "=" in line and not line.startswith("#"):
                key = line.split("=")[0].strip()
                if key in changes:
                    output_lines.append(f"{key}={changes[key]}\n")
                    keys_replaced.add(key)
                else:
                    output_lines.append(line)
            else:
                output_lines.append(line)
        
        # Append new keys that weren't in the file before
        for key, value in changes.items():
            if key not in keys_replaced:
                output_lines.append(f"{key}={value}\n")
        
        with open(env_file, "w") as f:
            f.writelines(output_lines)
        
        print()
        print(f"  {GREEN}✓ Credentials saved to ~/.hermes/.env{RESET}")
        print(f"  {GREEN}✓ Run 'hermes gateway restart' to activate{RESET}")
    else:
        print()
        print(f"  {YELLOW}No changes to save.{RESET}")
    
    print()