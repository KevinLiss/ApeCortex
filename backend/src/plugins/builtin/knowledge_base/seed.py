"""插件 seed——菜单 + 权限注册（install 时调用，幂等）。

.. important:: 菜单注册约定
   插件管理器通过以下规则识别插件拥有的菜单：
   - ``permission`` 以 ``knowledge_base:`` 开头
   - ``component`` 以 ``knowledge_base/`` 开头
   - ``path`` 以 ``/knowledge-base`` 开头（连字符版本）
   禁用插件时这些菜单 ``status = 0``（隐藏）。

.. note:: 菜单 i18n
   直接在 Menu 行上写 ``i18n_key``（前端 menu.json 按 key 翻译，无则回退中文名），
   实现全站中英双语，无需改动宿主 seed。
"""
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Menu, Role
from src.models.rbac import role_menu

_PLUGIN_NAME = "knowledge_base"


async def seed_knowledge_base_data(db: AsyncSession) -> None:
    """Seed 菜单 + 权限 + 绑定默认角色。幂等。"""
    # ── 1. 顶级目录 ─────────────────────────────────────
    result = await db.execute(
        select(Menu).where(Menu.path == "/knowledge-base", Menu.parent_id == 0)
    )
    parent = result.scalars().first()
    if not parent:
        parent = Menu(
            name="ApeCortex",
            parent_id=0,
            type="M",
            path="/knowledge-base",
            component=None,
            permission=None,
            icon="Collection",
            sort=40,
            visible=1,
            status=1,
            i18n_key="menu.kb.label",
        )
        db.add(parent)
        await db.flush()
        logger.info("Created 'ApeCortex' top-level menu")
    elif parent.name != "ApeCortex":
        # N1 迁移：旧库存量顶级目录（如"AI 知识库"）改名 ApeCortex
        parent.name = "ApeCortex"
        parent.i18n_key = "menu.kb.label"
        parent.icon = "Collection"
        await db.flush()
        logger.info(f"Migrated top-level menu name: {parent.name!r} → 'ApeCortex'")

    # ── 1.5 N1 菜单迁移：独立"词条管理"菜单并入"知识空间"──────────
    # 旧库已存在 path=pages 的 C 菜单 → 删除该菜单行，其下 F 按钮（发布词条/编辑词条）
    # 挂到"知识空间"菜单下；后续 menu_specs 会幂等补齐新结构。
    old_pages_menu = next(
        (m for m in (await db.execute(
            select(Menu).where(
                Menu.parent_id == parent.id, Menu.path == "pages", Menu.type == "C"
            )
        )).scalars().all()),
        None,
    )
    if old_pages_menu is not None:
        spaces_menu = next(
            (m for m in (await db.execute(
                select(Menu).where(
                    Menu.parent_id == parent.id, Menu.path == "spaces", Menu.type == "C"
                )
            )).scalars().all()),
            None,
        )
        children = list((await db.execute(
            select(Menu).where(Menu.parent_id == old_pages_menu.id)
        )).scalars().all())
        for child in children:
            if spaces_menu is not None:
                child.parent_id = spaces_menu.id
            else:
                # 知识空间菜单尚未 seed：先临时挂顶级目录，specs 阶段再归位
                child.parent_id = parent.id
        await db.flush()
        await db.delete(old_pages_menu)
        await db.flush()
        logger.info("Migrated: '词条管理' menu merged into '知识空间'")

    # ── 1.6 菜单迁移：promotions 菜单统一改名「推送中心」（周期三）──────────
    review_menu = next(
        (m for m in (await db.execute(
            select(Menu).where(
                Menu.parent_id == parent.id, Menu.path == "promotions", Menu.type == "C"
            )
        )).scalars().all()),
        None,
    )
    if review_menu is not None and review_menu.name != "推送中心":
        old_name = review_menu.name
        review_menu.name = "推送中心"
        review_menu.permission = "knowledge_base:submission:review"
        await db.flush()
        logger.info(f"Migrated: {old_name!r} menu renamed to '推送中心'")

    # ── 1.7 菜单迁移：删共享/晋升遗留按钮（周期三下线功能）──────────
    dead_perms = (
        "knowledge_base:promotion:submit",
        "knowledge_base:promotion:review",
        "knowledge_base:page:publish",
    )
    stale = list((await db.execute(
        select(Menu).where(Menu.permission.in_(dead_perms))
    )).scalars().all())
    for m in stale:
        await db.delete(m)
    if stale:
        await db.flush()
        logger.info(f"Removed {len(stale)} stale menu buttons (promotion/publish)")

    # ── 2. 子菜单 + 按钮权限（周期三结构）─────────
    # (name, parent_name, type, path, component, permission, icon, sort, i18n_key)
    menu_specs = [
        ("知识中心", None, "C", "hub", "knowledge_base/hub/index",
         "knowledge_base:hub:list", "Reading", 1, "menu.kb.hub"),
        ("分类管理", "知识中心", "F", None, None, "knowledge_base:category:manage", None, 1, None),
        ("知识空间", None, "C", "spaces", "knowledge_base/spaces/index",
         "knowledge_base:space:list", "Folder", 2, "menu.kb.spaces"),
        ("新建空间", "知识空间", "F", None, None, "knowledge_base:space:create", None, 1, None),
        ("编辑空间", "知识空间", "F", None, None, "knowledge_base:space:update", None, 2, None),
        ("删除空间", "知识空间", "F", None, None, "knowledge_base:space:delete", None, 3, None),
        ("上传文档", "知识空间", "F", None, None, "knowledge_base:doc:upload", None, 4, None),
        ("删除文档", "知识空间", "F", None, None, "knowledge_base:doc:delete", None, 5, None),
        ("编辑词条", "知识空间", "F", None, None, "knowledge_base:page:edit", None, 6, None),
        ("编译文档", "知识空间", "F", None, None, "knowledge_base:doc:compile", None, 7, None),
        ("推送知识中心", "知识空间", "F", None, None, "knowledge_base:submission:submit", None, 8, None),
        ("推送中心", None, "C", "promotions", "knowledge_base/promotions/index",
         "knowledge_base:submission:review", "Stamp", 3, "menu.kb.promotions"),
        ("推送留痕", "推送中心", "F", None, None, "knowledge_base:submission:review", None, 1, None),
        ("处置违规", "推送中心", "F", None, None, "knowledge_base:submission:dispose", None, 2, None),
        ("任务中心", None, "C", "tasks", "knowledge_base/tasks/index",
         "knowledge_base:task:list", "Timer", 4, "menu.kb.tasks"),
        ("重试任务", "任务中心", "F", None, None, "knowledge_base:task:retry", None, 1, None),
        ("记忆画像", None, "C", "profile", "knowledge_base/profile/index",
         "knowledge_base:profile:list", "User", 5, "menu.kb.profile"),
        ("查询记忆", "记忆画像", "F", None, None, "knowledge_base:profile:list", None, 1, None),
        ("删除记忆", "记忆画像", "F", None, None, "knowledge_base:profile:delete", None, 2, None),
        ("重写画像", "记忆画像", "F", None, None, "knowledge_base:profile:rewrite", None, 3, None),
        ("插件设置", None, "C", "settings", "knowledge_base/settings/index",
         "knowledge_base:setting:read", "Setting", 6, "menu.kb.settings"),
        ("保存设置", "插件设置", "F", None, None, "knowledge_base:setting:update", None, 1, None),
    ]

    existing = list((await db.execute(select(Menu))).scalars().all())

    # ── 2.1 排序自愈：存量 C 菜单的 sort 强制对齐 specs（旧库只建不改导致同 sort
    #     按 id 排序，如记忆画像/知识空间/知识中心三条同为 1——知识中心被挤后位）
    c_sort_expected = {
        spec[3]: spec[7]
        for spec in menu_specs
        if spec[2] == "C"
    }
    c_children = [m for m in existing if m.parent_id == parent.id and m.type == "C"]
    fixed_sorts: list[str] = []
    for m in c_children:
        expected = c_sort_expected.get(m.path)
        if expected is not None and m.sort != expected:
            m.sort = expected
            fixed_sorts.append(f"{m.name}→{expected}")
    if fixed_sorts:
        await db.flush()
        logger.info(f"Resorted C menus: {', '.join(fixed_sorts)}")

    created_menus: list[Menu] = []
    for name, parent_name, mtype, path, component, permission, icon, sort, i18n_key in menu_specs:
        target_name = parent_name or parent.name  # None = 挂到顶级目录下（品牌名随库而定）
        parent_menu = next(
            (m for m in existing + created_menus if m.name == target_name), None
        )
        if not parent_menu:
            logger.warning(f"Skip menu '{name}': parent '{parent_name}' not found")
            continue

        dup = any(m.name == name and m.parent_id == parent_menu.id for m in existing)
        if dup:
            continue

        menu = Menu(
            name=name,
            parent_id=parent_menu.id,
            type=mtype,
            path=path,
            component=component,
            permission=permission,
            icon=icon,
            sort=sort,
            visible=1,
            status=1,
            i18n_key=i18n_key,
        )
        db.add(menu)
        await db.flush()
        existing.append(menu)
        created_menus.append(menu)

    if created_menus:
        logger.info(f"Created {len(created_menus)} knowledge_base menus")

    # ── 2.5 默认分类（N1 知识中心开箱体验：空库预置三个一级分类）──
    from src.plugins.builtin.knowledge_base.models import KbCategory

    cat_count = (await db.execute(
        select(KbCategory).limit(1)
    )).scalars().first()
    if cat_count is None:
        for name, icon, sort in (
            ("技术教程", "Monitor", 1),
            ("行业报告", "DataAnalysis", 2),
            ("产品资料", "Goods", 3),
        ):
            db.add(KbCategory(name=name, icon=icon, sort=sort))
        await db.flush()
        logger.info("Seeded 3 default knowledge hub categories")

    # ── 3. 绑定到 admin 角色（幂等）──────────────────
    admin_result = await db.execute(select(Role).where(Role.code == "admin"))
    admin_role = admin_result.scalars().first()
    if admin_role and created_menus:
        bound_ids = {m.id for m in admin_role.menus}
        new_bindings = [
            {"role_id": admin_role.id, "menu_id": menu.id}
            for menu in created_menus
            if menu.id not in bound_ids
        ]
        if new_bindings:
            from sqlalchemy import insert
            await db.execute(insert(role_menu), new_bindings)
            await db.flush()
            logger.info(f"Bound {len(new_bindings)} knowledge_base menus to admin role")

