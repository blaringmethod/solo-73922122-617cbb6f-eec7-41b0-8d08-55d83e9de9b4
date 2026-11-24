"""SQLite 持久层。

表结构：
* ``rule_versions``  各版规则头（版号唯一）
* ``rules``          每版最多 20 条规则，seq 即判定顺序
* ``active_version`` 单行表，保存当前启用版本（重判期间可随时切换）
* ``activation_log`` 启用审计 + 单调换版纪元：每次启用追加一行，仅当目标版与当前
  启用版不同（实际换版）时 ``switched=1``；重启用现行版记 ``switched=0``，
  不推进换版纪元。重判入仓事务据此判断"期间是否实际换过版"，即使又切回快照版也能识别。
* ``batch_results``  每批最后一次成功结果（同批号重跑成功即覆盖）
* ``batch_rejections`` 退回记录（原等级无解 / 期间换版），便于审计，不影响下游查询
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .rejudge import Rule

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL REFERENCES rule_versions(id),
    seq         INTEGER NOT NULL,
    grade       TEXT NOT NULL,
    moisture_lo INTEGER NOT NULL,
    moisture_hi INTEGER NOT NULL,
    ash_lo      INTEGER NOT NULL,
    ash_hi      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_version ON rules(version_id, seq);

CREATE TABLE IF NOT EXISTS active_version (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version_id INTEGER NOT NULL REFERENCES rule_versions(id)
);

CREATE TABLE IF NOT EXISTS activation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL REFERENCES rule_versions(id),
    code        TEXT NOT NULL,
    switched    INTEGER NOT NULL CHECK (switched IN (0, 1)),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activation_switched ON activation_log(switched, id);

CREATE TABLE IF NOT EXISTS batch_results (
    batch_no       TEXT PRIMARY KEY,
    original_code  TEXT NOT NULL,
    judged_code    TEXT NOT NULL,
    record_count   INTEGER NOT NULL,
    result_json    TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_rejections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_batch ON batch_rejections(batch_no);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ActiveSnapshot:
    """重判开始时对启用版本拍的快照。

    ``switch_epoch`` 为截至快照时刻最后一次"实际换版"（switched=1）的审计行 id；
    尚未发生过实际换版时为 0。入仓事务以它为基线检测期间换版。
    """

    version_id: int
    code: str
    switch_epoch: int


@dataclass(frozen=True)
class CommitOutcome:
    """入仓写事务的复核结果。"""

    committed: bool
    review_version_id: Optional[int]
    review_code: Optional[str]
    switches: List[Dict[str, Any]]


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_activation_baseline(conn)

    @staticmethod
    def _migrate_activation_baseline(conn: sqlite3.Connection) -> None:
        """旧库升级：补建 activation_log 后写入一条 switched=0 的基线行。

        旧库中已有的现行版本、批次结果与退回记录全部原样保留；基线行不推进
        换版纪元（switched=0），因此升级前开始语义下不会凭空产生换版退回。
        """
        row = conn.execute("SELECT COUNT(*) AS n FROM activation_log").fetchone()
        if row["n"] > 0:
            return
        active = conn.execute(
            "SELECT a.version_id AS version_id, v.code AS code "
            "FROM active_version a JOIN rule_versions v ON v.id = a.version_id "
            "WHERE a.id = 1"
        ).fetchone()
        if active is not None:
            conn.execute(
                "INSERT INTO activation_log(version_id, code, switched, created_at) "
                "VALUES (?, ?, 0, ?)",
                (active["version_id"], active["code"], _utc_now()),
            )
        conn.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ versions
    def create_version(
        self, code: str, rules: Sequence[Dict[str, Any]], note: Optional[str], activate: bool
    ) -> int:
        """发布新版本；activate=True 时在同一事务内启用。返回版本 id。

        发布即启用时，只有该新版与当前启用版不同才算实际换版（首次启用必然换版）；
        仅发布不启用不写 activation_log，不推进换版纪元。
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT INTO rule_versions(code, note, created_at) VALUES (?, ?, ?)",
                    (code, note, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:  # 版号重复
                conn.rollback()
                raise VersionConflict(code) from exc
            version_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO rules(version_id, seq, grade, moisture_lo, moisture_hi, "
                "ash_lo, ash_hi) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        version_id,
                        seq,
                        r["grade"],
                        r["moisture"]["lo"],
                        r["moisture"]["hi"],
                        r["ash"]["lo"],
                        r["ash"]["hi"],
                    )
                    for seq, r in enumerate(rules)
                ],
            )
            switched = 0
            if activate:
                row = conn.execute(
                    "SELECT version_id FROM active_version WHERE id = 1"
                ).fetchone()
                switched = 0 if (row is not None and row["version_id"] == version_id) else 1
                conn.execute(
                    "INSERT INTO active_version(id, version_id) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version_id = excluded.version_id",
                    (version_id,),
                )
                conn.execute(
                    "INSERT INTO activation_log(version_id, code, switched, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (version_id, code, switched, _utc_now()),
                )
            conn.commit()
        return version_id

    def get_version_id(self, code: str) -> Optional[int]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM rule_versions WHERE code = ?", (code,)
            ).fetchone()
            return row["id"] if row else None

    def get_rules(self, version_id: int) -> List[Rule]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT grade, moisture_lo, moisture_hi, ash_lo, ash_hi "
                "FROM rules WHERE version_id = ? ORDER BY seq",
                (version_id,),
            ).fetchall()
        return [
            (r["grade"], r["moisture_lo"], r["moisture_hi"], r["ash_lo"], r["ash_hi"])
            for r in rows
        ]

    def get_active(self) -> Optional[Tuple[int, str]]:
        """返回 (version_id, code)；尚无启用版本时返回 None。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT v.id AS id, v.code AS code FROM active_version a "
                "JOIN rule_versions v ON v.id = a.version_id WHERE a.id = 1"
            ).fetchone()
            return (row["id"], row["code"]) if row else None

    def get_active_snapshot(self) -> Optional[ActiveSnapshot]:
        """读取现行版快照：版本 id、版号与当前换版纪元（最后一次实际换版行 id）。

        同一条只读连接内完成两次读取；尚无启用版本时返回 None。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT v.id AS id, v.code AS code FROM active_version a "
                "JOIN rule_versions v ON v.id = a.version_id WHERE a.id = 1"
            ).fetchone()
            if row is None:
                return None
            epoch_row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS epoch FROM activation_log "
                "WHERE switched = 1"
            ).fetchone()
            return ActiveSnapshot(
                version_id=row["id"], code=row["code"], switch_epoch=epoch_row["epoch"]
            )

    def activate(self, code: str) -> Optional[Dict[str, Any]]:
        """启用指定版号；版号不存在返回 None。

        仅当目标版与当前启用版不同时记 switched=1（实际换版，推进换版纪元）；
        重启用现行版记 switched=0，不影响进行中重判的入仓资格。
        返回 ``{"code", "switched"}``。
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM rule_versions WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            target_id = row["id"]
            current = conn.execute(
                "SELECT version_id FROM active_version WHERE id = 1"
            ).fetchone()
            switched = 0 if (current is not None and current["version_id"] == target_id) else 1
            conn.execute(
                "INSERT INTO active_version(id, version_id) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET version_id = excluded.version_id",
                (target_id,),
            )
            conn.execute(
                "INSERT INTO activation_log(version_id, code, switched, created_at) "
                "VALUES (?, ?, ?, ?)",
                (target_id, code, switched, _utc_now()),
            )
            conn.commit()
        return {"code": code, "switched": bool(switched)}

    def list_versions(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT v.id, v.code, v.note, v.created_at, "
                "  (SELECT COUNT(*) FROM rules r WHERE r.version_id = v.id) AS rule_count, "
                "  (SELECT version_id FROM active_version WHERE id = 1) AS active_id "
                "FROM rule_versions v ORDER BY v.id"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "code": r["code"],
                "active": r["active_id"] == r["id"],
                "rule_count": r["rule_count"],
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_rules(self, code: str) -> Optional[List[Dict[str, Any]]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM rule_versions WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return None
            rules = conn.execute(
                "SELECT seq, grade, moisture_lo, moisture_hi, ash_lo, ash_hi "
                "FROM rules WHERE version_id = ? ORDER BY seq",
                (row["id"],),
            ).fetchall()
        return [dict(r) for r in rules]

    # ------------------------------------------------------------------- batches
    def save_batch_result(
        self,
        batch_no: str,
        original_code: str,
        judged_code: str,
        record_count: int,
        result_payload: Dict[str, Any],
        expected_active_version_id: int,
        baseline_switch_epoch: int,
    ) -> CommitOutcome:
        """在同一写事务内复核后，再覆盖入仓最后一次成功结果。

        退回（不写入、不覆盖历史结果）的两种情形：
        1. 重判开始到提交前发生过**实际换版**——activation_log 中存在 id 大于
           基线纪元的 switched=1 记录。即使启用版又切回快照版（v2→v3→v2），
           纪元推进过也照样整批退回；
        2. 复核时启用版与重判快照不一致（兜底，覆盖"无启用版"等异常）。

        复核时刻的版号与换版链路都在本事务内读取，避免提交后再查导致
        把复核版误报成快照版。
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_row = conn.execute(
                "SELECT a.version_id AS version_id, v.code AS code "
                "FROM active_version a JOIN rule_versions v ON v.id = a.version_id "
                "WHERE a.id = 1"
            ).fetchone()
            switch_rows = conn.execute(
                "SELECT id, version_id, code, switched, created_at FROM activation_log "
                "WHERE switched = 1 AND id > ? ORDER BY id",
                (baseline_switch_epoch,),
            ).fetchall()
            switches = [dict(r) for r in switch_rows]

            drifted = bool(switches) or (
                active_row is None or active_row["version_id"] != expected_active_version_id
            )
            if drifted:
                conn.rollback()
                return CommitOutcome(
                    committed=False,
                    review_version_id=active_row["version_id"] if active_row else None,
                    review_code=active_row["code"] if active_row else None,
                    switches=switches,
                )

            conn.execute(
                "INSERT INTO batch_results(batch_no, original_code, judged_code, "
                "record_count, result_json, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_no) DO UPDATE SET "
                "original_code = excluded.original_code, judged_code = excluded.judged_code, "
                "record_count = excluded.record_count, result_json = excluded.result_json, "
                "updated_at = excluded.updated_at",
                (
                    batch_no,
                    original_code,
                    judged_code,
                    record_count,
                    json.dumps(result_payload, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            conn.commit()
        return CommitOutcome(
            committed=True,
            review_version_id=active_row["version_id"],
            review_code=active_row["code"],
            switches=[],
        )

    def get_batch_result(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batch_results WHERE batch_no = ?", (batch_no,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["result_json"])
        return {
            "batch_no": row["batch_no"],
            "original_version": row["original_code"],
            "judged_version": row["judged_code"],
            "record_count": row["record_count"],
            "updated_at": row["updated_at"],
            "result": payload,
        }

    def record_rejection(
        self, batch_no: str, reason: str, detail: Dict[str, Any]
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO batch_rejections(batch_no, reason, detail_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (batch_no, reason, json.dumps(detail, ensure_ascii=False), _utc_now()),
            )
            conn.commit()

    def list_rejections(self, batch_no: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            if batch_no is None:
                rows = conn.execute(
                    "SELECT * FROM batch_rejections ORDER BY id DESC LIMIT 100"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM batch_rejections WHERE batch_no = ? ORDER BY id DESC",
                    (batch_no,),
                ).fetchall()
        return [
            {
                "id": r["id"],
                "batch_no": r["batch_no"],
                "reason": r["reason"],
                "detail": json.loads(r["detail_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


class VersionConflict(Exception):
    def __init__(self, code: str):
        super().__init__(f"规则版号已存在: {code}")
        self.code = code
