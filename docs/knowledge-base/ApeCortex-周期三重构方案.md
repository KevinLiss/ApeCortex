# ApeCortex 周期三：知识中心唯一化重构方案

> 2026-11-26 用户五点反馈驱动。前置：周期二 N1–N3（v0.5.0–v0.7.0）。
> 参照 Dify 的知识库交互（标签筛选、无类型概念、按需挂载）。

## 用户拍板决策（2026-11-26）

1. **空间无类型概念**：所有空间都是个人知识库；共享空间/成员管理（M3）**彻底移除**
2. **入公库统一为一条推送链**：词条晋升流（PR 式、门槛、贡献榜）**移除**，词条与文档都走「推送知识中心」
3. **知识中心 = 唯一大公共知识库**：不再有多个公开库；「按知识库」视图移除
4. **AI 甄选自动入库**：推送 → AI 分类（可自主创建新分类）+ 违规初筛 → 无违规**直接入库**并留痕可复查（无审批环节）；判违规才进人工处置队列
5. **聊天页知识库弹窗**：可启用知识中心（公共库）+ 多选自己的空间
6. **下载修复**：上传文件保存原始二进制，下载优先给原文件（PDF 下载回来还是排版完好的 PDF）；文本型文档给 .md

## 数据模型变化

| 表/字段 | 变化 |
|---|---|
| kb_space | visibility 语义废弃（列保留不删）；不再有 shared/public 空间 |
| kb_space_member | **删除**（M3 下线） |
| kb_promotion | **删除**（晋升流下线） |
| kb_public_submission | target_space_id 语义废弃；status: ai_reviewing → approved(自动)/violation(人工处置)；新增 category_name（AI 可建新分类） |
| kb_doc_source | file_data：上传原始文件二进制存文件系统 uploads/kb_docs/，下载优先原文件 |
| chat_prefs | scope/space_id → enabled_spaces(list) + include_hub + memory_enabled + auto_inject |

## 推送流（新）

```
文档/词条 → 推送知识中心
   → AI 甄选（1 次 LLM：分类（可新建）+ 违规判定 + 拆解预览）
   ├─ 无违规 → 自动入库知识中心（快照→编译拆词条）→ status=approved（留痕，可复查登记）
   └─ 判违规 → violation 队列 → 人工处置（保留存证/清理）
```

「审核中心」→「推送中心」：Tab = 推送记录（全状态留痕 + 复查登记）/ 违规处置。
自审制衡随之消失（没有审批动作）。

## 实施批次

- **C1 后端**：下载原文件、空间去类型、共享/晋升删除、知识中心唯一化、AI 自动入库+建分类、chat_prefs 多选模型、seed 菜单
- **C2 前端**：spaces 标签筛选、hub 重构、推送中心、聊天页弹窗多选
- **C3 收尾**：测试重写（权限矩阵大改）、文档、版本 0.8.0

## 实施进度（2026-11-26 完成）

C1/C2/C3 全部落地，版本 0.7.0 → **0.8.0**。

- **C1 后端**：`pipeline/hub.py`（hub 单例 + AI 建分类）；`submission.py` 重写
  （推送统一入口 `submit_public_content`，AI 甄选 → 无违规 `_auto_approve` 自动入库 →
  编译拆词条挂分类；violation 队列；`register_check` 复查登记；`dispose_violation` 处置；
  CAS 原子认领防并发双跑）；`retrieval.py` 双通道（个人 owner 硬过滤 + hub live-only，
  `space_ids`/`include_hub` 过滤参数）；`compiler.py` hub 词条直通 live + 挂 category_id；
  `api_wiki.py` 收敛 owner-only；`api_hub.py` 只留分类 CRUD + hub_pages；`api_push.py`
  重写（submit/submissions/check/dispose）；`api_public.py` 出口对齐 hub 单例；
  `api_docs.py` 原始文件下载（`_raw_dir` + 扩展名 MIME）；`chat_prefs.py` 启用集合模型
  四键；`models.py` 删 `KbPromotion`/`KbSpaceMember`、加 `category_id` 等四列；
  `seed.py` 周期三菜单结构 + 遗留按钮自愈清理；`plugin.py` 列迁移清单。
- **C2 前端**：spaces 卡片网格 + 标签筛选（Dify 式）；hub 分类浏览（无「按知识库」视图）；
  推送中心两 Tab；聊天页偏好弹窗（知识中心开关 + 空间多选勾选模式 + 记忆/自动注入）；
  i18n 全量更新（含 `pushFlowTitle`/`pushPickHint`/`common.action.label` 补齐）。
- **C3 测试**：`test_knowledge_hub_n1.py`（10）、`test_permission_matrix.py` 重写为周期三
  基线（14）、`test_public_submission_n2.py` 重写为自动入库流（10）、`test_chat_prefs_n3.py`
  重写为集合模型（9）、`test_knowledge_base_plugin.py` 去晋升流改 hub 语义（40）。
  全量 **123 passed / ruff 清零 / vue-tsc 通过 / vite build 通过**。
- **dev 实测**（Playwright 端到端）：登录 → 空间卡片/筛选 → 文档 Tab（下载/推送按钮）→
  推送弹窗 → AI 甄选真实跑通（判无违规自动入库 → hub 6 词条 + 分类「技术教程」）→
  知识中心分类浏览 → 推送中心留痕复查登记落库 → 聊天页多选偏好保存（`chat_enabled_spaces=[1]`）。
- **实测中修复的产品缺陷**：① Map 编译 max_tokens 1800 截断导致 0 词条（提额至 4000 +
  `_parse_pages` 截断宽容解析）；② `api_wiki` 四处 `get_space_role` 未导入（运行时
  NameError）；③ `analyze_submission` check-then-act 竞态（CAS 认领）；④ 聊天页
  `kbSpacesAll` readonly computed 被 v-model 写入报错（改 `:model-value` + 勾选模式先全选）；
  ⑤ 公开出口按废弃 visibility 过滤（改 hub 单例过滤）。
