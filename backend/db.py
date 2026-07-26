from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from logger_config import get_logger
from secrets_store import decrypt, encrypt

logger = get_logger("db")

SCHEMA_VERSION = 212

# ---------------------------------------------------------------------------
# 数据库锁策略（N1 锁优化）
# ---------------------------------------------------------------------------
# 历史实现使用全局 threading.RLock() 在 connect() 内包裹整个连接生命周期，
# 这会串行化所有数据库访问（包括只读 SELECT），无法利用 SQLite WAL 模式的并发读能力。
#
# 现采用读写分离策略：
#   - 只读操作（SELECT）：通过 connect() 直接获取连接，不持有任何 Python 层锁，
#     依赖 SQLite 的 WAL 模式与 busy_timeout=30000 实现并发读。
#   - 写操作 / Schema 迁移（init_db 等需串行化的写场景）：使用 _DB_WRITE_LOCK 串行化。
# connect() 本身不再持有全局锁，普通读写请求均依赖 SQLite 自身的并发控制。
_DB_WRITE_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path() -> Path:
    configured = os.environ.get("STUDIO_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "data" / "studio.db").resolve()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    # N1 锁优化：不再使用全局锁包裹整个连接生命周期。
    # 只读操作直接依赖 SQLite WAL 模式 + busy_timeout 实现并发；
    # 需串行化的写场景（如 init_db 的 schema 迁移）由调用方按需持有 _DB_WRITE_LOCK。
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# N5 WAL checkpoint 策略
# ---------------------------------------------------------------------------
# 系统使用 SQLite WAL 模式（见 connect() 中的 PRAGMA journal_mode = WAL），
# 但从未执行 PRAGMA wal_checkpoint(TRUNCATE)，导致 WAL 文件无限增长。
#
# 现引入后台守护线程周期性执行 wal_checkpoint(TRUNCATE)：
#   - 将 WAL 日志合并回主库并截断 WAL 文件，回收磁盘空间。
#   - 默认每 6 小时执行一次，可通过 STUDIO_WAL_CHECKPOINT_INTERVAL 环境变量
#     （单位：秒）覆盖；设为非正数可禁用该后台任务。
#   - 线程为 daemon，不会阻塞进程关闭。
_DEFAULT_WAL_CHECKPOINT_INTERVAL = 6 * 60 * 60  # 6 小时，单位秒

# 模块级句柄，用于跟踪 checkpoint 守护线程与停止信号（便于测试优雅关闭）。
_wal_checkpoint_stop: threading.Event | None = None
_wal_checkpoint_thread: threading.Thread | None = None


def wal_checkpoint() -> bool:
    """执行 PRAGMA wal_checkpoint(TRUNCATE)，将 WAL 日志合并回主库并截断 WAL 文件。

    使用 _DB_WRITE_LOCK 串行化，避免与 schema 迁移等需串行化的写场景冲突。
    PRAGMA wal_checkpoint(TRUNCATE) 返回 (busy, log, checkpointed) 三元组：
      - busy: 1 表示有读写连接正在使用数据库，checkpoint 未完整执行；
      - log: WAL 文件中的帧数；
      - checkpointed: 实际写入主库的帧数。
    返回 True 表示 PRAGMA 执行成功（不等同于 busy=0）。
    """
    try:
        with _DB_WRITE_LOCK:
            with connect() as conn:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, log_frames, checkpointed = (row[0] if row else -1, row[1] if row else -1, row[2] if row else -1)
        logger.info(
            "WAL checkpoint(TRUNCATE) 完成: busy=%s log=%s checkpointed=%s",
            busy,
            log_frames,
            checkpointed,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("WAL checkpoint 执行失败: %s", exc, exc_info=True)
        return False


def _wal_checkpoint_loop(interval: int, stop_event: threading.Event) -> None:
    """后台周期性执行 WAL checkpoint 的守护线程主循环。

    使用 stop_event.wait(interval) 替代 time.sleep，收到停止信号时可立即退出。
    首次执行前先等待一个完整周期，避免与 init_db 收尾阶段竞争资源。
    """
    logger.info("WAL checkpoint 守护线程已启动，执行间隔 %d 秒", interval)
    while not stop_event.wait(interval):
        try:
            wal_checkpoint()
        except Exception as exc:  # noqa: BLE001
            # 兜底保护：单次 checkpoint 异常不应终止守护线程
            logger.error("WAL checkpoint 守护线程捕获异常（已忽略，继续运行）: %s", exc, exc_info=True)
    logger.info("WAL checkpoint 守护线程已停止")


def _start_wal_checkpoint_thread() -> None:
    """启动后台 WAL checkpoint 守护线程（若已存活则跳过）。

    间隔由 STUDIO_WAL_CHECKPOINT_INTERVAL 环境变量（秒）控制，默认 6 小时。
    设为非正数则禁用后台 checkpoint。
    """
    global _wal_checkpoint_stop, _wal_checkpoint_thread
    if _wal_checkpoint_thread is not None and _wal_checkpoint_thread.is_alive():
        return
    try:
        interval = int(os.environ.get("STUDIO_WAL_CHECKPOINT_INTERVAL", str(_DEFAULT_WAL_CHECKPOINT_INTERVAL)))
    except (TypeError, ValueError):
        interval = _DEFAULT_WAL_CHECKPOINT_INTERVAL
    if interval <= 0:
        logger.info(
            "STUDIO_WAL_CHECKPOINT_INTERVAL=%d，跳过 WAL checkpoint 守护线程启动",
            interval,
        )
        return
    _wal_checkpoint_stop = threading.Event()
    _wal_checkpoint_thread = threading.Thread(
        target=_wal_checkpoint_loop,
        args=(interval, _wal_checkpoint_stop),
        name="db-wal-checkpoint",
        daemon=True,
    )
    _wal_checkpoint_thread.start()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _schema_version(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
            if not row:
                return 0
            value = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            return int(value[0]) if value else 0
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"数据库无法读取，迁移已中止：{exc}") from exc


def _integrity_check(conn: sqlite3.Connection, label: str) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if not result or str(result[0]).lower() != "ok":
        raise RuntimeError(f"{label}完整性校验失败：{result[0] if result else '无结果'}")


def _backup_database(path: Path, old_version: int) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-2.1.2-v{old_version}-{timestamp}.bak")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
        _integrity_check(source, "迁移前数据库")
        source.backup(target)
        _integrity_check(target, "迁移备份")
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup


def _restore_database(backup: Path, path: Path) -> None:
    restore_temp = path.with_name(path.name + ".restore.tmp")
    restore_temp.unlink(missing_ok=True)
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restore_temp)) as target:
        _integrity_check(source, "回滚备份")
        source.backup(target)
        _integrity_check(target, "回滚临时库")
    os.replace(restore_temp, path)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS migration_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_version INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            backup_path TEXT NOT NULL DEFAULT '',
            error_detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            source_input TEXT NOT NULL DEFAULT '',
            requirements TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'topic',
            status TEXT NOT NULL DEFAULT 'draft',
            archived INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            outline_json TEXT NOT NULL DEFAULT '[]',
            body_markdown TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            cover_data_url TEXT NOT NULL DEFAULT '',
            review_json TEXT NOT NULL DEFAULT '[]',
            review_fingerprint TEXT NOT NULL DEFAULT '',
            review_approved INTEGER NOT NULL DEFAULT 0,
            review_revision INTEGER NOT NULL DEFAULT 0,
            reviewed_at TEXT NOT NULL DEFAULT '',
            publish_status TEXT NOT NULL DEFAULT 'not_synced',
            publish_remote_id TEXT NOT NULL DEFAULT '',
            published_revision INTEGER NOT NULL DEFAULT 0,
            publish_fingerprint TEXT NOT NULL DEFAULT '',
            publish_preview_hash TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            current_step TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_detail TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            auto_review INTEGER NOT NULL DEFAULT 1,
            retry_mode TEXT NOT NULL DEFAULT 'full',
            base_revision INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            level TEXT NOT NULL,
            step TEXT NOT NULL,
            message TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS source_snapshots(
            id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            source_url TEXT NOT NULL,
            final_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            publisher TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            content_text TEXT NOT NULL,
            preview TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            extraction_method TEXT NOT NULL DEFAULT '',
            UNIQUE(source_url, content_hash)
        );
        CREATE TABLE IF NOT EXISTS project_sources(
            project_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            source_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(project_id, snapshot_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(snapshot_id) REFERENCES source_snapshots(id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS publish_receipts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            body_fingerprint TEXT NOT NULL,
            preview_hash TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            status TEXT NOT NULL,
            response_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS health_checks(
            service TEXT PRIMARY KEY,
            configured INTEGER NOT NULL DEFAULT 0,
            reachable INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT ''
        );
        """
    )


def _rebuild_source_snapshots_if_needed(conn: sqlite3.Connection) -> None:
    sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='source_snapshots'").fetchone()
    sql = str(sql_row[0] if sql_row else "").lower().replace(" ", "")
    if not sql or "unique(source_url,content_hash)" in sql:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE source_snapshots_v212(
            id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            source_url TEXT NOT NULL,
            final_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            publisher TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            content_text TEXT NOT NULL,
            preview TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            extraction_method TEXT NOT NULL DEFAULT '',
            UNIQUE(source_url, content_hash)
        );
        INSERT OR IGNORE INTO source_snapshots_v212
        SELECT id,content_hash,source_url,final_url,title,publisher,author,published_at,content_text,preview,fetched_at,extraction_method
        FROM source_snapshots;
        CREATE TABLE project_sources_v212(
            project_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            source_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(project_id,snapshot_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(snapshot_id) REFERENCES source_snapshots_v212(id) ON DELETE RESTRICT
        );
        INSERT OR IGNORE INTO project_sources_v212 SELECT project_id,snapshot_id,source_order FROM project_sources;
        DROP TABLE project_sources;
        DROP TABLE source_snapshots;
        ALTER TABLE source_snapshots_v212 RENAME TO source_snapshots;
        ALTER TABLE project_sources_v212 RENAME TO project_sources;
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_hash ON source_snapshots(source_url,content_hash);
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def init_db() -> None:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old_version = _schema_version(path)
    backup: Path | None = None
    # schema 迁移属于写操作，使用写锁串行化，避免迁移期间并发写入导致状态不一致
    with _DB_WRITE_LOCK:
        if path.exists() and old_version < SCHEMA_VERSION:
            backup = _backup_database(path, old_version)
        migration_id: int | None = None
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("BEGIN IMMEDIATE")
            _create_schema(conn)
            if conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
                conn.execute("INSERT INTO schema_meta(version) VALUES (0)")
            migration_id = conn.execute(
                "INSERT INTO migration_log(target_version,started_at,status,backup_path) VALUES(?,?,?,?)",
                (SCHEMA_VERSION, utc_now(), "running", str(backup or "")),
            ).lastrowid

            project_cols = {
                "goal": "TEXT NOT NULL DEFAULT ''",
                "source_input": "TEXT NOT NULL DEFAULT ''",
                "requirements": "TEXT NOT NULL DEFAULT ''",
                "source_kind": "TEXT NOT NULL DEFAULT 'topic'",
                "status": "TEXT NOT NULL DEFAULT 'draft'",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "deleted": "INTEGER NOT NULL DEFAULT 0",
                "outline_json": "TEXT NOT NULL DEFAULT '[]'",
                "body_markdown": "TEXT NOT NULL DEFAULT ''",
                "summary": "TEXT NOT NULL DEFAULT ''",
                "cover_data_url": "TEXT NOT NULL DEFAULT ''",
                "review_json": "TEXT NOT NULL DEFAULT '[]'",
                "review_fingerprint": "TEXT NOT NULL DEFAULT ''",
                "review_approved": "INTEGER NOT NULL DEFAULT 0",
                "review_revision": "INTEGER NOT NULL DEFAULT 0",
                "reviewed_at": "TEXT NOT NULL DEFAULT ''",
                "publish_status": "TEXT NOT NULL DEFAULT 'not_synced'",
                "publish_remote_id": "TEXT NOT NULL DEFAULT ''",
                "published_revision": "INTEGER NOT NULL DEFAULT 0",
                "publish_fingerprint": "TEXT NOT NULL DEFAULT ''",
                "publish_preview_hash": "TEXT NOT NULL DEFAULT ''",
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, ddl in project_cols.items():
                _add_column(conn, "projects", name, ddl)
            task_cols = {
                "parent_task_id": "TEXT NOT NULL DEFAULT ''",
                "current_step": "TEXT NOT NULL DEFAULT ''",
                "progress": "INTEGER NOT NULL DEFAULT 0",
                "message": "TEXT NOT NULL DEFAULT ''",
                "error_code": "TEXT NOT NULL DEFAULT ''",
                "error_detail": "TEXT NOT NULL DEFAULT ''",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "auto_review": "INTEGER NOT NULL DEFAULT 1",
                "retry_mode": "TEXT NOT NULL DEFAULT 'full'",
                "base_revision": "INTEGER NOT NULL DEFAULT 1",
                "started_at": "TEXT NOT NULL DEFAULT ''",
                "finished_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, ddl in task_cols.items():
                _add_column(conn, "tasks", name, ddl)
            _rebuild_source_snapshots_if_needed(conn)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(deleted,archived,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id,status,started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id,id);
                CREATE INDEX IF NOT EXISTS idx_versions_project ON project_versions(project_id,id DESC);
                CREATE INDEX IF NOT EXISTS idx_snapshots_source_hash ON source_snapshots(source_url,content_hash);
                CREATE INDEX IF NOT EXISTS idx_publish_receipts_project ON publish_receipts(project_id,id DESC);
                """
            )

            defaults = {
                "ai": {
                    "providerId": "openai-compatible",
                    "baseUrl": "https://api.openai.com/v1",
                    "apiKey": "",
                    "apiKeyHintStored": "",
                    "model": "gpt-4.1-mini",
                    "temperature": 0.4,
                    "autoReview": True,
                    "lastVerifiedAt": "",
                    "lastVerifyMessage": "",
                },
                "wechat": {
                    "appId": "",
                    "appSecret": "",
                    "appSecretHintStored": "",
                    "accountName": "",
                    "thumbMediaId": "",
                    "verifiedAt": "",
                    "lastVerifyMessage": "",
                },
                "general": {"defaultLength": 1800, "strictFacts": False, "allowNetwork": True},
            }
            now = utc_now()
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            for key, field, hint_field in (
                ("ai", "apiKey", "apiKeyHintStored"),
                ("wechat", "appSecret", "appSecretHintStored"),
            ):
                row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
                value = json_load(row["value_json"], {}) if row else {}
                secret = str(value.get(field) or "")
                if secret and not secret.startswith("enc:v1:"):
                    value[hint_field] = secret[-4:]
                    value[field] = encrypt(secret)
                    conn.execute(
                        "UPDATE settings SET value_json=?,updated_at=? WHERE key=?",
                        (json.dumps(value, ensure_ascii=False), now, key),
                    )
            conn.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))
            conn.execute(
                "UPDATE migration_log SET status='succeeded',completed_at=? WHERE id=?",
                (utc_now(), migration_id),
            )
            conn.commit()
            _integrity_check(conn, "迁移后数据库")
        except Exception as exc:
            conn.rollback()
            try:
                if migration_id is not None:
                    conn.execute(
                        "UPDATE migration_log SET status='failed',completed_at=?,error_detail=? WHERE id=?",
                        (utc_now(), repr(exc)[:2000], migration_id),
                    )
                    conn.commit()
            except Exception:
                pass
            conn.close()
            if backup:
                _restore_database(backup, path)
            raise RuntimeError(f"数据库迁移失败，已执行回滚：{exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    # N5：schema 迁移完成后启动 WAL checkpoint 守护线程，周期性截断 WAL 文件
    _start_wal_checkpoint_thread()


def json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _value(row: sqlite3.Row, name: str, default: Any = "") -> Any:
    return row[name] if name in row.keys() else default


def row_to_project(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "goal": _value(row, "goal"),
        "sourceInput": _value(row, "source_input"),
        "requirements": _value(row, "requirements"),
        "sourceKind": _value(row, "source_kind", "topic"),
        "status": _value(row, "status", "draft"),
        "archived": bool(_value(row, "archived", 0)),
        "deleted": bool(_value(row, "deleted", 0)),
        "outline": json_load(_value(row, "outline_json", "[]"), []),
        "bodyMarkdown": _value(row, "body_markdown"),
        "summary": _value(row, "summary"),
        "coverDataUrl": _value(row, "cover_data_url"),
        "review": json_load(_value(row, "review_json", "[]"), []),
        "reviewFingerprint": _value(row, "review_fingerprint"),
        "reviewApproved": bool(_value(row, "review_approved", 0)),
        "reviewRevision": int(_value(row, "review_revision", 0)),
        "reviewedAt": _value(row, "reviewed_at"),
        "publishStatus": _value(row, "publish_status", "not_synced"),
        "publishRemoteId": _value(row, "publish_remote_id"),
        "publishedRevision": int(_value(row, "published_revision", 0)),
        "publishFingerprint": _value(row, "publish_fingerprint"),
        "publishPreviewHash": _value(row, "publish_preview_hash"),
        "revision": int(_value(row, "revision", 1)),
        "createdAt": _value(row, "created_at"),
        "updatedAt": _value(row, "updated_at"),
    }


def row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "projectTitle": _value(row, "project_title"),
        "parentTaskId": _value(row, "parent_task_id"),
        "status": row["status"],
        "currentStep": _value(row, "current_step"),
        "progress": int(_value(row, "progress", 0)),
        "message": _value(row, "message"),
        "errorCode": _value(row, "error_code"),
        "errorDetail": _value(row, "error_detail"),
        "cancelRequested": bool(_value(row, "cancel_requested", 0)),
        "autoReview": bool(_value(row, "auto_review", 1)),
        "retryMode": _value(row, "retry_mode", "full"),
        "baseRevision": int(_value(row, "base_revision", 1)),
        "startedAt": _value(row, "started_at"),
        "finishedAt": _value(row, "finished_at"),
        "updatedAt": _value(row, "updated_at"),
    }


def _decrypt_setting(key: str, value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    if key == "ai" and result.get("apiKey"):
        result["apiKey"] = decrypt(str(result["apiKey"]))
    if key == "wechat" and result.get("appSecret"):
        result["appSecret"] = decrypt(str(result["appSecret"]))
    return result


def settings_bundle() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT key,value_json FROM settings ORDER BY key").fetchall()
    return {row["key"]: _decrypt_setting(row["key"], json_load(row["value_json"], {})) for row in rows}


def get_setting(key: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return _decrypt_setting(key, json_load(row["value_json"], {}) if row else {})


def set_setting(key: str, value: dict[str, Any]) -> None:
    stored = dict(value)
    if key == "ai" and stored.get("apiKey"):
        secret = str(stored["apiKey"])
        stored["apiKeyHintStored"] = secret[-4:]
        stored["apiKey"] = encrypt(secret)
    if key == "wechat" and stored.get("appSecret"):
        secret = str(stored["appSecret"])
        stored["appSecretHintStored"] = secret[-4:]
        stored["appSecret"] = encrypt(secret)
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (key, json.dumps(stored, ensure_ascii=False), utc_now()),
        )


def record_project_version(conn: sqlite3.Connection, project_id: str, reason: str) -> None:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        return
    conn.execute(
        "INSERT INTO project_versions(project_id,revision,snapshot_json,reason,created_at) VALUES(?,?,?,?,?)",
        (project_id, row["revision"], json.dumps(row_to_project(row), ensure_ascii=False), reason[:200], utc_now()),
    )
    conn.execute(
        "DELETE FROM project_versions WHERE project_id=? AND id NOT IN "
        "(SELECT id FROM project_versions WHERE project_id=? ORDER BY id DESC LIMIT 100)",
        (project_id, project_id),
    )


def get_project(project_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    query = "SELECT * FROM projects WHERE id=?"
    if not include_deleted:
        query += " AND deleted=0"
    with connect() as conn:
        row = conn.execute(query, (project_id,)).fetchone()
    return row_to_project(row) if row else None


def _project_filter(
    include_archived: bool,
    include_deleted: bool,
    deleted_only: bool,
    search: str,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if deleted_only:
        clauses.append("deleted=1")
    elif not include_deleted:
        clauses.append("deleted=0")
    if not include_archived and not deleted_only:
        clauses.append("archived=0")
    normalized = search.strip()[:200]
    if normalized:
        clauses.append("(instr(lower(title), lower(?)) > 0 OR instr(lower(summary), lower(?)) > 0)")
        params.extend([normalized, normalized])
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def count_projects(
    include_archived: bool = True,
    include_deleted: bool = False,
    *,
    deleted_only: bool = False,
    search: str = "",
) -> int:
    where, params = _project_filter(include_archived, include_deleted, deleted_only, search)
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM projects" + where, params).fetchone()
    return int(row[0]) if row else 0


def list_projects(
    include_archived: bool = True,
    include_deleted: bool = False,
    *,
    deleted_only: bool = False,
    search: str = "",
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _project_filter(include_archived, include_deleted, deleted_only, search)
    query = "SELECT * FROM projects" + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 1000)), max(0, offset)])
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [row_to_project(row) for row in rows]


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT t.*,p.title AS project_title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
    return row_to_task(row) if row else None


def list_tasks(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT t.*,p.title AS project_title FROM tasks t JOIN projects p ON p.id=t.project_id "
            "ORDER BY t.started_at DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 500)), max(0, offset)),
        ).fetchall()
    return [row_to_task(row) for row in rows]


def list_task_events(task_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT level,step,message,detail_json,created_at FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    return [
        {
            "level": row["level"],
            "step": row["step"],
            "message": row["message"],
            "detail": json_load(row["detail_json"], {}),
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def set_health_check(service: str, *, configured: bool, reachable: bool, message: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO health_checks(service,configured,reachable,verified_at,message) VALUES(?,?,?,?,?) "
            "ON CONFLICT(service) DO UPDATE SET configured=excluded.configured,reachable=excluded.reachable,"
            "verified_at=excluded.verified_at,message=excluded.message",
            (service, 1 if configured else 0, 1 if reachable else 0, utc_now(), message[:500]),
        )


def get_health_checks() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM health_checks").fetchall()
    return {
        row["service"]: {
            "configured": bool(row["configured"]),
            "reachable": bool(row["reachable"]),
            "verifiedAt": row["verified_at"],
            "message": row["message"],
        }
        for row in rows
    }
