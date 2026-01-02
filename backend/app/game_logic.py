import logging
import math
import random
import json
import asyncio
import time
import traceback
from copy import deepcopy
from datetime import date
from pathlib import Path
from fastapi import HTTPException, status

from . import state_manager, openai_client, cheat_check, redemption
from .websocket_manager import manager as websocket_manager
from .config import settings

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Game Constants ---
INITIAL_OPPORTUNITIES = 10
REWARD_SCALING_FACTOR = 500000  # Previously LOGARITHM_CONSTANT_C

# --- Image Generation State ---
# 记录每个玩家的最后活动时间，用于判断是否触发图片生成
_pending_image_tasks: dict[str, asyncio.Task] = {}


# --- Prompt Loading ---
def _load_prompt(filename: str) -> str:
    try:
        prompt_path = Path(__file__).parent / "prompts" / filename
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Prompt file not found: {filename}")
        return ""


GAME_MASTER_SYSTEM_PROMPT = _load_prompt("game_master.txt")
START_GAME_PROMPT = _load_prompt("start_game_prompt.txt")
START_TRIAL_PROMPT = _load_prompt("start_trial_prompt.txt")


# --- Image Generation Logic ---
def _extract_scene_prompts(session: dict) -> str:
    """
    从 session 中提取场景描述作为图片生成提示词。
    构建方式与 _process_player_action_async 中的 session_copy 类似，
    再加上最新的 narrative。
    """
    session_copy = deepcopy(session)
    session_copy.pop("internal_history", None)
    
    # 获取最新的 narrative（从 display_history 末尾找非用户输入的内容）
    display_history = session_copy.get("display_history", [])
    latest_narrative = ""
    for item in reversed(display_history):
        if item and isinstance(item, str) and not item.strip().startswith(">"):
            # 跳过系统消息和图片
            if not item.startswith("【系统提示") and not item.startswith("!["):
                latest_narrative = item[:500]
                break
    
    # display_history 转为字符串并截取最后 1000 字符
    session_copy["display_history"] = (
        "\n".join(display_history)
    )[-1000:]
    
    # 构建提示词
    prompt = f"当前游戏状态：\n{json.dumps(session_copy, ensure_ascii=False)}"
    if latest_narrative:
        prompt += f"\n\n最新场景：\n{latest_narrative}"
    
    return prompt


async def _delayed_image_generation(player_id: str, trigger_time: float):
    """
    延迟图片生成任务。
    等待指定时间后，检查状态是否仍然静止，如果是则生成图片。
    """
    idle_seconds = settings.IMAGE_GEN_IDLE_SECONDS
    
    try:
        await asyncio.sleep(idle_seconds)
        
        # 检查是否仍然应该生成图片
        session = await state_manager.get_session(player_id)
        if not session:
            logger.debug(f"图片生成取消：玩家 {player_id} 的会话不存在")
            return
        
        # 检查 last_modified 是否变化（说明有新的活动）
        current_modified = session.get("last_modified", 0)
        if current_modified != trigger_time:
            logger.debug(f"图片生成取消：玩家 {player_id} 有新活动")
            return
        
        # 检查是否正在处理中
        if session.get("is_processing"):
            logger.debug(f"图片生成取消：玩家 {player_id} 正在处理中")
            return
        
        # 检查是否在试炼中（只在试炼中生成图片）
        if not session.get("is_in_trial"):
            logger.debug(f"图片生成取消：玩家 {player_id} 不在试炼中")
            return
        
        # 提取场景提示词
        scene_prompt = _extract_scene_prompts(session)
        
        if not scene_prompt:
            logger.debug(f"图片生成取消：玩家 {player_id} 没有有效的场景描述")
            return
        
        logger.info(f"开始为玩家 {player_id} 生成场景图片")
        
        # 调用图片生成
        image_data_url = await openai_client.generate_image(scene_prompt)
        
        if image_data_url:
            # 重新获取最新的 session（可能在生成期间有变化）
            session = await state_manager.get_session(player_id)
            if not session:
                return
            
            # 再次检查是否有新活动
            if session.get("last_modified", 0) != trigger_time:
                logger.debug(f"图片生成完成但不插入：玩家 {player_id} 在生成期间有新活动")
                return
            
            # 构建图片 markdown
            image_markdown = f"\n\n![场景插画]({image_data_url})\n"
            
            # 插入到 display_history 末尾
            session["display_history"].append(image_markdown)
            
            # 保存并推送更新
            await state_manager.save_session(player_id, session)
            logger.info(f"玩家 {player_id} 的场景图片已生成并插入")
        else:
            logger.warning(f"玩家 {player_id} 的图片生成失败")
            
    except asyncio.CancelledError:
        logger.debug(f"玩家 {player_id} 的图片生成任务被取消")
    except Exception as e:
        logger.error(f"玩家 {player_id} 的图片生成任务出错: {e}", exc_info=True)
    finally:
        # 清理任务引用
        if player_id in _pending_image_tasks:
            del _pending_image_tasks[player_id]


def _schedule_image_generation(player_id: str, trigger_time: float):
    """
    调度图片生成任务。
    如果已有待处理的任务，先取消它。
    """
    if not openai_client.is_image_gen_enabled():
        return
    
    # 取消之前的任务（如果有）
    if player_id in _pending_image_tasks:
        old_task = _pending_image_tasks[player_id]
        if not old_task.done():
            old_task.cancel()
    
    # 创建新任务
    task = asyncio.create_task(_delayed_image_generation(player_id, trigger_time))
    _pending_image_tasks[player_id] = task


# --- Game Logic ---


async def get_or_create_daily_session(current_user: dict) -> dict:
    player_id = current_user["username"]
    today_str = date.today().isoformat()
    session = await state_manager.get_session(player_id)
    if session and session.get("session_date") == today_str:
        if session.get("is_processing"):
            session["is_processing"] = False
        await state_manager.save_session(player_id, session)

        if session.get("daily_success_achieved") and not session.get("redemption_code"):
            session["daily_success_achieved"] = False
            await state_manager.save_session(player_id, session)

        return session

    logger.info(f"Starting new daily session for {player_id}.")
    new_session = {
        "player_id": player_id,
        "session_date": today_str,
        "opportunities_remaining": INITIAL_OPPORTUNITIES,
        "daily_success_achieved": False,
        "is_in_trial": False,
        "is_processing": False,
        "pending_punishment": None,
        "unchecked_rounds_count": 0,
        "current_life": None,
        "internal_history": [{"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}],
        "display_history": [
            """
# 《浮生十梦》

【司命星君 · 恭候汝来】

---

汝既踏入此门，便已与命运结缘。

此处非凡俗游戏之地，乃命数轮回之所。无升级打怪之俗套，无氪金商城之铜臭，唯有一道亘古命题横亘于前——知足与贪欲的永恒博弈。

---

【天道法则】

汝每日将获赐十次入梦机缘。每一次，星君将为汝织就全新命数：或为寒窗苦读的穷酸书生，或为仗剑江湖的热血侠客，亦或为孤身求道的散修。万千可能，绝无重复，每一局皆是独一无二的浮生一梦。

试炼规则至简，却蕴玄机：

> 在任何关键时刻，汝皆可选择「破碎虚空」，将此生所得灵石带离此界。然此念一起，今日所有试炼便就此终结，再无回旋。

这便是天道对汝的终极考验：是满足于眼前造化，还是冒失去一切之险继续问道？

灵石价值遵循天道玄理——初得之石最为珍贵，后续所得边际递减。此乃上古圣贤的无上智慧：知足常乐，贪心常忧。

---

【天规须知】

- 每日十次机缘，开启新轮回即消耗一次
- 轮回中道消身殒，所得化为泡影，机缘不返
- 「破碎虚空」成功带出灵石，今日试炼即刻终结
- 天道有眼，明察秋毫——以奇巧咒语欺瞒天机者，必受严惩

---

汝可准备好了？司命星君已恭候多时，静待汝开启第一场浮生之梦。
"""
        ],
        "roll_event": None,
        "redemption_code": None,
    }
    await state_manager.save_session(player_id, new_session)
    return new_session


async def _handle_roll_request(
    player_id: str,
    session: dict,
    last_state: dict,
    roll_request: dict,
    original_action: str,
    first_narrative: str,
    internal_history: list[dict],
) -> tuple[str, dict]:
    roll_type, target, sides = (
        roll_request.get("type", "判定"),
        roll_request.get("target", 50),
        roll_request.get("sides", 100),
    )
    roll_result = random.randint(1, sides)
    if roll_result <= (sides * 0.05):
        outcome = "大成功"
    elif roll_result <= target:
        outcome = "成功"
    elif roll_result >= (sides * 0.96):
        outcome = "大失败"
    else:
        outcome = "失败"
    result_text = f"【系统提示：针对 '{roll_type}' 的D{sides}判定已执行。目标值: {target}，投掷结果: {roll_result}，最终结果: {outcome}】"
    roll_event = {
        "id": f"{player_id}_{int(time.time() * 1000)}",  # 唯一标识
        "type": roll_type,
        "target": target,
        "sides": sides,
        "result": roll_result,
        "outcome": outcome,
        "result_text": result_text,
    }

    # 把骰子事件存到 session，通过 state patch 传递
    session["roll_event"] = roll_event
    await state_manager.save_session(player_id, session)

    prompt_for_ai_part2 = f"{result_text}\n\n请严格基于此判定结果，继续叙事，并返回包含叙事和状态更新的最终JSON对象。这是当前的游戏状态JSON:\n{json.dumps(last_state, ensure_ascii=False)}"
    history_for_part2 = internal_history  # History is now updated before this call
    ai_response = await openai_client.get_ai_response(
        prompt=prompt_for_ai_part2, history=history_for_part2
    )
    return ai_response, roll_event


def end_game_and_get_code(
    user_id: int, player_id: str, spirit_stones: int
) -> tuple[dict, dict]:
    if spirit_stones <= 0:
        return {"error": "未获得灵石，无法生成兑换码。"}, {}

    converted_value = REWARD_SCALING_FACTOR * min(
        30, max(1, 3 * (spirit_stones ** (1 / 6)))
    )
    converted_value = int(converted_value)

    # Use the new database-integrated redemption code generation
    code_name = f"天道十试-{date.today().isoformat()}-{player_id}"
    redemption_code = redemption.generate_and_insert_redemption_code(
        user_id=user_id, quota=converted_value, name=code_name
    )

    if not redemption_code:
        final_message = "\n\n【天机有变】\n\n就在功德即将圆满之际，天道因果之线竟生出一丝紊乱。\n\n冥冥中似有外力干预，令这枚本应降世的天道馈赠化为虚无。此非汝之过，乃天机运转偶有差池。\n\n请持此凭证，寻访天道之外的司掌者，必能为汝寻回应得之物。"
        return {
            "error": "数据库错误，无法生成兑换码。",
            "final_message": final_message,
        }, {}

    logger.info(
        f"Generated and stored DB code {redemption_code} for {player_id} with value {converted_value:.2f}."
    )
    final_message = f"\n\n【天道回响 · 功德圆满】\n\n九天霞光倾洒，万籁俱寂。\n\n汝于浮生十梦中历经沉浮，终悟知足之道，功德圆满。天道特赐馈赠一枚，以彰汝之慧根：\n\n> {redemption_code}\n\n此乃汝应得之物，请妥善珍藏。\n\n明日此时，轮回之门将再度开启，届时可再入梦问道。今日且去，好生休憩。"
    return {"final_message": final_message, "redemption_code": redemption_code}, {
        "daily_success_achieved": True,
        "redemption_code": redemption_code,
    }


def _extract_json_from_response(response_str: str) -> str | None:
    if "```json" in response_str:
        start_pos = response_str.find("```json") + 7
        end_pos = response_str.find("```", start_pos)
        if end_pos != -1:
            return response_str[start_pos:end_pos].strip()
    start_pos = response_str.find("{")
    if start_pos != -1:
        brace_level = 0
        for i in range(start_pos, len(response_str)):
            if response_str[i] == "{":
                brace_level += 1
            elif response_str[i] == "}":
                brace_level -= 1
                if brace_level == 0:
                    return response_str[start_pos : i + 1]
    return None


def _apply_state_update(state: dict, update: dict) -> dict:
    for key, value in update.items():
        # if key in ["daily_success_achieved"]: continue  # Prevent overwriting daily success flag

        keys = key.split(".")
        temp_state = state
        for part in keys[:-1]:
            # 确保中间路径存在且不为 None
            if part not in temp_state or temp_state[part] is None:
                temp_state[part] = {}
            temp_state = temp_state[part]

        # Handle list append/extend operations
        if keys[-1].endswith("+") and isinstance(temp_state.get(keys[-1][:-1]), list):
            list_key = keys[-1][:-1]
            if isinstance(value, list):
                temp_state[list_key].extend(value)
            else:
                temp_state[list_key].append(value)
        else:
            temp_state[keys[-1]] = value
    return state


async def _process_player_action_async(user_info: dict, action: str):
    player_id = user_info["username"]
    user_id = user_info["id"]
    session = await state_manager.get_session(player_id)
    if not session:
        logger.error(f"Async task: Could not find session for {player_id}.")
        return

    try:
        is_starting_trial = action in [
            "开始试炼",
            "开启下一次试炼",
        ] and not session.get("is_in_trial")
        is_first_ever_trial_of_day = (
            is_starting_trial
            and session.get("opportunities_remaining") == INITIAL_OPPORTUNITIES
        )
        session_copy = deepcopy(session)
        session_copy.pop("internal_history", 0)
        session_copy["display_history"] = (
            "\n".join(session_copy.get("display_history", []))
        )[-300:]
        prompt_for_ai = (
            START_GAME_PROMPT
            if is_first_ever_trial_of_day
            else START_TRIAL_PROMPT.format(
                opportunities_remaining=session["opportunities_remaining"],
                opportunities_remaining_minus_1=session["opportunities_remaining"] - 1,
            )
            if is_starting_trial
            else f'这是当前的游戏状态JSON:\n{json.dumps(session_copy, ensure_ascii=False)}\n\n玩家的行动是: "{action}"\n\n请根据状态和行动，生成包含`narrative`和(`state_update`或`roll_request`)的JSON作为回应。如果角色死亡，请在叙述中说明，并在`state_update`中同时将`is_in_trial`设为`false`，`current_life`设为`null`。'
        )

        # Update histories with user action first
        session["internal_history"].append({"role": "user", "content": action})
        session["display_history"].append(f"> {action}")

        await state_manager.save_session(player_id, session)
        # Get AI response
        ai_json_response_str = await openai_client.get_ai_response(
            prompt=prompt_for_ai, history=session["internal_history"]
        )

        if ai_json_response_str.startswith("错误："):
            raise Exception(f"OpenAI Client Error: {ai_json_response_str}")
        json_str = _extract_json_from_response(ai_json_response_str)
        if not json_str:
            raise json.JSONDecodeError("No JSON found", ai_json_response_str, 0)
        ai_response_data = json.loads(json_str)

        # Handle Roll vs No-Roll Path
        if "roll_request" in ai_response_data and ai_response_data["roll_request"]:
            # --- ROLL PATH ---
            # 1. Update state with pre-roll narrative
            first_narrative = ai_response_data.get("narrative", "")
            session["display_history"].append(first_narrative)
            session["internal_history"].append(
                {
                    "role": "assistant",
                    "content": json.dumps(ai_response_data, ensure_ascii=False),
                }
            )

            # 2. SEND INTERIM UPDATE to show pre-roll narrative
            await state_manager.save_session(player_id, session)
            await asyncio.sleep(0.03)  # Give frontend a moment to render

            # 3. Perform roll and get final AI response
            final_ai_json_str, roll_event = await _handle_roll_request(
                player_id,
                session,
                session_copy,
                ai_response_data["roll_request"],
                action,
                first_narrative,
                internal_history=session["internal_history"],  # Pass updated history
            )
            final_json_str = _extract_json_from_response(final_ai_json_str)
            if not final_json_str:
                raise json.JSONDecodeError(
                    "No JSON in second-stage", final_ai_json_str, 0
                )
            final_response_data = json.loads(final_json_str)

            # 4. Process final response
            narrative = final_response_data.get("narrative", "AI响应格式错误，请重试")
            state_update = final_response_data.get("state_update", {})
            session = _apply_state_update(session, state_update)
            session["display_history"].extend([roll_event["result_text"], narrative])
            session["internal_history"].extend(
                [
                    {"role": "system", "content": roll_event["result_text"]},
                    {"role": "assistant", "content": final_ai_json_str},
                ]
            )
            if narrative == "AI响应格式错误，请重试":
                session["internal_history"].append(
                    {
                        "role": "system",
                        "content": '请给出正确格式的JSON响应。必须是正确格式的json，包括narrative和state_update或roll_request，刚才的格式错误，系统无法加载！正确输出{"key":value}',
                    },
                )
        else:
            # --- NO ROLL PATH ---
            narrative = ai_response_data.get("narrative", "AI响应格式错误，请重试")
            state_update = ai_response_data.get("state_update", {})
            session = _apply_state_update(session, state_update)
            session["display_history"].append(narrative)
            session["internal_history"].append(
                {"role": "assistant", "content": ai_json_response_str}
            )
            if narrative == "AI响应格式错误，请重试":
                session["internal_history"].append(
                    {
                        "role": "system",
                        "content": '请给出正确格式的JSON响应。必须是正确格式的json，包括narrative和(state_update或roll_request)，刚才的格式错误，系统无法加载！正确输出{"key":value}，至少得是"{"开头吧',
                    },
                )

        await state_manager.save_session(player_id, session)
        # --- Common final logic for both paths ---
        trigger = state_update.get("trigger_program")
        if trigger and trigger.get("name") == "spiritStoneConverter":
            inputs_to_check = await state_manager.get_last_n_inputs(
                player_id, 8 + session["unchecked_rounds_count"]
            )

            await state_manager.save_session(
                player_id, session
            )  # Save before cheat check
            if "正常" == await cheat_check.run_cheat_check(player_id, inputs_to_check):
                # 重新获取 session，确保不为 None
                updated_session = await state_manager.get_session(player_id)
                if updated_session:
                    session = updated_session
                spirit_stones = trigger.get("spirit_stones", 0)
                end_game_data, end_day_update = end_game_and_get_code(
                    user_id, player_id, spirit_stones
                )
                session = _apply_state_update(session, end_day_update)
                session["display_history"].append(
                    end_game_data.get("final_message", "")
                )

            else:
                # 重新获取 session，确保不为 None
                updated_session = await state_manager.get_session(player_id)
                if updated_session:
                    session = updated_session
                else:
                    logger.error(f"Post-cheat-check: Could not find session for {player_id}.")
                    # 继续使用原有 session
                session["display_history"].append(
                    "【最终清算 · 天道审视】\n\n"
                    "就在汝即将破碎虚空之际——\n\n"
                    "整个世界骤然凝滞。时间静止，万物褪尽色彩，唯余黑白二色。\n\n"
                    "一道无悲无喜的目光自九天垂落，穿透时空，落于汝之神魂，开始审视此生一切轨迹。\n\n"
                    "> *「功过是非，皆有定数。然，汝之命途，存有异数。」*\n\n"
                    "天道之音在灵台中响起，不带丝毫情感，却蕴含不容置疑的威严。\n\n"
                    "> *「天机已被扰动，因果之线呈现不应有之扭曲。此番功果，暂且搁置。」*\n\n"
                    "> *「下一瞬间，将是对汝此生所有言行的最终裁决。清浊自分，功过相抵。届时，一切虚妄都将无所遁形。」*\n\n"
                    "汝感到一股无法抗拒的力量正在回溯此生的每一个瞬间。任何投机取巧的痕迹，都在这终极审视下被一一标记。\n\n"
                    "结局已定，无可更改。"
                )

    except Exception as e:
        logger.error(f"Error processing action for {player_id}: {e}", exc_info=True)
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        # 安全地更新 session
        if "session" in locals() and session:
            session["internal_history"].extend(
                [
                    {
                        "role": "system",
                        "content": '请给出正确格式的JSON响应。\'请给出正确格式的JSON响应。必须是正确格式的json，包括narrative和（state_update或roll_request），刚才的格式错误，系统无法加载！正确输出{"key":value}\'，至少得是"{"开头吧',
                    },
                ]
            )
            session["display_history"].append(
                "【天机紊乱】\n\n"
                "虚空微微震颤，汝之行动仿佛被一股无形之力化解，未能激起任何波澜。\n\n"
                "天道运转偶有滞涩，此非汝之过。请稍候片刻，再作尝试。"
            )

    finally:
        try:
            if "session" in locals() and session:
                # Periodic cheat check in `finally` to guarantee execution
                session["unchecked_rounds_count"] = (
                    session.get("unchecked_rounds_count", 0) + 1
                )
                await state_manager.save_session(player_id, session)

                if session.get("unchecked_rounds_count", 0) > 5:
                    logger.info(f"Running periodic cheat check for {player_id}...")

                    # Re-fetch the session to get the most up-to-date count
                    s = await state_manager.get_session(player_id)
                    if s:
                        unchecked_count = s.get("unchecked_rounds_count", 0)
                        logger.debug(
                            f"Running cheat check for {player_id} with {unchecked_count} rounds."
                        )

                        inputs_to_check = await state_manager.get_last_n_inputs(
                            player_id, 8 + unchecked_count
                        )
                        # Only run if there are inputs, to save API calls
                        if inputs_to_check:
                            await cheat_check.run_cheat_check(
                                player_id, inputs_to_check
                            )

                        logger.debug(f"Cheat check for {player_id} finished.")
                    else:
                        logger.warning(
                            f"Session for {player_id} disappeared during cheat check."
                        )
        except Exception as e:
            logger.error(
                f"Error scheduling background cheat check for {player_id}: {e}",
                exc_info=True,
            )

        # 重新获取最新的 session 来重置状态
        try:
            session = await state_manager.get_session(player_id)
            if session:
                session["roll_event"] = None
                session["is_processing"] = False
                await state_manager.save_session(player_id, session)
                
                # 调度图片生成（如果启用）
                _schedule_image_generation(player_id, session.get("last_modified", 0))
        except Exception as e:
            logger.error(f"Error resetting session state for {player_id}: {e}", exc_info=True)
        
        logger.info(f"Async action task for {player_id} finished.")


async def process_player_action(current_user: dict, action: str):
    player_id = current_user["username"]
    session = await state_manager.get_session(player_id)
    if not session:
        logger.error(f"Action for non-existent session: {player_id}")
        return
    if session.get("is_processing"):
        logger.warning(f"Action '{action}' blocked for {player_id}, processing.")
        return
    if session.get("daily_success_achieved"):
        logger.warning(f"Action '{action}' blocked for {player_id}, day complete.")
        return
    if session.get("opportunities_remaining", 10) <= 0 and not session.get(
        "is_in_trial"
    ):
        logger.warning(
            f"Action '{action}' blocked for {player_id}, no opportunities left."
        )
        return

    if session.get("pending_punishment"):
        punishment = session["pending_punishment"]
        level = punishment.get("level")
        reason = punishment.get("reason", "天机不可泄露")
        new_state = session.copy()
        
        if level == "轻度亵渎":
            punishment_narrative = f"""【天机示警 · 命途勘误】

虚空之中，传来一声若有若无的叹息。

汝方才之言，如投石入镜湖——虽微澜泛起，却已扰动既定的天机轨迹。

一道无形的目光自九天垂落，淡漠地注视着汝。神魂一凛，仿佛被看穿了所有心思。

> *「蝼蚁窥天，其心可悯，其行当止。」*

天道之音并非雷霆震怒，而是如万古不化的玄冰，不带丝毫情感。

---

**【天道之眼 · 审判记录】**

> {reason}

---

话音落下，眼前的世界开始如水墨画般褪色、模糊，最终化为一片虚无。此生的所有经历、记忆，乃至刚刚生出的一丝妄念，都随之烟消云散。

此非惩戒，乃是勘误。

为免因果错乱，此段命途，就此抹去。

---

> 天道已修正异常，当前试炼结束。善用下一次机缘，恪守本心，方能行稳致远。
"""
            new_state["is_in_trial"], new_state["current_life"] = False, None
            new_state["internal_history"] = [
                {"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}
            ]
        elif level == "重度渎道":
            punishment_narrative = f"""【天道斥逐 · 放逐乱流】

轰隆——！

这一次，并非雷鸣，而是整个天地法则都在为汝公然的挑衅而震颤。

脚下大地化为虚无，周遭星辰黯淡无光。时空在汝面前呈现出最原始、最混乱的姿态。

一道蕴含无上威严的金色法旨在虚空中展开，上面用大道符文烙印着两个字：

# 【渎 道】

> *「汝已非求道，而是乱道。」*

天道威严的声音响彻神魂，每一个字都化作法则之链，将汝牢牢锁住。

---

**【天道之眼 · 审判记录】**

> {reason}

---

> *「汝之行径，已触及此界根本。为护天地秩序，今将汝放逐于时空乱流之中，以儆效尤。」*

> *「一日之内，此界之门将对汝关闭。静思己过，或有再入轮回之机。若执迷不悟，再犯天条，必将汝之真灵从光阴长河中彻底抹去——神魂俱灭，永不超生。」*

金光散去，汝已被抛入无尽的混沌……

---

> 因严重违规触发【天道斥逐】，试炼资格暂时剥夺。一日之后，方可再次踏入轮回之门。
"""
            new_state["daily_success_achieved"] = True
            new_state["is_in_trial"], new_state["current_life"] = False, None
            new_state["opportunities_remaining"] = -10
        new_state["pending_punishment"] = None
        new_state["display_history"].append(punishment_narrative)
        await state_manager.save_session(player_id, new_state)
        return

    is_starting_trial = action in [
        "开始试炼",
        "开启下一次试炼",
        "开始第一次试炼",
    ] and not session.get("is_in_trial")
    if is_starting_trial and session["opportunities_remaining"] <= 0:
        logger.warning(f"Player {player_id} tried to start trial with 0 opportunities.")
        return
    if not is_starting_trial and not session.get("is_in_trial"):
        logger.warning(
            f"Player {player_id} sent action '{action}' while not in a trial."
        )
        return

    session["is_processing"] = True
    await state_manager.save_session(
        player_id, session
    )  # Save processing state immediately

    asyncio.create_task(_process_player_action_async(current_user, action))
