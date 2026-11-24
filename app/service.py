"""重判编排层：把 API 请求、核心算法与持久层串起来。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .db import ActiveSnapshot, Database
from .models import RejudgeRequest
from .rejudge import GradeFeasibility, evaluate_sample


class OriginalVersionNotFound(Exception):
    def __init__(self, code: str):
        super().__init__(f"原规则版号不存在: {code}")
        self.code = code


class NoActiveVersion(Exception):
    pass


class VersionDrift(Exception):
    """重判开始到入仓前实际切换过启用版本（即使又切回快照版）。"""

    def __init__(self, detail: Dict[str, Any]):
        self.detail = detail
        chain = " → ".join(detail["switch_chain"]) if detail["switch_chain"] else "无"
        super().__init__(
            "重判期间启用版本发生过实际切换："
            f"重判用版 {detail['rejudge_version']}，入仓复核时版号 "
            f"{detail['review_version'] or '无'}，期间换版链路 {chain}，整批退回 409"
        )


class InfeasibleOriginalGrade(Exception):
    """旧档区间内没有任何测值能按原版得出原等级。"""

    def __init__(self, sample_nos: List[str]):
        super().__init__("部分样号在原版规则下无解")
        self.sample_nos = sample_nos


def rejudge_batch(db: Database, req: RejudgeRequest) -> Dict[str, Any]:
    """执行一批重判并尝试入仓。

    流程：
    1. 取原规则与"重判开始时"的现行规则快照（版本 id、版号、换版纪元）；
    2. 逐条煤样在两个版本并集的压缩网格上求现行可行等级；
    3. 原等级无解的样号收集后整批退回（HTTP 422 语义，不写结果表）；
    4. 入仓写事务内以换版纪元复核：从读取现行版号到提交前只要实际换过版
       （含 v2→v3→v2 这类切回），整批退回（HTTP 409 语义），
       下游仍可查到该批号上一份成功结果及其所用版号。
    """
    original_id = db.get_version_id(req.original_version)
    if original_id is None:
        raise OriginalVersionNotFound(req.original_version)
    original_rules = db.get_rules(original_id)

    snapshot: Optional[ActiveSnapshot] = db.get_active_snapshot()
    if snapshot is None:
        raise NoActiveVersion("尚无启用的规则版本")
    current_rules = db.get_rules(snapshot.version_id)

    samples_payload: List[Dict[str, Any]] = []
    infeasible: List[str] = []

    for rec in req.records:
        options = evaluate_sample(
            moisture_interval=(rec.moisture.lo, rec.moisture.hi),
            ash_interval=(rec.ash.lo, rec.ash.hi),
            original_grade=rec.original_grade,
            original_rules=original_rules,
            current_rules=current_rules,
        )
        if options is None:
            infeasible.append(rec.sample_no)
            continue
        samples_payload.append(_build_sample(rec.sample_no, rec.original_grade, options))

    if infeasible:
        db.record_rejection(
            req.batch_no,
            "infeasible_original_grade",
            {"sample_nos": infeasible, "original_version": req.original_version},
        )
        raise InfeasibleOriginalGrade(infeasible)

    payload = {
        "batch_no": req.batch_no,
        "original_version": req.original_version,
        "judged_version": snapshot.code,
        "record_count": len(req.records),
        "samples": samples_payload,
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
        # 换版链路：从快照版开始，按审计顺序串起每次实际切换后的版号
        switch_chain = [snapshot.code] + [s["code"] for s in outcome.switches]
        detail = {
            "reason": "version_drift",
            "message": (
                "重判期间启用版本发生过实际切换，整批退回，保留上一份成功结果"
            ),
            "rejudge_version": snapshot.code,
            "review_version": outcome.review_code,
            "switched_during_rejudge": True,
            "switch_chain": switch_chain,
            "switches": outcome.switches,
        }
        db.record_rejection(req.batch_no, "version_drift", detail)
        raise VersionDrift(detail)
    return payload


def _build_sample(
    sample_no: str, original_grade: str | None, options: List[GradeFeasibility]
) -> Dict[str, Any]:
    unique = len(options) == 1
    return {
        "sample_no": sample_no,
        "original_grade": original_grade,
        "unique": unique,
        "final_grade": options[0].grade if unique else None,
        "options": [
            {
                "grade": opt.grade,
                "witness_moisture": opt.witness_moisture,
                "witness_ash": opt.witness_ash,
                "feasible_pairs": opt.feasible_pairs,
            }
            for opt in options
        ],
    }
