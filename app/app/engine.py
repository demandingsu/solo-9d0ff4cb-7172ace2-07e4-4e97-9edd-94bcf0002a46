"""公告链还原与停运有效性判定。

规则（对应业务约定）：

* 每条公告按编号唯一标识；更正/撤销公告通过“引用编号”指向之前的公告，
  更正也可以引用另一条更正，从而形成链。
* 同一原公告（链根）在查询时刻已发布的全部更正/撤销归为一组判定：
  即使它们分别引用不同分支（分叉引用），也只产生一个结果——
  按发布时间依次应用，后发布的公告替换先发布公告的停运信息，
  查询时刻只保留最新的线路与区间，较早分支的结果不会残留。
* 撤销公告使它所在的整组失效。
* 查询时刻之后才发布的公告在该时刻尚不可见，不参与分组与判定。
* 不同根公告（同线路的独立公告）天然分别判断。
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


def reconstruct(
    announcements: list[Announcement], query_time: datetime
) -> ReconstructionResult:
    """根据引用关系与发布时间还原公告链并判定有效性。"""
    by_id: dict[str, Announcement] = {}
    for a in announcements:
        if a.notice_id in by_id:
            # 同编号重复抓取：保留第一条，其余忽略
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
        node.children.sort(key=lambda cid: (by_id[cid].published_at, cid))

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

    def collect_group(root_id: str) -> list[str]:
        """收集根公告及其查询时刻已发布的全部后继（分叉合并为一组）。

        沿引用边遍历所有可见后继——同一原公告被多条更正分别引用时，
        各分支都进入同一组；返回按发布时间排序的编号列表，
        判定按此顺序依次应用，后发布者替换先发布者的停运信息。
        """
        members = [root_id]
        seen = {root_id}
        queue = [root_id]
        while queue:
            cur = queue.pop(0)
            for child in nodes[cur].children:
                # 查询时刻之后才发布的后继当时尚不可见，不进入组
                if child in seen or child not in visible:
                    continue
                seen.add(child)
                members.append(child)
                queue.append(child)
        members.sort(key=lambda nid: (by_id[nid].published_at, nid))
        return members

    def _current_route(group_ann: list[Announcement]) -> str | None:
        route = None
        for a in group_ann:  # 已按发布时间排序
            if a.published_at > query_time:
                continue
            if a.route:
                route = a.route
        return route

    def _current_interval(
        group_ann: list[Announcement],
    ) -> tuple[str | None, datetime | None, datetime | None, datetime | None]:
        route = None
        start = end = None
        latest_pub = None
        for a in group_ann:  # 已按发布时间排序，后者替换前者
            if a.published_at > query_time:
                continue
            latest_pub = a.published_at
            if a.route:
                route = a.route
            if a.suspend_start is not None:
                start = a.suspend_start
            if a.suspend_end is not None:
                end = a.suspend_end
        return route, start, end, latest_pub

    active: list[ActiveSuspension] = []
    inactive: list[InactiveChain] = []

    def evaluate_group(root_id: str, group_ids: list[str]) -> None:
        group_ann = [by_id[i] for i in group_ids]
        sources = [a.url for a in group_ann]
        root = by_id[root_id]

        # 整组在查询时刻都还未发布
        if not any(i in visible for i in group_ids):
            inactive.append(
                InactiveChain(
                    root_id=root_id,
                    route=root.route,
                    reason="not_yet_effective",
                    chain=group_ids,
                    sources=sources,
                    latest_published_at=max(a.published_at for a in group_ann),
                )
            )
            return

        # 撤销 -> 整组失效（只统计查询时刻已发布的撤销）
        cancels = [
            a
            for a in group_ann
            if a.kind is NoticeKind.CANCELLATION and a.published_at <= query_time
        ]
        if cancels:
            inactive.append(
                InactiveChain(
                    root_id=root_id,
                    route=_current_route(group_ann),
                    reason="cancelled",
                    chain=group_ids,
                    sources=sources,
                    latest_published_at=cancels[-1].published_at,
                )
            )
            return

        # 按发布时间依次应用更正，后者替换前者的停运信息；
        # 分叉分支同样按此合并，查询时刻只保留最新线路与区间
        route, start, end, latest_pub = _current_interval(group_ann)
        if start is None or end is None:
            inactive.append(
                InactiveChain(
                    root_id=root_id,
                    route=route,
                    reason="no_interval",
                    chain=group_ids,
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
                    root_id=root_id,
                    route=route,
                    reason=reason,
                    chain=group_ids,
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
                    source_chain=group_ids,
                    sources=sources,
                    published_at=latest_pub,
                )
            )

    for root_id in roots:
        evaluate_group(root_id, collect_group(root_id))

    active.sort(key=lambda x: (x.route, x.suspend_start))
    inactive.sort(key=lambda x: (x.root_id, tuple(x.chain)))
    unmatched.sort(key=lambda x: (x.notice_id, x.missing_reference))

    return ReconstructionResult(
        active=active, inactive=inactive, unmatched=unmatched, by_id=by_id
    )
