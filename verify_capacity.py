from __future__ import annotations

from contextlib import closing

import json
import os
import random
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VERSION = "2.1.3"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(base: str, path: str, method: str = "GET", body: dict[str, Any] | None = None, headers=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            value = json.loads(raw) if raw else {}
            return response.status, value, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        value = json.loads(raw) if raw else {}
        return exc.code, value, (time.perf_counter() - started) * 1000


def wait_server(base: str, process: subprocess.Popen[str]) -> None:
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read() if process.stdout else "server exited")
        try:
            status, _, _ = request(base, "/api/v2/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def seed_database(path: Path, count: int) -> None:
    env = os.environ.copy()
    env["STUDIO_DB"] = str(path)
    env["STUDIO_MASTER_KEY_FILE"] = str(path.with_name("capacity.master.key"))
    code = "import sys; sys.path.insert(0, 'backend'); from db import init_db; init_db()"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, timeout=60)
    now = datetime.now(timezone.utc)
    paragraph = "容量验证正文。" * 120
    rows = []
    for index in range(count):
        stamp = (now - timedelta(seconds=index)).replace(microsecond=0).isoformat()
        rows.append(
            (
                f"cap_{index:06d}",
                f"容量文章 {index:06d}" + (" 唯一检索标记" if index == count // 2 else ""),
                "容量与稳定性验证",
                f"主题 {index}",
                "topic",
                "draft",
                0,
                0,
                "[]",
                paragraph + f"\n\n序号：{index}",
                f"第 {index} 篇容量测试摘要",
                "",
                "[]",
                "",
                0,
                "not_synced",
                "",
                1,
                stamp,
                stamp,
            )
        )
    with closing(sqlite3.connect(path)) as conn:
        conn.executemany(
            """
            INSERT INTO projects(
                id,title,goal,source_input,source_kind,status,archived,deleted,outline_json,body_markdown,
                summary,cover_data_url,review_json,review_fingerprint,review_approved,publish_status,publish_remote_id,
                revision,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"seeded database integrity check failed: {result}")


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def rss_mb(pid: int) -> float:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def run_validation(article_count: int, edit_count: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="studio-capacity-") as temp:
        temp_root = Path(temp)
        db_path = temp_root / "capacity.db"
        seed_started = time.perf_counter()
        seed_database(db_path, article_count)
        seed_seconds = time.perf_counter() - seed_started
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_")}
        env.update(
            {
                "STUDIO_DB": str(db_path),
                "STUDIO_MASTER_KEY_FILE": str(temp_root / "master.key"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_server(base, process)
            initial_rss = rss_mb(process.pid)
            status, first, first_ms = request(base, "/api/v2/projects?includeArchived=false&limit=50&offset=0")
            if status != 200 or first.get("total") != article_count or len(first.get("items") or []) != 50:
                raise RuntimeError(f"first page failed: status={status}, payload={first}")
            last_offset = max(0, article_count - 50)
            status, last, last_ms = request(base, f"/api/v2/projects?includeArchived=false&limit=50&offset={last_offset}")
            if status != 200 or not last.get("items"):
                raise RuntimeError("last page failed")
            status, searched, search_ms = request(
                base,
                "/api/v2/projects?includeArchived=false&limit=50&q=" + urllib.parse.quote("唯一检索标记"),
            )
            if status != 200 or searched.get("total") != 1:
                raise RuntimeError(f"server-side search failed: {searched}")

            page_latencies: list[float] = []
            for _ in range(60):
                offset = random.randrange(0, max(1, article_count // 50)) * 50
                status, payload, elapsed = request(
                    base,
                    f"/api/v2/projects?includeArchived=false&limit=50&offset={offset}",
                )
                if status != 200 or len(payload.get("items") or []) > 50:
                    raise RuntimeError("random page request failed")
                page_latencies.append(elapsed)

            project_id = "cap_000000"
            status, project, _ = request(base, f"/api/v2/projects/{project_id}")
            if status != 200:
                raise RuntimeError("edit target unavailable")
            edit_latencies: list[float] = []
            for index in range(edit_count):
                body = "持续编辑稳定性验证。" * 120 + f"\n\n保存序号：{index}"
                status, project, elapsed = request(
                    base,
                    f"/api/v2/projects/{project_id}",
                    method="PATCH",
                    body={"bodyMarkdown": body},
                    headers={"If-Match": str(project["revision"])},
                )
                if status != 200:
                    raise RuntimeError(f"continuous edit failed at {index}: {project}")
                edit_latencies.append(elapsed)

            status, versions, versions_ms = request(base, f"/api/v2/projects/{project_id}/versions")
            if status != 200 or len(versions.get("items") or []) > 100:
                raise RuntimeError("version retention limit failed")
            final_rss = rss_mb(process.pid)
            with closing(sqlite3.connect(db_path)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                project_rows = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                version_rows = conn.execute(
                    "SELECT COUNT(*) FROM project_versions WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                db_bytes = db_path.stat().st_size
            checks = {
                "articleCount": project_rows == article_count,
                "firstPageUnder1000ms": first_ms < 1000,
                "lastPageUnder1500ms": last_ms < 1500,
                "searchUnder1500ms": search_ms < 1500,
                "pageP95Under1000ms": percentile(page_latencies, 0.95) < 1000,
                "allContinuousEditsSucceeded": len(edit_latencies) == edit_count,
                "editP95Under1000ms": percentile(edit_latencies, 0.95) < 1000,
                "versionRetentionAtMost100": version_rows <= 100,
                "integrityCheckOk": integrity == "ok",
            }
            return {
                "product": "公众号 AI Studio",
                "version": VERSION,
                "generatedAt": utc_now(),
                "status": "succeeded" if all(checks.values()) else "failed",
                "mode": "real SQLite + real local HTTP API; accelerated continuous-edit soak",
                "dataset": {
                    "articles": article_count,
                    "edits": edit_count,
                    "seedSeconds": round(seed_seconds, 3),
                    "databaseBytes": db_bytes,
                },
                "latencyMs": {
                    "firstPage": round(first_ms, 2),
                    "lastPage": round(last_ms, 2),
                    "search": round(search_ms, 2),
                    "randomPageMedian": round(statistics.median(page_latencies), 2),
                    "randomPageP95": round(percentile(page_latencies, 0.95), 2),
                    "editMedian": round(statistics.median(edit_latencies), 2),
                    "editP95": round(percentile(edit_latencies, 0.95), 2),
                    "versions": round(versions_ms, 2),
                },
                "memoryMb": {
                    "serverInitialRss": round(initial_rss, 2),
                    "serverFinalRss": round(final_rss, 2),
                    "delta": round(final_rss - initial_rss, 2),
                },
                "versionRows": version_rows,
                "integrityCheck": integrity,
                "checks": checks,
                "note": "该验证关闭万级文章分页/检索和加速持续编辑门禁；生产环境仍应按实际机器执行更长时长 soak。",
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()


def main() -> None:
    article_count = max(10_000, int(os.environ.get("STUDIO_CAPACITY_ARTICLES", "10000")))
    edit_count = max(20, min(100, int(os.environ.get("STUDIO_CAPACITY_EDITS", "100"))))
    result = run_validation(article_count, edit_count)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    output_file = os.environ.get("STUDIO_CAPACITY_RESULT_FILE", "").strip()
    if output_file:
        Path(output_file).write_text(output + "\n", encoding="utf-8")
    print(output)
    if result["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
