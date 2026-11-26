"""N1 知识中心/文档管理测试。

覆盖：
- fileparse：md/txt 直读、pdf/docx 解析、不支持类型与空文件报错
- 列级迁移 _ensure_columns：旧库 kb_space 补 category_id（幂等）
- 菜单迁移自愈：旧库「AI 知识库」改名 ApeCortex、独立词条管理菜单并入知识空间、
  知识中心菜单与默认分类 seed
- hub API：分类树、公开空间列表、词条列表的「未分类/分类/库」过滤语义
"""
import io
import json
import os
import sys

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.models import Menu
from src.plugins.builtin.knowledge_base.api_hub import (
    hub_pages,
    hub_spaces,
)
from src.plugins.builtin.knowledge_base.models import (
    KbCategory,
    KbSpace,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import fileparse
from src.plugins.builtin.knowledge_base.plugin import _ensure_columns
from src.plugins.builtin.knowledge_base.seed import seed_knowledge_base_data


# ---------------------------------------------------------------- fileparse

def test_fileparse_md_and_txt():
    title, content = fileparse.parse_file_to_text("部署手册.md", "# 标题\n\n正文内容".encode())
    assert title == "部署手册"
    assert content.startswith("# 标题")

    _, txt = fileparse.parse_file_to_text("notes.txt", "纯文本内容".encode())
    assert txt == "纯文本内容"


def test_fileparse_unsupported_and_empty():
    with pytest.raises(ValueError, match="暂不支持"):
        fileparse.parse_file_to_text("a.xlsx", b"PK")
    with pytest.raises(ValueError, match="文件为空"):
        fileparse.parse_file_to_text("a.md", b"")


def test_fileparse_pdf():
    pdf_bytes = _minimal_pdf_with_text("Hello ApeCortex Knowledge Hub")
    title, content = fileparse.parse_file_to_text("report.pdf", pdf_bytes)
    assert title == "report"
    assert "Hello ApeCortex Knowledge Hub" in content


def test_fileparse_docx_with_table():
    import docx

    document = docx.Document()
    document.add_heading("项目周报", 1)
    document.add_paragraph("本周完成知识中心开发。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "模块"
    table.cell(0, 1).text = "状态"
    table.cell(1, 0).text = "知识中心"
    table.cell(1, 1).text = "完成"
    buf = io.BytesIO()
    document.save(buf)

    title, content = fileparse.parse_file_to_text("周报.docx", buf.getvalue())
    assert title == "周报"
    assert "# 项目周报" in content
    assert "本周完成知识中心开发" in content
    assert "| 模块 | 状态 |" in content
    assert "| 知识中心 | 完成 |" in content


def _minimal_pdf_with_text(message: str) -> bytes:
    """构造最小合法 PDF（单页含文本），供 fileparse 回路测试。"""
    content = f"BT /F1 12 Tf 72 720 Td ({message}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_at = out.tell()
    out.write(b"xref\n0 6\n0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_at).encode() + b"\n%%EOF")
    return out.getvalue()


# ---------------------------------------------------------------- 列迁移

@pytest.mark.asyncio
async def test_ensure_columns_adds_category_id(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/col.db")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: c.execute(text(
            "CREATE TABLE kb_space ("
            "id INTEGER PRIMARY KEY, name VARCHAR(100), visibility VARCHAR(20),"
            "owner_user_id INTEGER, description TEXT, page_count INTEGER, status VARCHAR(20),"
            "created_at TIMESTAMP, updated_at TIMESTAMP)"
        )))
    async with engine.begin() as conn:
        await conn.run_sync(_ensure_columns)
    async with engine.connect() as conn:
        cols = await conn.run_sync(
            lambda c: [row[1] for row in c.execute(text("PRAGMA table_info(kb_space)"))]
        )
    await engine.dispose()
    assert "category_id" in cols


# ---------------------------------------------------------------- 菜单迁移自愈

@pytest.fixture
async def menu_env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/menu.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    yield factory
    await engine.dispose()


async def _old_menu_tree(db):
    """构造旧库菜单结构（P1 形态）：顶级「AI 知识库」+ 知识空间/词条管理/…"""
    top = Menu(name="AI 知识库", parent_id=0, type="M", path="/knowledge-base", icon="Collection", sort=40, visible=1, status=1)
    db.add(top)
    await db.flush()
    spaces = Menu(name="知识空间", parent_id=top.id, type="C", path="spaces",
                  component="knowledge_base/spaces/index", permission="knowledge_base:space:list", visible=1, status=1)
    pages = Menu(name="词条管理", parent_id=top.id, type="C", path="pages",
                 component="knowledge_base/pages/index", permission="knowledge_base:page:list", visible=1, status=1)
    db.add_all([spaces, pages])
    await db.flush()
    db.add_all([
        Menu(name="新建空间", parent_id=spaces.id, type="F", permission="knowledge_base:space:create", visible=1, status=1),
        Menu(name="发布词条", parent_id=pages.id, type="F", permission="knowledge_base:page:publish", visible=1, status=1),
        Menu(name="编辑词条", parent_id=pages.id, type="F", permission="knowledge_base:page:edit", visible=1, status=1),
    ])
    await db.commit()
    return top


@pytest.mark.asyncio
async def test_seed_migrates_old_menu_tree(menu_env):
    async with menu_env() as db:
        top = await _old_menu_tree(db)
        top_id = top.id
        await seed_knowledge_base_data(db)
        await db.commit()

        menus = list((await db.execute(select(Menu))).scalars().all())
        by_name = {m.name: m for m in menus}

        # 顶级目录改名 ApeCortex
        top_new = next(m for m in menus if m.id == top_id)
        assert top_new.name == "ApeCortex"

        # 知识中心（hub）成为第一个子菜单
        hub = by_name.get("知识中心")
        assert hub is not None and hub.parent_id == top_id and hub.path == "hub"
        assert hub.permission == "knowledge_base:hub:list"

        # 词条管理 C 菜单消失；其 F 按钮挂到知识空间下
        assert by_name.get("词条管理") is None
        spaces_menu = by_name["知识空间"]
        f_children = [m for m in menus if m.parent_id == spaces_menu.id and m.type == "F"]
        f_names = {m.name for m in f_children}
        assert {"发布词条", "编辑词条"} <= f_names
        assert {"上传文档", "删除文档", "编译文档"} <= f_names

        # 默认分类（仅首装 seed）
        cats = list((await db.execute(select(KbCategory))).scalars().all())
        assert {c.name for c in cats} == {"技术教程", "行业报告", "产品资料"}

    # 幂等：再跑一次不重复创建
    async with menu_env() as db:
        await seed_knowledge_base_data(db)
        await db.commit()
        menus = list((await db.execute(select(Menu))).scalars().all())
        hubs = [m for m in menus if m.path == "hub" and m.type == "C"]
        cats = list((await db.execute(select(KbCategory))).scalars().all())
        assert len(hubs) == 1 and len(cats) == 3


# ---------------------------------------------------------------- hub 浏览逻辑

@pytest.fixture
async def hub_env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/hub.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    yield factory
    await engine.dispose()


class _StubUser:
    id = 1


@pytest.mark.asyncio
async def test_hub_spaces_and_pages_filters(hub_env):
    async with hub_env() as db:
        cat = KbCategory(name="技术教程", sort=1)
        db.add(cat)
        await db.flush()
        pub_a = KbSpace(owner_user_id=1, name="教程库A", visibility="public", category_id=cat.id)
        pub_b = KbSpace(owner_user_id=1, name="教程库B", visibility="public")
        priv = KbSpace(owner_user_id=1, name="私有库", visibility="private")
        db.add_all([pub_a, pub_b, priv])
        await db.flush()
        db.add_all([
            KbWikiPage(space_id=pub_a.id, slug="a-live", title="A 公开词条",
                       body_md="正文" * 20, status="live", confidence=0.9, source_count=3),
            KbWikiPage(space_id=pub_a.id, slug="a-draft", title="A 草稿",
                       body_md="正文" * 20, status="draft", confidence=0.9, source_count=3),
            KbWikiPage(space_id=pub_b.id, slug="b-live", title="B 公开词条",
                       body_md="正文" * 20, status="live", confidence=0.9, source_count=3),
            KbWikiPage(space_id=priv.id, slug="p-live", title="私有词条",
                       body_md="正文" * 20, status="live", confidence=0.9, source_count=3),
        ])
        await db.commit()
        cat_id, a_id, b_id = cat.id, pub_a.id, pub_b.id

    # 公开库列表：仅 visibility=public 的两个；各带 live 词条数
    async with hub_env() as db:
        res = await hub_spaces(db=db, user=_StubUser(), _perm=None, category_id=None)
        items = res["data"]["items"]
        assert {s["name"] for s in items} == {"教程库A", "教程库B"}
        by_name = {s["name"]: s for s in items}
        assert by_name["教程库A"]["page_count"] == 1  # 草稿不计
        assert by_name["教程库B"]["page_count"] == 1

    # 按分类过滤：cat 下只有 教程库A
    async with hub_env() as db:
        res = await hub_spaces(db=db, user=_StubUser(), _perm=None, category_id=cat_id)
        assert {s["name"] for s in res["data"]["items"]} == {"教程库A"}

        # category_id=0 = 未分类 → 教程库B
        res0 = await hub_spaces(db=db, user=_StubUser(), _perm=None, category_id=0)
        assert {s["name"] for s in res0["data"]["items"]} == {"教程库B"}

        # hub_pages：默认只返回两个公开 live 词条
        res_pages = await hub_pages(db=db, user=_StubUser(), _perm=None, space_id=None, category_id=None, search="", page=1, page_size=20)
        titles = {p["title"] for p in res_pages["data"]["items"]}
        assert titles == {"A 公开词条", "B 公开词条"}

        # 分类过滤词条
        res_cat = await hub_pages(db=db, user=_StubUser(), _perm=None, space_id=None, category_id=cat_id, search="", page=1, page_size=20)
        assert {p["title"] for p in res_cat["data"]["items"]} == {"A 公开词条"}

        # 未分类
        res_uncat = await hub_pages(db=db, user=_StubUser(), _perm=None, space_id=None, category_id=0, search="", page=1, page_size=20)
        assert {p["title"] for p in res_uncat["data"]["items"]} == {"B 公开词条"}

        # 指定库
        res_lib = await hub_pages(db=db, user=_StubUser(), _perm=None, space_id=a_id, category_id=None, search="", page=1, page_size=20)
        assert {p["title"] for p in res_lib["data"]["items"]} == {"A 公开词条"}
