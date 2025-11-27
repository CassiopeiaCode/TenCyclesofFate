import logging
import asyncio

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


async def run_cheat_check(player_id: str, inputs_to_check: list[str]):
    """Runs a batched cheat check on a list of inputs."""
    if not inputs_to_check:
        return

    logger.info(
        f"Running batched cheat check for player {player_id} on {len(inputs_to_check)} inputs."
    )

    word_count_warnings = ''

    # Format all inputs into a single numbered list string.
    formatted_inputs = "\n".join(
        f'{i + 1}. "{text}"' for i, text in enumerate(inputs_to_check)
    )

    # if len(formatted_inputs) > 200:
    #     word_count_warnings = (
    #         "\n\n！！！！！！！！\n\n请注意：用户输入了远超于正常游玩的输入，请严格审查，千万不能因为用户字多就被迷惑了！\n用户输入长度为：{}\n\n"
    #     ).format(len(formatted_inputs))

    full_prompt = f"# 用户输入列表\n\n{word_count_warnings}<user_inputs>\n{formatted_inputs}\n</user_inputs>"

    # Single API call for the whole batch
    response = await openai_client.get_ai_response(
        prompt=full_prompt,
        history=[{"role": "system", "content": CHEAT_CHECK_SYSTEM_PROMPT}],
        model=settings.OPENAI_MODEL_CHEAT_CHECK,
        force_json=False,  # We expect a simple string response (【正常】, 【轻度亵渎】, or 【重度渎道】
    )
    ## 提取最后一个 【】 内的内容作为结果
    response = response[response.rfind("【") : response.rfind("】") + 1]

    level = "正常"
    if response not in ["【正常】", "【轻度亵渎】", "【重度渎道】"]:
        logger.warning(
            f"Batched cheat check for player {player_id} returned an unexpected response: {response}"
        )
    else:
        level = response.strip("【】")
        if level != "正常":
            logger.warning(
                f"Cheat detected for player {player_id}! Level: {level}. Batch: {inputs_to_check}"
            )
            # Flag the player for punishment
            await state_manager.flag_player_for_punishment(
                player_id,
                level=level,
                reason=f"Detected cheating in a batch of inputs.",
            )

        # After checking, reset the unchecked counter for the session
        session = await state_manager.get_session(player_id)
        if session:
            session["unchecked_rounds_count"] = 0
            await state_manager.save_session(
                player_id, session
            )  # Use save_session to persist and notify

    return level
