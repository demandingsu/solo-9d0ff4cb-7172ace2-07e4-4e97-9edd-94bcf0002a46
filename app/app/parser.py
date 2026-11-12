"""公告页解析逻辑。

解析器对 HTML 的假设（也是随附示例站点遵循的结构约定）：

列表页（分页）
  <nav class="pager"> 中存放“下一页”链接 <a rel="next" href="...">；
  每条公告为 <article class="notice" data-id="编号" data-kind="suspension|correction|cancellation">，
  内含 <a class="detail-link" href="详情相对/绝对 URL">标题</a>。

详情页
  <table class="notice-fields"> 内 <tr data-field="..."> 定义字段，
  字段名用中文键（见 FIELD_KEYS）；停运时间放在
  <time datetime="ISO8601 带偏移"> 元素里；
  引用写作 <a class="reference" data-ref-id="被引用编号" href="...">。
  列表页也带 data-* 摘要，详情页信息为准。

这种“data 属性 + time 元素”的约定使解析对模板文案不敏感；
真实站点接入时只需替换本模块中的选择器。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Announcement, NoticeKind

FIELD_KEYS = {
    "notice_id": ("公告编号", "编号"),
    "route": ("线路", "线路名称"),
    "kind": ("类型", "公告类型"),
    "suspend_start": ("停运开始", "停运起始", "停运开始时间"),
    "suspend_end": ("停运结束", "停运截止", "停运结束时间"),
    "published_at": ("发布时间", "发布日期"),
}

KIND_MAP = {
    "suspension": NoticeKind.SUSPENSION,
    "correction": NoticeKind.CORRECTION,
    "cancellation": NoticeKind.CANCELLATION,
    "停运": NoticeKind.SUSPENSION,
    "更正": NoticeKind.CORRECTION,
    "撤销": NoticeKind.CANCELLATION,
}


def _abs_url(base: str, href: str) -> str:
    return urljoin(base, href)


def is_same_site(url: str, base_url: str) -> bool:
    """同站判定：协议+主机+端口一致。"""
    a, b = urlparse(url), urlparse(base_url)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


def parse_next_page(html: str, page_url: str) -> str | None:
    """从列表页提取 rel=next 的分页链接（同站绝对 URL），没有则 None。"""
    soup = BeautifulSoup(html, "html.parser")
    nxt = soup.select_one("nav.pager a[rel=next]") or soup.select_one("a[rel=next]")
    if not nxt or not nxt.get("href"):
        return None
    return _abs_url(page_url, nxt["href"])


def parse_detail_links(html: str, page_url: str) -> list[tuple[str, str | None, str | None]]:
    """列表页 -> (详情绝对 URL, 编号, 类型) 列表，保持页面顺序。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str | None, str | None]] = []
    for art in soup.select("article.notice"):
        link = art.select_one("a.detail-link[href]") or art.select_one("a[href]")
        if link is None:
            continue
        out.append(
            (
                _abs_url(page_url, link["href"]),
                art.get("data-id"),
                art.get("data-kind"),
            )
        )
    return out


def _parse_time(value: str) -> datetime:
    # 支持 2026-11-11T10:00:00+08:00 及 2026-11-11 10:00:00+08:00
    v = value.strip().replace(" ", "T", 1) if " " in value.strip() else value.strip()
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区信息: {value!r}")
    return dt


def _field_rows(soup: BeautifulSoup) -> dict[str, str]:
    rows: dict[str, str] = {}
    for tr in soup.select("table.notice-fields tr[data-field]"):
        key = tr["data-field"]
        cell = tr.select_one("td")
        if cell is None:
            continue
        time_el = cell.select_one("time[datetime]")
        rows[key] = time_el["datetime"] if time_el else cell.get_text(strip=True)
    return rows


def parse_detail(html: str, detail_url: str) -> Announcement:
    """解析公告详情页。"""
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h1.notice-title, title")
    title = title_el.get_text(strip=True) if title_el else None

    rows = _field_rows(soup)

    def pick(*names: str) -> str | None:
        for n in names:
            if n in rows and rows[n] != "":
                return rows[n]
        return None

    notice_id = pick("notice_id", *FIELD_KEYS["notice_id"])
    if not notice_id:
        raise ValueError(f"详情页缺少公告编号: {detail_url}")

    kind_raw = pick("kind", *FIELD_KEYS["kind"]) or ""
    kind = KIND_MAP.get(kind_raw.strip())
    if kind is None:
        # 回退：从编号/标题推断
        text = f"{kind_raw} {title or ''}"
        if "撤销" in text:
            kind = NoticeKind.CANCELLATION
        elif "更正" in text:
            kind = NoticeKind.CORRECTION
        else:
            kind = NoticeKind.SUSPENSION

    published_raw = pick("published_at", *FIELD_KEYS["published_at"])
    if not published_raw:
        raise ValueError(f"详情页缺少发布时间: {detail_url}")

    def dt_or_none(raw: str | None) -> datetime | None:
        return _parse_time(raw) if raw else None

    references = [
        a["data-ref-id"].strip()
        for a in soup.select("a.reference[data-ref-id]")
        if a.get("data-ref-id", "").strip()
    ]

    return Announcement(
        notice_id=notice_id.strip(),
        route=(pick("route", *FIELD_KEYS["route"]) or None),
        kind=kind,
        suspend_start=dt_or_none(pick("suspend_start", *FIELD_KEYS["suspend_start"])),
        suspend_end=dt_or_none(pick("suspend_end", *FIELD_KEYS["suspend_end"])),
        published_at=_parse_time(published_raw),
        references=references,
        url=detail_url,
        title=title,
    )
