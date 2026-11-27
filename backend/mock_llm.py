import asyncio
import random

# 這是模擬的 AI 回覆庫，讓每次回答看起來不一樣
MOCK_RESPONSES = [
    "這是一個模擬的 AI 回覆，用於測試串流效果。",
    "你好！我是你的 AI 助手。雖然我現在還沒有接上大腦，但我可以模擬打字機的效果給你看！",
    "FastAPI 是一個非常高效能的 Python 框架，特別適合用於 AI 應用程式的後端開發。",
    "你剛剛說的內容很有趣，可以多告訴我一點嗎？",
    "這是第一段話。\n\n這是第二段話，測試換行的顯示效果。",
]


async def mock_chat_stream(message: str, history: list):
    """模擬 AI 串流回覆（逐字 yield）。"""
    # 模擬延遲
    await asyncio.sleep(1)

    intro = ""
    if history:
        last_msg = history[-1]
        intro = f"（我看到你上一句說了：{last_msg.content}）\n"

    responses = [
        "這是一個模擬的 AI 回覆。",
        "FastAPI + PostgreSQL 是個強大的組合！",
        "我正在查閱你的對話紀錄...",
        f"我們已經對話了 {len(history)} 次囉。",
    ]
    response_text = intro + random.choice(responses)

    # 特定輸入給固定回答，方便測試
    if "你好" in message:
        response_text = intro + "你好呀！很高興見到你。"

    # 逐字吐出，模擬打字機效果
    for char in response_text:
        yield char
        # 隨機停頓，模擬打字節奏
        await asyncio.sleep(random.uniform(0.05, 0.2))