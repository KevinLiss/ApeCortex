"""AI 知识库插件——数据模型（P0：记忆层）。

.. important::
   - 模型继承 ``PluginBase``（插件专属 MetaData，与系统表隔离）。
   - ``install()`` 时 ``create_all`` 建表；``uninstall()`` 时 ``drop_all`` 删表。

设计依据（开源对标精读结论，见 research/notes/代码精读调研笔记.md）：
- ``KbMemoryItem``：mem0 的 ADD-only 提取 + MD5 去重（mem0/memory/main.py V3 管线）
  外加 graphiti 的双时序字段（graphiti_core/edges.py:228 valid_at/invalid_at/expired_at），
  旧事实不删只打标，画像随时间演化且全程可审计。
- ``KbProfile``：EverOS 的"画像单文件重写"（episode 粗存 + profile 整体重写双粒度）。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mixins import IDMixin, TimestampMixin
from src.plugins.base import make_plugin_base

PluginBase = make_plugin_base("knowledge_base")

# 记忆类型（对齐调研结论的四种分型；habit 靠频次聚合产生）
MEMORY_TYPES = ("fact", "preference", "habit", "event")


class KbMemoryItem(IDMixin, TimestampMixin, PluginBase):
    """用户记忆条目——一条自包含的知识陈述。

    表名约定：``kb_`` 前缀 + 资源名。
    """

    __tablename__ = "kb_memory_item"

    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="所属用户ID（行级隔离键）"
    )
    mem_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fact",
        comment="记忆类型: fact=事实 preference=偏好 habit=习惯 event=事件",
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="自包含记忆陈述（15~80字）"
    )
    content_hash: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="归一化内容的MD5（去重键）"
    )
    # 置信度与证据（去重命中即强化——mem0 哲学：重复出现=证据+1）
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.6, comment="置信度0-1，多来源加权"
    )
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="证据次数（同事实重复出现计数）"
    )
    access_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="召回计数（淘汰依据）"
    )
    # 双时序（graphiti 范式）：事实有效轴 + 系统事务轴，旧事实不删只打标
    valid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="事实开始生效时间"
    )
    invalid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="事实停止为真时间（软失效）"
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统标记失效时间（淘汰）"
    )
    # 溯源
    session_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="首次提取自的聊天会话ID"
    )
    source_run_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="首次提取自的 ChatRun ID"
    )

    def __repr__(self) -> str:
        return f"<KbMemoryItem {self.id} user={self.user_id} type={self.mem_type}>"


class KbProfile(IDMixin, TimestampMixin, PluginBase):
    """用户画像——Markdown 整体重写式（EverOS 双粒度中的 profile 层）。

    每次重写全量生成而非追加，避免碎片累积；``version`` 随重写递增。
    """

    __tablename__ = "kb_profile"

    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True, index=True, comment="所属用户ID"
    )
    profile_md: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="画像Markdown（整体重写式）"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="重写版本号"
    )

    def __repr__(self) -> str:
        return f"<KbProfile user={self.user_id} v{self.version}>"
