"""訪客身分相關的工具函式。不 import main 或 auth_routes，避免循環匯入。"""
import hashlib
import hmac
import os
from datetime import date, datetime, timedelta, timezone

from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlmodel import Session

from database import engine

# 台灣時區固定 UTC+8（不用 zoneinfo：slim 映像可能缺 tzdata）
TAIPEI_TZ = timezone(timedelta(hours=8))


def taiwan_today() -> date:
    """目前的台灣日期。main.py 有同名函式，這裡重複是為了不讓 auth_routes 匯入 main。"""
    return datetime.now(timezone.utc).astimezone(TAIPEI_TZ).date()


def _int_env(name: str, default: int) -> int:
    """讀整數環境變數；空白或不是整數就用預設值（同 main.py）。"""
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def guest_ip_daily_guests() -> int:
    """同一 IP 每個台灣日期最多能建立的訪客數（GUEST_IP_DAILY_GUESTS，預設 10）。每次呼叫都重讀環境變數。"""
    return _int_env("GUEST_IP_DAILY_GUESTS", 10)


def client_ip(request) -> str:
    """來源 IP（沿用 slowapi 的判斷；反向代理設定由 uvicorn 的 proxy_headers 處理）。"""
    return get_remote_address(request)


def hash_ip(ip: str) -> str:
    """IP 的 HMAC-SHA256 hex digest。資料庫只存這個，不存原始 IP。

    金鑰順序：IP_HASH_SECRET → SESSION_SECRET_KEY → 開發預設值。每次呼叫都重讀環境變數。
    """
    secret = (
        os.getenv("IP_HASH_SECRET")
        or os.getenv("SESSION_SECRET_KEY")
        or "dev-secret-change-me"
    )
    return hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()


def try_count_guest_creation(session: Session, ip_hash: str, day: date) -> bool:
    """該 IP 當日訪客建立數 +1，成功回 True、達上限回 False。單句 upsert 防併發超發，不 commit。

    無回傳列時立即 rollback，釋放該列的鎖。
    """
    limit = guest_ip_daily_guests()
    if limit < 1:
        return False

    row = session.execute(
        text("""
        INSERT INTO guest_ip_usage (ip_hash, day, guests_created, messages) VALUES (:h, :d, 1, 0)
        ON CONFLICT (ip_hash, day) DO UPDATE SET guests_created = guest_ip_usage.guests_created + 1
        WHERE guest_ip_usage.guests_created < :limit
        RETURNING guests_created
        """),
        {"h": ip_hash, "d": day, "limit": limit},
    ).first()
    if row is None:
        session.rollback()
        return False
    return True


def guest_ip_daily_messages() -> int:
    """同一 IP 每個台灣日期、所有訪客合計的訊息則數上限（GUEST_IP_DAILY_MESSAGES，預設 100）。每次呼叫都重讀環境變數。"""
    return _int_env("GUEST_IP_DAILY_MESSAGES", 100)


def try_count_guest_message(session: Session, ip_hash: str, day: date) -> bool:
    """該 IP 當日訊息數 +1，成功回 True、達上限回 False。不 commit：由 charge_daily_quota 的交易提交或 rollback。

    無回傳列時立即 rollback，釋放該列的鎖。
    """
    limit = guest_ip_daily_messages()
    if limit < 1:
        return False

    row = session.execute(
        text("""
        INSERT INTO guest_ip_usage (ip_hash, day, guests_created, messages) VALUES (:h, :d, 0, 1)
        ON CONFLICT (ip_hash, day) DO UPDATE SET messages = guest_ip_usage.messages + 1
        WHERE guest_ip_usage.messages < :limit
        RETURNING messages
        """),
        {"h": ip_hash, "d": day, "limit": limit},
    ).first()
    if row is None:
        session.rollback()
        return False
    return True


def refund_guest_message(session: Session, ip_hash: str, day: date) -> None:
    """退一則 IP 訊息計數（不低於 0）。不 commit：由呼叫端的 commit 一併提交。"""
    session.execute(
        text("UPDATE guest_ip_usage SET messages = GREATEST(messages - 1, 0) WHERE ip_hash = :h AND day = :d"),
        {"h": ip_hash, "d": day},
    )


def trial_daily_limit() -> int:
    """試用者每日則數上限（DAILY_MESSAGE_LIMIT，預設 10）。每次呼叫都重讀環境變數。main.daily_message_limit() 轉呼叫這裡。"""
    return _int_env("DAILY_MESSAGE_LIMIT", 10)


def count_chat_roots(session: Session, user_id) -> int:
    """使用者的對話 root 數。根條件須與 main.get_chat_roots 一致（不算幽靈分支的根）。"""
    return session.execute(
        text("""
        SELECT count(*) FROM message
        WHERE parent_id IS NULL AND user_id = :uid AND branch_id IS NULL
        """),
        {"uid": user_id},
    ).scalar_one()


def transfer_today_usage(session: Session, guest_id, account_id, day: date) -> None:
    """把訪客當日已用次數併入登入帳號當日的列，合計上限為 trial_daily_limit()。

    訪客當日次數為 0（或沒有列）時不動帳號。不 commit：由呼叫端的 commit 提交。
    """
    count = session.execute(
        text("SELECT count FROM daily_usage WHERE user_id = :g AND day = :d"),
        {"g": guest_id, "d": day},
    ).scalar()
    if not count or count <= 0:
        return
    limit = trial_daily_limit()
    session.execute(
        text("""
        INSERT INTO daily_usage (user_id, day, count) VALUES (:a, :d, LEAST(:c, :limit))
        ON CONFLICT (user_id, day) DO UPDATE SET count = LEAST(daily_usage.count + :c, :limit)
        """),
        {"a": account_id, "d": day, "c": count, "limit": limit},
    )


def delete_user_data(session: Session, user_id) -> None:
    """刪除某使用者的所有訊息與分支；不 commit。

    刪列前必須先把指向 message/branch 的 FK 欄位（branch 4 欄、message 3 欄，見 models.py）清成 NULL。
    """
    params = {"u": user_id}
    session.execute(
        text("""
        UPDATE branch SET root_message_id = NULL, anchor_message_id = NULL,
                          attach_root_id = NULL, parent_branch_id = NULL
        WHERE user_id = :u
        """),
        params,
    )
    session.execute(
        text("""
        UPDATE message SET parent_id = NULL, branch_id = NULL, anchor_message_id = NULL
        WHERE user_id = :u
        """),
        params,
    )
    session.execute(text("DELETE FROM message WHERE user_id = :u"), params)
    session.execute(text("DELETE FROM branch WHERE user_id = :u"), params)


def delete_guest_user(session: Session, guest_id, *, keep_data: bool) -> None:
    """刪除訪客列（keep_data=False 時連同訊息與分支）；不 commit。provider 條件是安全網，絕不刪真實帳號。"""
    if not keep_data:
        delete_user_data(session, guest_id)
    params = {"g": guest_id}
    session.execute(text("DELETE FROM auth_session WHERE user_id = :g"), params)
    session.execute(text("DELETE FROM daily_usage WHERE user_id = :g"), params)
    session.execute(
        text("DELETE FROM \"user\" WHERE id = :g AND oauth_provider = 'guest'"),
        params,
    )


def move_user_data(session: Session, guest_id, account_id) -> None:
    """把訪客的訊息與分支的 user_id 改成登入帳號。不 commit：由呼叫端的交易提交。"""
    params = {"g": guest_id, "a": account_id}
    session.execute(text("UPDATE message SET user_id = :a WHERE user_id = :g"), params)
    session.execute(text("UPDATE branch SET user_id = :a WHERE user_id = :g"), params)


IP_USAGE_KEEP_DAYS = 2  # guest_ip_usage 保留天數（台灣日期）


def guest_retention_days() -> int:
    """訪客保留天數（GUEST_RETENTION_DAYS，預設 7）。每次呼叫都重讀環境變數。"""
    return _int_env("GUEST_RETENTION_DAYS", 7)


def purge_expired_guests(session: Session, now: datetime | None = None) -> int:
    """刪除超過保留期限沒有活動的訪客（連同訊息、分支），並清掉過期的 guest_ip_usage 列。回傳刪除的訪客數。

    每位訪客各自 commit 一次，單一訪客失敗只 rollback 該筆、不影響其他訪客。
    days < 1 時不刪任何訪客（設定錯誤時絕不一次刪光），但仍清 guest_ip_usage。
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

    deleted = 0
    days = guest_retention_days()
    if days >= 1:
        cutoff = now - timedelta(days=days)
        ids = session.execute(
            text("""
            SELECT id FROM "user"
            WHERE oauth_provider = 'guest' AND COALESCE(last_active_at, created_at) < :cutoff
            """),
            {"cutoff": cutoff},
        ).scalars().all()
        for gid in ids:
            try:
                delete_guest_user(session, gid, keep_data=False)
                session.commit()
                deleted += 1
            except Exception:
                session.rollback()

    d = taiwan_today() - timedelta(days=IP_USAGE_KEEP_DAYS)
    session.execute(text("DELETE FROM guest_ip_usage WHERE day < :d"), {"d": d})
    session.commit()
    return deleted


def run_purge_once() -> int:
    """自己開一條 session 執行一次清理。背景 task 用 asyncio.to_thread 呼叫這個函式。"""
    with Session(engine) as session:
        return purge_expired_guests(session)
