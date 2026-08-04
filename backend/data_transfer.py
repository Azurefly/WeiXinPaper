"""数据导出/导入模块。

提供全量历史数据的 JSON 导出与导入能力：

- 导出：项目（含版本历史、任务日志、来源快照、发布回执）+ 通用设置 + AI 配置（不含密钥）
- 导入：支持 merge（跳过已存在的项目）和 replace（覆盖已存在的项目）两种模式
- 安全：不导出用户凭证、会话令牌、API 密钥等敏感信息
"""

from __future__ import annotations

import re
from typing import Any

from db import connect, get_setting, settings_bundle, set_setting, utc_now
from logger_config import get_logger

logger = get_logger("data_transfer")

BACKUP_FORMAT = "studio-backup"
BACKUP_VERSION = 1


# ===== X3 审计修复：数据导入内容安全校验防 XSS =====
# 以下清洗函数用于在数据导入阶段对文本字段进行 HTML 标签白名单过滤，
# 移除危险的脚本/事件/协议载荷，同时保留合法的 Markdown 格式内容。

# 危险的完整标签（含内容）正则：成对出现的标签及其内部文本一并移除
_DANGEROUS_BLOCK_TAGS = (
    "script",
    "iframe",
    "object",
    "embed",
)

# 预编译正则：移除成对危险标签及内容（支持多行）
_BLOCK_TAG_CONTENT_RES = [
    re.compile(
        rf"<{tag}\b[^>]*>.*?</{tag}>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for tag in _DANGEROUS_BLOCK_TAGS
]

# 预编译正则：移除未闭合或自闭合的危险标签起始部分
_BLOCK_TAG_OPEN_RES = [
    re.compile(rf"<{tag}\b[^>]*/?>", flags=re.IGNORECASE)
    for tag in _DANGEROUS_BLOCK_TAGS
]

# 预编译正则：移除所有 on* 事件属性（如 onclick、onerror、onload 等）
# 需求给定的正则仅匹配带引号的值，这里额外补充未加引号的情况以防遗漏
_ON_EVENT_ATTR_RE = re.compile(
    r"\son\w+\s*=\s*[\"'][^\"']*[\"']", flags=re.IGNORECASE
)
# 预编译正则：移除未加引号的 on* 事件属性（如 onerror=alert(1)）
_ON_EVENT_ATTR_UNQUOTED_RE = re.compile(
    r"\son\w+\s*=\s*[^\s\"'<>]*", flags=re.IGNORECASE
)

# 预编译正则：移除 javascript: 协议
_JS_PROTOCOL_RE = re.compile(r"javascript:", flags=re.IGNORECASE)

# 预编译正则：移除带有事件属性的 <svg ...> 标签（如 <svg onload=...>）
_SVG_WITH_EVENT_RE = re.compile(
    r"<svg\b[^>]*\bon\w+[^>]*>", flags=re.IGNORECASE
)


def _sanitize_imported_text(text: str) -> str:
    """清洗导入的文本字段，移除危险的 HTML/JS 内容以防 XSS。

    X3 审计修复：数据导入增加内容安全校验防 XSS。

    清洗策略（保留合法的 Markdown 内容，如 ``![图片](url)``、``[链接](url)``）：
      1. 移除 <script>/<iframe>/<object>/<embed> 等危险标签及其内部内容；
      2. 移除未闭合的危险标签起始部分；
      3. 移除 <svg onload=...> 等带事件属性的 SVG 标签；
      4. 移除所有 on* 事件属性（onclick/onerror/onload 等）；
      5. 移除 javascript: 协议（阻断 <a href="javascript:..."> 等载荷）。

    Args:
        text: 待清洗的文本，可能是 str / None / 其他类型。

    Returns:
        清洗后的字符串；若入参非 str 类型则原样返回。
    """
    # 非 str 类型（含 None、int 等）直接返回，不做处理
    if not isinstance(text, str):
        return text

    # 空字符串直接返回，避免无谓的正则开销
    if not text:
        return text

    # 1. 移除成对危险标签及其内容（如 <script>...</script>）
    for pattern in _BLOCK_TAG_CONTENT_RES:
        text = pattern.sub("", text)

    # 2. 移除未闭合或自闭合的危险标签起始部分
    for pattern in _BLOCK_TAG_OPEN_RES:
        text = pattern.sub("", text)

    # 3. 移除带事件属性的 <svg ...> 标签（如 <svg onload=alert(1)>）
    text = _SVG_WITH_EVENT_RE.sub("", text)

    # 4. 移除所有 on* 事件属性（如 onclick="..."、onerror='...'）
    #    Markdown 链接/图片不包含此类属性，因此不会误伤合法内容
    text = _ON_EVENT_ATTR_RE.sub("", text)
    # 补充移除未加引号的 on* 事件属性（如 onerror=alert(1)）
    text = _ON_EVENT_ATTR_UNQUOTED_RE.sub("", text)

    # 5. 移除 javascript: 协议（阻断 <a href="javascript:..."> 载荷）
    text = _JS_PROTOCOL_RE.sub("", text)

    return text


def _sanitize_project_row(row: dict[str, Any]) -> dict[str, Any]:
    """统一清洗项目行数据中的文本字段。

    X3 审计修复：在插入 projects 表前对文本字段进行 XSS 清洗。

    处理字段：
      - title / summary / body_markdown / outline_json：通用文本清洗；
      - cover_data_url：仅允许 ``data:image/`` 开头，否则清空以防伪装脚本。

    Args:
        row: 项目行字典（会原地修改并返回）。

    Returns:
        清洗后的项目行字典。
    """
    text_fields = ("title", "summary", "body_markdown", "outline_json")
    for field in text_fields:
        if field in row:
            row[field] = _sanitize_imported_text(row[field])

    # cover_data_url 只允许 data:image/ 开头，防止注入非法 data URI 载荷
    if "cover_data_url" in row:
        val = row["cover_data_url"]
        if isinstance(val, str) and val and not val.startswith("data:image/"):
            row["cover_data_url"] = ""

    return row


def _sanitize_source_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """清洗来源快照行数据中的文本字段。

    X3 审计修复：对 source_snapshots 表的 content_text 和 preview 字段进行清洗，
    防止通过来源快照内容注入 XSS 载荷。

    Args:
        snapshot: 来源快照行字典（会原地修改并返回）。

    Returns:
        清洗后的来源快照行字典。
    """
    text_fields = ("content_text", "preview")
    for field in text_fields:
        if field in snapshot:
            snapshot[field] = _sanitize_imported_text(snapshot[field])

    return snapshot


# ===== X3 审计修复结束 =====


def export_data() -> dict[str, Any]:
    """导出全量数据为可序列化的字典。

    返回结构：
    {
        "format": "studio-backup",
        "version": 1,
        "exportedAt": "ISO时间",
        "schemaVersion": 213,
        "settings": {"general": {...}, "ai": {...}},  # ai 不含 apiKey
        "projects": [
            {
                "project": {...},
                "versions": [...],
                "tasks": [{"...": ..., "events": [...]}],
                "sources": [{"snapshot": {...}, "sourceOrder": 0}],
                "publishReceipts": [...],
            }
        ]
    }
    """
    with connect() as conn:
        projects_data: list[dict[str, Any]] = []

        for prow in conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall():
            pid = prow["id"]
            project = dict(prow)

            # 项目版本历史
            versions = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM project_versions WHERE project_id=? ORDER BY id", (pid,)
                ).fetchall()
            ]

            # 任务及其事件日志
            tasks: list[dict[str, Any]] = []
            for trow in conn.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY started_at", (pid,)
            ).fetchall():
                task = dict(trow)
                task["events"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (trow["id"],)
                    ).fetchall()
                ]
                tasks.append(task)

            # 来源快照（含关联排序）
            sources: list[dict[str, Any]] = []
            for srow in conn.execute(
                """SELECT ss.*, ps.source_order FROM source_snapshots ss
                   JOIN project_sources ps ON ps.snapshot_id = ss.id
                   WHERE ps.project_id=? ORDER BY ps.source_order""",
                (pid,),
            ).fetchall():
                s = dict(srow)
                order = s.pop("source_order", 0)
                sources.append({"snapshot": s, "sourceOrder": order})

            # 发布回执
            receipts = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM publish_receipts WHERE project_id=? ORDER BY id", (pid,)
                ).fetchall()
            ]

            projects_data.append(
                {
                    "project": project,
                    "versions": versions,
                    "tasks": tasks,
                    "sources": sources,
                    "publishReceipts": receipts,
                }
            )

        # 设置（脱敏：不含 apiKey / appSecret）
        bundle = settings_bundle()
        ai = bundle.get("ai", {})
        ai_safe = {k: v for k, v in ai.items() if k not in ("apiKey", "apiKeyHintStored")}
        general = bundle.get("general", {})

    result: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exportedAt": utc_now(),
        "schemaVersion": 213,
        "settings": {"general": general, "ai": ai_safe},
        "projects": projects_data,
    }
    logger.info("数据导出完成：%d 个项目", len(projects_data))
    return result


def import_data(
    data: dict[str, Any], mode: str = "merge", *, import_ai_config: bool = False
) -> dict[str, Any]:
    """导入 JSON 数据。

    Args:
        data: 导出的备份数据字典
        mode: "merge"（跳过已存在的项目）或 "replace"（覆盖已存在的项目）
        import_ai_config: 是否导入 AI 配置（不含 apiKey，保留现有密钥）

    Returns:
        导入统计：{"projects": N, "versions": N, "tasks": N, "taskEvents": N,
                   "sources": N, "receipts": N, "settings": bool, "aiConfig": bool,
                   "skipped": N}
    """
    if data.get("format") != BACKUP_FORMAT:
        raise ValueError("文件格式不正确：缺少 studio-backup 标识")

    backup_version = data.get("version", 0)
    if backup_version != BACKUP_VERSION:
        raise ValueError(f"不支持的备份版本：{backup_version}，当前支持版本：{BACKUP_VERSION}")

    if mode not in ("merge", "replace"):
        raise ValueError("导入模式必须是 merge 或 replace")

    counts: dict[str, Any] = {
        "projects": 0,
        "versions": 0,
        "tasks": 0,
        "taskEvents": 0,
        "sources": 0,
        "receipts": 0,
        "settings": False,
        "aiConfig": False,
        "skipped": 0,
    }

    with connect() as conn:
        for item in data.get("projects", []):
            proj = item.get("project", {})
            pid = proj.get("id")
            if not pid:
                continue

            existing = conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()
            if existing and mode == "merge":
                counts["skipped"] += 1
                continue

            # 删除已存在的项目（级联删除关联数据）
            if existing:
                conn.execute("DELETE FROM projects WHERE id=?", (pid,))

            # X3 审计修复：插入项目前对文本字段进行 XSS 清洗
            _sanitize_project_row(proj)

            # 插入项目
            _insert_row(conn, "projects", proj)
            counts["projects"] += 1

            # 插入版本历史
            for ver in item.get("versions", []):
                ver.pop("id", None)  # 自增主键由数据库分配
                _insert_row(conn, "project_versions", ver)
                counts["versions"] += 1

            # 插入任务及事件
            for task in item.get("tasks", []):
                events = task.pop("events", [])
                _insert_row(conn, "tasks", task)
                counts["tasks"] += 1

                for evt in events:
                    evt.pop("id", None)
                    _insert_row(conn, "task_events", evt)
                    counts["taskEvents"] += 1

            # 插入来源快照（INSERT OR IGNORE 去重）
            for src_item in item.get("sources", []):
                snapshot = src_item.get("snapshot", {})
                source_order = src_item.get("sourceOrder", 0)

                # X3 审计修复：插入来源快照前对 content_text / preview 进行 XSS 清洗
                _sanitize_source_snapshot(snapshot)

                _insert_row(conn, "source_snapshots", snapshot, or_ignore=True)
                counts["sources"] += 1

                # 关联项目与来源
                conn.execute(
                    "INSERT OR REPLACE INTO project_sources(project_id, snapshot_id, source_order) VALUES(?,?,?)",
                    (pid, snapshot.get("id", ""), source_order),
                )

            # 插入发布回执
            for rec in item.get("publishReceipts", []):
                rec.pop("id", None)
                _insert_row(conn, "publish_receipts", rec)
                counts["receipts"] += 1

    # 导入通用设置（在事务外调用，避免写锁竞争）
    general = data.get("settings", {}).get("general")
    if general and isinstance(general, dict):
        set_setting("general", general)
        counts["settings"] = True

    # D3 审计修复：导入 AI 配置（不含 apiKey，保留现有密钥）
    if import_ai_config:
        ai_imported = data.get("settings", {}).get("ai")
        if ai_imported and isinstance(ai_imported, dict):
            existing_ai = get_setting("ai")
            merged_ai = dict(existing_ai)
            # 覆盖非敏感字段，保留现有的 apiKey / apiKeyHintStored
            for field in ("baseUrl", "model", "temperature", "maxTokens", "autoReview"):
                if field in ai_imported:
                    merged_ai[field] = ai_imported[field]
            set_setting("ai", merged_ai)
            counts["aiConfig"] = True

    logger.info(
        "数据导入完成（mode=%s）：项目 %d，跳过 %d，版本 %d，任务 %d，来源 %d，回执 %d，设置 %s，AI配置 %s",
        mode,
        counts["projects"],
        counts["skipped"],
        counts["versions"],
        counts["tasks"],
        counts["sources"],
        counts["receipts"],
        "是" if counts["settings"] else "否",
        "是" if counts["aiConfig"] else "否",
    )
    return counts


def _insert_row(
    conn, table: str, row: dict[str, Any], *, or_ignore: bool = False
) -> None:
    """通用行插入辅助函数。

    Args:
        conn: 数据库连接
        table: 目标表名
        row: 列名到值的映射
        or_ignore: 是否使用 INSERT OR IGNORE（用于去重插入）
    """
    cols = list(row.keys())
    if not cols:
        return
    placeholders = ",".join("?" * len(cols))
    col_names = ",".join(cols)
    verb = "INSERT OR IGNORE INTO" if or_ignore else "INSERT INTO"
    conn.execute(f"{verb} {table}({col_names}) VALUES({placeholders})", list(row.values()))
