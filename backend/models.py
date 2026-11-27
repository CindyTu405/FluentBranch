from typing import Optional, List
from sqlmodel import SQLModel, Field
from sqlalchemy import Text, UniqueConstraint
from datetime import date, datetime, timezone
from pydantic import BaseModel, field_serializer
import uuid


def to_utc_iso(value: Optional[datetime]) -> Optional[str]:
    """把 datetime 序列化成 UTC 且結尾 Z 的 ISO 字串。

    DB 讀出的 datetime 是 naive（實際為 UTC），不帶時區標記時前端會當成本機時間，所以由後端統一輸出 Z。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        # naive 一律視為 UTC（DB 存的就是 UTC）
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 一個 Branch 代表從某則訊息 (anchor) 分岔出來的一條對話線。
# root_message_id 是這條分支的對話起點，anchor_message_id 是分岔點；
# parent_branch_id 讓分支可以再分支 (樹狀結構)。
class Branch(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # 這條分支的對話起點；分支剛建立、第一則訊息還沒送出前是 None
    root_message_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="message.id", index=True
    )
    # 三態：
    #   inline 錨定：anchor 非空、attach_root_id 空、is_orphaned False
    #   底部入口：  anchor 空、attach_root_id 非空、is_orphaned False（重連後才有）
    #   幽靈：      anchor 空、attach_root_id 空、is_orphaned True
    anchor_message_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="message.id", index=True
    )
    # 底部入口所屬的對話 root id
    attach_root_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="message.id"
    )
    # 幽靈分支：錨點所在的 AI 回覆被原地重新生成後設為 True
    is_orphaned: bool = Field(default=False)
    # 分支當下的錨點文字快照：anchor 訊息被編輯/刪除時仍能顯示分支來源
    anchor_text: Optional[str] = None
    # 錨點在來源 markdown 的字元位移（半開區間：[start, end)），None 代表沒有精確位置
    anchor_start: Optional[int] = None
    anchor_end: Optional[int] = None
    title: Optional[str] = None
    parent_branch_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="branch.id"
    )
    # 擁有者，必填（DB NOT NULL）；API 回應不輸出
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True, exclude=True)
    # naive UTC，理由同 Message.created_at
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class MessageBase(SQLModel):
    content: str
    role: str  # "user" 或 "assistant"

    # 生成此回覆的模型
    model_used: Optional[str] = None

    # 存 naive UTC：aware datetime 寫入 TIMESTAMP 欄位時會被 DB 時區（Asia/Taipei）換算，偏移 8 小時。
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    parent_id: Optional[uuid.UUID] = Field(default=None, foreign_key="message.id")

    # 只有對話開頭（root）的訊息有 title，其他通常為 None
    title: str | None = Field(default=None)

    # 此訊息之下是否有分支
    has_branch: bool = Field(default=False)

    # 這則訊息屬於哪個分支 (None = 尚未歸屬任何分支)
    branch_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="branch.id", index=True
    )

    # 這個分支從哪一則訊息分岔出去 (分支的錨點訊息)
    anchor_message_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="message.id"
    )

    @field_serializer("created_at")
    def serialize_created_at(self, value: Optional[datetime], _info) -> Optional[str]:
        return to_utc_iso(value)

class Message(MessageBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # 擁有者，必填（DB NOT NULL）；API 回應不輸出
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True, exclude=True)

# 回傳給前端的白名單；必須重新定義 id，否則序列化時匯不出
class MessageWithActivity(MessageBase):
    id: uuid.UUID
    last_activity: Optional[datetime] = None
    model_config = {"from_attributes": True}  # 讓 Pydantic 能從 DB Row 讀屬性

    @field_serializer("last_activity")
    def serialize_last_activity(self, value: Optional[datetime], _info) -> Optional[str]:
        return to_utc_iso(value)


# 對話歷史／樹的回應模型：與 Message 同欄位但不含 user_id（不回傳擁有者）
class MessageRead(MessageBase):
    id: uuid.UUID
    model_config = {"from_attributes": True}

# API 請求模型（不存入資料庫）
class ChatRequest(SQLModel):
    message: str = Field(max_length=20000)
    parent_id: Optional[uuid.UUID] = None
    model: str = Field(default="gpt-3.5-turbo", max_length=200)
    # 這次對話要寫入哪條分支 (None = 不歸屬分支，維持舊行為)
    branch_id: Optional[uuid.UUID] = None
    # 分支的錨點訊息 (從哪則訊息分岔出去的)
    anchor_message_id: Optional[uuid.UUID] = None

class UpdateTitleRequest(SQLModel):
    title: str = Field(max_length=200)


# 重新生成最後一則 AI 回覆：只帶新模型名稱，內容沿用原 user 訊息。
class RegenerateRequest(SQLModel):
    model: str = Field(max_length=200)
    # content 有值 = 編輯最後一則 user 訊息；沒有 = 純重傳
    content: Optional[str] = Field(default=None, max_length=20000)


# anchor_message_id: 從哪一則既有的訊息分岔出去
# anchor_text: 錨點文字快照，用來當分支標題（不預先建立訊息）
# content: 保留欄位，目前不使用
class CreateBranchRequest(SQLModel):
    anchor_message_id: uuid.UUID
    content: Optional[str] = None
    anchor_text: Optional[str] = Field(default=None, max_length=5000)
    # 錨點在來源 markdown 的字元位移（半開區間：[start, end)）
    anchor_start: Optional[int] = None
    anchor_end: Optional[int] = None


class FreeModelsCache(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    models_json: str = Field(default="[]")
    # 欄位型別是 TIMESTAMP WITHOUT TIME ZONE，統一存 naive UTC，
    # 讀取時再用 scraper._as_utc() 補上時區，避免 naive / aware 混用出錯。
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


GUEST_PROVIDER = "guest"  # 訪客的 oauth_provider 值


class User(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("oauth_provider", "oauth_sub", name="uq_user_oauth_provider_sub"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # OAuth provider 名稱："google" 或 "github"
    oauth_provider: str = Field(index=True)
    # provider 端的唯一使用者識別碼（sub）
    oauth_sub: str = Field(index=True)
    email: Optional[str] = None
    # 最後一次送訊息的時間（naive UTC）；訪客保留期限從這裡算，None 時用 created_at
    last_active_at: Optional[datetime] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    @field_serializer("created_at")
    def serialize_created_at(self, value: Optional[datetime], _info) -> Optional[str]:
        return to_utc_iso(value)

    @property
    def is_guest(self) -> bool:
        """訪客：oauth_provider 為 "guest" 的使用者列。"""
        return self.oauth_provider == GUEST_PROVIDER


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_session"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    # 只存 token 的 hash，不存原始 token（原始 token 只放在 httpOnly cookie）
    token_hash: str = Field(unique=True, index=True)
    expires_at: datetime
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

# 每位使用者每個台灣日期一列，count 為當日已扣次數；主鍵 (user_id, day)。
# 使用者刪除時一併刪除（ON DELETE CASCADE）；刪除對話不退次數。
class DailyUsage(SQLModel, table=True):
    __tablename__ = "daily_usage"
    user_id: uuid.UUID = Field(foreign_key="user.id", primary_key=True, ondelete="CASCADE")
    day: date = Field(primary_key=True)
    count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})


# 每個 (ip_hash, 台灣日期) 一列。guests_created：當日由這個 IP 建立的訪客數；messages：當日訪客送出的則數。
class GuestIpUsage(SQLModel, table=True):
    __tablename__ = "guest_ip_usage"
    ip_hash: str = Field(primary_key=True, max_length=64)
    day: date = Field(primary_key=True)
    guests_created: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    messages: int = Field(default=0, sa_column_kwargs={"server_default": "0"})


# 一位使用者最多一列；使用者刪除時一併刪除。key_encrypted 絕不回傳前端、不寫 log。
class OpenRouterCredential(SQLModel, table=True):
    __tablename__ = "openrouter_credential"
    user_id: uuid.UUID = Field(foreign_key="user.id", primary_key=True, ondelete="CASCADE")
    key_encrypted: str = Field(sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
