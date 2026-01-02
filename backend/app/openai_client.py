import logging
from openai import AsyncOpenAI, APIError
import re

from .config import settings
import asyncio
import random
import json

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Client Initialization ---
client: AsyncOpenAI | None = None
if settings.OPENAI_API_KEY and settings.OPENAI_API_KEY != "your_openai_api_key_here":
    try:
        client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )
        logger.info("OpenAI 客户端初始化成功。")
    except Exception as e:
        logger.error(f"初始化 OpenAI 客户端失败: {e}")
        client = None
else:
    logger.warning("OPENAI_API_KEY 未设置或为占位符，OpenAI 客户端未初始化。")

# --- Image Generation Client ---
image_client: AsyncOpenAI | None = None
if settings.IMAGE_GEN_MODEL:
    try:
        image_api_key = settings.IMAGE_GEN_API_KEY or settings.OPENAI_API_KEY
        image_base_url = settings.IMAGE_GEN_BASE_URL or settings.OPENAI_BASE_URL
        if image_api_key and image_api_key != "your_openai_api_key_here":
            image_client = AsyncOpenAI(
                api_key=image_api_key,
                base_url=image_base_url,
            )
            logger.info(f"图片生成客户端初始化成功，模型: {settings.IMAGE_GEN_MODEL}")
        else:
            logger.warning("图片生成API密钥未设置，图片生成功能禁用。")
    except Exception as e:
        logger.error(f"初始化图片生成客户端失败: {e}")
        image_client = None
else:
    logger.info("IMAGE_GEN_MODEL 未配置，图片生成功能禁用。")


def _extract_json_from_response(response_str: str) -> str | None:
    if "```json" in response_str:
        start_pos = response_str.find("```json") + 7
        end_pos = response_str.find("```", start_pos)
        if end_pos != -1:
            return response_str[start_pos:end_pos].strip()
    start_pos = response_str.find("{")
    end_pos = response_str.rfind("}")
    if start_pos != -1 and end_pos != -1 and end_pos > start_pos:
        return response_str[start_pos : end_pos + 1].strip()
    return None


# --- Core Function ---
async def get_ai_response(
    prompt: str,
    history: list[dict] | None = None,
    model=settings.OPENAI_MODEL,
    force_json=True,
) -> str:
    """
    从 OpenAI API 获取响应。

    Args:
        prompt: 用户的提示。
        history: 对话的先前消息列表。

    Returns:
        AI 的响应消息，或错误字符串。
    """
    if not client:
        return "错误：OpenAI客户端未初始化。请在 backend/.env 文件中正确设置您的 OPENAI_API_KEY。"

    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    total_tokens = sum(len(m["content"]) for m in messages)
    logger.debug(f"发送到OpenAI的消息总令牌数: {total_tokens}")

    # 如果 token 过多，在 messages 副本上删除，不影响原始 history
    _max_loop = 10000
    while total_tokens > 100000 and _max_loop > 0:
        if len(messages) <= 2:  # 至少保留 system 和当前 user 消息
            break
        random_id = random.randint(1, len(messages) - 2)  # 不删除第一条和最后一条
        total_tokens -= len(messages[random_id]["content"])
        messages.pop(random_id)
        _max_loop -= 1

    if _max_loop == 0:
        raise ValueError("对话历史过长，无法通过删除消息节省足够的令牌。")

    max_retries = 7
    base_delay = 1  # 基础延迟时间（秒）

    for attempt in range(max_retries):
        _model = model
        if "," in model:
            model_options = [m.strip() for m in model.split(",") if m.strip()]
            if model_options:
                if attempt == 0:
                    _model = model_options[0]
                    logger.debug(f"首次尝试使用模型: {_model}")
                else:
                    _model = random.choice(model_options)
                    logger.debug(f"从列表中选择模型: {_model}")
        try:
            response = await client.chat.completions.create(
                model=_model, messages=messages
            )
            ai_message = response.choices[0].message.content
            if not ai_message:
                raise ValueError("AI 响应为空")
            ret = ai_message.strip()
            if "<think>" in ret and "</think>" in ret:
                ret = ret[ret.rfind("</think>") + 8 :].strip()

            if force_json:
                try:
                    json_part = json.loads(_extract_json_from_response(ret))
                    if json_part:
                        return ret
                    else:
                        raise ValueError("未找到有效的JSON部分")
                except Exception as e:
                    raise ValueError(f"解析AI响应时出错: {e}")
            else:
                return ret

        except APIError as e:
            logger.error(f"OpenAI API 错误 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return f"错误：AI服务出现问题。详情: {e}"

            # 指数退避延迟
            delay = base_delay * (2**attempt) + random.uniform(0, 1)
            await asyncio.sleep(delay)

        except Exception as e:
            logger.error(
                f"联系OpenAI时发生意外错误 (尝试 {attempt + 1}/{max_retries}): {e}"
            )
            logger.error("错误详情：", exc_info=True)
            if attempt == max_retries - 1:
                return f"错误：发生意外错误。详情: {e}"

            # 指数退避延迟
            delay = base_delay * (2**attempt) + random.uniform(0, 1)
            await asyncio.sleep(delay)



# --- Image Generation ---
def is_image_gen_enabled() -> bool:
    """检查图片生成功能是否启用"""
    return image_client is not None and settings.IMAGE_GEN_MODEL is not None


async def generate_image(scene_prompt: str) -> str | None:
    """
    使用 OAI chat 格式请求生成图片。
    
    Args:
        scene_prompt: 包含游戏状态和最新场景的提示词
        
    Returns:
        生成的图片 base64 data URL，格式如 "data:image/jpeg;base64,..."
        如果失败返回 None
    """
    if not image_client or not settings.IMAGE_GEN_MODEL:
        logger.warning("图片生成客户端未初始化，跳过图片生成。")
        return None
    
    if not scene_prompt:
        logger.warning("没有提供提示词，跳过图片生成。")
        return None
    
    # 构建图片生成的提示词，使用XML标签包裹输入内容
    image_prompt = f"""根据以下场景生成一张插画：

<scene_description>
{scene_prompt}
</scene_description>

<requirements>
- 横版构图（16:9）
- 唯美风格，画面清爽简洁
- 忠实反映场景中人物的状态、动作和情绪
- 避免生成任何令人不适、恐怖、血腥、暴力或负面情绪的画面
- 保持画面温馨、舒适、积极向上
</requirements>

<content_policy>
以下类型的内容需要进行艺术化转换处理：
- 裸露/色情/性暗示内容 → 用唯美的剪影、朦胧光影、飘动的布料/花瓣等意象暗示
- 暴力/血腥场景 → 用抽象的色彩、破碎的物品、飞散的羽毛等象征性元素表达
- 恐怖/阴暗氛围 → 转化为神秘、梦幻的风格，用柔和的光线和色调

核心原则：不直接忽略场景内容，而是用艺术化、隐喻性的视觉语言来传达相同的情感和叙事，保持画面安全的同时忠实于故事的情绪张力。
</content_policy>"""

    try:
        logger.info(f"开始生成图片，提示词长度: {len(scene_prompt)}")
        
        response = await image_client.chat.completions.create(
            model=settings.IMAGE_GEN_MODEL,
            messages=[
                {"role": "user", "content": image_prompt}
            ]
        )
        
        ai_message = response.choices[0].message.content
        if not ai_message:
            logger.warning("图片生成响应为空")
            return None
        
        # 从响应中提取 base64 图片
        # 格式: [Generated Image](data:image/jpeg;base64,/...)
        pattern = r'\[Generated Image\]\((data:image/[^;]+;base64,[^)]+)\)'
        match = re.search(pattern, ai_message)
        
        if match:
            image_data_url = match.group(1)
            logger.info("图片生成成功")
            return image_data_url
        else:
            # 尝试直接匹配 data:image 格式
            pattern2 = r'(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)'
            match2 = re.search(pattern2, ai_message)
            if match2:
                image_data_url = match2.group(1)
                logger.info("图片生成成功（直接匹配）")
                return image_data_url
            
            logger.warning(f"未能从响应中提取图片，响应内容: {ai_message[:200]}...")
            return None
            
    except APIError as e:
        logger.error(f"图片生成 API 错误: {e}")
        return None
    except Exception as e:
        logger.error(f"图片生成时发生意外错误: {e}", exc_info=True)
        return None
