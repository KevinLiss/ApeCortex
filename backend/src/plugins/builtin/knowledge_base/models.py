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

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
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


# ==========================================================================
# P1：Wiki 编译层 + 公开库晋升（WeKnora 模型直译 + PandaWiki 三态 + PR 式晋升）
# ==========================================================================

# 空间可见性
SPACE_VISIBILITY = ("private", "public")  # shared 留 P3（成员 ACL）
# 词条状态：candidate 候选（晋升门槛未满足）→ draft 草稿 → live 已发布 → archived
PAGE_STATUS = ("candidate", "draft", "live", "archived")
# 晋升申请状态
PROMOTION_STATUS = ("draft", "submitted", "approved", "rejected", "withdrawn")
# 编辑来源（WeKnora LastEditSource 四来源）
EDIT_SOURCES = ("pipeline", "agent", "user", "revert")


class KbSpace(IDMixin, TimestampMixin, PluginBase):
    """知识库空间（P1）。

    - private 空间：owner 可读写，AI 召回仅限 owner（行级隔离与记忆层同纪律）
    - public 空间：全员可读（AI 双通道召回），写入仅限晋升流或空间维护者（API 硬校验）
    - owner_user_id 语义自 user_id（规划 10.2 节）
    """

    __tablename__ = "kb_space"

    owner_user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="空间所有者（private 时唯一读写者）"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="空间名")
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="空间描述"
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default="private", index=True,
        comment="可见性: private=私有 public=公开（shared 留 P3）",
    )
    page_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="词条计数（冗余，展示用）"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", comment="active/archived"
    )

    def __repr__(self) -> str:
        return f"<KbSpace {self.id} owner={self.owner_user_id} vis={self.visibility}>"


class KbDocSource(IDMixin, TimestampMixin, PluginBase):
    """原始资料层（不可变——唯一证据源，WeKnora 原始层纪律）。

    内容写入后不再修改；content_hash（blake2b）用于编译增量失效判定
    （llmwiki-suite 三向比对思路：mtime+size+hash）。
    """

    __tablename__ = "kb_doc_source"

    space_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="所属空间"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="资料标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="原始内容（Markdown/纯文本）")
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="blake2b 内容哈希（增量失效判定）"
    )
    url: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, comment="来源 URL（可空=本地/聊天产出）"
    )
    source_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="upload",
        comment="来源: upload/chat/research/web/paper",
    )
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="字符数")
    compiled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次参与编译时间"
    )

    def __repr__(self) -> str:
        return f"<KbDocSource {self.id} space={self.space_id} {self.title}>"


class KbWikiPage(IDMixin, TimestampMixin, PluginBase):
    """Wiki 词条（WeKnora wiki_page 模型直译，含公开库溯源字段）。

    - slug 空间内唯一；in/out_links 双向链接；source_refs 来源引用
    - version 乐观锁：用户可见内容变更才 +1（编辑冲突返回 409）
    - last_edit_source 四来源：pipeline/agent/user/revert
    - status: candidate（晋升门槛未满足）→ draft → live → archived
    - origin_* 晋升溯源：公开库副本指回私库原页与原作者（PR 合并心智）
    """

    __tablename__ = "kb_wiki_page"

    space_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="所属空间"
    )
    slug: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="URL 友好标识（空间内唯一）"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="词条标题")
    body_md: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="正文 Markdown")
    in_links: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="入链 slug 数组(JSON)")
    out_links: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="出链 slug 数组(JSON)")
    source_refs: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", comment="来源引用 JSON: [{doc_id,url,title}]"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="乐观锁版本（可见内容变更+1）"
    )
    last_edit_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pipeline",
        comment="当前版本作者: pipeline/agent/user/revert",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="candidate", index=True,
        comment="candidate/draft/live/archived",
    )
    # 晋升门槛数据（swarmvault 六参数中可计算的两项 + 门槛留 settings）
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, comment="编译置信度（来源数/一致性加权）"
    )
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="支撑该词条的原始资料数"
    )
    origin_space_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="晋升来源空间（公开库副本指回私库）"
    )
    origin_page_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="晋升来源词条页"
    )
    origin_author_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="原作者署名（晋升副本保留）"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True, comment="软删除"
    )

    def __repr__(self) -> str:
        return f"<KbWikiPage {self.id} space={self.space_id} {self.slug} v{self.version}>"


class KbWikiRevision(IDMixin, TimestampMixin, PluginBase):
    """词条修订历史（WeKnora 两级保留：机器快照限 50/硬顶 200，保护人工编辑）。"""

    __tablename__ = "kb_wiki_revision"

    page_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="词条ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="页版本号")
    body_md: Mapped[str] = mapped_column(Text, nullable=False, comment="正文快照")
    edit_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pipeline", comment="编辑来源"
    )
    editor_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="编辑者（pipeline 时为空）")
    is_machine_snapshot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="机器快照（裁剪对象）"
    )
    note: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", comment="变更说明"
    )

    def __repr__(self) -> str:
        return f"<KbWikiRevision page={self.page_id} v{self.version}>"


class KbPromotion(IDMixin, TimestampMixin, PluginBase):
    """晋升申请单（PR 心智模型，规划 10.3 节）。

    提交时冻结内容快照 snapshot_md——审核期间私库继续编辑不影响审核对象；
    批准后在公开库生成独立副本（published_page_id），私库原件不动（双轨）。
    """

    __tablename__ = "kb_promotion"

    page_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="私库源词条ID"
    )
    source_space_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="源空间")
    target_space_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="目标公开空间"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", index=True,
        comment="draft/submitted/approved/rejected/withdrawn",
    )
    snapshot_md: Mapped[str] = mapped_column(Text, nullable=False, comment="提交时正文快照（冻结）")
    snapshot_slug: Mapped[str] = mapped_column(String(200), nullable=False, comment="快照 slug")
    snapshot_title: Mapped[str] = mapped_column(String(200), nullable=False, comment="快照标题")
    submit_note: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="提交说明"
    )
    submitted_by: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="提交人（=源页 owner）"
    )
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="审核人")
    review_comment: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="审核意见"
    )
    published_page_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="批准后公开库生成的页ID"
    )
    id_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="内容寻址哈希（同内容重复提交合并）"
    )

    def __repr__(self) -> str:
        return f"<KbPromotion {self.id} page={self.page_id} {self.status}>"


class KbJob(IDMixin, TimestampMixin, PluginBase):
    """任务中枢（仿宿主 ChatRun 模式：状态机 + 增量结果落库）。

    job_type: wiki_ingest（文档编译）/ promotion_publish / memory_extract / research / collect
    """

    __tablename__ = "kb_job"

    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="发起用户")
    space_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="关联空间（可空）"
    )
    job_type: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True, comment="任务类型"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True,
        comment="pending/running/done/failed/cancelled",
    )
    payload: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", comment="任务参数(JSON)"
    )
    result: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", comment="任务结果(JSON，增量更新)"
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="进度0-100")
    step: Mapped[str] = mapped_column(
        String(60), nullable=False, default="", comment="当前步骤名"
    )
    llm_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="LLM 调用计数（成本台账）"
    )
    token_usage: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", comment="token 用量 JSON {in,out}"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )

    def __repr__(self) -> str:
        return f"<KbJob {self.id} {self.job_type} {self.status}>"


class KbSetting(IDMixin, TimestampMixin, PluginBase):
    """插件设置（键值对，settings 页读写；ome.toml 思想——策略可整体关停）。"""

    __tablename__ = "kb_setting"

    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True, comment="所属用户（0=全局默认）"
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False, comment="配置键")
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="配置值(JSON)")

    def __repr__(self) -> str:
        return f"<KbSetting {self.key}={self.value[:20]}>"
