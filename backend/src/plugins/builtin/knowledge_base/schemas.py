"""Pydantic 请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryOut(BaseModel):
    """记忆条目响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    mem_type: str
    content: str
    confidence: float
    source_count: int
    access_count: int
    session_id: int | None = None
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProfileOut(BaseModel):
    """画像响应。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: int
    profile_md: str
    version: int
    updated_at: datetime


class StatsOut(BaseModel):
    """记忆统计响应。"""

    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    last_rewrite_at: datetime | None = None
