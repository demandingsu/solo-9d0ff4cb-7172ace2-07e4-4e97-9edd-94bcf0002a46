"""公告链还原与停运有效性判定。

规则（对应业务约定）：

* 每条公告按编号唯一标识；更正/撤销公告通过“引用编号”指向上一条公告，
  更正也可以引用另一条更正，从而形成链。
* 链的顺序由引用关系确定，同一节点存在多个后继时按发布时间排序。
* 更正公告携带的停运起止（及线路）替换其之前链上的停运信息；
  链上最后一条非撤销公告的信息即当前生效信息。
* 撤销公告使它所在的整条链失效。
* 查询时刻之后才发布的公告在该时刻尚不可见，不参与判定。
* 同一起始公告若出现分叉后继，每个分支独立成链、独立判定；
  不同根公告（同线路的独立公告）天然分别判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    ActiveSuspension,
    Announcement,
    InactiveChain,
    NoticeKind,
    UnmatchedReference,
)


@dataclass
class _Node:
    announcement: Announcement
    children: list[str] = field(default_factory=list)  # 引用本公告的后继编号


@dataclass
class ReconstructionResult:
    active: list[ActiveSuspension]
    inactive: list[InactiveChain]
    unmatched: list[UnmatchedReference]
    # 编号 -> 公告（便于测试/诊断）
    by_id: dict[str, Announcement]


def _sort_key(published_at: datetime) -> datetime:
    return published_at


def reconstruct(
    announcements: list[Announcement], query_time: datetime
) -> ReconstructionResult:
    """根据引用关系与发布时间还原公告链并判定有效性。"""
    by_id: dict[str, Announcement] = {}
    dupes: set[str] = set()
    for a in announcements:
        if a.notice_id in by_id:
            # 同编号重复抓取：保留发布时间一致的第一条，其余忽略
            dupes.add(a.notice_id)
            continue
        by_id[a.notice_id] = a

    # 1) 无法匹配的引用：被引用编号在整站公告中不存在
    unmatched: list[UnmatchedReference] = []
    for a in announcements:
        for ref in a.references:
            if ref not in by_id:
                unmatched.append(
                    UnmatchedReference(
                        notice_id=a.notice_id, missing_reference=ref, url=a.url, kind=a.kind
                    )
                )

    # 2) 构建后继索引（只连存在的边），并区分查询时刻可见/不可见
    nodes: dict[str, _Node] = {nid: _Node(a) for nid, a in by_id.items()}
    for a in by_id.values():
        for ref in a.references:
            if ref in nodes:
                nodes[ref].children.append(a.notice_id)
    for node in nodes.values():
        node.children.sort(key=lambda cid: _sort_key(by_id[cid].published_at))

    visible = {nid for nid, a in by_id.items() if a.published_at <= query_time}

    # 3) 根 = 不引用任何“查询时刻已发布且存在”的公告的节点（链的出边起点）。
    #    悬空引用（编号不存在）与指向未来公告的引用不阻止其成为根。
    def has_visible_existing_ref(a: Announcement) -> bool:
        return any(
            ref in by_id and by_id[ref].published_at <= query_time
            for ref in a.references
        )

    roots = [nid for nid, a in by_id.items() if not has_visible_existing_ref(a)]
    roots.sort(key=lambda nid: (by_id[nid].published_at, nid))

    active: list[ActiveSuspension] = []
    inactive: list[InactiveChain] = []

    def walk(path: list[str], seen: set[str]) -> None:
        tail = path[-1]
        # 查询时刻之后才发布的后继公告当时尚不可见，不进入链
        kids = [c for c in nodes[tail].children if c not in seen and c in visible]
        if not kids:
            evaluate_chain(path)
            return
        for kid in kids:  # 已按发布时间排序
            walk(path + [kid], seen | {kid})

    def evaluate_chain(chain_ids: list[str]) -> None:
        chain_ann = [by_id[i] for i in chain_ids]
        sources = [a.url for a in chain_ann]
        root = chain_ann[0]

        # 整条链在查询时刻都还未发布
        if not any(i in visible for i in chain_ids):
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=root.route,
                    reason="not_yet_effective",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=max(a.published_at for a in chain_ann),
                )
            )
            return

        # 撤销 -> 整条链失效（只统计查询时刻已发布的撤销）
        cancels = [
            a
            for a in chain_ann
            if a.kind is NoticeKind.CANCELLATION and a.published_at <= query_time
        ]
        if cancels:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=_current_route(chain_ann),
                    reason="cancelled",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=cancels[-1].published_at,
                )
            )
            return

        # 按发布时间依次应用更正，后者替换前者的停运信息
        route, start, end, latest_pub = _current_interval(chain_ann, query_time)
        if start is None or end is None:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=route,
                    reason="no_interval",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_pub,
                )
            )
            return

        if start > query_time:
            reason = "not_yet_effective"
        elif end <= query_time:
            # 半开区间 [start, end)：查询时刻恰为结束时刻时已失效
            reason = "expired"
        else:
            reason = None

        if reason:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=route,
                    reason=reason,
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_pub,
                )
            )
        else:
            active.append(
                ActiveSuspension(
                    route=route or "(未知线路)",
                    suspend_start=start,
                    suspend_end=end,
                    source_chain=chain_ids,
                    sources=sources,
                    published_at=latest_pub,
                )
            )

    def _current_route(chain_ann: list[Announcement]) -> str | None:
        route = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > query_time:
                continue
            if a.route:
                route = a.route
        return route

    def _current_interval(
        chain_ann: list[Announcement], now: datetime
    ) -> tuple[str | None, datetime | None, datetime | None, datetime | None]:
        route = None
        start = end = None
        latest_pub = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > now:
                continue
            latest_pub = a.published_at
            if a.route:
                route = a.route
            if a.suspend_start is not None:
                start = a.suspend_start
            if a.suspend_end is not None:
                end = a.suspend_end
        return route, start, end, latest_pub

    for root_id in roots:
        walk([root_id], {root_id})

    active.sort(key=lambda x: (x.route, x.suspend_start))
    inactive.sort(key=lambda x: (x.root_id, tuple(x.chain)))
    unmatched.sort(key=lambda x: (x.notice_id, x.missing_reference))

    return ReconstructionResult(
        active=active, inactive=inactive, unmatched=unmatched, by_id=by_id
    )
