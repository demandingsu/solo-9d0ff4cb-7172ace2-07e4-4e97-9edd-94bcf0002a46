"""引擎层：公告链还原与有效性判定。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.engine import reconstruct
from app.models import Announcement, NoticeKind

TZ = "+08:00"


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def make(
    notice_id: str,
    kind: NoticeKind,
    published: str,
    *,
    route: str | None = "12 路",
    start: str | None = None,
    end: str | None = None,
    refs: list[str] | None = None,
) -> Announcement:
    return Announcement(
        notice_id=notice_id,
        route=route,
        kind=kind,
        suspend_start=parse_dt(start) if start else None,
        suspend_end=parse_dt(end) if end else None,
        published_at=parse_dt(published),
        references=refs or [],
        url=f"http://x/notices/{notice_id}.html",
    )


Q = parse_dt(f"2026-11-11T10:00:00{TZ}")


def test_plain_active_suspension():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-12T22:00:00{TZ}")
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A1"]


def test_correction_chain_replaces_interval():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-07T09:00:00{TZ}",
             start=f"2026-11-09T06:00:00{TZ}", end=f"2026-11-12T20:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T15:00:00{TZ}",
             start=f"2026-11-10T00:00:00{TZ}", end=f"2026-11-15T22:00:00{TZ}",
             refs=["A1"]),
        make("A3", NoticeKind.CORRECTION, f"2026-11-10T08:00:00{TZ}",
             start=f"2026-11-10T00:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}",
             refs=["A2"]),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    a = res.active[0]
    assert a.source_chain == ["A1", "A2", "A3"]
    assert a.suspend_end == parse_dt(f"2026-11-20T22:00:00{TZ}")
    assert a.published_at == parse_dt(f"2026-11-10T08:00:00{TZ}")
    assert a.sources == [f"http://x/notices/{i}.html" for i in ("A1", "A2", "A3")]


def test_cancellation_kills_whole_chain():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T08:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-21T22:00:00{TZ}",
             refs=["A1"]),
        make("A3", NoticeKind.CANCELLATION, f"2026-11-09T10:00:00{TZ}",
             route=None, refs=["A2"]),
    ]
    res = reconstruct(anns, Q)
    assert res.active == []
    assert len(res.inactive) == 1
    assert res.inactive[0].reason == "cancelled"
    assert res.inactive[0].chain == ["A1", "A2", "A3"]


def test_future_correction_is_invisible():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-09T09:00:00{TZ}",
             start=f"2026-11-11T05:00:00{TZ}", end=f"2026-11-13T21:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-12T09:00:00{TZ}",
             start=f"2026-11-11T05:00:00{TZ}", end=f"2026-11-14T21:00:00{TZ}",
             refs=["A1"]),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A1"]
    assert res.active[0].suspend_end == parse_dt(f"2026-11-13T21:00:00{TZ}")


def test_independent_notices_same_route_judged_separately():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-05T09:00:00{TZ}",
             start=f"2026-11-05T06:00:00{TZ}", end=f"2026-11-06T20:00:00{TZ}"),
        make("A2", NoticeKind.SUSPENSION, f"2026-11-09T09:00:00{TZ}",
             start=f"2026-11-11T05:00:00{TZ}", end=f"2026-11-13T21:00:00{TZ}"),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A2"]
    assert {i.reason for i in res.inactive} == {"expired"}
    assert res.inactive[0].root_id == "A1"


def test_unmatched_reference_reported_but_chain_still_evaluated():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-10T09:00:00{TZ}",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-13T20:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-10T18:00:00{TZ}",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-16T20:00:00{TZ}",
             refs=["A1", "GHOST"]),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].suspend_end == parse_dt(f"2026-11-16T20:00:00{TZ}")
    assert len(res.unmatched) == 1
    assert res.unmatched[0].notice_id == "A2"
    assert res.unmatched[0].missing_reference == "GHOST"


def test_not_yet_started_and_whole_chain_future():
    future_chain = make("F1", NoticeKind.SUSPENSION, f"2026-11-14T09:00:00{TZ}",
                        route="50 路",
                        start=f"2026-11-15T06:00:00{TZ}",
                        end=f"2026-11-18T20:00:00{TZ}")
    upcoming = make("U1", NoticeKind.SUSPENSION, f"2026-11-09T08:00:00{TZ}",
                    route="5 路",
                    start=f"2026-11-12T06:00:00{TZ}",
                    end=f"2026-11-14T20:00:00{TZ}")
    res = reconstruct([future_chain, upcoming], Q)
    reasons = {i.root_id: i.reason for i in res.inactive}
    assert reasons == {"F1": "not_yet_effective", "U1": "not_yet_effective"}


def test_boundary_end_exclusive_start_inclusive():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-09T09:00:00{TZ}",
             start=f"2026-11-11T10:00:00{TZ}", end=f"2026-11-12T10:00:00{TZ}"),
    ]
    # 起点 == 查询时刻：有效
    assert len(reconstruct(anns, parse_dt(f"2026-11-11T10:00:00{TZ}")).active) == 1
    # 终点 == 查询时刻：已失效（半开区间 [start, end)）
    res = reconstruct(anns, parse_dt(f"2026-11-12T10:00:00{TZ}"))
    assert res.active == []
    assert res.inactive[0].reason == "expired"


def test_dangling_correction_is_its_own_root():
    # 引用全部悬空的更正公告自身作为根，仍需可被解释
    anns = [
        make("A9", NoticeKind.CORRECTION, f"2026-11-10T18:00:00{TZ}",
             refs=["GHOST"]),
    ]
    res = reconstruct(anns, Q)
    assert res.active == []
    assert res.inactive[0].reason == "no_interval"
    assert len(res.unmatched) == 1


def test_timezone_comparison_uses_instant():
    # 同一时刻不同偏移表达，判定结果应一致
    anns = [
        make("A1", NoticeKind.SUSPENSION, "2026-11-08T09:00:00+08:00",
             start="2026-11-11T08:00:00+08:00", end="2026-11-11T12:00:00+08:00"),
    ]
    q_utc = parse_dt("2026-11-11T03:00:00+00:00")  # == 11:00 +08:00
    res = reconstruct(anns, q_utc)
    assert len(res.active) == 1


def test_naive_datetime_rejected_by_model():
    with pytest.raises(Exception):
        Announcement(
            notice_id="X", route="r", kind=NoticeKind.SUSPENSION,
            suspend_start=None, suspend_end=None,
            published_at=parse_dt("2026-11-11T10:00:00"),  # 无时区
            references=[], url="http://x", title=None,
        )
