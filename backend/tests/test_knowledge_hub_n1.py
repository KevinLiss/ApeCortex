"""知识中心（周期三）测试：hub 唯一空间、分类树、词条浏览过滤、列迁移、菜单自愈。

覆盖：
- get_hub_space_id 幂等（同名同 owner=0 唯一）
- 分类树含词条计数；未分类过滤；搜索过滤
- 列级迁移 _ensure_columns（kb_space.category_id / kb_doc_source.file_ext / kb_wiki_page.category_id）
- 菜单迁移自愈：顶级改名 ApeCortex、推送中心改名、遗留按钮清理
- fileparse：md/txt 直读、pdf/docx 解析、不支持类型与空文件报错
"""
import io

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.models import Menu
from src.plugins.builtin.knowledge_base.api_hub import hub_pages
from src.plugins.builtin.knowledge_base.models import (
    KbCategory,
    KbDocSource,
    KbSpace,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import fileparse
from src.plugins.builtin.knowledge_base.pipeline.hub import (
    ai_ensure_category,
    get_hub_space_id,
    reset_hub_cache,
)
from src.plugins.builtin.knowledge_base.plugin import _ensure_columns
from src.plugins.builtin.knowledge_base.seed import seed_knowledge_base_data

# ---------------------------------------------------------------- fixtures

@pytest.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/hub.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    import src.plugins.builtin.knowledge_base.pipeline.hub as hub_mod
    monkeypatch.setattr(hub_mod, "SessionLocal", factory)
    reset_hub_cache()
    yield factory
    reset_hub_cache()
    await engine.dispose()


class _StubUser:
    id = 1


# ---------------------------------------------------------------- hub 空间与 AI 建分类

@pytest.mark.asyncio
async def test_hub_space_singleton(env):
    sid1 = await get_hub_space_id()
    sid2 = await get_hub_space_id()
    assert sid1 == sid2
    async with env() as db:
        hubs = list((await db.execute(
            select(KbSpace).where(KbSpace.owner_user_id == 0)
        )).scalars().all())
        assert len(hubs) == 1 and hubs[0].name.startswith("ApeCortex 知识中心")


@pytest.mark.asyncio
async def test_ai_ensure_category_dedup(env):
    c1 = await ai_ensure_category("AI 工程")
    c2 = await ai_ensure_category("AI 工程")  # 同名幂等
    assert c1 == c2
    c3 = await ai_ensure_category("")  # 空名拒绝
    assert c3 is None
    async with env() as db:
        cats = list((await db.execute(select(KbCategory))).scalars().all())
        assert [c.name for c in cats] == ["AI 工程"]


# ---------------------------------------------------------------- 词条浏览过滤

@pytest.mark.asyncio
async def test_hub_pages_filters(env):
    hub_id = await get_hub_space_id()
    async with env() as db:
        cat = KbCategory(name="技术教程", sort=1)
        db.add(cat)
        await db.flush()
        priv = KbSpace(owner_user_id=1, name="我的库", visibility="private")
        db.add(priv)
        await db.flush()
        db.add_all([
            KbWikiPage(space_id=hub_id, category_id=cat.id, slug="h-cat", title="中心内分类词条",
                       body_md="正文" * 20, status="live", confidence=0.9, source_count=3),
            KbWikiPage(space_id=hub_id, slug="h-uncat", title="中心内未分类词条",
                       body_md="正文" * 20, status="live", confidence=0.9, source_count=3),
            KbWikiPage(space_id=hub_id, slug="h-draft", title="中心草稿不可见",
                       body_md="正文" * 20, status="draft", confidence=0.9, source_count=3),
            KbWikiPage(space_id=priv.id, slug="p-1", title="个人库词条不入中心浏览",
                       body_md="正文" * 20, status="live", confidence=0.9, source_count=3),
        ])
        await db.commit()
        cat_id = cat.id

    async with env() as db:
        # 默认：仅 hub 的 live 词条（2 条）
        res = await hub_pages(db=db, user=_StubUser(), _perm=None, category_id=None, search="", page=1, page_size=20)
        titles = {p["title"] for p in res["data"]["items"]}
        assert titles == {"中心内分类词条", "中心内未分类词条"}

        # 按分类
        res_cat = await hub_pages(
            db=db, user=_StubUser(), _perm=None, category_id=cat_id, search="", page=1, page_size=20)
        assert {p["title"] for p in res_cat["data"]["items"]} == {"中心内分类词条"}

        # 未分类
        res_uncat = await hub_pages(db=db, user=_StubUser(), _perm=None, category_id=0, search="", page=1, page_size=20)
        assert {p["title"] for p in res_uncat["data"]["items"]} == {"中心内未分类词条"}

        # 搜索
        res_search = await hub_pages(
            db=db, user=_StubUser(), _perm=None, category_id=None, search="未分类", page=1, page_size=20)
        assert {p["title"] for p in res_search["data"]["items"]} == {"中心内未分类词条"}


# ---------------------------------------------------------------- 列迁移

@pytest.mark.asyncio
async def test_ensure_columns_all_tables(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cols.db")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: c.execute(text(
            "CREATE TABLE kb_space (id INTEGER PRIMARY KEY, name VARCHAR(100), visibility VARCHAR(20),"
            "owner_user_id INTEGER, description TEXT, page_count INTEGER, status VARCHAR(20),"
            "created_at TIMESTAMP, updated_at TIMESTAMP)"
        )))
        await conn.run_sync(lambda c: c.execute(text(
            "CREATE TABLE kb_doc_source (id INTEGER PRIMARY KEY, space_id INTEGER, title VARCHAR(200),"
            "content TEXT, content_hash VARCHAR(64), url VARCHAR(1000), source_type VARCHAR(20),"
            "char_count INTEGER, compiled_at TIMESTAMP, created_at TIMESTAMP, updated_at TIMESTAMP)"
        )))
        await conn.run_sync(lambda c: c.execute(text(
            "CREATE TABLE kb_wiki_page (id INTEGER PRIMARY KEY, space_id INTEGER, slug VARCHAR(200),"
            "title VARCHAR(200), body_md TEXT, status VARCHAR(20), version INTEGER,"
            "created_at TIMESTAMP, updated_at TIMESTAMP)"
        )))
        await conn.run_sync(lambda c: c.execute(text(
            "CREATE TABLE kb_public_submission (id INTEGER PRIMARY KEY, source_space_id INTEGER,"
            "source_doc_id INTEGER, doc_title VARCHAR(200), doc_content TEXT, content_hash VARCHAR(48),"
            "status VARCHAR(20), submitted_by INTEGER, created_at TIMESTAMP, updated_at TIMESTAMP)"
        )))
    async with engine.begin() as conn:
        await conn.run_sync(_ensure_columns)
    async with engine.connect() as conn:
        space_cols = await conn.run_sync(lambda c: [r[1] for r in c.execute(text("PRAGMA table_info(kb_space)"))])
        doc_cols = await conn.run_sync(lambda c: [r[1] for r in c.execute(text("PRAGMA table_info(kb_doc_source)"))])
        page_cols = await conn.run_sync(lambda c: [r[1] for r in c.execute(text("PRAGMA table_info(kb_wiki_page)"))])
        sub_cols = await conn.run_sync(
            lambda c: [r[1] for r in c.execute(text("PRAGMA table_info(kb_public_submission)"))])
    await engine.dispose()
    assert "category_id" in space_cols
    assert "file_ext" in doc_cols
    assert "category_id" in page_cols
    assert {"source_page_id", "check_comment", "checked_by", "checked_at"} <= set(sub_cols)


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
    """旧库菜单（周期二 N2 形态）：ApeCortex + 审核中心 + 晋升按钮。"""
    top = Menu(name="ApeCortex", parent_id=0, type="M", path="/knowledge-base",
               icon="Collection", sort=40, visible=1, status=1)
    db.add(top)
    await db.flush()
    spaces = Menu(name="知识空间", parent_id=top.id, type="C", path="spaces",
                  component="knowledge_base/spaces/index", permission="knowledge_base:space:list", visible=1, status=1)
    review = Menu(name="审核中心", parent_id=top.id, type="C", path="promotions",
                   component="knowledge_base/promotions/index",
                   permission="knowledge_base:promotion:review", visible=1, status=1)
    db.add_all([spaces, review])
    await db.flush()
    db.add_all([
        Menu(name="提交晋升", parent_id=review.id, type="F",
             permission="knowledge_base:promotion:submit", visible=1, status=1),
        Menu(name="审核晋升", parent_id=review.id, type="F",
             permission="knowledge_base:promotion:review", visible=1, status=1),
        Menu(name="发布词条", parent_id=spaces.id, type="F",
             permission="knowledge_base:page:publish", visible=1, status=1),
    ])
    await db.commit()
    return top


@pytest.mark.asyncio
async def test_seed_migrates_to_cycle3(menu_env):
    async with menu_env() as db:
        top = await _old_menu_tree(db)
        top_id = top.id
        await seed_knowledge_base_data(db)
        await db.commit()

        menus = list((await db.execute(select(Menu))).scalars().all())
        by_name = {m.name: m for m in menus}

        # 审核中心 → 推送中心；权限点换 submission:review
        push_center = by_name["推送中心"]
        assert push_center.path == "promotions"
        assert push_center.permission == "knowledge_base:submission:review"

        # 晋升/发布按钮清理
        assert by_name.get("提交晋升") is None
        assert by_name.get("审核晋升") is None
        assert by_name.get("发布词条") is None

        # 知识中心仍是第一菜单；推送知识中心按钮存在
        assert by_name["知识中心"].parent_id == top_id and by_name["知识中心"].path == "hub"
        assert by_name["推送知识中心"] is not None

        # C 菜单排序自愈：hub=1 spaces=2 promotions=3
        c_children = {m.name: m.sort for m in menus if m.parent_id == top_id and m.type == "C"}
        assert c_children["知识中心"] == 1
        assert c_children["知识空间"] == 2
        assert c_children["推送中心"] == 3

    # 幂等：再跑一次不重复创建/不重复删
    async with menu_env() as db:
        await seed_knowledge_base_data(db)
        await db.commit()
        menus = list((await db.execute(select(Menu))).scalars().all())
        hubs = [m for m in menus if m.path == "hub" and m.type == "C"]
        pushes = [m for m in menus if m.name == "推送中心"]
        assert len(hubs) == 1 and len(pushes) == 1


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
    assert "| 模块 | 状态 |" in content


def _minimal_pdf_with_text(message: str) -> bytes:
    """构造最小合法 PDF（单页含文本），供 fileparse 回路测试。"""
    content = f"BT /F1 12 Tf 72 720 Td ({message}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
            b" /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
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


# ---------------------------------------------------------------- 下载（周期三：原始文件优先）

@pytest.mark.asyncio
async def test_doc_download_prefers_raw_file(env, monkeypatch):

    from src.plugins.builtin.knowledge_base.api_docs import download_doc

    class _U:
        id = 1

    async with env() as db:
        space = KbSpace(owner_user_id=1, name="我的库", visibility="private")
        db.add(space)
        await db.flush()
        doc = KbDocSource(
            space_id=space.id, title="下载测试/文档 名", content="解析后的纯文本" * 20,
            content_hash="dl1", source_type="upload", file_ext="pdf", char_count=160,
        )
        db.add(doc)
        await db.commit()
        sid, did = space.id, doc.id

    import src.plugins.builtin.knowledge_base.api_docs as api_docs_mod

    async def _fake_access(user_id, space_id, need_write=False):
        return True, ""

    monkeypatch.setattr(api_docs_mod, "ensure_space_access", _fake_access)

    # ① 有原始文件 → 返回原始字节
    fake_raw = b"%PDF-1.4 fake-but-valid-bytes"
    raw_dir = api_docs_mod._raw_dir()
    (raw_dir / f"{did}.pdf").write_bytes(fake_raw)

    from urllib.parse import quote

    try:
        async with env() as db:
            resp = await download_doc(space_id=sid, doc_id=did, db=db, user=_U(), _perm=None)
        assert resp.body == fake_raw
        assert "pdf" in resp.media_type
        assert quote("下载测试_文档_名.pdf") in resp.headers["content-disposition"]

        # ② 无原始文件（chat 型）→ Markdown 回退
        async with env() as db:
            doc2 = KbDocSource(
                space_id=sid, title="聊天结论", content="结论" * 30,
                content_hash="dl2", source_type="chat", char_count=60,
            )
            db.add(doc2)
            await db.commit()
            did2 = doc2.id
        async with env() as db:
            resp2 = await download_doc(space_id=sid, doc_id=did2, db=db, user=_U(), _perm=None)
        assert "markdown" in resp2.media_type
        assert quote("聊天结论.md") in resp2.headers["content-disposition"]
    finally:
        for f in raw_dir.glob("*.pdf"):
            f.unlink()
