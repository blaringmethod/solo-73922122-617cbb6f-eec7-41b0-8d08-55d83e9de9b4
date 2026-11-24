"""服务编排与 SQLite 持久化测试：整批退回、版本漂移、最后成功结果语义。"""

from __future__ import annotations

import threading

import pytest

from app.db import Database
from app.models import (
    Interval,
    RejudgeRequest,
    RuleIn,
    SampleRecord,
    VersionCreate,
)
from app.service import (
    InfeasibleOriginalGrade,
    OriginalVersionNotFound,
    VersionDrift,
    rejudge_batch,
)

V1_RULES = [
    RuleIn(grade="优质煤", moisture=Interval(lo=0, hi=800), ash=Interval(lo=0, hi=2000)),
    RuleIn(grade="普通煤", moisture=Interval(lo=0, hi=1200), ash=Interval(lo=0, hi=3000)),
    RuleIn(grade="等外煤", moisture=Interval(lo=0, hi=10000), ash=Interval(lo=0, hi=10000)),
]
V2_RULES = [
    RuleIn(grade="优质煤", moisture=Interval(lo=0, hi=700), ash=Interval(lo=0, hi=1500)),
    RuleIn(grade="普通煤", moisture=Interval(lo=0, hi=1000), ash=Interval(lo=0, hi=2500)),
]
V3_RULES = [
    RuleIn(grade="精煤", moisture=Interval(lo=0, hi=10000), ash=Interval(lo=0, hi=10000)),
]


def _req(batch_no="B1", original="v1"):
    return RejudgeRequest(
        batch_no=batch_no,
        original_version=original,
        records=[
            SampleRecord(
                sample_no="S-OK",
                moisture=Interval(lo=400, hi=600),
                ash=Interval(lo=1000, hi=1400),
                original_grade="优质煤",
            ),
            SampleRecord(
                sample_no="S-MULTI",
                moisture=Interval(lo=900, hi=1150),
                ash=Interval(lo=2300, hi=2700),
                original_grade="普通煤",
            ),
            SampleRecord(
                sample_no="S-FULL",
                moisture=Interval(lo=0, hi=10000),
                ash=Interval(lo=0, hi=10000),
                original_grade="优质煤",
            ),
        ],
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.create_version("v1", [r.model_dump() for r in V1_RULES], note="old", activate=False)
    database.create_version("v2", [r.model_dump() for r in V2_RULES], note="new", activate=True)
    return database


def test_unique_and_multi_grade_payload(db):
    payload = rejudge_batch(db, _req())
    assert payload["judged_version"] == "v2"
    by_no = {s["sample_no"]: s for s in payload["samples"]}

    ok = by_no["S-OK"]
    assert ok["unique"] is True and ok["final_grade"] == "优质煤"
    assert len(ok["options"]) == 1

    multi = by_no["S-MULTI"]
    assert multi["unique"] is False and multi["final_grade"] is None
    grades = {o["grade"] for o in multi["options"]}
    assert "普通煤" in grades and None in grades  # 现行版下部分点未分级
    for opt in multi["options"]:
        # 见证点必须落在旧档闭区间内
        assert 900 <= opt["witness_moisture"] <= 1150
        assert 2300 <= opt["witness_ash"] <= 2700
        assert opt["feasible_pairs"] > 0

    # S-FULL 原等级优质煤：可行点即 v1 优质煤矩形（m≤800,a≤2000），
    # 该矩形在 v2 下被切成 优质煤/普通煤 两档（v2 优质煤是其子矩形）
    full = by_no["S-FULL"]
    assert {o["grade"] for o in full["options"]} == {"优质煤", "普通煤"}
    assert sum(o["feasible_pairs"] for o in full["options"]) == 801 * 2001
    # 优质煤点数 = v2 优质煤矩形 701×1501
    assert next(o for o in full["options"] if o["grade"] == "优质煤")["feasible_pairs"] == 701 * 1501


def test_unique_ungraded_when_current_has_no_match(db):
    """原版兜底区域在现行版全部落空 => 唯一定级为未分级（grade=null）。"""
    req = RejudgeRequest(
        batch_no="B-UNG",
        original_version="v1",
        records=[
            SampleRecord(
                sample_no="S-ONLY-CATCHALL",
                moisture=Interval(lo=5000, hi=5010),
                ash=Interval(lo=5000, hi=5010),
                original_grade="等外煤",  # v1 兜底，v2 完全不覆盖该区域
            )
        ],
    )
    payload = rejudge_batch(db, req)
    sample = payload["samples"][0]
    assert sample["unique"] is True and sample["final_grade"] is None
    assert sample["options"][0]["grade"] is None
    assert sample["options"][0]["feasible_pairs"] == 121  # 11×11


def test_infeasible_original_grade_rejects_whole_batch(db):
    req = _req()
    req.records.append(
        SampleRecord(
            sample_no="S-BAD",
            moisture=Interval(lo=9000, hi=9500),
            ash=Interval(lo=9000, hi=9500),
            original_grade="优质煤",  # v1 下该区域只有等外煤，无解
        )
    )
    with pytest.raises(InfeasibleOriginalGrade) as exc:
        rejudge_batch(db, req)
    assert exc.value.sample_nos == ["S-BAD"]
    # 整批未入仓
    assert db.get_batch_result("B1") is None
    rejections = db.list_rejections("B1")
    assert len(rejections) == 1 and rejections[0]["reason"] == "infeasible_original_grade"


def test_original_version_missing(db):
    req = _req(original="vX")
    with pytest.raises(OriginalVersionNotFound):
        rejudge_batch(db, req)


def _commit_with_snapshot(db: Database, batch_no: str, snapshot):
    """用给定（可能已过期的）快照走一次入仓复核，返回事务结果。"""
    req = _req(batch_no)
    payload = {
        "batch_no": batch_no,
        "original_version": "v1",
        "judged_version": snapshot.code,
        "record_count": len(req.records),
        "samples": [],
    }
    return db.save_batch_result(
        batch_no=batch_no,
        original_code="v1",
        judged_code=snapshot.code,
        record_count=len(req.records),
        result_payload=payload,
        expected_active_version_id=snapshot.version_id,
        baseline_switch_epoch=snapshot.switch_epoch,
    )


def test_switch_away_and_back_still_rejects_409(db):
    """核心缺陷：v2 重判期间 v2→v3→v2，复核时版号相同也必须整批退回。"""
    first = rejudge_batch(db, _req("B2"))
    assert first["judged_version"] == "v2"

    # 重判开始于 v2，先拍快照
    snapshot = db.get_active_snapshot()
    assert snapshot.code == "v2"

    # 计算期间先发布启用 v3，又切回 v2（复核时现行版与重判用版同为 v2）
    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True)
    assert db.activate("v2")["switched"] is True
    assert db.get_active()[1] == "v2"

    outcome = _commit_with_snapshot(db, "B2", snapshot)
    assert outcome.committed is False
    # 复核时确为 v2，但期间实际换过版 => 退回，并准确报告复核版
    assert outcome.review_code == "v2"
    assert [s["code"] for s in outcome.switches] == ["v3", "v2"]

    # 服务层用同一过期快照也必须抛 VersionDrift（直接驱动编排层守卫）
    with pytest.raises(VersionDrift) as exc:
        rejudge_batch_with_snapshot(db, _req("B2"), snapshot)
    detail = exc.value.detail
    assert detail["rejudge_version"] == "v2"
    assert detail["review_version"] == "v2"
    assert detail["switched_during_rejudge"] is True
    assert detail["switch_chain"] == ["v2", "v3", "v2"]
    assert all(s["switched"] == 1 for s in detail["switches"])

    # 退回记录与 409 同口径，且上一份成功结果原样保留
    rej = db.list_rejections("B2")[0]
    assert rej["reason"] == "version_drift"
    assert rej["detail"]["rejudge_version"] == "v2"
    assert rej["detail"]["review_version"] == "v2"
    assert rej["detail"]["switch_chain"] == ["v2", "v3", "v2"]
    row = db.get_batch_result("B2")
    assert row["judged_version"] == "v2"
    assert row["record_count"] == 3  # 未被空/新结果覆盖


def rejudge_batch_with_snapshot(db: Database, req: RejudgeRequest, snapshot):
    """测试辅助：强制以指定快照走完整编排（模拟换版发生在拍快照之后）。"""
    from app.rejudge import evaluate_sample

    original_rules = db.get_rules(db.get_version_id(req.original_version))
    current_rules = db.get_rules(snapshot.version_id)
    samples = []
    for rec in req.records:
        options = evaluate_sample(
            (rec.moisture.lo, rec.moisture.hi),
            (rec.ash.lo, rec.ash.hi),
            rec.original_grade,
            original_rules,
            current_rules,
        )
        assert options is not None
        samples.append({"sample_no": rec.sample_no})
    payload = {
        "batch_no": req.batch_no,
        "original_version": req.original_version,
        "judged_version": snapshot.code,
        "record_count": len(req.records),
        "samples": samples,
    }
    outcome = db.save_batch_result(
        batch_no=req.batch_no,
        original_code=req.original_version,
        judged_code=snapshot.code,
        record_count=len(req.records),
        result_payload=payload,
        expected_active_version_id=snapshot.version_id,
        baseline_switch_epoch=snapshot.switch_epoch,
    )
    if not outcome.committed:
        switch_chain = [snapshot.code] + [s["code"] for s in outcome.switches]
        detail = {
            "reason": "version_drift",
            "message": "重判期间启用版本发生过实际切换，整批退回，保留上一份成功结果",
            "rejudge_version": snapshot.code,
            "review_version": outcome.review_code,
            "switched_during_rejudge": True,
            "switch_chain": switch_chain,
            "switches": outcome.switches,
        }
        db.record_rejection(req.batch_no, "version_drift", detail)
        from app.service import VersionDrift as _VD

        raise _VD(detail)
    return payload


def test_review_version_reported_correctly_when_still_on_v3(db):
    """复核时仍是 v3 的场景：退回记录不得把复核版报成重判用版 v2。"""
    rejudge_batch(db, _req("B3"))
    snapshot = db.get_active_snapshot()
    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True)

    outcome = _commit_with_snapshot(db, "B3", snapshot)
    assert outcome.committed is False
    assert outcome.review_code == "v3"
    assert [s["code"] for s in outcome.switches] == ["v3"]
    # 历史成功结果保留
    assert db.get_batch_result("B3")["judged_version"] == "v2"


def test_publish_only_does_not_count_as_switch(db):
    """单独发布（不启用）不推进换版纪元，重判照常入仓。"""
    snapshot = db.get_active_snapshot()
    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=False)
    assert db.get_active()[1] == "v2"
    # 发布后纪元不变，旧快照仍可入仓
    assert _commit_with_snapshot(db, "B4", snapshot).committed is True

    payload = rejudge_batch(db, _req("B4"))
    assert payload["judged_version"] == "v2"
    assert db.list_rejections("B4") == []
    assert db.get_batch_result("B4")["judged_version"] == "v2"


def test_reactivate_current_version_does_not_count_as_switch(db):
    """重启用现行版（switched=0）不算换版，进行中的重判可照常入仓。"""
    snapshot = db.get_active_snapshot()
    result = db.activate("v2")
    assert result["switched"] is False
    assert _commit_with_snapshot(db, "B5", snapshot).committed is True

    payload = rejudge_batch(db, _req("B5"))
    assert payload["judged_version"] == "v2"
    assert db.list_rejections("B5") == []


def test_publish_with_activation_of_new_version_counts_as_switch(db):
    """发布即启用另一个版号 => 实际换版，进行中重判退回 409。"""
    rejudge_batch(db, _req("B6"))
    snapshot = db.get_active_snapshot()
    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True)

    with pytest.raises(VersionDrift) as exc:
        rejudge_batch_with_snapshot(db, _req("B6"), snapshot)
    detail = exc.value.detail
    assert detail["rejudge_version"] == "v2"
    assert detail["review_version"] == "v3"
    assert detail["switch_chain"] == ["v2", "v3"]
    # 上一份成功结果保留
    assert db.get_batch_result("B6")["judged_version"] == "v2"


def test_successful_rejudge_after_switch_uses_new_version(db):
    """换版后重新发起的重判以新版为快照，成功入仓并覆盖最后成功结果。"""
    rejudge_batch(db, _req("B7"))
    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True)

    payload = rejudge_batch(db, _req("B7"))
    assert payload["judged_version"] == "v3"
    row = db.get_batch_result("B7")
    assert row["judged_version"] == "v3" and len(row["result"]["samples"]) == 3


def test_drift_guard_is_process_safe_via_sqlite(db):
    """两个 Database 实例（各自独立连接，等价同库多进程）遵守同一入仓规则。"""
    other = Database(db.path)
    rejudge_batch(db, _req("B8"))
    snapshot = db.get_active_snapshot()  # 重判开始于 v2

    barrier = threading.Barrier(2)

    def switcher():
        barrier.wait()
        other.create_version(
            "v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True
        )
        other.activate("v2")  # 切回 v2，制造 v2→v3→v2

    t = threading.Thread(target=switcher)
    t.start()
    barrier.wait()
    try:
        with pytest.raises(VersionDrift):
            rejudge_batch_with_snapshot(db, _req("B8"), snapshot)
    finally:
        t.join()

    assert db.get_active()[1] == "v2"
    assert db.get_batch_result("B8")["judged_version"] == "v2"  # 旧结果保留
    rej = db.list_rejections("B8")[0]
    assert rej["detail"]["switch_chain"] == ["v2", "v3", "v2"]


def test_activation_log_epoch_semantics(db):
    """审计纪元：仅 switched=1 推进；重启用现行版不推进。"""
    snap0 = db.get_active_snapshot()
    assert snap0.code == "v2"  # 种子 create_version(activate=True) 记一次实际换版
    baseline_epoch = snap0.switch_epoch
    assert baseline_epoch > 0

    db.activate("v2")  # no-op：不推进纪元
    assert db.get_active_snapshot().switch_epoch == baseline_epoch

    db.create_version("v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True)
    snap_v3 = db.get_active_snapshot()
    assert snap_v3.code == "v3" and snap_v3.switch_epoch > baseline_epoch

    db.activate("v2")
    snap_v2 = db.get_active_snapshot()
    assert snap_v2.code == "v2" and snap_v2.switch_epoch > snap_v3.switch_epoch

    db.activate("v2")  # 再次 no-op
    assert db.get_active_snapshot().switch_epoch == snap_v2.switch_epoch


def test_duplicate_version_conflict(db):
    from app.db import VersionConflict

    with pytest.raises(VersionConflict):
        db.create_version("v1", [r.model_dump() for r in V3_RULES], note=None, activate=False)


def test_rules_capped_at_twenty(db):
    rules = [
        RuleIn(grade="G", moisture=Interval(lo=0, hi=1), ash=Interval(lo=0, hi=1))
        for _ in range(21)
    ]
    with pytest.raises(Exception):
        VersionCreate(code="v-bad", rules=rules)


def test_interval_validation(db):
    with pytest.raises(Exception):
        Interval(lo=10, hi=5)
    with pytest.raises(Exception):
        Interval(lo=-1, hi=5)
    with pytest.raises(Exception):
        Interval(lo=0, hi=10001)


def test_legacy_sqlite_db_upgrade_preserves_everything(tmp_path):
    """旧结构库（无 activation_log 表）升级后：现行版、批次结果、退回记录全保留。"""
    import sqlite3

    legacy_path = str(tmp_path / "legacy.db")
    # 1) 用当前代码造一份带数据的库，再删掉新表，模拟上线前的旧结构库
    seeded = Database(legacy_path)
    seeded.create_version("v1", [r.model_dump() for r in V1_RULES], note="old", activate=False)
    seeded.create_version("v2", [r.model_dump() for r in V2_RULES], note="new", activate=True)
    rejudge_batch(seeded, _req("B-LEGACY"))
    seeded.record_rejection("B-LEGACY", "infeasible_original_grade", {"sample_nos": ["X"]})
    del seeded

    with sqlite3.connect(legacy_path) as raw:
        raw.execute("DROP TABLE activation_log")
        raw.commit()
        legacy_tables = {
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "activation_log" not in legacy_tables

    # 2) 用新代码重新打开 => 自动迁移
    upgraded = Database(legacy_path)

    # 现行版保留
    active = upgraded.get_active_snapshot()
    assert active is not None and active.code == "v2"
    # 迁移基线不推进换版纪元，重判可照常入仓
    assert active.switch_epoch == 0

    # 已有批次结果保留
    row = upgraded.get_batch_result("B-LEGACY")
    assert row is not None and row["judged_version"] == "v2"
    assert len(row["result"]["samples"]) == 3

    # 退回记录保留
    rejections = upgraded.list_rejections("B-LEGACY")
    assert any(r["reason"] == "infeasible_original_grade" for r in rejections)

    # 迁移后语义完整：换版仍会被纪元守卫拦截（先拍 v2 快照再换版）
    snapshot = upgraded.get_active_snapshot()
    upgraded.create_version(
        "v3", [r.model_dump() for r in V3_RULES], note="v3", activate=True
    )
    upgraded.activate("v2")
    with pytest.raises(VersionDrift):
        rejudge_batch_with_snapshot(upgraded, _req("B-LEGACY"), snapshot)
    # 旧结果仍在
    assert upgraded.get_batch_result("B-LEGACY")["judged_version"] == "v2"
