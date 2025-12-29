import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from .websocket_manager import manager as websocket_manager
from .live_system import live_manager
from . import security

# --- Module-level State ---
SESSIONS: dict[str, dict] = {}
_sessions_modified: bool = False
_data_file_path: Path = Path("game_data.json")
_auto_save_interval: int = 300  # 5 minutes

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Core Functions ---
def _is_session_expired(session: dict, days: int = 3) -> bool:
    """Check if a session is older than the specified number of days."""
    session_date_str = session.get("session_date")
    if not session_date_str:
        return False  # Keep sessions without a date
    
    try:
        session_date = datetime.strptime(session_date_str, "%Y-%m-%d").date()
        cutoff_date = datetime.now().date() - timedelta(days=days)
        return session_date < cutoff_date
    except ValueError:
        logger.warning(f"Invalid session_date format: {session_date_str}")
        return False  # Keep sessions with invalid date format


def load_from_json():
    """Load game sessions from the JSON file at startup, filtering out sessions older than 3 days."""
    global SESSIONS
    if _data_file_path.exists():
        try:
            with open(_data_file_path, "r", encoding="utf-8") as f:
                raw_sessions = json.load(f)
            
            # Filter out expired sessions (older than 3 days)
            original_count = len(raw_sessions)
            SESSIONS = {
                player_id: session
                for player_id, session in raw_sessions.items()
                if not _is_session_expired(session)
            }
            filtered_count = original_count - len(SESSIONS)
            
            if filtered_count > 0:
                logger.info(f"Filtered out {filtered_count} expired sessions (older than 3 days)")
            logger.info(f"Successfully loaded {len(SESSIONS)} sessions from {_data_file_path}")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Could not load data from {_data_file_path}: {e}")
            SESSIONS = {}
    else:
        logger.info(f"{_data_file_path} not found. Starting with empty sessions.")
        SESSIONS = {}

def save_to_json():
    """Save the current sessions to the JSON file."""
    global _sessions_modified
    try:
        with open(_data_file_path, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f, ensure_ascii=False, indent=4)
        _sessions_modified = False
        logger.info(f"Successfully saved data to {_data_file_path}")
    except IOError as e:
        logger.error(f"Could not save data to {_data_file_path}: {e}")

async def _auto_save_task():
    """Periodically check if data needs to be saved."""
    while True:
        await asyncio.sleep(_auto_save_interval)
        if _sessions_modified:
            logger.info("Auto-saving modified data...")
            save_to_json()

def start_auto_save_task():
    """Creates and starts the background auto-save task."""
    logger.info(f"Starting auto-save task. Interval: {_auto_save_interval} seconds.")
    asyncio.create_task(_auto_save_task())

async def save_session(player_id: str, session_data: dict):
    """
    Saves the entire session data for a player and pushes it to their WebSocket.
    """
    global _sessions_modified
    
    # Check if the new session is the same as the existing one using JSON comparison
    # existing_session = SESSIONS.get(player_id)
    # if existing_session is not None:
    #     try:
    #         if json.dumps(existing_session, sort_keys=True) == json.dumps(session_data, sort_keys=True):
    #             return
    #     except (TypeError, ValueError) as e:
    #         logger.warning(f"Could not compare sessions for player {player_id}: {e}")
    
    session_data["last_modified"] = time.time()
    SESSIONS[player_id] = session_data
    _sessions_modified = True
    
    # Create tasks for both the player and any live viewers
    tasks = [
        websocket_manager.send_json_to_player(
            player_id, {"type": "full_state", "data": session_data}
        ),
        live_manager.broadcast_state_update(player_id, session_data)
    ]
    asyncio.gather(*tasks)


async def get_last_n_inputs(player_id: str, n: int) -> list[str]:
    """Get the last N player inputs for a session."""
    session = SESSIONS.get(player_id, {})
    internal_history = session.get("internal_history", [])
    
    # Filter for user inputs and get the content
    player_inputs = [
        item["content"]
        for item in internal_history
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    
    return player_inputs[-n:]

async def get_session(player_id: str) -> dict | None:
    """Gets the entire session object, which might contain metadata."""
    return SESSIONS.get(player_id)

def get_most_recent_sessions(limit: int = 10) -> list[dict]:
    """Gets the most recently active sessions, sorted by last_modified."""
    # Filter out sessions that don't have the 'last_modified' timestamp
    valid_sessions = [s for s in SESSIONS.values() if "last_modified" in s]
    
    # Sort sessions by 'last_modified' in descending order
    sorted_sessions = sorted(valid_sessions, key=lambda s: s["last_modified"], reverse=True)
    
    # Return the top 'limit' sessions, with encrypted player IDs
    results = []
    for s in sorted_sessions[:limit]:
        player_id = s["player_id"]
        encrypted_id = security.encrypt_player_id(player_id)
        
        # The display name is now also the encrypted ID for simplicity,
        # or we can still use a masked version of the real ID if preferred.
        # Let's stick to a masked real ID for better readability.
        display_name = (
            f"{player_id[0]}...{player_id[-1]}"
            if len(player_id) > 2
            else player_id
        )

        results.append({
            "player_id": encrypted_id, # Send encrypted ID to the frontend
            "display_name": display_name,
            "last_modified": s["last_modified"]
        })
    return results

async def create_or_get_session(player_id: str) -> dict:
    """Creates a session if it doesn't exist, and returns it."""
    global _sessions_modified
    if player_id not in SESSIONS:
        SESSIONS[player_id] = {}  # A session is now a dictionary
        _sessions_modified = True
    return SESSIONS[player_id]

async def clear_session(player_id: str):
    """Clears all data for a given player's session."""
    global _sessions_modified
    if player_id in SESSIONS:
        SESSIONS[player_id] = {} # Reset to an empty dictionary
        _sessions_modified = True
        logger.info(f"Session for player {player_id} has been cleared.")

async def flag_player_for_punishment(player_id: str, level: str, reason: str):
    """Flags a player's session for punishment to be handled by game_logic."""
    global _sessions_modified
    session = SESSIONS.get(player_id)
    if not session:
        logger.warning(f"Attempted to flag non-existent session for player {player_id}")
        return

    # Add the flag directly to the session object.
    session["pending_punishment"] = {
        "level": level,
        "reason": reason
    }
    _sessions_modified = True
    logger.info(f"Player {player_id} flagged for {level} punishment. Reason: {reason}")
    # Immediately notify the client about the punishment flag
    await websocket_manager.send_json_to_player(
        player_id, {"type": "full_state", "data": session}
    )