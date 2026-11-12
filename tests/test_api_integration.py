"""端到端：FastAPI + 爬虫 + 引擎，对本地示例公告站做完整查询。

前置：示例站点已在 127.0.0.1:8080 提供服务（见 README / demo 脚本）。
"""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

SITE = os.environ.get("TEST_SITE_URL", "http://127.0.0.1:8080/index.html")
Q = "2026-11-11T10:00:00+08:00"

pytestmark = pytest.mark.asyncio


async def test_full_query_workflow():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # lifespan 由 LifespanManager 之外的方式不保证执行，手动触发
        async with app.router.lifespan_context(app):
            r = await ac.post("/query", json={"index_url": SITE, "query_time": Q})
    assert r.status_code == 200, r.text
    data = r.json()

    # 四个列表页全部爬到，17 条同站详情（跨站 1 条被跳过）
    assert len(data["list_pages_crawled"]) == 4
    assert data["detail_pages_crawled"] == 17
    assert data["announcements_parsed"] == 17
    assert len(data["skipped_offsite_links"]) == 1

    active = data["active_suspensions"]
    chains = {tuple(a["source_chain"]) for a in active}
    # 有效链：001 原始；005 原始（012 次日更正不可见）；
    # 003→006→010 两次更正；009→011 更正（带悬空引用）；
    # 014 分叉更正合并为一组（015、016 分叉引用 014，017 沿 015 分支再更正）
    assert ("N-2026-001",) in chains
    assert ("N-2026-005",) in chains
    assert ("N-2026-003", "N-2026-006", "N-2026-010") in chains
    assert ("N-2026-009", "N-2026-011") in chains
    assert ("N-2026-014", "N-2026-015", "N-2026-016", "N-2026-017") in chains

    by_route = {a["route"]: a for a in active}
    # 两次更正后的最终区间
    assert by_route["25 路"]["suspend_end"].startswith("2026-11-20T22:00:00")
    # 同线路两条独立公告分别判断：12 路有两条有效停运
    routes_12 = [a for a in active if a["route"] == "12 路"]
    assert len(routes_12) == 2

    # 分叉更正：60 路只保留一条有效停运，为最晚发布的 017 替换后的
    # 线路与区间；016 引入的“60 路区间”分支不残留
    routes_60 = [a for a in active if a["route"].startswith("60 路")]
    assert len(routes_60) == 1
    fork = routes_60[0]
    assert fork["route"] == "60 路"
    assert fork["suspend_start"].startswith("2026-11-11T06:00:00")
    assert fork["suspend_end"].startswith("2026-11-15T22:00:00")
    assert fork["published_at"].startswith("2026-11-11T09:00:00")
    # 引用路径与来源链接一一对应、完整可追溯
    assert fork["source_chain"] == ["N-2026-014", "N-2026-015", "N-2026-016", "N-2026-017"]
    assert fork["sources"] == [
        f"http://127.0.0.1:8080/notices/N-2026-{i}.html" for i in ("014", "015", "016", "017")
    ]

    reasons = {(c["root_id"], c["reason"]) for c in data["inactive_chains"]}
    assert ("N-2026-002", "expired") in reasons
    assert ("N-2026-004", "cancelled") in reasons
    assert ("N-2026-008", "not_yet_effective") in reasons
    assert ("N-2026-013", "not_yet_effective") in reasons

    unmatched = data["unmatched_references"]
    assert len(unmatched) == 1
    assert unmatched[0]["notice_id"] == "N-2026-011"
    assert unmatched[0]["missing_reference"] == "N-2026-999"

    # 来源链接完整
    chain25 = next(a for a in active if a["route"] == "25 路")
    assert all(u.startswith("http://127.0.0.1:8080/notices/") for u in chain25["sources"])


async def test_fork_group_result_evolves_with_query_time():
    """同一分叉公告组在不同查询时刻只返回当时最新的一支。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):

            async def query_at(t: str) -> dict:
                r = await ac.post("/query", json={"index_url": SITE, "query_time": t})
                assert r.status_code == 200, r.text
                return r.json()

            # 017 尚未发布：016 分支（60 路区间）为当时最新，独立一条有效停运
            data = await query_at("2026-11-11T08:00:00+08:00")
            fork = [a for a in data["active_suspensions"] if a["route"].startswith("60 路")]
            assert len(fork) == 1
            assert fork[0]["route"] == "60 路区间"
            assert fork[0]["source_chain"] == ["N-2026-014", "N-2026-015", "N-2026-016"]
            assert fork[0]["suspend_end"].startswith("2026-11-14T22:00:00")

            # 017 发布后：替换为 60 路 / 11-15 结束，016 分支不残留
            data = await query_at("2026-11-11T10:00:00+08:00")
            fork = [a for a in data["active_suspensions"] if a["route"].startswith("60 路")]
            assert len(fork) == 1
            assert fork[0]["route"] == "60 路"
            assert fork[0]["suspend_end"].startswith("2026-11-15T22:00:00")

            # 最终区间结束后：整组转为 expired，不再出现在有效列表
            data = await query_at("2026-11-16T10:00:00+08:00")
            assert [a for a in data["active_suspensions"] if a["route"].startswith("60 路")] == []
            reasons = {
                (c["root_id"], c["reason"]) for c in data["inactive_chains"]
            }
            assert ("N-2026-014", "expired") in reasons


async def test_naive_query_time_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={"index_url": SITE, "query_time": "2026-11-11T10:00:00"},
            )
    assert r.status_code == 422


async def test_bad_index_url_returns_502():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={
                    "index_url": "http://127.0.0.1:9/never.html",
                    "query_time": Q,
                },
            )
    assert r.status_code == 502
