# ApeCortex v0.4.0 · 底座 v2.12.0 发布说明

> 周期一（M1–M5）交付。安装流程见 [安装与升级](安装与升级.md)，规划见 [周期一开发计划](ApeCortex-周期一开发计划.md)。

## 亮点

| 能力 | 说明 |
|---|---|
| **质量与信任（M1）** | 检索零命中不再返回兜底垃圾，结果带 `relevance`；记忆提取拒绝"工具报错/系统状态"类噪声；前端错误可读化（404 → 后端未加载最新代码） |
| **品牌与分发（M2）** | 统一 ApeCortex 品牌；安装/升级/故障排查文档；`knowledge_base-0.4.0.zip` 可在干净环境安装 |
| **团队协作（M3）** | 共享空间（reader/writer 成员）、三通道召回（私有/共享/公开）、公开库只读出口（`X-Kb-Key`，限流 10/分钟）、晋升"待发布"队列、自审策略可配置 |
| **检索与运营（M4）** | 标题权重 2× + 同义词扩展；记忆演化（旧事实 `invalid_at` 软失效）；贡献榜按周/月/全部 |
| **硬化（M5）** | 宿主 zip 解壳判定修复（`zip -r kb.zip knowledge_base` 现可直接安装）；任务日限额护栏 |

## 升级须知

1. 插件版本 `0.3.x → 0.4.0` 可直接通过插件管理上传新包覆盖（旧文件自动备份到 `_backups`）
2. 新增两张表：`kb_space_member`、`kb_public_key`（插件启动时幂等建表，无需手工迁移）
3. `kb_space.visibility` 新增 `shared` 取值（老数据不受影响）
4. 普通角色使用知识库 MCP 工具需 `mcp:tools:call`（宿主总开关）
5. 若要限制每日任务量：在 `kb_setting` 写入 `daily_job_limit`（user_id=0，0=不限）

## 行为变更（需要留意）

- **检索更严格**：完全不相关的查询现在返回空，而不是"最接近的一条"。这是刻意的：宁可空，不给错。
- **记忆会演化**：同主体不同值的旧事实会被标记失效（不删除，可在画像中看到历史脉络）。
- **晋升后需发布**：批准结果仍是 `draft`，公开空间 owner 在「晋升审核 → 待发布」一键发布为 `live`。

## 测试基线

- 后端：全量 **83 passed**（含插件 43 条、zip 结构 3 条）
- 前端：`vue-tsc --noEmit` + `vite build` 通过
- 已知 flaky（与本周期改动无关）：`tests/test_chat_runs.py::test_different_sessions_run_in_parallel` 单独运行时偶发失败，全量运行时通过

## 分发包

- `knowledge_base-0.4.0.zip`（zip 根含 `plugin.json` + `knowledge_base/`）
- 校验：`_validate_plugin_zip` 通过 → `import_plugin` 解压成功 → 落盘 manifest `0.4.0 / ApeCortex`
- 升级实测：`0.2.0 → 0.4.0` 覆盖安装，目录与清单正确

## 下一步

见周期一计划"下一周期候选"：embedding 混合检索（D1 待定）、公开库运营增强（举报/下线工作流）、记忆治理面板、多租户/组织模型。