"""联网采集器（P2）：网页抓取 + SSRF/白名单护栏。

.. design:: 设计依据（调研结论）
   - 默认栈：httpx（trust_env=False——代理兼容，已踩坑修复）+ trafilatura 纯函数抽取
     （只当 extract(html) 用，避免双 HTTP 栈）
   - 防御抄 gpt-researcher/scraper.py:41-113：SSRF 校验（禁私网 IP）+ 域名白名单 +
     robots 尊重 + 响应大小/类型限制
   - 三层开关（vane 范式）：用户级总开关（默认关）→ 关闭时 MCP 工具不注册（LLM 看不见）
"""
import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from loguru import logger

# ---------------------------------------------------------------- 护栏

# 私网/保留地址（SSRF 防线）
_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10", "0.0.0.0/8")
]

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5MB
_FETCH_TIMEOUT = 20.0
_USER_AGENT = "ApeAdmin-KnowledgeBase/1.0 (+collect)"


def check_ssrf(url: str) -> tuple[bool, str]:
    """SSRF 校验：解析 host → DNS 解析 → 私网拒绝。"""
    try:
        host = urlparse(url).hostname
        if not host:
            return False, "URL 无主机名"
        addrs = socket.getaddrinfo(host, None)
        ip = ipaddress.ip_address(addrs[0][4][0])
        if any(ip in net for net in _BLOCKED_NETS):
            return False, f"目标解析为私网地址 {ip}（SSRF 防护）"
        return True, "ok"
    except (ValueError, socket.gaierror) as exc:
        return False, f"域名解析失败: {exc}"


def check_domain(url: str, extra_allow: list[str]) -> tuple[bool, str]:
    """域名白名单校验（extra_allow 为空=不限制；有则必须命中）。"""
    host = (urlparse(url).hostname or "").lower()
    if not extra_allow:
        return True, "ok"  # 未配置白名单=仅 SSRF 防线
    for allow in extra_allow:
        allow = allow.lower().lstrip(".")
        if host == allow or host.endswith("." + allow):
            return True, "ok"
    return False, f"域名 {host} 不在白名单"


async def fetch_page(
    url: str,
    extra_allow: list[str] | None = None,
    respect_robots: bool = True,
    render: bool = False,
) -> dict:
    """抓取单个网页 → trafilatura 抽正文 Markdown。返回 {ok, url, title, content_md}。

    护栏顺序：URL 协议 → SSRF → 域名白名单 → robots → 大小/类型限制。
    """
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "msg": "仅支持 http/https"}

    ok, msg = check_ssrf(url)
    if not ok:
        return {"ok": False, "msg": msg}
    ok, msg = check_domain(url, extra_allow or [])
    if not ok:
        return {"ok": False, "msg": msg}

    if respect_robots:
        allowed = await _robots_allows(url)
        if not allowed:
            return {"ok": False, "msg": "robots.txt 禁止抓取该路径"}

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT, trust_env=False, follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {resp.status_code}"}
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype and "xml" not in ctype:
                return {"ok": False, "msg": f"不支持的 content-type: {ctype[:60]}"}
            if len(resp.content) > _MAX_RESPONSE_BYTES:
                return {"ok": False, "msg": "响应过大（>5MB）"}
            html = resp.text
    except httpx.HTTPError as exc:
        detail = str(exc).strip() or type(exc).__name__  # ConnectTimeout 等异常 str 可能为空
        return {"ok": False, "msg": f"抓取失败: {type(exc).__name__}: {detail}"}

    result = _extract_markdown(url, html)
    if not result.get("ok") and render:
        rendered = await _render_fetch(url)
        if rendered.get("ok"):
            return rendered
    return result


def _crawl4ai_available() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except ImportError:
        return False


async def _render_fetch(url: str) -> dict:
    """渲染通道（可选）：装了 crawl4ai extras（含 playwright）才可用，否则明确降级提示。"""
    if not _crawl4ai_available():
        return {
            "ok": False,
            "msg": "该站点需要渲染引擎（extras 未安装：pip install crawl4ai 可选启用）",
        }
    try:
        from crawl4ai import AsyncWebCrawler

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
            md = (getattr(result, "markdown", None) or "").strip()
            if not md or len(md) < 80:
                return {"ok": False, "msg": "渲染后正文为空"}
            return {"ok": True, "url": url, "title": url[:100], "content_md": md}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "msg": f"渲染抓取失败: {exc}"}


def _extract_markdown(url: str, html: str) -> dict:
    """trafilatura 纯函数抽取。"""
    import trafilatura

    extracted = trafilatura.extract(
        html, output_format="markdown", include_links=True, include_tables=True,
        url=url,
    )
    if not extracted or len(extracted.strip()) < 80:
        # JS 渲染站兜底提示（调研风险 #3：不静默失败）
        return {
            "ok": False,
            "msg": "正文抽取为空（可能是 JS 渲染站点，需要渲染引擎——当前版本不支持）",
            "url": url,
        }
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        title = (meta.title if meta else "") or ""
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "url": url, "title": title or url[:100], "content_md": extracted}


async def _robots_allows(url: str) -> bool:
    """robots.txt 检查（宽松版：失败=允许，不打断采集）。"""
    try:
        import re
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        async with httpx.AsyncClient(timeout=8.0, trust_env=False,
                                      headers={"User-Agent": _USER_AGENT}) as client:
            resp = await client.get(robots_url)
            if resp.status_code >= 400:
                return True  # 无 robots = 允许
        path = parts.path or "/"
        ua = _USER_AGENT.split("/")[0]
        # 简化解析：匹配 User-agent: * 或我们的 UA 的 Disallow 规则
        disallowed = False
        current_ua = "*"
        for line in resp.text.splitlines():
            line = line.strip()
            m = re.match(r"(?i)user-agent:\s*(.+)", line)
            if m:
                current_ua = m.group(1).strip()
                continue
            d = re.match(r"(?i)disallow:\s*(.*)", line)
            if d and (current_ua in ("*", ua)):
                rule = d.group(1).strip()
                if rule and (path.startswith(rule.rstrip("*")) if rule.endswith("*") else path == rule or path.startswith(rule)):
                    disallowed = True
        return not disallowed
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- 联网开关

async def get_web_enabled(user_id: int) -> bool:
    """用户级联网总开关（默认关——P2 的第一层）。"""
    from sqlalchemy import select

    from src.db import SessionLocal
    from src.plugins.builtin.knowledge_base.models import KbSetting

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(KbSetting).where(
                    KbSetting.user_id == user_id, KbSetting.key == "web_enabled"
                )
            )
        ).scalars().first()
    if row is None:
        return False  # 默认关
    try:
        import json
        return bool(json.loads(row.value))
    except (ValueError, TypeError):
        return False


async def set_web_enabled(user_id: int, enabled: bool) -> None:
    import json

    from sqlalchemy import select

    from src.db import SessionLocal
    from src.plugins.builtin.knowledge_base.models import KbSetting

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(KbSetting).where(
                    KbSetting.user_id == user_id, KbSetting.key == "web_enabled"
                )
            )
        ).scalars().first()
        value = json.dumps(bool(enabled))
        if row is None:
            db.add(KbSetting(user_id=user_id, key="web_enabled", value=value))
        else:
            row.value = value
        await db.commit()


async def get_domain_allowlist(user_id: int) -> list[str]:
    """用户域名白名单（空=不限制，仅 SSRF 防线）。"""
    import json

    from sqlalchemy import select

    from src.db import SessionLocal
    from src.plugins.builtin.knowledge_base.models import KbSetting

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(KbSetting).where(
                    KbSetting.user_id == user_id, KbSetting.key == "web_domain_allow"
                )
            )
        ).scalars().first()
    if row is None:
        return []
    try:
        v = json.loads(row.value)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []
