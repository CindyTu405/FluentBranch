from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from sqlmodel import Session, select, text
from sqlalchemy import delete as sa_delete, update as sa_update, func as sa_func
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import hashlib
import hmac
import logging
import os
import uuid
from slowapi import _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from database import get_session, engine
from auth_routes import cookie_secure, get_current_user, router as auth_router, SESSION_COOKIE_NAME
from log_redaction import install_access_log_redaction  # uvicorn access log 遮罩 /auth/ 查詢字串
from openrouter_connect import router as openrouter_router  # 必須先於 auth_router 掛載（/auth/{provider}/callback 會匹配 openrouter）
from rate_limit import limiter  # 單一實例，auth_routes 也會用
import guest  # 訪客 IP 雜湊與每 IP 訊息上限
from models import (
    User,
    Message,
    ChatRequest,
    UpdateTitleRequest,
    MessageWithActivity,
    MessageRead,
    FreeModelsCache,
    Branch,
    CreateBranchRequest,
    RegenerateRequest,
    DailyUsage,
    OpenRouterCredential,
    to_utc_iso,
)
from gemini_llm import gemini_chat_stream
from openrouter_llm import openrouter_chat_stream, UserKeyInvalid, UserKeyInsufficientCredit, UserKeyUpstreamError
from crypto_utils import CredentialKeyError, decrypt_key
from scraper import get_free_models



# 進行中 / 已完成的 AI 回覆緩衝。key = AI 訊息 id（str），value = {"text", "done"}。
# 生成在獨立的 asyncio task 裡跑，不綁 HTTP 連線；切走對話或斷線不會停止生成。
# 只存在程序記憶體，重啟即遺失。
GENERATIONS: dict[str, dict] = {}

logger = logging.getLogger(__name__)

# 生成完成後保留緩衝的秒數，讓續讀端讀到 done；harness 可調短以測競態。
BUFFER_RETENTION_SECONDS = 60


# 台灣時區固定 UTC+8（不用 zoneinfo：slim 映像可能缺 tzdata）
TAIPEI_TZ = timezone(timedelta(hours=8))
# 生成失敗時給使用者看的固定提示（不含供應商的錯誤訊息）
GENERATION_FAILED_TEXT = "\n\n⚠️ 生成中斷，請稍後再試"


def taiwan_date_of(utc_now: datetime) -> date:
    """把一個時間點換成台灣日期。純函式，給 harness 測日界（16:00Z 即台灣次日 00:00）。"""
    return utc_now.astimezone(TAIPEI_TZ).date()


def taiwan_today() -> date:
    """目前的台灣日期。"""
    return taiwan_date_of(datetime.now(timezone.utc))


def next_reset_at_utc(day: date) -> str:
    """day 的下一個台灣 00:00，轉成 UTC ISO 字串（結尾 Z）。"""
    next_midnight = datetime.combine(day + timedelta(days=1), time(0), tzinfo=TAIPEI_TZ)
    return to_utc_iso(next_midnight)


def _int_env(name: str, default: int) -> int:
    """讀整數環境變數；空白或不是整數就用預設值。"""
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def daily_message_limit() -> int:
    """試用者（訪客與未連結 OpenRouter 的登入用戶）每個台灣日期的次數上限（DAILY_MESSAGE_LIMIT，預設 10）。"""
    return guest.trial_daily_limit()


def site_daily_message_limit() -> int:
    """全站每個台灣日期的總次數上限（SITE_DAILY_MESSAGE_LIMIT，預設 900）。"""
    return _int_env("SITE_DAILY_MESSAGE_LIMIT", 900)


def _daily_quota_exceeded(day: date) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": "daily_quota_exceeded", "reset_at": next_reset_at_utc(day)},
    )


def charge_daily_quota(
    session: Session, user_id: uuid.UUID, guest_ip_hash: str | None = None, linked: bool = False
) -> date | None:
    """扣一次當日額度，回傳扣款的台灣日期（退額度用）。不 commit：與訊息寫入同一交易。

    依序檢查：全站上限（503 site_quota_exceeded）→ 試用上限（429）→ 訪客 IP 訊息數（429 guest_ip_limit）
    → 個人每日額度（429）。任一層失敗都 rollback，不留下任何扣款。
    """
    # 已連結者（用自己的 OpenRouter 金鑰）：不檢查全站上限、不寫 daily_usage、不計 IP，只更新 last_active_at。
    # 回傳 None，之後的失敗也不會退額度（沒有扣款）。
    if linked:
        session.execute(
            text('UPDATE "user" SET last_active_at = :now WHERE id = :u'),
            {"now": datetime.now(timezone.utc).replace(tzinfo=None), "u": user_id},
        )
        return None

    day = taiwan_today()

    site_used = session.exec(
        select(sa_func.coalesce(sa_func.sum(DailyUsage.count), 0)).where(DailyUsage.day == day)
    ).one()
    if site_used >= site_daily_message_limit():
        raise HTTPException(status_code=503, detail={"code": "site_quota_exceeded"})

    user_limit = daily_message_limit()
    if user_limit < 1:
        raise _daily_quota_exceeded(day)

    if guest_ip_hash is not None:
        if not guest.try_count_guest_message(session, guest_ip_hash, day):
            raise HTTPException(
                status_code=429,
                detail={"code": "guest_ip_limit", "reset_at": next_reset_at_utc(day)},
            )

    row = session.execute(
        text("""
        INSERT INTO daily_usage (user_id, day, count) VALUES (:u, :d, 1)
        ON CONFLICT (user_id, day) DO UPDATE SET count = daily_usage.count + 1
        WHERE daily_usage.count < :limit
        RETURNING count
        """),
        {"u": user_id, "d": day, "limit": user_limit},
    ).first()
    if row is None:
        # 被拒絕時立刻結束交易：ON CONFLICT 已對該列取得鎖，若留到 session 關閉才釋放，
        # 同一個事件迴圈裡的其他 DB 呼叫會卡住（實測造成死結）。
        # rollback 同時丟掉第 3 層剛加的 IP 計數（同一交易）。
        session.rollback()
        raise _daily_quota_exceeded(day)

    session.execute(
        text('UPDATE "user" SET last_active_at = :now WHERE id = :u'),
        {"now": datetime.now(timezone.utc).replace(tzinfo=None), "u": user_id},
    )
    return day


def refund_daily_quota(user_id: uuid.UUID, day: date, guest_ip_hash: str | None = None) -> None:
    """退 1 次（不低於 0），自開 session；guest_ip_hash 有值時同一交易也退該 IP 當日訊息數。"""
    with Session(engine) as db_session:
        db_session.execute(
            text("UPDATE daily_usage SET count = GREATEST(count - 1, 0) WHERE user_id = :u AND day = :d"),
            {"u": user_id, "d": day},
        )
        if guest_ip_hash is not None:
            guest.refund_guest_message(db_session, guest_ip_hash, day)
        db_session.commit()


def _persist_generation(ai_msg_id: str, text: str) -> None:
    """同步寫回生成結果；由 run_generation 以 asyncio.to_thread 呼叫，使用獨立 session。"""
    with Session(engine) as db_session:
        msg = db_session.get(Message, uuid.UUID(ai_msg_id))
        if msg:
            msg.content = text
            db_session.add(msg)
            db_session.commit()


# 使用者自己的金鑰失敗時的固定文字（不含供應商回應）
USER_KEY_INVALID_TEXT = "⚠️ 你的 OpenRouter 連結已失效，請重新連結"
USER_KEY_CREDIT_TEXT = "⚠️ 你的 OpenRouter 帳號額度不足"


def resolve_user_api_key(session: Session, user: User) -> str | None:
    """連結者回傳解密後的金鑰；試用者（含訪客）或沒有連結列回 None。
    只用已驗證的 user.id 查列。金鑰無法解密（加密金鑰已更換）→ 刪除該列並回 409，不建立任何訊息。"""
    if user.is_guest:
        return None
    row = session.get(OpenRouterCredential, user.id)
    if row is None:
        return None
    try:
        return decrypt_key(row.key_encrypted)
    except CredentialKeyError:
        session.delete(row)
        session.commit()
        raise HTTPException(status_code=409, detail={"code": "openrouter_reconnect_required"})


def _drop_credential_if_same(user_id: uuid.UUID, failed_key: str) -> bool:
    """401 後移除失效連結，但只在目前的密文仍解出 failed_key 時才刪。
    使用者若已在此期間重新連結（列已是新金鑰），保留新列。DELETE 同時比對所讀到的密文。"""
    with Session(engine) as db_session:
        row = db_session.get(OpenRouterCredential, user_id)
        if row is None:
            return False
        enc = row.key_encrypted
        try:
            current = decrypt_key(enc)
        except CredentialKeyError:
            return False
        if not hmac.compare_digest(current.encode("utf-8"), failed_key.encode("utf-8")):
            return False
        result = db_session.execute(
            sa_delete(OpenRouterCredential).where(
                OpenRouterCredential.user_id == user_id,
                OpenRouterCredential.key_encrypted == enc,
            )
        )
        db_session.commit()
        return result.rowcount > 0


def _is_linked(session: Session, user: User) -> bool:
    """已連結者：非訪客且有 openrouter_credential 列。"""
    return (not user.is_guest) and session.get(OpenRouterCredential, user.id) is not None


async def run_generation(
    ai_msg_id: str,
    message: str,
    history: list,
    model: str,
    usage_user_id: uuid.UUID | None = None,
    usage_day: date | None = None,
    usage_ip_hash: str | None = None,
    api_key: str | None = None,
    key_owner_id: uuid.UUID | None = None,
):
    """背景執行一次 AI 生成：邊收 chunk 邊寫緩衝，完成後寫回 DB。

    失敗只給固定提示（不記錄內容）；若一個字都沒產出則退回扣款。DB 呼叫都用 asyncio.to_thread。
    """
    buf = GENERATIONS[ai_msg_id]
    try:
        if api_key is not None:
            # 連結者：一律走 OpenRouter 並用自己的金鑰（即使是 gemini* 模型，也不走直連 Gemini）
            stream = openrouter_chat_stream(message, history, model, api_key=api_key)
        elif model.startswith("gemini"):
            stream = gemini_chat_stream(message, history, model)
        else:
            # 試用者走平台金鑰（不帶 api_key）
            stream = openrouter_chat_stream(message, history, model)
        async for chunk in stream:
            buf["text"] += chunk
    except UserKeyInvalid:
        # 401：固定文字；只有目前的密文仍是這把失效金鑰時才移除連結。不重試、不改用平台金鑰。
        logger.warning("(G2-9) 使用者 OpenRouter 金鑰被拒絕 ai_msg=%s", ai_msg_id)
        buf["text"] = USER_KEY_INVALID_TEXT
        if api_key is not None and key_owner_id is not None:
            try:
                await asyncio.to_thread(_drop_credential_if_same, key_owner_id, api_key)
            except Exception:
                logger.warning("(G2-9) 移除失效連結失敗 ai_msg=%s", ai_msg_id)
    except UserKeyInsufficientCredit:
        # 402：固定文字；連結保留
        logger.warning("(G2-9) 使用者 OpenRouter 額度不足 ai_msg=%s", ai_msg_id)
        buf["text"] = USER_KEY_CREDIT_TEXT
    except UserKeyUpstreamError:
        # 其他錯誤：沿用現有的固定提示；不重試、不改用平台金鑰。連結者的生成不扣額度，也無需退額度。
        logger.warning("(G2-9) 使用者 OpenRouter 生成失敗 ai_msg=%s", ai_msg_id)
        buf["text"] += GENERATION_FAILED_TEXT
    except Exception:
        logger.exception("(V1-2) 生成失敗 ai_msg=%s model=%s", ai_msg_id, model)
        had_output = bool(buf["text"])
        buf["text"] += GENERATION_FAILED_TEXT
        if not had_output and usage_user_id is not None and usage_day is not None:
            try:
                await asyncio.to_thread(refund_daily_quota, usage_user_id, usage_day, usage_ip_hash)
            except Exception:
                logger.exception("(V1-2) 退回額度失敗 user=%s day=%s", usage_user_id, usage_day)
    finally:
        # 先寫回 DB 再標記 done：串流不能在 DB 還沒有回覆時就回報完成。
        # done 放在內層 finally：寫回失敗或 task 被取消也一定會設，否則讀取端會一直等。
        try:
            # 寫回失敗只記 log，仍要往下做緩衝保留與清理
            try:
                await asyncio.to_thread(_persist_generation, ai_msg_id, buf["text"])
            except Exception:
                logger.exception("(B12) 寫回生成結果失敗 ai_msg=%s", ai_msg_id)
        finally:
            buf["done"] = True
        # 寫入完成後保留緩衝一小段時間，讓正在續讀的前端能讀到 done
        await asyncio.sleep(BUFFER_RETENTION_SECONDS)
        # 只清「自己放進去的那一份」：重傳時同一個 id 可能已經有新緩衝，不能誤清
        if GENERATIONS.get(ai_msg_id) is buf:
            GENERATIONS.pop(ai_msg_id, None)


# 保留期限清理的執行間隔（秒）
GUEST_PURGE_INTERVAL_SECONDS = 3600


async def _guest_purge_loop():
    """每小時清一次過期訪客。guest.run_purge_once 是阻塞的 DB 呼叫，丟到執行緒執行。"""
    while True:
        try:
            n = await asyncio.to_thread(guest.run_purge_once)
            if n:
                logger.info("(G2-5) 清除過期訪客 %s 位", n)
        except Exception:
            logger.exception("(G2-5) 清除過期訪客失敗")
        await asyncio.sleep(GUEST_PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 資料表由 Alembic 管理（alembic upgrade head），啟動時不建表。
    purge_task = asyncio.create_task(_guest_purge_loop())
    # uvicorn 可能覆寫 logging 設定，啟動時再裝一次（冪等）
    install_access_log_redaction()
    yield
    purge_task.cancel()
    try:
        await purge_task
    except asyncio.CancelledError:
        pass
    print("伺服器關閉中...")


def _api_docs_enabled() -> bool:
    """API 文件（/docs、/redoc、/openapi.json）預設關閉，只在 ENABLE_API_DOCS 為真時開啟。"""
    return (os.getenv("ENABLE_API_DOCS", "false") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# 模組匯入時就裝一次；lifespan 啟動時會再裝一次，確保 uvicorn 套用 logging 設定後仍有效
install_access_log_redaction()

_DOCS_ON = _api_docs_enabled()
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)

# 限流：預設依來源 IP；/chat 與 regenerate 改依登入者（rate_limit_key）。/chat 會呼叫付費 LLM。
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def rate_limit_key(request: Request) -> str:
    """限流 key：有登入 cookie 時用 sha256(cookie)（同 IP 的不同使用者互不影響），否則用來源 IP。限流表不存原始 token。"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return "user:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    return get_remote_address(request)

# CORS 設定：只允許白名單來源（環境變數 CORS_ORIGINS，逗號分隔）。
# 不可用 "*"：allow_credentials=True 時萬用 origin 會讓任何網站帶著 cookie 呼叫 API。
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://localhost:8000"
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Message-Id", "X-User-Message-Id", "X-Orphaned-Branch-Ids"],
)

# 模型白名單（免費 tier）：環境變數 ALLOWED_MODELS，逗號分隔。
# 支援 `prefix*` 前綴匹配（例如 "gemini*" 允許所有 gemini 開頭的模型），其餘為精確比對。
# 目的：避免使用者填入任意 OpenRouter 付費 model ID，燒掉平台金鑰。
# 字面值 "none" 不是模型名，代表空白名單（只剩免費模型清單可用）。
def _parse_allowed_models(raw: str) -> list[str]:
    return [m.strip() for m in raw.split(",") if m.strip() and m.strip() != "none"]

ALLOWED_MODELS = _parse_allowed_models(os.getenv("ALLOWED_MODELS", "gemini*"))

def is_model_allowed(model: str, session: Session | None = None) -> bool:
    for entry in ALLOWED_MODELS:
        if entry.endswith("*"):
            if model.startswith(entry[:-1]):
                return True
        elif model == entry:
            return True

    # 環境變數白名單沒中，再查 OpenRouter 免費模型清單（動態，由 scraper 快取）。
    # 這段包 try/except：快取/網路出問題時只是「少允許一些模型」，
    # 絕不能讓白名單本身拋錯（否則整個 chat 端點 500）。
    if session is not None:
        try:
            for item in get_free_models(session):
                if isinstance(item, dict) and item.get("id") == model:
                    return True
        except Exception as e:
            print(f"[whitelist] 取得免費模型清單失敗，忽略: {e}")

    return False

# 模型選單：presets 來自 ALLOWED_MODELS（每次呼叫時讀取，harness 可替換），
# default_model 來自 DEFAULT_MODEL（同樣每次讀取）。
GEMINI_PRESET = {"value": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite"}

def _model_presets() -> list[dict]:
    presets: list[dict] = []
    seen: set[str] = set()
    for entry in ALLOWED_MODELS:
        if entry == "gemini*":
            items = [GEMINI_PRESET]
        elif entry.endswith("*"):
            continue  # 其他萬用字元不列入選單
        else:
            items = [{"value": entry, "label": entry}]
        for item in items:
            if item["value"] not in seen:
                seen.add(item["value"])
                presets.append(item)
    return presets

def _default_model(session: Session, presets: list[dict]) -> str | None:
    configured = (os.getenv("DEFAULT_MODEL") or "").strip()
    if configured and is_model_allowed(configured, session):
        return configured
    if presets:
        return presets[0]["value"]
    # 這段包 try/except：免費模型清單取得失敗時回傳 null，不能讓端點 500。
    try:
        for item in get_free_models(session):
            if isinstance(item, dict) and item.get("id"):
                return item["id"]
    except Exception as e:
        print(f"[model-options] 取得免費模型清單失敗，忽略: {e}")
    return None

# authlib 的 authorize_redirect 需要 session 存 OAuth state（CSRF 防護）。
# 生產環境務必用環境變數覆寫 SESSION_SECRET_KEY。
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-secret-change-me"),
    session_cookie="oauth_state",  # 不可與登入 cookie "session" 同名
    max_age=600,
    same_site="lax",
    https_only=cookie_secure(),
)

# OpenRouter 連結：必須在 auth_router 之前，否則 /auth/openrouter/callback 會被 oauth_callback 先匹配
app.include_router(openrouter_router)

app.include_router(auth_router)


@app.get("/")
def read_root():
    return {"status": "ok", "message": "Chat API is running"}


def get_owned_message(
    session: Session, message_id: uuid.UUID, user: User, detail: str = "not found"
) -> Message:
    """取訊息：不存在或不屬於 user 一律 404，不洩漏存在與否。"""
    msg = session.get(Message, message_id)
    if msg is None or msg.user_id != user.id:
        raise HTTPException(status_code=404, detail=detail)
    return msg


def get_owned_branch(
    session: Session, branch_id: uuid.UUID, user: User, detail: str = "not found"
) -> Branch:
    """取分支：不存在或不屬於 user 一律 404。"""
    branch = session.get(Branch, branch_id)
    if branch is None or branch.user_id != user.id:
        raise HTTPException(status_code=404, detail=detail)
    return branch


def get_conversation_history(session: Session, parent_id: uuid.UUID | None) -> list[Message]:
    """從 parent_id 往上追到根，回傳 [最舊, ..., 最新的父訊息]。"""
    history = []
    current_id = parent_id

    while current_id:
        msg = session.get(Message, current_id)
        if not msg:
            break  # 找不到就斷掉（預防萬一）

        history.append(msg)
        current_id = msg.parent_id

    # 往上找時列表是 [子, 父, 爺]，AI 需要 [爺, 父, 子]，所以反轉
    history.reverse()

    return history

@app.post("/chat")
@limiter.limit("10/minute", key_func=rate_limit_key)
async def chat_endpoint(
    payload: ChatRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    # 連結者的金鑰：最先解密（失效 → 409，不建立訊息、不扣額度）
    api_key = resolve_user_api_key(session, user)
    linked = api_key is not None

    # 0. 模型白名單檢查（免費 tier）：必須在任何寫入 DB / 呼叫 LLM 之前就擋掉，
    #    否則會留下沒有回覆的孤兒訊息；而且串流一旦開始就無法再回 403。
    # 連結者跳過白名單（只有長度限制，由 ChatRequest 檢查）。
    if not linked and not is_model_allowed(payload.model, session):
        raise HTTPException(status_code=403, detail=f"Model '{payload.model}' is not allowed on this plan")

    # 擁有者檢查：帶了的 parent_id / branch_id / anchor_message_id 都要屬於本人，
    # 必須在讀歷史、任何寫入與 LLM 呼叫之前。
    if payload.parent_id is not None:
        get_owned_message(session, payload.parent_id, user)
    if payload.branch_id is not None:
        get_owned_branch(session, payload.branch_id, user)
    if payload.anchor_message_id is not None:
        get_owned_message(session, payload.anchor_message_id, user)

    # 生成用的歷史對話
    history = get_conversation_history(session, payload.parent_id)

    # 扣每日額度：在任何寫入之前；不 commit，下面第一個 commit 與訊息寫入同一交易
    user_id = user.id  # 先取出，commit 之後 ORM 物件會過期
    guest_ip_hash = guest.hash_ip(guest.client_ip(request)) if user.is_guest else None  # 訪客才計 IP
    usage_day = charge_daily_quota(session, user_id, guest_ip_hash, linked=linked)

    # 存使用者訊息
    user_msg = Message(role="user", content=payload.message, parent_id=payload.parent_id,
                       user_id=user.id,
                       branch_id=payload.branch_id, anchor_message_id=payload.anchor_message_id)
    session.add(user_msg)
    session.commit()
    session.refresh(user_msg)

    # 分支的 root 綁定到第一則 user 訊息（建立分支時不預先建立訊息）
    if payload.branch_id:
        branch = session.get(Branch, payload.branch_id)
        if branch and branch.root_message_id is None:
            branch.root_message_id = user_msg.id
            session.add(branch)
            session.commit()

    if payload.parent_id and len(history) > 0:
        children = session.exec(select(Message.id).where(Message.parent_id == payload.parent_id)).all()
        
        # 有多個子訊息代表這是分支操作
        if len(children) > 1:
            root_node = history[0]  # history 第一個元素即樹根
            
            if not root_node.has_branch:
                root_node.has_branch = True
                session.add(root_node)
                session.commit()

    # 先建立 AI 佔位訊息，拿到 ID 後才能讓串流連結
    ai_msg = Message(
        role="assistant", 
        content="",  # 先留空，等串流完再補
        parent_id=user_msg.id, 
        model_used=payload.model,
        user_id=user.id,
        branch_id=payload.branch_id  # 跟隨 user_msg 的分支（不回填 anchor）
    )
    session.add(ai_msg)
    session.commit()
    session.refresh(ai_msg)

    # 生成交給背景 task；連線只負責讀緩衝。
    ai_id = str(ai_msg.id)
    user_msg_id = str(user_msg.id)
    GENERATIONS[ai_id] = {"text": "", "done": False}
    # 背景 task 不能拿請求 session 的 ORM 物件（commit 後會過期），先轉成純資料
    history_plain = [SimpleNamespace(role=m.role, content=m.content) for m in history]
    asyncio.create_task(run_generation(
        ai_id, payload.message, history_plain, payload.model,
        usage_user_id=user_id, usage_day=usage_day, usage_ip_hash=guest_ip_hash,
        api_key=api_key, key_owner_id=user_id,
    ))
    # 串流可能持續數十秒；這裡先還回連線。之後的回應只用上面的純值，不碰 ORM 物件。
    session.close()

    async def stream_generator():
        # 用續讀端點的同一套邏輯：從 0 開始讀到 done
        async for piece in _read_buffer(ai_id, 0):
            yield piece

    return StreamingResponse(
        stream_generator(),
        media_type="text/plain",
        headers={"X-Message-Id": ai_id,
                "X-User-Message-Id": user_msg_id,  # 讓前端把樂觀顯示的 user 訊息補上真 id
                "X-Accel-Buffering": "no",      # 不要讓 Nginx 緩衝
                "Cache-Control": "no-cache",    # 不快取
                "Connection": "keep-alive"      # 保持連線
                }
    )


async def _read_buffer(ai_id: str, offset: int):
    """從緩衝的 offset 位置開始吐新文字，直到 done。緩衝不存在則結束。
    offset 以「字元」計，與前端 aiResponseText.length 對齊。"""
    while True:
        buf = GENERATIONS.get(ai_id)
        if buf is None:
            return
        text = buf["text"]
        if len(text) > offset:
            yield text[offset:]
            offset = len(text)
        if buf["done"]:
            return
        await asyncio.sleep(0.1)


@app.get("/messages/{message_id}/stream")
async def stream_message(
    message_id: str,
    offset: int = 0,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """續讀一則 AI 回覆：回傳從 offset 之後的新文字。
    - 生成中：會持續等待直到完成（切回對話時用來接續顯示）。
    - 已完成（緩衝已清掉）：直接從 DB 回傳 offset 之後的內容。
    - 找不到、不是 AI 回覆、或不屬於本人：404。
    一定先查擁有者，再查 GENERATIONS 緩衝，否則別人可以讀到生成中的內容。"""
    try:
        msg_uuid = uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")

    msg = get_owned_message(session, msg_uuid, user)
    if msg.role != "assistant":
        raise HTTPException(status_code=404, detail="not found")

    # 先取出需要的值再關 session：串流期間不能佔著連線，之後也不能再碰 ORM 物件
    buf_key = str(msg.id)
    saved_content = msg.content
    session.close()

    if buf_key in GENERATIONS:
        return StreamingResponse(_read_buffer(buf_key, max(offset, 0)), media_type="text/plain",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    return StreamingResponse(iter([saved_content[max(offset, 0):]]), media_type="text/plain",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.post("/messages/{message_id}/regenerate")
@limiter.limit("10/minute", key_func=rate_limit_key)  # 跟 /chat 一樣會打付費 LLM，限流同等級
async def regenerate_message(
    message_id: str,
    payload: RegenerateRequest,
    request: Request,  # slowapi 限流需要 request 參數
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """原地重新生成一則 AI 回覆（只允許「最後一則」）。

    可選帶 content：同時編輯最後一則 user 訊息的內容，再用新內容生成。

    檢查順序：404（不存在／非 AI／不屬於本人）→ 422（content 帶了但全是空白）→
    403（模型不在白名單，改資料之前擋）→
    409（還在生成中）→ 409（同一條線上還有子訊息，不是最後一則）→
    幽靈化錨在此則上的分支 → 內容清空、換模型（有 content 則一併改 user 內容），一次 commit → 背景生成 → 串流。
    """
    # a. 404：id 格式錯、不存在、不屬於本人、或不是 AI 回覆
    try:
        ai_uuid = uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    ai = get_owned_message(session, ai_uuid, user)
    if ai.role != "assistant" or ai.parent_id is None:
        raise HTTPException(status_code=404, detail="not found")
    user_msg = get_owned_message(session, ai.parent_id, user)

    # 422：帶了 content 但全是空白，在任何寫入之前擋掉
    if payload.content is not None and not payload.content.strip():
        raise HTTPException(status_code=422, detail="content is empty")

    # 連結者的金鑰：在模型檢查與任何改寫之前解密（失效 → 409，不改任何訊息）
    api_key = resolve_user_api_key(session, user)
    linked = api_key is not None

    # b. 403：模型白名單（跟 /chat 同一個檢查，必須在任何寫入之前）；連結者跳過白名單
    if not linked and not is_model_allowed(payload.model, session):
        raise HTTPException(status_code=403, detail=f"Model '{payload.model}' is not allowed on this plan")

    ai_id = str(ai.id)

    # c. 409：這則還在生成中，兩個 task 寫同一則會互相覆蓋
    buf = GENERATIONS.get(ai_id)
    if buf is not None and not buf["done"]:
        raise HTTPException(status_code=409, detail="still generating")

    # d. 409：只允許最後一則。同一條線（branch_id 相同）上還有子訊息就拒絕。
    #    錨在這則上的分支其訊息 branch_id 不同，不算「後面還有對話」，會在 e 被幽靈化。
    later_child = session.exec(
        select(Message.id).where(
            Message.parent_id == ai.id,
            Message.branch_id.is_not_distinct_from(ai.branch_id),
        )
    ).first()
    if later_child is not None:
        raise HTTPException(status_code=409, detail="not the last message")

    # 扣每日額度：通過所有 4xx 檢查之後、任何改寫之前；不 commit，f 的 commit 一併提交
    usage_user_id = user.id  # 先取出，commit 之後 ORM 物件會過期
    guest_ip_hash = guest.hash_ip(guest.client_ip(request)) if user.is_guest else None  # 訪客才計 IP
    usage_day = charge_daily_quota(session, usage_user_id, guest_ip_hash, linked=linked)

    # 編輯 user 訊息：與下面的幽靈化、AI 清空同一個 commit，不能分開寫
    if payload.content is not None:
        user_msg.content = payload.content
        session.add(user_msg)

    # 先把會用到的欄位取出來，commit 之後 ORM 物件會過期（生成要用的是新內容）
    user_content = user_msg.content
    user_parent_id = user_msg.parent_id

    # e. 幽靈化：錨在這則上的分支 → anchor 清空、is_orphaned=True；分支的 root 斷開 parent 與 anchor
    orphaned_ids: list[str] = []
    branches = session.exec(select(Branch).where(Branch.anchor_message_id == ai.id)).all()
    # 幽靈記住原本所屬的對話：寫入這則 AI 回覆所在樹的樹根
    tree_root_id = _find_tree_root(session, ai).id
    for br in branches:
        br.anchor_message_id = None
        br.anchor_start = None
        br.anchor_end = None
        br.is_orphaned = True  # anchor_text / title 保留，之後幽靈清單可顯示原本選的字
        br.attach_root_id = tree_root_id
        session.add(br)
        orphaned_ids.append(str(br.id))
        if br.root_message_id:
            branch_root = session.get(Message, br.root_message_id)
            if branch_root:
                branch_root.parent_id = None
                branch_root.anchor_message_id = None
                session.add(branch_root)

    # f. 內容清空、換模型，與幽靈化同一個 commit（原子）
    ai.content = ""
    ai.model_used = payload.model
    session.add(ai)
    session.commit()

    # g. 重新生成用的上下文：跟當初 /chat 一樣，取 user 訊息之前的祖先鏈
    history = get_conversation_history(session, user_parent_id)
    history_plain = [SimpleNamespace(role=m.role, content=m.content) for m in history]
    GENERATIONS[ai_id] = {"text": "", "done": False}
    asyncio.create_task(run_generation(
        ai_id, user_content, history_plain, payload.model,
        usage_user_id=usage_user_id, usage_day=usage_day, usage_ip_hash=guest_ip_hash,
        api_key=api_key, key_owner_id=usage_user_id,
    ))

    # h. 串流：與 /chat 相同，讀緩衝直到 done
    # 回傳前先還連線；下面只用上面的純值（ai_id、orphaned_ids）
    session.close()
    return StreamingResponse(
        _read_buffer(ai_id, 0),
        media_type="text/plain",
        headers={
            "X-Message-Id": ai_id,
            "X-Orphaned-Branch-Ids": ",".join(orphaned_ids),  # 可能為空字串
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/chats/roots", response_model=list[MessageWithActivity])
def get_chat_roots(
    sort_by: str = "updated",
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """側邊欄對話列表：sort_by 為 created（依建立時間）或 updated（依整棵樹的最後活動時間）。"""
    if sort_by == "created":
        # 簡單模式：只看根節點的建立時間
        # 只列主線根（branch_id IS NULL）：幽靈分支的 root 斷開 parent 後也沒有 parent，不能被當成新對話
        statement = select(Message).where(
            Message.parent_id == None,
            Message.user_id == user.id,
            Message.branch_id == None,
        ).order_by(Message.created_at.desc())
        return session.exec(statement).all()
    else:
        # 解決 PostgreSQL 的 m.* 解析問題，明確列出所有欄位
        query = text("""
        WITH RECURSIVE chat_tree AS (
            SELECT id AS root_id, id AS msg_id, created_at
            FROM message
            -- (G6-1) 同 created 模式：幽靈分支的 root 不算主線根
            WHERE parent_id IS NULL AND user_id = :uid AND branch_id IS NULL

            UNION ALL
            
            SELECT ct.root_id, m.id, m.created_at
            FROM message m
            JOIN chat_tree ct ON m.parent_id = ct.msg_id
        ),
        tree_max_time AS (
            SELECT root_id, MAX(created_at) as last_activity
            -- created_at 欄位是 TIMESTAMP WITHOUT TIME ZONE，存的就是 UTC。
            -- 這裡刻意「不要」用 AT TIME ZONE 'UTC'：那會把結果變成 timestamptz，
            -- 回傳的 offset 會跟著 DB 的 TimeZone 設定跑 (本機 Asia/Taipei → +08:00，
            -- EC2 若是 UTC → +00:00)，前端很容易判斷錯時區。
            -- 維持 naive UTC，由 models.to_utc_iso() 統一輸出結尾帶 Z 的字串。
            FROM chat_tree
            GROUP BY root_id
        )
        SELECT 
            m.id, 
            m.content, 
            m.role, 
            m.model_used, 
            m.created_at,
            m.parent_id,
            m.title, 
            m.has_branch,
            tmt.last_activity AS "last_activity"
        FROM message m
        JOIN tree_max_time tmt ON m.id = tmt.root_id
        ORDER BY tmt.last_activity DESC;
        """)
        
        results = session.exec(query, params={"uid": user.id}).mappings().all()
        
        # 手動映射到白名單模型，避免 FastAPI/driver 悄悄丟欄位
        return [MessageWithActivity(**row) for row in results]

@app.get("/chats/{root_id}/history", response_model=list[MessageRead])
def get_chat_history(
    root_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    history 只回「跟 root 相同 branch_id」的那條線。

    root 是主幹 (branch_id IS NULL) 就只走主幹；
    root 是分支根 (branch_id = X) 就只走 X 這條分支，不跨越回祖先主幹。
    這樣主線與分支各自乾淨，不會被別條分支的最新訊息污染。
    """
    # 起點必須屬於本人，否則 404（不存在與不屬於本人無法區分）
    get_owned_message(session, root_id, user)

    # 單一遞迴 CTE：從 root 往下遞迴，只沿著 branch_id 與父節點相同的子訊息走。
    # IS NOT DISTINCT FROM：NULL 對 NULL 視為相等，主幹 (NULL) 才比對得起來。
    path_query = text("""
    WITH RECURSIVE chat_path AS (
        SELECT id, role, content, model_used, created_at, parent_id, title, has_branch, branch_id, anchor_message_id
        FROM message WHERE id = :root_id
        UNION ALL
        SELECT m.id, m.role, m.content, m.model_used, m.created_at, m.parent_id, m.title, m.has_branch, m.branch_id, m.anchor_message_id
        FROM message m
        JOIN chat_path cp ON m.parent_id = cp.id
        WHERE m.branch_id IS NOT DISTINCT FROM cp.branch_id
    )
    SELECT * FROM chat_path ORDER BY created_at ASC;
    """)
    
    # mappings() 將 SQL 結果轉為字典列表，FastAPI 才能正確轉換成 JSON
    results = session.exec(path_query, params={"root_id": root_id}).mappings().all()
    
    return results

@app.patch("/chats/{root_id}/title")
@limiter.limit("30/minute", key_func=rate_limit_key)
def update_chat_title(
    request: Request,
    root_id: uuid.UUID,
    payload: UpdateTitleRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    msg = get_owned_message(session, root_id, user, detail="Chat not found")

    msg.title = payload.title
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return {"status": "ok", "title": msg.title}

def _descendant_message_ids(session: Session, seed_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """種子訊息本身加上所有子孫（沿 parent_id）。UNION 會去重，循環也不會無限迴圈。"""
    if not seed_ids:
        return set()
    rows = session.exec(
        text("""
        WITH RECURSIVE chat_descendants AS (
            SELECT id FROM message WHERE id = ANY(CAST(:seeds AS uuid[]))
            UNION
            SELECT m.id FROM message m JOIN chat_descendants cd ON m.parent_id = cd.id
        )
        SELECT id FROM chat_descendants;
        """),
        params={"seeds": [str(i) for i in seed_ids]},
    ).all()
    return {uuid.UUID(str(r[0])) for r in rows}


def _collect_delete_scope(session: Session, root_id: uuid.UUID) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """算出刪除 root_id 這串對話的範圍：(訊息 id 集合 D, 分支 id 集合 B)。

    規則，重複擴大直到兩個集合都不再變大：
    - D 起始為 root_id 與它的所有子孫。
    - B = 錨在 D、根在 D、attach_root_id 在 D 的分支，加上 id 被 D 內訊息的 branch_id 引用的分支。
    - D 再加入：B 內每條分支 root_message_id 的子孫，以及 branch_id 屬於 B 的訊息（連同子孫）。
    attach_root_id 為 NULL 的舊幽靈連不到這串對話，不會被收進來。
    """
    D = _descendant_message_ids(session, {root_id})
    B: set[uuid.UUID] = set()
    while True:
        # 由訊息找分支：錨點／根／attach 落在 D，或 D 內訊息屬於該分支
        B_new = set(session.exec(select(Branch.id).where(
            Branch.anchor_message_id.in_(list(D))
            | Branch.root_message_id.in_(list(D))
            | Branch.attach_root_id.in_(list(D))
        )).all())
        B_new |= {
            b for b in session.exec(select(Message.branch_id).where(
                Message.id.in_(list(D)), Message.branch_id.is_not(None)
            )).all()
        }
        B_new |= B

        # 由分支找訊息：分支的根，以及 branch_id 屬於 B 的訊息
        seeds = set(D)
        if B_new:
            seeds |= set(session.exec(select(Message.id).where(
                Message.branch_id.in_(list(B_new))
            )).all())
            seeds |= {
                r for r in session.exec(select(Branch.root_message_id).where(
                    Branch.id.in_(list(B_new)), Branch.root_message_id.is_not(None)
                )).all()
            }
        D_new = _descendant_message_ids(session, seeds)

        if B_new == B and D_new == D:
            return D, B
        B, D = B_new, D_new


@app.delete("/chats/{root_id}")
def delete_chat(
    root_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """刪除整串對話（連同分支與幽靈）。範圍見 _collect_delete_scope；起點不屬於本人 → 404。

    範圍內任何列不屬於本人，或交易失敗，都 rollback 並回 500（固定訊息，不回傳錯誤細節）。
    PostgreSQL 的 FK 在每條 SQL 結束時才檢查，所以同一條 DELETE 內父子一起刪除不會擋。
    """
    # 起點必須屬於本人，在任何刪除之前
    get_owned_message(session, root_id, user, detail="Chat not found")

    D, B = _collect_delete_scope(session, root_id)
    D_list = list(D)
    B_list = list(B)

    # 範圍內任何一列不是本人的，整個中止
    foreign_msgs = session.exec(select(Message.id).where(
        Message.id.in_(D_list), Message.user_id != user.id
    )).all() if D_list else []
    foreign_branches = session.exec(select(Branch.id).where(
        Branch.id.in_(B_list), Branch.user_id != user.id
    )).all() if B_list else []
    if foreign_msgs or foreign_branches:
        session.rollback()
        logger.error(
            "(B11) 刪除對話 %s 的範圍內有他人資料，已中止：messages=%s branches=%s",
            root_id, foreign_msgs, foreign_branches,
        )
        raise HTTPException(status_code=500, detail="Failed to delete chat")

    # 同一交易：先解開分支的引用，再刪訊息與分支；只用 Core 批次 UPDATE/DELETE，避免 ORM 物件過期的副作用
    try:
        if B_list:
            session.execute(
                sa_update(Branch)
                .where(Branch.id.in_(B_list))
                .values(anchor_message_id=None, root_message_id=None,
                        attach_root_id=None, parent_branch_id=None)
                .execution_options(synchronize_session=False)
            )
            session.execute(
                sa_update(Branch)
                .where(Branch.parent_branch_id.in_(B_list), Branch.id.not_in(B_list))
                .values(parent_branch_id=None)
                .execution_options(synchronize_session=False)
            )
        session.execute(
            sa_delete(Message)
            .where(Message.id.in_(D_list))
            .execution_options(synchronize_session=False)
        )
        if B_list:
            session.execute(
                sa_delete(Branch)
                .where(Branch.id.in_(B_list))
                .execution_options(synchronize_session=False)
            )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("(B11) 刪除對話 %s 失敗（訊息 %d 筆、分支 %d 筆）", root_id, len(D_list), len(B_list))
        raise HTTPException(status_code=500, detail="Failed to delete chat")

    return {"status": "ok", "deleted_count": len(D_list), "deleted_branches": len(B_list)}

# 取得整棵對話樹
@app.get("/chats/{root_id}/tree", response_model=list[MessageRead])
def get_chat_tree(
    root_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    取得整棵對話樹的「所有」節點 (用來畫前端的 React Flow 樹狀圖)
    """
    # 起點必須屬於本人，否則 404
    get_owned_message(session, root_id, user)

    query = text("""
    WITH RECURSIVE chat_tree AS (
        -- 1. 找到樹根
        SELECT id, role, content, model_used, created_at, parent_id, title, has_branch, branch_id, anchor_message_id
        FROM message WHERE id = :root_id

        UNION ALL

        -- 2. 往下找出所有子子孫孫 (不論有幾個分支全抓)
        SELECT m.id, m.role, m.content, m.model_used, m.created_at, m.parent_id, m.title, m.has_branch, m.branch_id, m.anchor_message_id
        FROM message m
        JOIN chat_tree ct ON m.parent_id = ct.id
    )
    SELECT * FROM chat_tree ORDER BY created_at ASC;
    """)
    
    results = session.exec(query, params={"root_id": root_id}).mappings().all()
    return results


def demote_overlaps(
    session: Session,
    anchor_message_id: uuid.UUID,
    start: int,
    end: int,
    exclude_id: uuid.UUID | None = None,
) -> list[str]:
    """把同一則回覆上、與 [start, end) 重疊的 inline 分支降級為底部入口。

    重疊判定（半開區間）：舊.start < end 且 start < 舊.end；相鄰不算重疊。
    降級 = anchor_start／anchor_end 清空，其他欄位（anchor_message_id、anchor_text、title）不動。
    不 commit：由呼叫端與建新／重連放在同一個交易。回傳被降級的 branch id（str）。
    """
    query = select(Branch).where(
        Branch.anchor_message_id == anchor_message_id,
        Branch.is_orphaned == False,  # noqa: E712
        Branch.anchor_start.is_not(None),
        Branch.anchor_end.is_not(None),
        Branch.anchor_start < end,
        Branch.anchor_end > start,
    )
    if exclude_id is not None:
        query = query.where(Branch.id != exclude_id)
    demoted: list[str] = []
    for br in session.exec(query).all():
        br.anchor_start = None
        br.anchor_end = None
        session.add(br)
        demoted.append(str(br.id))
    return demoted


def _find_tree_root(session: Session, msg: Message) -> Message:
    """沿 parent_id 往上走，回傳 parent_id 為 NULL 的樹根訊息。"""
    cur = msg
    seen: set = set()
    while cur.parent_id is not None and cur.id not in seen:
        seen.add(cur.id)
        parent = session.get(Message, cur.parent_id)
        if parent is None:
            break
        cur = parent
    return cur


def _is_ancestor_or_self(session: Session, ancestor_id: uuid.UUID, descendant_id: uuid.UUID) -> bool:
    """ancestor_id 是否為 descendant_id 本身或其祖先。"""
    return session.exec(
        text("""
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_id FROM message WHERE id = :start_id
            UNION ALL
            SELECT m.id, m.parent_id FROM message m
            JOIN ancestors a ON a.parent_id = m.id
        )
        SELECT id FROM ancestors WHERE id = :ancestor_id LIMIT 1;
        """),
        params={"start_id": descendant_id, "ancestor_id": ancestor_id},
    ).first() is not None


class RelinkBranchRequest(BaseModel):
    """幽靈分支重連：把它接到 root 這棵樹裡的某則訊息的一段文字上。"""
    root_id: uuid.UUID
    anchor_message_id: uuid.UUID
    anchor_start: int | None = None
    anchor_end: int | None = None
    anchor_text: str | None = Field(default=None, max_length=5000)


def _branch_response(branch: Branch, reused: bool, demoted_ids: list[str]) -> dict:
    """建立分支的回傳格式（新建與重用共用）。"""
    return {
        "branch_id": str(branch.id),
        "anchor_message_id": str(branch.anchor_message_id) if branch.anchor_message_id else None,
        "anchor_text": branch.anchor_text,
        "anchor_start": branch.anchor_start,
        "anchor_end": branch.anchor_end,
        "precise": branch.anchor_start is not None and branch.anchor_end is not None,
        "root_message_id": str(branch.root_message_id) if branch.root_message_id else None,
        "title": branch.title,
        "reused": reused,
        "demoted_ids": demoted_ids,
    }


@app.post("/chats/{root_id}/branches")
@limiter.limit("30/minute", key_func=rate_limit_key)
def create_branch(
    request: Request,
    root_id: uuid.UUID,
    payload: CreateBranchRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """從 anchor 訊息分岔出新分支。root_message_id 留空，等第一則 user 訊息由 /chat 回填（不預建空訊息）。"""
    # root 與 anchor 都必須屬於本人（不存在與不屬於本人都是 404）
    root_msg = get_owned_message(session, root_id, user, detail="Chat not found")
    anchor_msg = get_owned_message(
        session, payload.anchor_message_id, user, detail="Anchor message not found"
    )

    # anchor 須位於 root 這棵樹上：從 anchor 往上找祖先，看是否走到 root
    belongs = session.exec(
        text("""
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_id FROM message WHERE id = :anchor_id
            UNION ALL
            SELECT m.id, m.parent_id FROM message m
            JOIN ancestors a ON a.parent_id = m.id
        )
        SELECT id FROM ancestors WHERE id = :root_id LIMIT 1;
        """),
        params={"anchor_id": payload.anchor_message_id, "root_id": root_id},
    ).first()
    if not belongs:
        raise HTTPException(
            status_code=400, detail="Anchor message does not belong to this chat"
        )

    # offset 驗證（在任何寫入之前）：start／end 必須成對，且 0 <= start < end <= 回覆長度
    if (payload.anchor_start is None) != (payload.anchor_end is None):
        raise HTTPException(status_code=422, detail="anchor_start and anchor_end must be given together")
    has_offset = payload.anchor_start is not None
    if has_offset:
        content_len = len(anchor_msg.content or "")
        if not (0 <= payload.anchor_start < payload.anchor_end <= content_len):
            raise HTTPException(status_code=422, detail="anchor offsets out of range")

        # 範圍完全相同的非幽靈分支已存在：不新建、不降級，直接回傳
        existing = session.exec(
            select(Branch)
            .where(
                Branch.anchor_message_id == payload.anchor_message_id,
                Branch.is_orphaned == False,  # noqa: E712
                Branch.anchor_start == payload.anchor_start,
                Branch.anchor_end == payload.anchor_end,
            )
            .order_by(Branch.created_at)
        ).first()
        if existing is not None:
            return _branch_response(existing, reused=True, demoted_ids=[])

    # title 取 anchor_text 單行前 50 字
    if payload.anchor_text and payload.anchor_text.strip():
        title = " ".join(payload.anchor_text.split())[:50]
    else:
        title = "新分支"

    # 有 offset 才檢查重疊：重疊的舊分支降級為底部入口（與下面的建新同一個 commit）
    demoted_ids: list[str] = []
    if has_offset:
        demoted_ids = demote_overlaps(
            session, payload.anchor_message_id, payload.anchor_start, payload.anchor_end
        )

    branch = Branch(
        root_message_id=None,
        anchor_message_id=payload.anchor_message_id,
        anchor_text=payload.anchor_text,
        anchor_start=payload.anchor_start,
        anchor_end=payload.anchor_end,
        title=title,
        # 若 anchor 本身已屬於某條分支，代表這是「分支再分支」
        parent_branch_id=anchor_msg.branch_id,
        user_id=user.id,
    )
    session.add(branch)
    session.flush()  # 取得 branch.id

    root_of_tree = root_msg
    if root_msg.parent_id is not None:
        # root_id 若不是樹根，往上找出 parent_id IS NULL 的那一則
        real_root_id = session.exec(
            text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_id FROM message WHERE id = :start_id
                UNION ALL
                SELECT m.id, m.parent_id FROM message m
                JOIN ancestors a ON a.parent_id = m.id
            )
            SELECT id FROM ancestors WHERE parent_id IS NULL LIMIT 1;
            """),
            params={"start_id": root_id},
        ).first()
        if real_root_id:
            root_of_tree = session.get(Message, real_root_id[0])

    if root_of_tree:
        root_of_tree.has_branch = True
        session.add(root_of_tree)

    session.commit()
    session.refresh(branch)

    return _branch_response(branch, reused=False, demoted_ids=demoted_ids)


@app.get("/chats/{root_id}/branches")
def list_branches(
    root_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """列出錨定在 root 所屬子樹內的分支（root 必須屬於本人，否則 404），依 created_at 升冪。"""
    get_owned_message(session, root_id, user)

    query = text("""
    WITH RECURSIVE tree AS (
        SELECT id FROM message WHERE id = :root_id
        UNION ALL
        SELECT m.id FROM message m
        JOIN tree t ON m.parent_id = t.id
    )
    SELECT b.id, b.title, b.anchor_message_id, b.anchor_text,
           b.anchor_start, b.anchor_end,
           b.root_message_id, b.parent_branch_id, b.created_at
    FROM branch b
    WHERE b.anchor_message_id IN (SELECT id FROM tree)
    ORDER BY b.created_at ASC;
    """)

    rows = session.exec(query, params={"root_id": root_id}).mappings().all()

    return [
        {
            "id": str(row["id"]),
            "title": row["title"],
            # anchor 可能為 NULL（幽靈／底部入口），不可產生字串 "None"
            "anchor_message_id": str(row["anchor_message_id"]) if row["anchor_message_id"] else None,
            "anchor_text": row["anchor_text"],
            "anchor_start": row["anchor_start"],
            "anchor_end": row["anchor_end"],
            "precise": row["anchor_start"] is not None and row["anchor_end"] is not None,
            "root_message_id": str(row["root_message_id"]) if row["root_message_id"] else None,
            "parent_branch_id": str(row["parent_branch_id"]) if row["parent_branch_id"] else None,
            "created_at": to_utc_iso(row["created_at"]),
        }
        for row in rows
    ]


@app.get("/branches/orphaned")
def list_orphaned_branches(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """幽靈分支清單（只列 user_id 為目前登入者的分支），依 created_at 新到舊。"""
    rows = session.exec(
        select(Branch)
        .where(Branch.is_orphaned == True, Branch.user_id == user.id)  # noqa: E712
        .order_by(Branch.created_at.desc())
    ).all()
    return [
        {
            "id": str(b.id),
            "title": b.title,
            "anchor_text": b.anchor_text,
            "root_message_id": str(b.root_message_id) if b.root_message_id else None,
            "created_at": to_utc_iso(b.created_at),
        }
        for b in rows
    ]


@app.post("/branches/{branch_id}/relink")
@limiter.limit("30/minute", key_func=rate_limit_key)
def relink_branch(
    request: Request,
    branch_id: uuid.UUID,
    payload: RelinkBranchRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """把幽靈分支接回 root 樹上某段文字（變回 inline 高亮）。title 不變。

    檢查順序：404（不洩漏存在）→ 409（非幽靈）→ 422（offset）→ 400（錨點不在樹上或位於分支內）→ 409（同範圍已存在）。
    通過後同一交易：降級重疊的舊分支、設錨點、attach_root_id=NULL、分支首則接到新錨點、樹根 has_branch=True。
    """
    # 1. 404（不洩漏存在與否）：分支、root、錨點都必須屬於本人
    branch = get_owned_branch(session, branch_id, user, detail="Branch not found")
    root_msg = get_owned_message(session, payload.root_id, user, detail="Branch not found")
    anchor_msg = get_owned_message(
        session, payload.anchor_message_id, user, detail="Anchor message not found"
    )

    # 2. 409：不是幽靈
    if branch.is_orphaned is not True:
        raise HTTPException(status_code=409, detail="Branch is not orphaned")

    # 3. 422：offset 成對且範圍合法（在任何寫入之前）
    if (payload.anchor_start is None) != (payload.anchor_end is None):
        raise HTTPException(status_code=422, detail="anchor_start and anchor_end must be given together")
    has_offset = payload.anchor_start is not None

    # 4. 錨點：屬於 root 這棵樹、不在分支自己的訊息之下
    if not _is_ancestor_or_self(session, root_msg.id, anchor_msg.id):
        raise HTTPException(status_code=400, detail="Anchor message does not belong to this chat")
    if branch.root_message_id is not None and _is_ancestor_or_self(
        session, branch.root_message_id, anchor_msg.id
    ):
        raise HTTPException(status_code=400, detail="Anchor message is inside this branch")

    if has_offset:
        content_len = len(anchor_msg.content or "")
        if not (0 <= payload.anchor_start < payload.anchor_end <= content_len):
            raise HTTPException(status_code=422, detail="anchor offsets out of range")

        # 5. 409：同一則上範圍完全相同的非幽靈分支已存在（不能把兩條分支放在同一段）
        same = session.exec(
            select(Branch.id).where(
                Branch.anchor_message_id == anchor_msg.id,
                Branch.is_orphaned == False,  # noqa: E712
                Branch.anchor_start == payload.anchor_start,
                Branch.anchor_end == payload.anchor_end,
            )
        ).first()
        if same is not None:
            raise HTTPException(status_code=409, detail="A branch with the same range already exists")

    # 同一交易寫入
    demoted_ids: list[str] = []
    if has_offset:
        demoted_ids = demote_overlaps(
            session, anchor_msg.id, payload.anchor_start, payload.anchor_end, exclude_id=branch.id
        )

    branch.anchor_message_id = anchor_msg.id
    branch.anchor_start = payload.anchor_start
    branch.anchor_end = payload.anchor_end
    branch.anchor_text = payload.anchor_text
    branch.is_orphaned = False
    branch.attach_root_id = None
    session.add(branch)

    # 分支首則訊息接到新錨點：之後在分支裡對話，上下文來自新位置
    if branch.root_message_id is not None:
        first = session.get(Message, branch.root_message_id)
        if first is not None:
            first.parent_id = anchor_msg.id
            first.anchor_message_id = anchor_msg.id
            session.add(first)

    tree_root = _find_tree_root(session, root_msg)
    tree_root.has_branch = True
    session.add(tree_root)

    session.commit()
    session.refresh(branch)
    return _branch_response(branch, reused=False, demoted_ids=demoted_ids)


@app.get("/api/free-models")
def get_free_models_endpoint(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    models = get_free_models(session)
    return {"models": models}


@app.get("/api/model-options")
def get_model_options_endpoint(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    presets = _model_presets()
    return {
        "presets": presets,
        "default_model": _default_model(session, presets),
        "allow_custom": _is_linked(session, user),  # 只有已連結者可以輸入自訂模型
    }