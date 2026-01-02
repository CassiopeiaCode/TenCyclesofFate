import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated
from pathlib import Path

from fastapi import (
    FastAPI, APIRouter, Depends, HTTPException, status,
    WebSocket, WebSocketDisconnect, Request
)
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from . import auth, game_logic, state_manager, security
from .websocket_manager import manager as websocket_manager
from .live_system import live_manager
from .config import settings

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- App Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Application startup...")
    await state_manager.init_storage()
    state_manager.start_auto_save_task()
    yield
    logging.info("Application shutdown...")
    await state_manager.shutdown_storage()

# --- FastAPI App Instance ---
app = FastAPI(lifespan=lifespan, title="浮生十梦")

# Add SessionMiddleware for OAuth flow state management
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

# --- Routers ---
# Router for /api prefixed routes
api_router = APIRouter(prefix="/api")
# Router for root-level routes like /callback
root_router = APIRouter()


# --- Authentication Routes ---
@api_router.get('/login/linuxdo')
async def login_linuxdo(request: Request):
    """
    Redirects the user to Linux.do for authentication.
    """
    # Use a hardcoded, absolute URL for the callback to avoid ambiguity
    # This must match the URL registered in your Linux.do OAuth application settings.
    redirect_uri = str(request.url.replace(path="/callback"))
    return await auth.oauth.linuxdo.authorize_redirect(request, redirect_uri)

@root_router.get('/callback')
async def auth_linuxdo_callback(request: Request):
    """
    Handles the callback from Linux.do after authentication.
    This route is now at the root to match the expected OAuth callback URL.
    Fetches user info, creates a JWT, and sets it in a cookie.
    """
    try:
        token = await auth.oauth.linuxdo.authorize_access_token(request)
    except Exception as e:
        logger.error(f"Error during OAuth callback: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not authorize access token",
        )

    resp = await auth.oauth.linuxdo.get('api/user', token=token)
    resp.raise_for_status()
    user_info = resp.json()

    # Create JWT with user info from linux.do
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    jwt_payload = {
        "sub": user_info.get("username"),
        "id": user_info.get("id"),
        "name": user_info.get("name"),
        "trust_level": user_info.get("trust_level"),
    }
    access_token = auth.create_access_token(
        data=jwt_payload, expires_delta=access_token_expires
    )

    # Set token in cookie and redirect to frontend
    response = RedirectResponse(url="/")
    response.set_cookie(
        "token",
        value=access_token,
        httponly=True,
        max_age=int(access_token_expires.total_seconds()),
        samesite="lax",
    )
    return response

@api_router.post("/logout")
async def logout():
    """
    Logs the user out by clearing the authentication cookie.
    """
    response = RedirectResponse(url="/")
    response.delete_cookie("token")
    return response

# --- Game Routes ---
@api_router.get("/live/players")
async def get_live_players():
    """Returns a list of the most recently active players for the live view."""
    return state_manager.get_most_recent_sessions(limit=10)

@api_router.post("/game/init")
async def init_game(
    current_user: Annotated[dict, Depends(auth.get_current_active_user)],
):
    """
    Initializes or retrieves the daily game session for the player.
    This does NOT start a trial, it just ensures the session for the day exists.
    """
    game_state = await game_logic.get_or_create_daily_session(current_user)
    return game_state

# --- WebSocket Endpoint ---
@api_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for real-time game state updates."""
    token = websocket.cookies.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return
    try:
        payload = auth.decode_access_token(token)
        username: str | None = payload.get("sub")
        if username is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token payload")
            return
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token validation failed")
        return

    await websocket_manager.connect(websocket, username)

    try:
        user_info = await auth.get_current_user(token)
        session = await state_manager.get_session(user_info["username"])
        if session:
            await websocket_manager.send_json_to_player(
                user_info["username"], {"type": "full_state", "data": session}
            )

        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action:
                await game_logic.process_player_action(user_info, action)

    except WebSocketDisconnect:
        websocket_manager.disconnect(username)

@api_router.websocket("/live/ws")
async def live_websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for the live viewing system."""
    token = websocket.cookies.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return
    try:
        user_info = await auth.get_current_user(token)
        viewer_id = user_info["username"]
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token validation failed")
        return

    await websocket_manager.connect(websocket, viewer_id)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "watch":
                encrypted_id = data.get("player_id")
                if encrypted_id:
                    target_id = security.decrypt_player_id(encrypted_id)
                    if not target_id:
                        logger.warning(f"Received invalid encrypted ID from {viewer_id}")
                        continue
                    
                    live_manager.add_viewer(viewer_id, target_id)
                    # Send the current state of the watched player immediately
                    target_state = await state_manager.get_session(target_id)
                    if target_state:
                        await websocket_manager.send_json_to_player(
                            viewer_id, {"type": "live_update", "data": target_state}
                        )

    except WebSocketDisconnect:
        websocket_manager.disconnect(viewer_id)
        live_manager.remove_viewer(viewer_id)


# --- Include API Router and Mount Static Files ---
app.include_router(api_router)
app.include_router(root_router) # Include the root router before mounting static files
static_files_dir = Path(__file__).parent.parent.parent / "frontend"
app.mount("/", StaticFiles(directory=static_files_dir, html=True), name="static")

# --- Uvicorn Runner ---
if __name__ == "__main__":
    import uvicorn
    # The first argument should be "main:app" and we should specify the app_dir
    # This makes running the script directly more robust.
    # For command line, the equivalent is:
    # uvicorn backend.app.main:app --host <host> --port <port> --reload
    uvicorn.run(
        "main:app",
        app_dir="backend/app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.UVICORN_RELOAD
    )