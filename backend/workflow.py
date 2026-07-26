from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urlsplit

from ai_engine import AIConfigurationRequired, AIEngine, AIEngineError
from cover_generator import generate_cover_data_url
from db import connect, get_project, get_setting, get_task, record_project_version, utc_now
from logger_config import get_logger
from source_fetcher import SourceFetchError, fetch_source

_MAX_WORKERS = max(2, min(6, (os.cpu_count() or 2)))
_MAX_PENDING = max(_MAX_WORKERS, int(os.environ.get("STUDIO_WORKFLOW_QUEUE_LIMIT", "32")))
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="studio-workflow")
_QUEUE_SLOTS = threading.BoundedSemaphore(_MAX_PENDING)
_START_LOCK = threading.RLock()
MAX_WORKFLOW_SECONDS = int(os.environ.get("STUDIO_WORKFLOW_TIMEOUT", "1200"))
# N3 分步超时：每个步骤独立超时阈值，避免单步耗时过长挤占其他步骤
STEP_TIMEOUTS = {
    "source": int(os.environ.get("STUDIO_TIMEOUT_SOURCE", "30")),
    "research": int(os.environ.get("STUDIO_TIMEOUT_RESEARCH", "10")),
    "outline": int(os.environ.get("STUDIO_TIMEOUT_OUTLINE", "120")),
    "draft": int(os.environ.get("STUDIO_TIMEOUT_DRAFT", "120")),
    "cover": int(os.environ.get("STUDIO_TIMEOUT_COVER", "30")),
    "review": int(os.environ.get("STUDIO_TIMEOUT_REVIEW", "90")),
}
_ACTIVE_STATUSES = {"queued", "running"}
_RETRYABLE_STATUSES = {"failed", "blocked", "cancelled", "timeout"}
_RETRY_MODES = {"review_only", "preserve_body", "from_outline", "full"}


class WorkflowCancelled(RuntimeError):
    pass


class WorkflowTimedOut(RuntimeError):
    pass


class WorkflowConflict(RuntimeError):
    pass


class WorkflowQueueFull(RuntimeError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _is_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _event(task_id: str, level: str, step: str, message: str, detail: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO task_events(task_id, level, step, message, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, level, step, message, json.dumps(detail or {}, ensure_ascii=False), utc_now()),
        )


def _task_update(task_id: str, **values: Any) -> None:
    allowed = {
        "status",
        "current_step",
        "progress",
        "message",
        "error_code",
        "error_detail",
        "cancel_requested",
        "finished_at",
        "updated_at",
    }
    columns = [(key, value) for key, value in values.items() if key in allowed]
    if not columns:
        return
    if "updated_at" not in values:
        columns.append(("updated_at", utc_now()))
    sql = "UPDATE tasks SET " + ", ".join(f"{key} = ?" for key, _ in columns) + " WHERE id = ?"
    with connect() as conn:
        conn.execute(sql, tuple(value for _, value in columns) + (task_id,))


def _guard(task_id: str, started_monotonic: float, *, step: str = "", step_started: float = 0.0) -> None:
    task = get_task(task_id)
    if not task:
        raise WorkflowCancelled("任务不存在")
    if task["cancelRequested"]:
        raise WorkflowCancelled("用户已取消任务")
    if time.monotonic() - started_monotonic > MAX_WORKFLOW_SECONDS:
        raise WorkflowTimedOut(f"工作流超过 {MAX_WORKFLOW_SECONDS} 秒上限")
    # N3 分步超时检查
    if step and step_started:
        step_limit = STEP_TIMEOUTS.get(step, MAX_WORKFLOW_SECONDS)
        step_elapsed = time.monotonic() - step_started
        if step_elapsed > step_limit:
            raise WorkflowTimedOut(f"步骤 '{step}' 超过 {step_limit} 秒上限（实际 {step_elapsed:.0f} 秒）")


def _step(task_id: str, step: str, progress: int, message: str) -> None:
    _task_update(task_id, status="running", current_step=step, progress=progress, message=message)
    _event(task_id, "info", step, message)


def _skip(task_id: str, step: str, message: str) -> None:
    _event(task_id, "info", step, message, {"skipped": True})


def _submit(task_id: str) -> None:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        raise WorkflowQueueFull(f"工作流队列已满（上限 {_MAX_PENDING}），请稍后重试")
    try:
        future = _EXECUTOR.submit(_run_workflow, task_id)
    except Exception:
        _QUEUE_SLOTS.release()
        raise
    future.add_done_callback(lambda _future: _QUEUE_SLOTS.release())


def _active_task_for_project(conn: Any, project_id: str) -> Any:
    return conn.execute(
        "SELECT id,status FROM tasks WHERE project_id=? AND status IN ('queued','running') ORDER BY started_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _snapshot_source(project_id: str, source_input: str) -> tuple[str, str]:
    snapshot = fetch_source(source_input)
    identity = hashlib.sha256(f"{snapshot.source_url}\n{snapshot.content_hash}".encode("utf-8")).hexdigest()
    snapshot_id = f"src_{identity[:24]}"
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO source_snapshots(
                id, content_hash, source_url, final_url, title, publisher, author, published_at,
                content_text, preview, fetched_at, extraction_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                snapshot.content_hash,
                snapshot.source_url,
                snapshot.final_url,
                snapshot.title,
                snapshot.publisher,
                snapshot.author,
                snapshot.published_at,
                snapshot.content_text,
                snapshot.preview,
                utc_now(),
                snapshot.extraction_method,
            ),
        )
        row = conn.execute(
            "SELECT id FROM source_snapshots WHERE source_url=? AND content_hash=?",
            (snapshot.source_url, snapshot.content_hash),
        ).fetchone()
        if not row:
            raise RuntimeError("来源快照写入失败")
        actual_id = row["id"]
        conn.execute(
            "INSERT OR IGNORE INTO project_sources(project_id, snapshot_id, source_order) VALUES (?, ?, 0)",
            (project_id, actual_id),
        )
    return snapshot.title, snapshot.content_text


def _source_text(project_id: str) -> str:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.title, s.publisher, s.author, s.published_at, s.final_url, s.content_text
            FROM project_sources ps
            JOIN source_snapshots s ON s.id = ps.snapshot_id
            WHERE ps.project_id = ?
            ORDER BY ps.source_order, s.fetched_at
            """,
            (project_id,),
        ).fetchall()
    parts: list[str] = []
    for index, row in enumerate(rows, start=1):
        meta = " | ".join(
            value
            for value in [row["title"], row["publisher"], row["author"], row["published_at"], row["final_url"]]
            if value
        )
        parts.append(f"[来源{index}] {meta}\n{row['content_text']}")
    return "\n\n---\n\n".join(parts)


def _strict_evidence_gate(project: dict[str, Any], source_text: str, strict_facts: bool) -> None:
    if not strict_facts:
        return
    if project["sourceKind"] != "url" or len(source_text.strip()) < 200 or "[来源" not in source_text:
        raise AIEngineError(
            "strict_facts_no_evidence",
            "严格事实模式需要可核验的网页来源。当前只有主题或来源证据不足，任务已暂停。",
        )


def _conditional_project_update(
    project_id: str,
    expected_revision: int,
    reason: str,
    values: dict[str, Any],
) -> int:
    with connect() as conn:
        current = conn.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
        if not current or int(current["revision"]) != expected_revision:
            raise WorkflowConflict("文章在 AI 任务执行期间已被人工修改；为保护人工内容，本次结果未覆盖文章")
        record_project_version(conn, project_id, reason)
        updates = dict(values)
        updates["revision"] = expected_revision + 1
        updates["updated_at"] = utc_now()
        sql = "UPDATE projects SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=? AND revision=?"
        cursor = conn.execute(sql, tuple(updates.values()) + (project_id, expected_revision))
        if cursor.rowcount != 1:
            raise WorkflowConflict("文章版本已变化；AI 结果已安全放弃")
    return expected_revision + 1


def create_workflow(source_input: str, *, auto_review: bool | None = None, parent_task_id: str = "") -> dict[str, Any]:
    value = source_input.strip()
    if not value:
        raise ValueError("请输入来源链接或创作目标")
    if len(value) > 4_000:
        raise ValueError("来源链接或创作目标不能超过 4000 个字符")
    now = utc_now()
    project_id = _id("prj")
    task_id = _id("tsk")
    source_kind = "url" if _is_url(value) else "topic"
    ai = get_setting("ai")
    review_enabled = bool(ai.get("autoReview", True) if auto_review is None else auto_review)
    provisional_title = (urlsplit(value).hostname if source_kind == "url" else value) or "新文章"
    with _START_LOCK, connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(
                id, title, goal, source_input, source_kind, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'working', ?, ?)
            """,
            (project_id, provisional_title[:120], value, value, source_kind, now, now),
        )
        conn.execute(
            """
            INSERT INTO tasks(
                id, project_id, parent_task_id, status, current_step, progress, message,
                auto_review, retry_mode, base_revision, started_at, updated_at
            ) VALUES (?, ?, ?, 'queued', 'queued', 0, '任务已进入队列', ?, 'full', 1, ?, ?)
            """,
            (task_id, project_id, parent_task_id, 1 if review_enabled else 0, now, now),
        )
    _event(task_id, "info", "queued", "任务已进入统一工作流")
    try:
        _submit(task_id)
    except WorkflowQueueFull as exc:
        now = utc_now()
        _task_update(
            task_id,
            status="failed",
            message=str(exc),
            error_code="workflow_queue_full",
            error_detail=str(exc),
            finished_at=now,
        )
        with connect() as conn:
            conn.execute("UPDATE projects SET status='draft',updated_at=? WHERE id=?", (now, project_id))
        raise
    return {"project": get_project(project_id), "task": get_task(task_id)}


def retry_workflow(task_id: str, retry_mode: str = "review_only") -> dict[str, Any]:
    if retry_mode not in _RETRY_MODES:
        raise ValueError("重试范围无效，可选 review_only、preserve_body、from_outline、full")
    original = get_task(task_id)
    if not original:
        raise KeyError("任务不存在")
    if original["status"] not in _RETRYABLE_STATUSES:
        raise ValueError("仅失败、阻断、取消或超时的任务可以重试")
    project = get_project(original["projectId"], include_deleted=True)
    if not project or project["deleted"]:
        raise ValueError("原文章已删除，无法重试")
    if retry_mode in {"review_only", "from_outline"} and not project["bodyMarkdown"] and retry_mode == "review_only":
        raise ValueError("当前文章没有正文，不能只重做审校")
    if retry_mode == "from_outline" and not project["outline"]:
        raise ValueError("当前文章没有可复用框架，不能从框架重做")

    now = utc_now()
    new_task_id = _id("tsk")
    with _START_LOCK, connect() as conn:
        active = _active_task_for_project(conn, project["id"])
        if active:
            raise ValueError(f"该文章已有活跃任务 {active['id']}，不能并发重试")
        conn.execute("UPDATE projects SET status='working', updated_at=? WHERE id=?", (now, project["id"]))
        conn.execute(
            """
            INSERT INTO tasks(
                id,project_id,parent_task_id,status,current_step,progress,message,auto_review,
                retry_mode,base_revision,started_at,updated_at
            ) VALUES(?,?,?,'queued','queued',0,'重试任务已进入队列',?,?,?,?,?)
            """,
            (
                new_task_id,
                project["id"],
                task_id,
                1 if original["autoReview"] else 0,
                retry_mode,
                project["revision"],
                now,
                now,
            ),
        )
    _event(
        new_task_id,
        "info",
        "queued",
        "正在原文章上重新执行工作流",
        {"retryOf": task_id, "retryMode": retry_mode, "baseRevision": project["revision"]},
    )
    try:
        _submit(new_task_id)
    except WorkflowQueueFull as exc:
        now = utc_now()
        _task_update(
            new_task_id,
            status="failed",
            message=str(exc),
            error_code="workflow_queue_full",
            error_detail=str(exc),
            finished_at=now,
        )
        with connect() as conn:
            conn.execute("UPDATE projects SET status='draft',updated_at=? WHERE id=?", (now, project["id"]))
        raise
    return {"project": get_project(project["id"]), "task": get_task(new_task_id)}


def cancel_workflow(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise KeyError("任务不存在")
    if task["status"] in {"succeeded", "failed", "cancelled", "blocked", "timeout"}:
        return task
    _task_update(task_id, cancel_requested=1, message="已请求取消，正在安全停止")
    _event(task_id, "warning", task["currentStep"], "用户请求取消任务")
    return get_task(task_id) or task


def mark_interrupted_tasks() -> None:
    now = utc_now()
    with connect() as conn:
        rows = conn.execute("SELECT id, project_id FROM tasks WHERE status IN ('queued', 'running')").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET status='failed', error_code='server_restarted', error_detail='服务重启导致任务中断', message='任务因服务重启中断', finished_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            conn.execute("UPDATE projects SET status='draft', updated_at=? WHERE id=?", (now, row["project_id"]))


def _load_plan(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": project["title"],
        "summary": project["summary"],
        "outline": project["outline"],
    }


def _run_workflow(task_id: str) -> None:
    started = time.monotonic()
    task = get_task(task_id)
    if not task:
        return
    project_id = task["projectId"]
    project = get_project(project_id)
    if not project:
        return
    expected_revision = int(task.get("baseRevision") or project["revision"])
    retry_mode = str(task.get("retryMode") or "full")
    wlog = get_logger("workflow", task_id)
    wlog.info("工作流启动: project=%s mode=%s revision=%d", project_id, retry_mode, expected_revision)
    try:
        if project["revision"] != expected_revision:
            raise WorkflowConflict("任务启动前文章版本已变化，本次任务已停止")
        _guard(task_id, started)
        source_text = _source_text(project_id)
        source_title = ""
        if project["sourceKind"] == "url" and not source_text:
            if not bool(get_setting("general").get("allowNetwork", True)):
                raise AIEngineError("network_disabled", "联网读取来源已在设置中关闭")
            _step(task_id, "source", 10, "正在安全读取来源")
            source_title, _ = _snapshot_source(project_id, project["sourceInput"])
            source_text = _source_text(project_id)
            wlog.info("来源抓取完成: %s", project["sourceInput"][:80])
        elif project["sourceKind"] == "url":
            _skip(task_id, "source", "已复用文章现有不可变来源快照")
            wlog.info("复用已有来源快照")
        else:
            _step(task_id, "research", 10, "正在理解创作目标")
            source_text = f"用户提供的创作主题与约束：\n{project['sourceInput']}"
            _skip(task_id, "source", "主题创作没有网页来源读取步骤")
            wlog.info("主题创作模式: %s", project["sourceInput"][:80])
        _guard(task_id, started)

        general = get_setting("general")
        strict_facts = bool(general.get("strictFacts", False))
        _strict_evidence_gate(project, source_text, strict_facts)
        ai_config = get_setting("ai")
        engine = AIEngine(ai_config)

        plan = _load_plan(project)
        if retry_mode in {"full", "preserve_body"}:
            _step(task_id, "outline", 35, "正在生成文章框架")
            plan = engine.plan(project["goal"], source_text, strict_facts)
            wlog.info("文章框架生成完成: title=%s", (plan.get("title") or "")[:60])
            _guard(task_id, started)
            expected_revision = _conditional_project_update(
                project_id,
                expected_revision,
                "AI 生成文章框架前",
                {
                    "title": (plan["title"] or source_title or project["title"])[:120],
                    "summary": plan["summary"],
                    "outline_json": json.dumps(plan["outline"], ensure_ascii=False),
                    "publish_status": "not_synced",
                    "publish_remote_id": "",
                },
            )
        else:
            _skip(task_id, "outline", "按所选重试范围复用现有文章框架")

        body = project["bodyMarkdown"]
        if retry_mode in {"full", "from_outline"}:
            _step(task_id, "draft", 60, "正在生成正文")
            body = engine.draft(
                project["goal"],
                source_text,
                plan,
                int(general.get("defaultLength", 1800)),
                strict_facts=strict_facts,
            )
            wlog.info("正文生成完成: %d 字符", len(body))
            _guard(task_id, started)
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            expected_revision = _conditional_project_update(
                project_id,
                expected_revision,
                "AI 生成正文前",
                {
                    "body_markdown": body,
                    "review_json": "[]",
                    "review_fingerprint": "",
                    "review_approved": 0,
                    "review_revision": 0,
                    "reviewed_at": "",
                    "publish_status": "not_synced",
                    "publish_remote_id": "",
                    "published_revision": 0,
                    "publish_fingerprint": "",
                    "publish_preview_hash": "",
                },
            )
        else:
            if not body:
                raise AIEngineError("body_required", "所选重试范围需要已有正文")
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            _skip(task_id, "draft", "按所选重试范围保留人工正文")

        # 封面生成：如果用户未手动上传封面，则根据标题自动生成
        existing_cover = project.get("coverDataUrl") or ""
        if not existing_cover.strip():
            _step(task_id, "cover", 72, "正在生成封面图片")
            try:
                cover_url = generate_cover_data_url(
                    plan.get("title") or project["title"],
                    plan.get("summary") or "",
                )
                wlog.info("封面图片生成完成")
                _guard(task_id, started)
                expected_revision = _conditional_project_update(
                    project_id,
                    expected_revision,
                    "封面图片生成后",
                    {"cover_data_url": cover_url},
                )
            except Exception as exc:  # noqa: BLE001
                _event(task_id, "warning", "cover", f"封面自动生成失败，可手动上传：{exc}", {})
        else:
            _skip(task_id, "cover", "已有手动封面，跳过自动生成")

        if task["autoReview"]:
            _step(task_id, "review", 82, "正在执行发布前审校")
            checks = engine.review(body, source_text)
            wlog.info("AI 审校完成: %d 项检查", len(checks))
            _guard(task_id, started)

            # 内容安全检测：查重 + 微信内容安全 API
            try:
                from content_security import run_content_security_checks
                wechat_settings = get_setting("wechat")
                wechat_app_id = str(wechat_settings.get("appId") or "")
                wechat_secret = str(wechat_settings.get("appSecret") or "")
                security_token = ""
                if wechat_app_id and wechat_secret:
                    try:
                        from wechat_api import _token_manager as _wtm
                        security_token = _wtm.get_token(wechat_app_id, wechat_secret)
                    except Exception:  # noqa: BLE001
                        wlog.warning("获取内容安全检测 token 失败，跳过微信 API 检测")

                security_checks = run_content_security_checks(
                    body, source_text,
                    token=security_token,
                    app_id=wechat_app_id,
                    app_secret=wechat_secret,
                )
                checks.extend(security_checks)
                wlog.info("内容安全检测完成: +%d 项检查", len(security_checks))
            except Exception as exc:  # noqa: BLE001
                wlog.warning("内容安全检测异常: %s", exc)
                checks.append({
                    "id": "content_security",
                    "label": "内容安全检测",
                    "status": "warning",
                    "message": f"内容安全检测异常：{exc}",
                })

            _guard(task_id, started)
            expected_revision = _conditional_project_update(
                project_id,
                expected_revision,
                "AI 审校结果写入前",
                {
                    "review_json": json.dumps(checks, ensure_ascii=False),
                    "review_fingerprint": fingerprint,
                    "review_approved": 0,
                    "review_revision": 0,
                    "reviewed_at": "",
                },
            )
        else:
            _skip(task_id, "review", "已按设置跳过自动审校")

        now = utc_now()
        with connect() as conn:
            conn.execute("UPDATE projects SET status='draft', updated_at=? WHERE id=?", (now, project_id))
            conn.execute(
                "UPDATE tasks SET status='succeeded', current_step='completed', progress=100, message='文章已生成', finished_at=?, updated_at=? WHERE id=?",
                (now, now, task_id),
            )
        _event(task_id, "success", "completed", "统一工作流已完成", {"finalRevision": expected_revision})
        wlog.info("工作流完成: project=%s final_revision=%d", project_id, expected_revision)
    except AIConfigurationRequired as exc:
        wlog.warning("工作流阻塞: %s - %s", exc.code, exc)
        _finish_error(task_id, project_id, "blocked", exc.code, str(exc), "warning", "blocked")
    except WorkflowConflict as exc:
        wlog.warning("工作流冲突: %s", exc)
        _finish_error(task_id, project_id, "blocked", "project_changed", str(exc), "warning", "blocked")
    except WorkflowCancelled as exc:
        wlog.info("工作流已取消: %s", exc)
        _finish_error(task_id, project_id, "cancelled", "cancelled", str(exc), "warning", "cancelled")
    except WorkflowTimedOut as exc:
        wlog.error("工作流超时: %s", exc)
        _finish_error(
            task_id, project_id, "timeout", "workflow_timeout", str(exc), "error", "timeout",
            base_revision=int(task.get("baseRevision") or 0),
            retry_mode=retry_mode,
        )
    except (SourceFetchError, AIEngineError) as exc:
        wlog.error("工作流失败: %s - %s", exc.code, exc, exc_info=exc)
        _finish_error(
            task_id, project_id, "failed", exc.code, str(exc), "error", "failed",
            base_revision=int(task.get("baseRevision") or 0),
            retry_mode=retry_mode,
        )
    except Exception as exc:  # noqa: BLE001
        wlog.error("工作流未预期错误: %s", exc, exc_info=exc)
        _finish_error(
            task_id,
            project_id,
            "failed",
            "workflow_error",
            "工作流发生未预期错误",
            "error",
            "failed",
            {"detail": repr(exc)},
            base_revision=int(task.get("baseRevision") or 0),
            retry_mode=retry_mode,
        )


# D1 回滚：工作流失败时需要从快照恢复的内容字段。
# 映射 snapshot_json 的键（camelCase，由 row_to_project 产生）到 projects 表列名。
# 不包含 id/goal/source_input/source_kind/status/archived/deleted/revision/created_at/updated_at
# 等身份与元数据字段——这些字段在回滚时不能被覆盖。
_ROLLBACK_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "title"),
    ("summary", "summary"),
    ("outline", "outline_json"),
    ("bodyMarkdown", "body_markdown"),
    ("coverDataUrl", "cover_data_url"),
    ("review", "review_json"),
    ("reviewFingerprint", "review_fingerprint"),
    ("reviewApproved", "review_approved"),
    ("reviewRevision", "review_revision"),
    ("reviewedAt", "reviewed_at"),
    ("publishStatus", "publish_status"),
    ("publishRemoteId", "publish_remote_id"),
    ("publishedRevision", "published_revision"),
    ("publishFingerprint", "publish_fingerprint"),
    ("publishPreviewHash", "publish_preview_hash"),
)
_ROLLBACK_JSON_FIELDS = frozenset({"outline", "review"})


def _rollback_to_base_revision(project_id: str, base_revision: int) -> None:
    """将文章尽力恢复到 base_revision 对应的版本快照。

    当 full 重试的工作流中途失败时调用，避免文章停留在半完成状态
    （例如框架已更新但正文未生成）。本函数是 best-effort：任何异常都会被
    记录并吞掉，以保证外层错误处理仍能向用户展示失败状态。
    """
    wlog = get_logger("workflow", project_id)
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM project_versions "
                "WHERE project_id=? AND revision=? ORDER BY id DESC LIMIT 1",
                (project_id, base_revision),
            ).fetchone()
            if not row:
                wlog.warning(
                    "回滚跳过: 未找到 base_revision=%d 的版本快照，保留当前状态",
                    base_revision,
                )
                return
            snapshot = json.loads(row["snapshot_json"])
            updates: dict[str, Any] = {}
            for snapshot_key, column in _ROLLBACK_FIELDS:
                if snapshot_key not in snapshot:
                    continue
                value = snapshot[snapshot_key]
                if snapshot_key in _ROLLBACK_JSON_FIELDS:
                    value = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, bool):
                    value = 1 if value else 0
                updates[column] = value
            updates["revision"] = base_revision + 1
            updates["status"] = "draft"
            updates["updated_at"] = utc_now()
            sql = "UPDATE projects SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=?"
            conn.execute(sql, tuple(updates.values()) + (project_id,))
            wlog.info(
                "已将文章回滚至 base_revision=%d 的快照（恢复 %d 个内容字段，新 revision=%d）",
                base_revision,
                len(updates) - 3,
                base_revision + 1,
            )
    except Exception as exc:  # noqa: BLE001
        wlog.error("回滚失败: %s", exc, exc_info=exc)


def _finish_error(
    task_id: str,
    project_id: str,
    status: str,
    code: str,
    message: str,
    level: str,
    step: str,
    detail: dict[str, Any] | None = None,
    *,
    base_revision: int | None = None,
    retry_mode: str = "full",
) -> None:
    now = utc_now()
    _task_update(
        task_id,
        status=status,
        message=message,
        error_code=code,
        error_detail=str((detail or {}).get("detail") or message),
        finished_at=now,
    )
    # D1: full 重试的硬失败（failed/timeout）回滚到工作流启动前的快照，
    # 避免文章停留在半完成状态。blocked/cancelled 保留当前状态供用户检查；
    # review_only/preserve_body/from_outline 等部分重试也不回滚。
    rolled_back = False
    if status in {"failed", "timeout"} and retry_mode == "full" and base_revision is not None:
        _rollback_to_base_revision(project_id, base_revision)
        rolled_back = True
    with connect() as conn:
        conn.execute("UPDATE projects SET status='draft', updated_at=? WHERE id=?", (now, project_id))
    payload = {"code": code, **(detail or {})}
    if rolled_back:
        payload["rolled_back_to"] = base_revision
    _event(task_id, level, step, message, payload)
