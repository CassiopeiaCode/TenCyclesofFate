import logging
import copy
import gzip
import json
import asyncio
import jsonpatch
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# Debounce 延迟（秒）
DEBOUNCE_DELAY = 0.2


class ConnectionManager:
    def __init__(self):
        # Maps player_id to connection info
        self.active_connections: dict[str, dict] = {}
        # Pending state updates (for debouncing)
        self._pending_updates: dict[str, dict] = {}
        # Debounce tasks
        self._debounce_tasks: dict[str, asyncio.Task] = {}

    async def connect(self, websocket: WebSocket, player_id: str):
        """Accepts a new WebSocket connection and stores it."""
        await websocket.accept()
        self.active_connections[player_id] = {
            "websocket": websocket,
            "last_sent_state": None,
        }
        logger.info(f"Player '{player_id}' connected via WebSocket.")

    def disconnect(self, player_id: str):
        """Removes a player's WebSocket connection."""
        if player_id in self.active_connections:
            del self.active_connections[player_id]
            # Cancel pending debounce task
            if player_id in self._debounce_tasks:
                self._debounce_tasks[player_id].cancel()
                del self._debounce_tasks[player_id]
            self._pending_updates.pop(player_id, None)
            logger.info(f"Player '{player_id}' disconnected from WebSocket.")

    def _prepare_player_payload(self, data: dict) -> dict:
        """Prepare payload for the actual player (remove internal_history)."""
        payload = copy.deepcopy(data)
        if payload.get("data"):
            payload["data"].pop("internal_history", None)
        return payload

    def _prepare_live_payload(self, data: dict) -> dict:
        """Prepare payload for live viewers (stripped-down and secure)."""
        original_session = data.get("data", {})
        
        live_payload = {
            "type": "live_update",
            "data": {
                "display_history": copy.deepcopy(original_session.get("display_history", [])),
                "current_life": copy.deepcopy(original_session.get("current_life"))
            }
        }

        if live_payload["data"]["display_history"]:
            live_payload["data"]["display_history"] = [
                msg for msg in live_payload["data"]["display_history"] 
                if not msg.strip().startswith("> ")
            ]

        if original_session.get("redemption_code"):
            full_code = original_session["redemption_code"]
            masked_code = f"{full_code[:1]}...{full_code[-1:]}"
            
            if live_payload["data"]["display_history"]:
                try:
                    for i, message in enumerate(live_payload["data"]["display_history"]):
                        if isinstance(message, str) and full_code in message:
                            live_payload["data"]["display_history"][i] = message.replace(full_code, masked_code)
                except (TypeError, AttributeError):
                    pass

        return live_payload

    async def _send_compressed(self, websocket: WebSocket, data: dict):
        """Send gzip compressed JSON data."""
        json_str = json.dumps(data)
        json_bytes = json_str.encode('utf-8')
        compressed_data = gzip.compress(json_bytes)
        await websocket.send_bytes(compressed_data)

    async def _do_send_with_diff(self, player_id: str, new_state: dict):
        """Actually send the state, using diff if possible."""
        conn_info = self.active_connections.get(player_id)
        if not conn_info:
            return

        websocket = conn_info["websocket"]
        last_sent_state = conn_info["last_sent_state"]

        # 提取 data 部分用于 diff（前端存的是 message.data）
        new_data = new_state.get("data", new_state)

        try:
            if last_sent_state is None:
                # 首次连接，发送全量
                await self._send_compressed(websocket, new_state)
                # 缓存 data 部分
                if player_id in self.active_connections:
                    self.active_connections[player_id]["last_sent_state"] = copy.deepcopy(new_data)
            else:
                # 计算 diff（基于 data 部分）
                patch = jsonpatch.make_patch(last_sent_state, new_data)
                patch_list = patch.patch

                if not patch_list:
                    return  # 无变化

                patch_message = {"type": "patch", "patch": patch_list}
                patch_json = json.dumps(patch_message)
                full_json = json.dumps(new_state)

                # 选择更小的发送
                if len(patch_json) < len(full_json) * 0.8:
                    await self._send_compressed(websocket, patch_message)
                else:
                    await self._send_compressed(websocket, new_state)

                # 更新缓存
                if player_id in self.active_connections:
                    self.active_connections[player_id]["last_sent_state"] = copy.deepcopy(new_data)

        except (WebSocketDisconnect, RuntimeError) as e:
            logger.warning(f"WebSocket for player '{player_id}' disconnected: {e}")
            self.disconnect(player_id)

    async def _debounced_send(self, player_id: str):
        """Wait for debounce delay, then send the latest pending state."""
        await asyncio.sleep(DEBOUNCE_DELAY)
        
        # Get and clear pending update
        pending = self._pending_updates.pop(player_id, None)
        self._debounce_tasks.pop(player_id, None)
        
        if pending:
            await self._do_send_with_diff(player_id, pending)

    async def send_json_to_player(self, player_id: str, data: dict):
        """Sends a JSON message to a specific player with debouncing for diff optimization."""
        conn_info = self.active_connections.get(player_id)
        if not conn_info:
            return

        # Live updates: send immediately without debounce
        if data and data.get("type") == "live_update":
            payload = self._prepare_live_payload(data)
            try:
                await self._send_compressed(conn_info["websocket"], payload)
            except (WebSocketDisconnect, RuntimeError) as e:
                logger.warning(f"WebSocket for player '{player_id}' disconnected: {e}")
                self.disconnect(player_id)
            return

        # Prepare payload
        if data and data.get("type") == "full_state":
            payload = self._prepare_player_payload(data)
        else:
            payload = data

        # 首次连接：立即发送全量，不 debounce
        if conn_info["last_sent_state"] is None:
            await self._do_send_with_diff(player_id, payload)
            return

        # 后续更新：debounce（throttle 模式：首次触发后固定延迟发送）
        self._pending_updates[player_id] = payload

        # 只有没有正在进行的 debounce task 时才创建新的
        if player_id not in self._debounce_tasks:
            self._debounce_tasks[player_id] = asyncio.create_task(
                self._debounced_send(player_id)
            )


# Create a single instance of the manager to be used across the application
manager = ConnectionManager()
