import google.generativeai as genai
import os
import asyncio

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# 未設定金鑰時不在 import 階段報錯；呼叫時才在 gemini_chat_stream 拋出。
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)


def _max_output_tokens() -> int:
    """單次回覆輸出上限（MAX_OUTPUT_TOKENS，預設 3000，每次呼叫重讀）。不從 main 匯入以免循環。"""
    raw = (os.getenv("MAX_OUTPUT_TOKENS") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 3000


async def gemini_chat_stream(message: str, history: list, model_name: str = "gemini-2.5-flash-lite"):
    """呼叫 Gemini 並串流回覆。錯誤往上拋由 main.run_generation 處理（不 yield 錯誤文字，避免外洩供應商訊息）。"""
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY 未設定，無法呼叫 Gemini")
    print(f"🤖 正在使用模型: {model_name}")
    model = genai.GenerativeModel(model_name)

    # Gemini 格式：role 為 user/model，內容放在 parts。
    gemini_history = []
    for msg in history:
        role = "user" if msg.role == "user" else "model"
        gemini_history.append({"role": role, "parts": [msg.content]})

    chat = model.start_chat(history=gemini_history)

    # stream=True 時回傳可 async 迭代的 chunk。
    response = await chat.send_message_async(
        message,
        stream=True,
        generation_config={"max_output_tokens": _max_output_tokens()},
    )

    async for chunk in response:
        try:
            if chunk.candidates and chunk.candidates[0].content.parts:
                yield chunk.text
        except ValueError:
            # Invalid operation 代表這個 chunk 沒有文字，略過。
            continue
        except Exception as e:
            print(f"Gemini Chunk Error: {e}")
            continue
