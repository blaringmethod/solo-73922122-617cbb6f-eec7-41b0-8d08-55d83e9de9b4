"""FastAPI 端到端测试：/docs 所需接口、HTTP 状态码、下游查询语义。"""

from __future__ import annotations

import os

import pytest

# 必须在导入 app.main 之前指定独立测试库
os.environ["COAL_DB_PATH"] = os.path.join(os.path.dirname(__file__), "_api_test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, get_db  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 每个测试模块使用全新临时库：先清掉播种文件，结束后再清理
    db_path = os.environ["COAL_DB_PATH"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
    try:
        with TestClient(app) as c:
            yield c
    finally:
        # 关闭残留连接后清理临时库文件（WAL 模式下还有 -wal/-shm）
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass


def test_health_and_seed(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["active_version"] == "v2"
    assert body["versions"] == 2

    r = client.get("/api/versions")
    codes = {v["code"]: v for v in r.json()}
    assert set(codes) == {"v1", "v2"}
    assert codes["v2"]["active"] is True
    assert codes["v1"]["rule_count"] == 3

    r = client.get("/api/versions/v2/rules")
    assert r.status_code == 200 and len(r.json()) == 2


def test_openapi_docs_available(client):
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_rejudge_unique_multi_and_persistence(client):
    payload = {
        "batch_no": "B-API-1",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 400, "hi": 600},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            },
            {
                "sample_no": "S-002",
                "moisture": {"lo": 900, "hi": 1150},
                "ash": {"lo": 2300, "hi": 2700},
                "original_grade": "普通煤",
            },
        ],
    }
    r = client.post("/api/rejudge", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["judged_version"] == "v2"
    s1, s2 = body["samples"]
    assert s1["unique"] is True and s1["final_grade"] == "优质煤"
    assert s2["unique"] is False and s2["final_grade"] is None
    assert len(s2["options"]) >= 2

    # 下游按批号查询
    r = client.get("/api/batches/B-API-1")
    assert r.status_code == 200
    assert r.json()["judged_version"] == "v2"


def test_rejudge_infeasible_returns_422_and_no_result(client):
    payload = {
        "batch_no": "B-API-2",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-BAD",
                "moisture": {"lo": 9000, "hi": 9500},
                "ash": {"lo": 9000, "hi": 9500},
                "original_grade": "优质煤",
            }
        ],
    }
    r = client.post("/api/rejudge", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "infeasible_original_grade"
    assert detail["sample_nos"] == ["S-BAD" ]
    assert client.get("/api/batches/B-API-2").status_code == 404
    rejs = client.get("/api/batches/B-API-2/rejections").json()
    assert rejs[0]["reason"] == "infeasible_original_grade"


def test_publish_only_and_reactivate_keep_rejudge_commit_ok(client):
    """单独发布不启用、重启用现行版都不算换版，重判照常入仓。"""
    good = {
        "batch_no": "B-API-NOOP",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 400, "hi": 600},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            }
        ],
    }
    # 只发布不启用
    new_ver = {
        "code": "v9",
        "rules": [
            {"grade": "精煤", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}}
        ],
    }
    r = client.post("/api/versions?activate=false", json=new_ver)
    assert r.status_code == 201 and r.json()["active"] is False
    assert client.post("/api/rejudge", json=good).status_code == 200

    # 重启用现行版 v2 => switched=false，不触发退回
    r = client.post("/api/versions/v2/activate")
    assert r.status_code == 200 and r.json()["switched"] is False
    r = client.post("/api/rejudge", json=good)
    assert r.status_code == 200 and r.json()["judged_version"] == "v2"
    assert client.get("/api/batches/B-API-NOOP/rejections").json() == []


def test_rejudge_drift_409_when_switched_away_and_back(client, monkeypatch):
    """重判窗口内 v2→v3→v2：即使复核时又是 v2，也必须 409 并保留上一份结果。"""
    good = {
        "batch_no": "B-API-DRIFT",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 400, "hi": 600},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=good).status_code == 200

    # 发布 v3（先不启用）
    new_ver = {
        "code": "v3",
        "note": "重判期间发布",
        "rules": [
            {"grade": "精煤", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}}
        ],
    }
    assert client.post("/api/versions", json=new_ver).status_code == 201

    db = get_db()
    # 在重判计算的第一条样上触发 v2→v3→v2：此时快照已拍、入仓事务尚未开始
    import app.service as service_mod

    real_evaluate = service_mod.evaluate_sample

    def switched_evaluate(*args, **kwargs):
        if not getattr(switched_evaluate, "done", False):
            switched_evaluate.done = True
            assert db.activate("v3")["switched"] is True
            assert db.activate("v2")["switched"] is True
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(service_mod, "evaluate_sample", switched_evaluate)
    r = client.post("/api/rejudge", json=good)

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "version_drift"
    assert detail["rejudge_version"] == "v2"
    assert detail["review_version"] == "v2"  # 复核时确为 v2，不得误报
    assert detail["switch_chain"] == ["v2", "v3", "v2"]

    # 退回记录与 409 同口径；上一份成功结果保留且仍是 v2
    rejs = client.get("/api/batches/B-API-DRIFT/rejections").json()
    latest = rejs[0]
    assert latest["reason"] == "version_drift"
    assert latest["detail"]["rejudge_version"] == "v2"
    assert latest["detail"]["review_version"] == "v2"
    assert latest["detail"]["switch_chain"] == ["v2", "v3", "v2"]
    row = client.get("/api/batches/B-API-DRIFT").json()
    assert row["judged_version"] == "v2"

    # 全部退回审计里也能查到
    all_rejs = client.get("/api/rejections").json()
    assert any(rj["batch_no"] == "B-API-DRIFT" and rj["reason"] == "version_drift"
               for rj in all_rejs)


def test_rejudge_drift_409_review_version_is_v3(client, monkeypatch):
    """复核时仍是 v3：409 与退回记录要把复核版报成 v3，而不是重判用版 v2。"""
    good = {
        "batch_no": "B-API-V3",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 400, "hi": 600},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=good).status_code == 200
    new_ver = {
        "code": "v3r",
        "rules": [
            {"grade": "精煤", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}}
        ],
    }
    assert client.post("/api/versions", json=new_ver).status_code == 201

    db = get_db()
    import app.service as service_mod

    real_evaluate = service_mod.evaluate_sample

    def switched_evaluate(*args, **kwargs):
        if not getattr(switched_evaluate, "done", False):
            switched_evaluate.done = True
            db.activate("v3r")  # 重判窗口内只切到 v3r，不切回
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(service_mod, "evaluate_sample", switched_evaluate)
    r = client.post("/api/rejudge", json=good)

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["rejudge_version"] == "v2"
    assert detail["review_version"] == "v3r"
    assert detail["switch_chain"] == ["v2", "v3r"]
    latest = client.get("/api/batches/B-API-V3/rejections").json()[0]
    assert latest["detail"]["review_version"] == "v3r"
    # 上一份成功结果保留（v2 那次）
    assert client.get("/api/batches/B-API-V3").json()["judged_version"] == "v2"


def test_switch_then_rejudge_succeeds_with_new_version(client):
    """换版后重新发起的重判按新版计算并成功覆盖入仓。"""
    good = {
        "batch_no": "B-API-OK",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S-001",
                "moisture": {"lo": 400, "hi": 600},
                "ash": {"lo": 1000, "hi": 1400},
                "original_grade": "优质煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=good).status_code == 200

    # 发布并启用 v5r => 之后新发起的重判以 v5r 为快照，成功入仓
    new_ver = {
        "code": "v5r",
        "rules": [
            {"grade": "精煤", "moisture": {"lo": 0, "hi": 10000}, "ash": {"lo": 0, "hi": 10000}}
        ],
    }
    r = client.post("/api/versions?activate=true", json=new_ver)
    assert r.status_code == 201 and r.json()["active"] is True
    r = client.post("/api/rejudge", json=good)
    assert r.status_code == 200 and r.json()["judged_version"] == "v5r"
    assert client.get("/api/batches/B-API-OK").json()["judged_version"] == "v5r"


def test_validation_errors(client):
    # 区间反向
    bad_interval = {
        "batch_no": "B-BAD",
        "original_version": "v1",
        "records": [
            {
                "sample_no": "S",
                "moisture": {"lo": 9, "hi": 1},
                "ash": {"lo": 0, "hi": 1},
                "original_grade": "优质煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=bad_interval).status_code == 422

    # 超过 100 条
    too_many = {
        "batch_no": "B-BIG",
        "original_version": "v1",
        "records": [
            {
                "sample_no": f"S{i}",
                "moisture": {"lo": 0, "hi": 1},
                "ash": {"lo": 0, "hi": 1},
                "original_grade": "等外煤",
            }
            for i in range(101)
        ],
    }
    assert client.post("/api/rejudge", json=too_many).status_code == 422

    # 原规则版号不存在
    ghost = {
        "batch_no": "B-GHOST",
        "original_version": "nope",
        "records": [
            {
                "sample_no": "S",
                "moisture": {"lo": 0, "hi": 1},
                "ash": {"lo": 0, "hi": 1},
                "original_grade": "等外煤",
            }
        ],
    }
    assert client.post("/api/rejudge", json=ghost).status_code == 404


def test_duplicate_version_and_unknown_activate(client):
    payload = {
        "code": "v1",
        "rules": [
            {"grade": "X", "moisture": {"lo": 0, "hi": 1}, "ash": {"lo": 0, "hi": 1}}
        ],
    }
    assert client.post("/api/versions", json=payload).status_code == 409
    assert client.post("/api/versions/ghost/activate").status_code == 404
