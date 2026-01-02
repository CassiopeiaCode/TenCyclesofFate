import asyncio
import json
import logging
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from collections import OrderedDict
from typing import Any
import aiofiles
import aiofiles.os

from .websocket_manager import manager as websocket_manager
from .live_system import live_manager
from . import security

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Configuration ---
DATA_DIR = Path("game_data")
SESSIONS_DIR = DATA_DIR / "sessions"
INDEX_FILE = DATA_DIR / "index.json"
OLD_DATA_FILE = Path("game_data.json")  # 旧数据文件，用于迁移

# LRU 缓存配置
MAX_CACHED_SESSIONS = 5  # 最多缓存 20 个会话的元数据

# --- Module-level State ---
_meta_cache: OrderedDict[str, dict] = OrderedDict()  # LRU 缓存
_sessions_index: dict[str, float] = {}  # player_id -> last_modified
_index_modified: bool = False
_auto_save_interval: int = 300  # 5 minutes


# --- File Path Helpers ---
def _get_session_dir(player_id: str) -> Path:
    """获取玩家会话目录路径"""
    # 使用 player_id 的 hash 前两位作为子目录，避免单目录文件过多
    safe_id = player_id.replace("/", "_").replace("\\", "_")
    return SESSIONS_DIR / safe_id


def _get_meta_path(player_id: str) -> Path:
    return _get_session_dir(player_id) / "meta.json"


def _get_internal_history_path(player_id: str) -> Path:
    return _get_session_dir(player_id) / "internal_history.jsonl"


def _get_display_history_path(player_id: str) -> Path:
    return _get_session_dir(player_id) / "display_history.jsonl"


# --- LRU Cache Management ---
def _cache_meta(player_id: str, meta: dict):
    """将元数据加入 LRU 缓存"""
    if player_id in _meta_cache:
        _meta_cache.move_to_end(player_id)
    _meta_cache[player_id] = meta
    
    # 超出容量时移除最旧的
    while len(_meta_cache) > MAX_CACHED_SESSIONS:
        _meta_cache.popitem(last=False)


def _get_cached_meta(player_id: str) -> dict | None:
    """从缓存获取元数据"""
    if player_id in _meta_cache:
        _meta_cache.move_to_end(player_id)
        return _meta_cache[player_id]
    return None


def _invalidate_cache(player_id: str):
    """使缓存失效"""
    _meta_cache.pop(player_id, None)


# --- Core File Operations ---
async def _read_json_file(path: Path) -> dict | None:
    """异步读取 JSON 文件"""
    try:
        if not path.exists():
            return None
        async with aiofiles.open(path, 'r', encoding='utf-8') as f:
            content = await f.read()
            return json.loads(content)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取文件失败 {path}: {e}")
        return None


async def _write_json_file(path: Path, data: dict):
    """异步写入 JSON 文件"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except IOError as e:
        logger.error(f"写入文件失败 {path}: {e}")


async def _read_jsonl_file(path: Path) -> list:
    """异步读取 JSONL 文件（每行一个 JSON）"""
    result = []
    try:
        if not path.exists():
            return result
        async with aiofiles.open(path, 'r', encoding='utf-8') as f:
            async for line in f:
                line = line.strip()
                if line:
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(f"跳过无效 JSON 行: {line[:50]}...")
    except IOError as e:
        logger.error(f"读取 JSONL 文件失败 {path}: {e}")
    return result


async def _append_jsonl_file(path: Path, item: Any):
    """异步追加一行到 JSONL 文件"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, 'a', encoding='utf-8') as f:
            await f.write(json.dumps(item, ensure_ascii=False) + '\n')
    except IOError as e:
        logger.error(f"追加 JSONL 文件失败 {path}: {e}")


async def _write_jsonl_file(path: Path, items: list):
    """异步写入整个 JSONL 文件（覆盖）"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, 'w', encoding='utf-8') as f:
            for item in items:
                await f.write(json.dumps(item, ensure_ascii=False) + '\n')
    except IOError as e:
        logger.error(f"写入 JSONL 文件失败 {path}: {e}")


# --- Session Expiration ---
def _is_session_expired(session_date_str: str | None, days: int = 3) -> bool:
    """检查会话是否过期"""
    if not session_date_str:
        return False
    try:
        session_date = datetime.strptime(session_date_str, "%Y-%m-%d").date()
        cutoff_date = datetime.now().date() - timedelta(days=days)
        return session_date < cutoff_date
    except ValueError:
        return False


# --- Migration from Old Format ---
async def _migrate_from_old_format():
    """从旧的 game_data.json 迁移到新的文件结构（流式处理，避免 OOM）"""
    if not OLD_DATA_FILE.exists():
        logger.info("未检测到旧数据文件，跳过迁移")
        return
    
    logger.info("检测到旧数据文件，开始流式迁移...")
    
    import ijson  # 流式 JSON 解析
    
    migrated_count = 0
    skipped_count = 0
    error_count = 0
    
    try:
        with open(OLD_DATA_FILE, 'rb') as f:
            # 使用 ijson 流式解析，每次只处理一个会话
            parser = ijson.kvitems(f, '')
            
            for player_id, session in parser:
                try:
                    if not isinstance(session, dict):
                        logger.warning(f"跳过无效会话数据: {player_id}")
                        continue
                    
                    if _is_session_expired(session.get("session_date")):
                        skipped_count += 1
                        continue
                    
                    # 分离数据
                    internal_history = session.pop("internal_history", [])
                    display_history = session.pop("display_history", [])
                    
                    # 添加计数
                    session["internal_history_count"] = len(internal_history)
                    session["display_history_count"] = len(display_history)
                    
                    # 写入新格式
                    await _write_json_file(_get_meta_path(player_id), session)
                    await _write_jsonl_file(_get_internal_history_path(player_id), internal_history)
                    await _write_jsonl_file(_get_display_history_path(player_id), display_history)
                    
                    # 更新索引
                    _sessions_index[player_id] = session.get("last_modified", time.time())
                    migrated_count += 1
                    
                    # 释放内存
                    del internal_history
                    del display_history
                    
                    if migrated_count % 10 == 0:
                        logger.info(f"已迁移 {migrated_count} 个会话...")
                        
                except Exception as e:
                    logger.error(f"迁移会话 {player_id} 失败: {e}")
                    error_count += 1
        
        # 保存索引
        logger.info("保存索引...")
        await _save_index()
        
        # 备份旧文件
        try:
            backup_path = OLD_DATA_FILE.with_suffix('.json.bak')
            import shutil
            shutil.move(str(OLD_DATA_FILE), str(backup_path))
            logger.info(f"旧文件已备份为 {backup_path}")
        except Exception as e:
            logger.warning(f"备份旧文件失败: {e}，但迁移已完成")
        
        logger.info(f"迁移完成: 成功 {migrated_count}, 跳过 {skipped_count}, 失败 {error_count}")
        
    except ImportError:
        logger.error("ijson 未安装，尝试使用传统方式迁移（可能导致内存问题）")
        await _migrate_from_old_format_legacy()
    except Exception as e:
        logger.error(f"迁移失败: {e}", exc_info=True)
        raise


async def _migrate_from_old_format_legacy():
    """传统迁移方式（备用，可能 OOM）"""
    logger.warning("使用传统迁移方式，大文件可能导致内存问题")
    
    with open(OLD_DATA_FILE, 'r', encoding='utf-8') as f:
        old_sessions = json.load(f)
    
    migrated_count = 0
    for player_id, session in old_sessions.items():
        if _is_session_expired(session.get("session_date")):
            continue
        
        internal_history = session.pop("internal_history", [])
        display_history = session.pop("display_history", [])
        
        session["internal_history_count"] = len(internal_history)
        session["display_history_count"] = len(display_history)
        
        await _write_json_file(_get_meta_path(player_id), session)
        await _write_jsonl_file(_get_internal_history_path(player_id), internal_history)
        await _write_jsonl_file(_get_display_history_path(player_id), display_history)
        
        _sessions_index[player_id] = session.get("last_modified", time.time())
        migrated_count += 1
    
    await _save_index()
    
    backup_path = OLD_DATA_FILE.with_suffix('.json.bak')
    import shutil
    shutil.move(str(OLD_DATA_FILE), str(backup_path))
    
    logger.info(f"传统迁移完成，共迁移 {migrated_count} 个会话")


# --- Index Management ---
async def _load_index():
    """加载会话索引"""
    global _sessions_index
    data = await _read_json_file(INDEX_FILE)
    if data:
        _sessions_index = data
        logger.info(f"加载了 {len(_sessions_index)} 个会话索引")
    else:
        _sessions_index = {}


async def _save_index():
    """保存会话索引"""
    global _index_modified
    await _write_json_file(INDEX_FILE, _sessions_index)
    _index_modified = False


async def _rebuild_index():
    """重建索引（扫描所有会话目录）"""
    global _sessions_index
    _sessions_index = {}
    
    if not SESSIONS_DIR.exists():
        return
    
    for session_dir in SESSIONS_DIR.iterdir():
        if session_dir.is_dir():
            meta_path = session_dir / "meta.json"
            if meta_path.exists():
                meta = await _read_json_file(meta_path)
                if meta:
                    player_id = meta.get("player_id", session_dir.name)
                    _sessions_index[player_id] = meta.get("last_modified", 0)
    
    await _save_index()
    logger.info(f"重建索引完成，共 {len(_sessions_index)} 个会话")


# --- Initialization ---
async def init_storage():
    """异步初始化存储（启动时调用）"""
    await _async_init()


def load_from_json():
    """启动时加载数据（同步包装，已废弃）"""
    logger.warning("load_from_json() 已废弃，请使用 await init_storage()")
    asyncio.create_task(_async_init())


async def _async_init():
    """异步初始化"""
    logger.info("初始化文件存储...")
    
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"数据目录: {DATA_DIR.absolute()}")
    except Exception as e:
        logger.error(f"创建数据目录失败: {e}", exc_info=True)
        raise
    
    # 先尝试迁移旧数据
    await _migrate_from_old_format()
    
    # 加载索引
    await _load_index()
    
    # 如果索引为空但有会话目录，重建索引
    if not _sessions_index and SESSIONS_DIR.exists() and any(SESSIONS_DIR.iterdir()):
        await _rebuild_index()
    
    # 清理过期会话
    await _cleanup_expired_sessions()
    
    logger.info(f"文件存储初始化完成，当前 {len(_sessions_index)} 个会话")


async def _cleanup_expired_sessions():
    """清理过期会话"""
    expired = []
    for player_id in list(_sessions_index.keys()):
        meta = await _load_meta(player_id)
        if meta and _is_session_expired(meta.get("session_date")):
            expired.append(player_id)
    
    for player_id in expired:
        await _delete_session(player_id)
    
    if expired:
        logger.info(f"清理了 {len(expired)} 个过期会话")


async def _delete_session(player_id: str):
    """删除会话"""
    import shutil
    session_dir = _get_session_dir(player_id)
    if session_dir.exists():
        shutil.rmtree(session_dir)
    _sessions_index.pop(player_id, None)
    _invalidate_cache(player_id)
    global _index_modified
    _index_modified = True


# --- Auto Save ---
async def shutdown_storage():
    """关闭时保存数据"""
    if _index_modified:
        await _save_index()
    logger.info("存储已安全关闭")


def save_to_json():
    """保存数据（兼容旧接口，已废弃）"""
    logger.warning("save_to_json() 已废弃，请使用 await shutdown_storage()")
    asyncio.create_task(_async_save())


async def _async_save():
    """异步保存索引"""
    if _index_modified:
        await _save_index()


async def _auto_save_task():
    """定期保存索引"""
    while True:
        await asyncio.sleep(_auto_save_interval)
        if _index_modified:
            logger.info("自动保存索引...")
            await _save_index()


def start_auto_save_task():
    """启动自动保存任务"""
    logger.info(f"启动自动保存任务，间隔: {_auto_save_interval} 秒")
    asyncio.create_task(_auto_save_task())


# --- Meta Operations ---
async def _load_meta(player_id: str) -> dict | None:
    """加载会话元数据"""
    # 先查缓存
    cached = _get_cached_meta(player_id)
    if cached:
        return cached.copy()  # 返回副本，避免外部修改影响缓存
    
    # 从文件加载
    meta = await _read_json_file(_get_meta_path(player_id))
    if meta:
        _cache_meta(player_id, meta.copy())  # 缓存副本
    return meta


async def _save_meta(player_id: str, meta: dict):
    """保存会话元数据"""
    await _write_json_file(_get_meta_path(player_id), meta)
    _cache_meta(player_id, meta.copy())  # 缓存副本
    
    # 更新索引
    global _index_modified
    _sessions_index[player_id] = meta.get("last_modified", time.time())
    _index_modified = True


# --- Public API ---
async def get_session(player_id: str) -> dict | None:
    """获取完整会话数据（包括历史记录）"""
    meta = await _load_meta(player_id)
    if not meta:
        return None
    
    # 加载历史记录
    internal_history = await _read_jsonl_file(_get_internal_history_path(player_id))
    display_history = await _read_jsonl_file(_get_display_history_path(player_id))
    
    # 组装完整会话
    session = meta.copy()
    session["internal_history"] = internal_history
    session["display_history"] = display_history
    
    return session


async def save_session(player_id: str, session_data: dict):
    """保存完整会话数据"""
    global _index_modified
    
    # 复制一份，避免修改原始数据
    data_to_save = session_data.copy()
    
    # 分离历史记录
    internal_history = data_to_save.pop("internal_history", None)
    display_history = data_to_save.pop("display_history", None)
    
    # 更新时间戳
    data_to_save["last_modified"] = time.time()
    session_data["last_modified"] = data_to_save["last_modified"]  # 同步回原数据
    
    # 获取旧的计数
    old_meta = await _load_meta(player_id)
    old_internal_count = old_meta.get("internal_history_count", 0) if old_meta else 0
    old_display_count = old_meta.get("display_history_count", 0) if old_meta else 0
    
    # 处理 internal_history
    if internal_history is not None:
        new_count = len(internal_history)
        data_to_save["internal_history_count"] = new_count
        
        if new_count > old_internal_count:
            # 只追加新增的部分
            new_items = internal_history[old_internal_count:]
            for item in new_items:
                await _append_jsonl_file(_get_internal_history_path(player_id), item)
        elif new_count < old_internal_count or (new_count == 0 and old_internal_count > 0):
            # 历史被重置或清空，重写整个文件
            await _write_jsonl_file(_get_internal_history_path(player_id), internal_history)
    
    # 处理 display_history
    if display_history is not None:
        new_count = len(display_history)
        data_to_save["display_history_count"] = new_count
        
        if new_count > old_display_count:
            # 只追加新增的部分
            new_items = display_history[old_display_count:]
            for item in new_items:
                await _append_jsonl_file(_get_display_history_path(player_id), item)
        elif new_count < old_display_count or (new_count == 0 and old_display_count > 0):
            # 历史被重置或清空，重写整个文件
            await _write_jsonl_file(_get_display_history_path(player_id), display_history)
    
    # 保存元数据
    await _save_meta(player_id, data_to_save)
    
    # 推送更新（使用原始 session_data，包含历史记录）
    tasks = [
        websocket_manager.send_json_to_player(
            player_id, {"type": "full_state", "data": session_data}
        ),
        live_manager.broadcast_state_update(player_id, session_data)
    ]
    await asyncio.gather(*tasks)


async def get_last_n_inputs(player_id: str, n: int) -> list[str]:
    """获取最后 N 条玩家输入"""
    internal_history = await _read_jsonl_file(_get_internal_history_path(player_id))
    
    player_inputs = [
        item["content"]
        for item in internal_history
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    
    return player_inputs[-n:]


def get_most_recent_sessions(limit: int = 10) -> list[dict]:
    """获取最近活跃的会话列表"""
    # 按 last_modified 排序
    sorted_sessions = sorted(
        _sessions_index.items(),
        key=lambda x: x[1],
        reverse=True
    )[:limit]
    
    results = []
    for player_id, last_modified in sorted_sessions:
        encrypted_id = security.encrypt_player_id(player_id)
        display_name = (
            f"{player_id[0]}...{player_id[-1]}"
            if len(player_id) > 2
            else player_id
        )
        results.append({
            "player_id": encrypted_id,
            "display_name": display_name,
            "last_modified": last_modified
        })
    
    return results


async def create_or_get_session(player_id: str) -> dict:
    """创建或获取会话"""
    session = await get_session(player_id)
    if session:
        return session
    
    # 创建新会话
    new_session = {
        "player_id": player_id,
        "internal_history_count": 0,
        "display_history_count": 0,
        "internal_history": [],
        "display_history": []
    }
    await save_session(player_id, new_session)
    return new_session


async def clear_session(player_id: str):
    """清空会话数据"""
    await _delete_session(player_id)
    logger.info(f"会话 {player_id} 已清空")


async def flag_player_for_punishment(player_id: str, level: str, reason: str):
    """标记玩家待惩罚"""
    session = await get_session(player_id)
    if not session:
        logger.warning(f"尝试标记不存在的会话: {player_id}")
        return
    
    session["pending_punishment"] = {
        "level": level,
        "reason": reason
    }
    await save_session(player_id, session)
    logger.info(f"玩家 {player_id} 被标记为 {level} 惩罚，原因: {reason}")
