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

    # ── 2. 子菜单 + 按钮权限（P1 扩容）─────────────────
    # (name, parent_name, type, path, component, permission, icon, sort, i18n_key)
    menu_specs = [
        ("知识空间", None, "C", "spaces", "knowledge_base/spaces/index",
         "knowledge_base:space:list", "Folder", 1, "menu.kb.spaces"),
        ("新建空间", "知识空间", "F", None, None, "knowledge_base:space:create", None, 1, None),
        ("词条管理", None, "C", "pages", "knowledge_base/pages/index",
         "knowledge_base:page:list", "Document", 2, "menu.kb.pages"),
        ("发布词条", "词条管理", "F", None, None, "knowledge_base:page:publish", None, 1, None),
        ("编辑词条", "词条管理", "F", None, None, "knowledge_base:page:edit", None, 2, None),
        ("晋升审核", None, "C", "promotions", "knowledge_base/promotions/index",
         "knowledge_base:promotion:review", "Stamp", 3, "menu.kb.promotions"),
        ("提交晋升", "晋升审核", "F", None, None, "knowledge_base:promotion:submit", None, 1, None),
        ("审核晋升", "晋升审核", "F", None, None, "knowledge_base:promotion:review", None, 2, None),
        ("任务中心", None, "C", "tasks", "knowledge_base/tasks/index",
         "knowledge_base:task:list", "Timer", 4, "menu.kb.tasks"),
        ("重试任务", "任务中心", "F", None, None, "knowledge_base:task:retry", None, 1, None),
        ("插件设置", None, "C", "settings", "knowledge_base/settings/index",
         "knowledge_base:setting:read", "Setting", 6, "menu.kb.settings"),
        ("保存设置", "插件设置", "F", None, None, "knowledge_base:setting:update", None, 1, None),
        ("记忆画像", None, "C", "profile", "knowledge_base/profile/index",
         "knowledge_base:profile:list", "User", 5, "menu.kb.profile"),
        ("查询记忆", "记忆画像", "F", None, None, "knowledge_base:profile:list", None, 1, None),
        ("删除记忆", "记忆画像", "F", None, None, "knowledge_base:profile:delete", None, 2, None),
        ("重写画像", "记忆画像", "F", None, None, "knowledge_base:profile:rewrite", None, 3, None),
    ]

    existing = list((await db.execute(select(Menu))).scalars().all())

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

