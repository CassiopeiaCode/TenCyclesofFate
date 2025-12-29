import logging
import copy
import gzip
import json
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        # Maps player_id to their active WebSocket connection
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, player_id: str):
        """Accepts a new WebSocket connection and stores it."""
        await websocket.accept()
        self.active_connections[player_id] = websocket
        logger.info(f"Player '{player_id}' connected via WebSocket.")

    def disconnect(self, player_id: str):
        """Removes a player's WebSocket connection."""
        if player_id in self.active_connections:
            del self.active_connections[player_id]
            logger.info(f"Player '{player_id}' disconnected from WebSocket.")

    async def send_json_to_player(self, player_id: str, data: dict):
        """Sends a JSON message to a specific player, compressing it with gzip."""
        websocket = self.active_connections.get(player_id)
        if not websocket:
            return

        payload_to_send = data
        # For live viewers, create a stripped-down and secure payload
        if data and data.get("type") == "live_update":
            original_session = data.get("data", {})
            
            # Create a safe, minimal payload for live view
            live_payload = {
                "type": "live_update",
                "data": {
                    "display_history": copy.deepcopy(original_session.get("display_history", [])),
                    "current_life": copy.deepcopy(original_session.get("current_life"))
                }
            }

            # For privacy, remove player's own inputs from the live broadcast
            if live_payload["data"]["display_history"]:
                live_payload["data"]["display_history"] = [
                    msg for msg in live_payload["data"]["display_history"] if not msg.strip().startswith("> ")
                ]

            # Mask the redemption code if it exists - scan ALL messages in display_history
            if original_session.get("redemption_code"):
                full_code = original_session["redemption_code"]
                masked_code = f"{full_code[:1]}...{full_code[-1:]}"
                
                # Scan and mask the code in ALL messages of the display history
                if live_payload["data"]["display_history"]:
                    try:
                        for i, message in enumerate(live_payload["data"]["display_history"]):
                            if isinstance(message, str) and full_code in message:
                                live_payload["data"]["display_history"][i] = message.replace(full_code, masked_code)
                    except (TypeError, AttributeError):
                        pass  # Ignore if history format is unexpected
            
            payload_to_send = live_payload

        # For the actual player, just remove the internal history
        elif data and data.get("type") == "full_state":
            payload_to_send = copy.deepcopy(data)
            if payload_to_send.get("data"):
                payload_to_send["data"].pop("internal_history", None)
        
        try:
            # 1. Serialize dict to JSON string
            json_str = json.dumps(payload_to_send)
            # 2. Encode string to bytes
            json_bytes = json_str.encode('utf-8')
            # 3. Compress bytes using gzip
            compressed_data = gzip.compress(json_bytes)
            # 4. Send compressed bytes
            await websocket.send_bytes(compressed_data)
        except (WebSocketDisconnect, RuntimeError) as e:
            logger.warning(f"WebSocket for player '{player_id}' disconnected before message could be sent: {e}")
            self.disconnect(player_id)

# Create a single instance of the manager to be used across the application
manager = ConnectionManager()