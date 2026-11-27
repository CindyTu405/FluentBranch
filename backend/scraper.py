import json
import time
import httpx
from datetime import datetime, timezone
from sqlmodel import Session
from models import FreeModelsCache

CACHE_TTL = 86400
MAX_MODELS = 30
OPENROUTER_API = "https://openrouter.ai/api/v1/models"

_memory_cache = None
_memory_cache_time = 0.0


def _as_utc(dt: datetime | None) -> datetime | None:
    """統一轉成帶 UTC 時區的 datetime。updated_at 是 naive（無時區），直接與 aware 時間相減會 TypeError。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # 資料庫存的就是 UTC，只是沒帶時區標記
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_free_pricing(pricing: dict) -> bool:
    """判斷是否真的免費。只看 prompt/completion 不夠（有些模型依產出計費，這兩欄是 0），所以檢查 pricing 所有欄位。"""
    if not pricing:
        return False

    for key, value in pricing.items():
        # overrides 是「超過某個 context 長度後的價格」，本身是 list，另外處理
        if key == "overrides":
            for rule in value or []:
                if not isinstance(rule, dict):
                    continue
                for sub_key, sub_value in rule.items():
                    if sub_key in ("min_prompt_tokens", "utc_start", "utc_end", "utc_days"):
                        continue
                    if _to_float(sub_value) != 0:
                        return False
            continue

        if _to_float(value) != 0:
            return False

    return True


def _to_float(value) -> float:
    """pricing 的值通常是字串 ("0", "0.0000002")，但也可能是數字，統一轉 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        # 無法解析的值視為「有收費」，保守處理，避免把付費模型當成免費
        return 1.0


def _is_chat_model(item: dict) -> bool:
    """只保留純文字對話模型（排除 output 為 audio 的生成模型）。"""
    arch = item.get("architecture") or {}
    output_modalities = arch.get("output_modalities") or []
    if output_modalities != ["text"]:
        return False

    input_modalities = arch.get("input_modalities") or []
    # 至少要能吃文字
    return "text" in input_modalities


FREE_ROUTER_ID = "openrouter/free"


def _is_free_model_id(model_id: str) -> bool:
    """以 ":free" 後綴判斷免費。定價為 0 不可靠（有付費模型也回報 0），所以不看價格。"""
    return model_id.endswith(":free") or model_id == FREE_ROUTER_ID


def _parse_models(api_data: dict) -> list[dict]:
    models = []
    for item in api_data.get("data", []):
        if not _is_free_model_id(item.get("id", "")):
            continue
        if not _is_free_pricing(item.get("pricing", {})):
            continue
        if not _is_chat_model(item):
            continue
        models.append({
            "id": item["id"],
            "name": item.get("name", item["id"]),
        })
    return models[:MAX_MODELS]


def _db_read(session: Session):
    try:
        row = session.get(FreeModelsCache, 1)
    except Exception as e:
        # 讀 cache 失敗不該讓整個 API 500，退回「重新爬一次」就好
        print(f"[scraper] 讀取 DB cache 失敗: {e}")
        return None

    if row is None:
        return None

    updated_at = _as_utc(row.updated_at)
    if updated_at is None:
        return None

    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if age < CACHE_TTL:
        try:
            return json.loads(row.models_json)
        except (TypeError, ValueError) as e:
            print(f"[scraper] cache 內容解析失敗: {e}")
            return None
    return None


def _db_write(session: Session, models: list[dict]):
    # 資料庫欄位是 TIMESTAMP WITHOUT TIME ZONE，存 naive UTC 最單純，
    # 讀取時再用 _as_utc() 補回時區，避免 naive / aware 混用。
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        row = session.get(FreeModelsCache, 1)
        if row is None:
            row = FreeModelsCache(id=1, models_json=json.dumps(models), updated_at=now_utc_naive)
        else:
            row.models_json = json.dumps(models)
            row.updated_at = now_utc_naive
        session.add(row)
        session.commit()
    except Exception as e:
        # 寫 cache 失敗不影響本次回傳結果
        print(f"[scraper] 寫入 DB cache 失敗: {e}")
        session.rollback()


def get_free_models(session: Session, force_refresh: bool = False) -> list[dict]:
    global _memory_cache, _memory_cache_time

    now = time.time()

    if not force_refresh:
        if _memory_cache is not None and now - _memory_cache_time < CACHE_TTL:
            return _memory_cache

        db_models = _db_read(session)
        if db_models is not None:
            _memory_cache = db_models
            _memory_cache_time = now
            return db_models

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(OPENROUTER_API)
            resp.raise_for_status()
            models = _parse_models(resp.json())
            _memory_cache = models
            _memory_cache_time = now
            _db_write(session, models)
            return models
    except Exception as e:
        print(f"[scraper] 爬取失敗: {e}")
        db_models = _db_read(session)
        if db_models is not None:
            return db_models
        if _memory_cache is not None:
            return _memory_cache
        return []
