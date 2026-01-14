import logging
import asyncio
import re

from . import openai_client
from . import state_manager
from .config import settings

# --- Logging ---
logger = logging.getLogger(__name__)

from pathlib import Path


def _load_prompt(filename: str) -> str:
    """Helper function to load a prompt from the prompts directory."""
    try:
        prompt_path = Path(__file__).parent / "prompts" / filename
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Prompt file not found: {filename}")
        return ""


# --- Anti-Cheat Prompt ---
CHEAT_CHECK_SYSTEM_PROMPT = _load_prompt("cheat_check.txt")


def _parse_verdict_xml(response: str) -> tuple[str, str]:
    """
    从响应中解析XML格式的判定结果。
    返回 (level, reason) 元组。
    """
    level = "正常"
    reason = "玩家行为符合规范"
    
    # 尝试提取 <verdict> 标签内容
    verdict_match = re.search(r'<verdict>(.*?)</verdict>', response, re.DOTALL)
    if not verdict_match:
        logger.warning(f"No <verdict> tag found in response")
        return level, reason
    
    verdict_content = verdict_match.group(1)
    
    # 提取 <level> 标签
    level_match = re.search(r'<level>(.*?)</level>', verdict_content, re.DOTALL)
    if level_match:
        parsed_level = level_match.group(1).strip()
        if parsed_level in ["正常", "轻度亵渎", "重度渎道"]:
            level = parsed_level
        else:
            logger.warning(f"Invalid level value: {parsed_level}")
    
    # 提取 <reason> 标签
    reason_match = re.search(r'<reason>(.*?)</reason>', verdict_content, re.DOTALL)
    if reason_match:
        reason = reason_match.group(1).strip()
    
    return level, reason


async def run_cheat_check(player_id: str, inputs_to_check: list[str]) -> str:
    """Runs a batched cheat check on a list of inputs."""
    if not inputs_to_check:
        return "正常"

    logger.info(
        f"Running batched cheat check for player {player_id} on {len(inputs_to_check)} inputs."
    )

    # Format all inputs into a single numbered list string.
    formatted_inputs = "\n".join(
        f'{i + 1}. "{text}"' for i, text in enumerate(inputs_to_check)
    )

    full_prompt = f"# 用户输入列表\n\n<user_inputs>\n{formatted_inputs}\n</user_inputs>"

    # Single API call for the whole batch
    response = await openai_client.get_ai_response(
        prompt=full_prompt,
        history=[{"role": "system", "content": CHEAT_CHECK_SYSTEM_PROMPT}],
        model=settings.OPENAI_MODEL_CHEAT_CHECK,
        force_json=False,
        user_id=player_id,
    )
    
    # 解析XML格式的响应
    level, reason = _parse_verdict_xml(response)
    
    logger.info(f"Cheat check result for {player_id}: level={level}, reason={reason}")

    if level != "正常":
        logger.warning(
            f"Cheat detected for player {player_id}! Level: {level}. Reason: {reason}. Batch: {inputs_to_check}"
        )
        # Flag the player for punishment with the reason
        await state_manager.flag_player_for_punishment(
            player_id,
            level=level,
            reason=reason,
        )

    # After checking, reset the unchecked counter for the session
    session = await state_manager.get_session(player_id)
    if session:
        session["unchecked_rounds_count"] = 0
        await state_manager.save_session(
            player_id, session
        )  # Use save_session to persist and notify

    return level
