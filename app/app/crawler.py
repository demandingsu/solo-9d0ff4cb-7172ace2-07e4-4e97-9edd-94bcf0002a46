"""同站分页 + 详情抓取。

约束：
* 只沿同站链接爬行（协议/主机/端口一致），跨站分页或详情链接跳过并记录；
* 分页跟随 ``rel=next``，带最大页数与死循环保护；
* 详情 URL 去重，抓取失败不中断整体查询，作为错误收集返回。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .models import Announcement
from .parser import is_same_site, parse_detail, parse_detail_links, parse_next_page


@dataclass
class CrawlResult:
    announcements: list[Announcement] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    detail_pages: int = 0
    skipped_offsite: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Crawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_list_pages: int = 50,
        max_detail_pages: int = 500,
    ) -> None:
        self._client = client
        self._max_list_pages = max_list_pages
        self._max_detail_pages = max_detail_pages

    async def _get(self, url: str) -> str | None:
        try:
            resp = await self._client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return f"__ERROR__:{type(exc).__name__}:{exc}"
        return resp.text

    async def crawl(self, index_url: str) -> CrawlResult:
        result = CrawlResult()
        seen_pages: set[str] = set()
        detail_urls: list[str] = []
        seen_details: set[str] = set()

        # 1) 抓列表分页（只走同站 rel=next）
        page_url: str | None = index_url
        while page_url and page_url not in seen_pages and len(seen_pages) < self._max_list_pages:
            seen_pages.add(page_url)
            html = await self._get(page_url)
            if html is None or html.startswith("__ERROR__:"):
                result.errors.append(f"列表页抓取失败 {page_url}: {html}")
                break
            result.list_pages.append(page_url)

            for detail_url, _id, _kind in parse_detail_links(html, page_url):
                if not is_same_site(detail_url, index_url):
                    result.skipped_offsite.append(detail_url)
                    continue
                if detail_url not in seen_details:
                    seen_details.add(detail_url)
                    detail_urls.append(detail_url)

            nxt = parse_next_page(html, page_url)
            if nxt and not is_same_site(nxt, index_url):
                result.skipped_offsite.append(nxt)
                nxt = None
            page_url = nxt

        # 2) 抓详情（同站已保证），限量保护
        for detail_url in detail_urls[: self._max_detail_pages]:
            html = await self._get(detail_url)
            if html is None or html.startswith("__ERROR__:"):
                result.errors.append(f"详情页抓取失败 {detail_url}: {html}")
                continue
            result.detail_pages += 1
            try:
                result.announcements.append(parse_detail(html, detail_url))
            except ValueError as exc:
                result.errors.append(f"详情页解析失败 {detail_url}: {exc}")

        return result
