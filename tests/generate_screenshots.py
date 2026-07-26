from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("STUDIO_SCREENSHOT_DIR") or (ROOT / "docs" / "screenshots"))

NOW = "2026-07-23T08:00:00+00:00"
PROJECT = {
    "id": "demo-project",
    "title": "AI 原生内容工作流：从证据到公众号草稿",
    "goal": "说明内容生产如何同时保证效率、安全与发布一致性",
    "sourceInput": "https://example.com/research/article",
    "sourceKind": "url",
    "status": "draft",
    "archived": False,
    "deleted": False,
    "outline": ["为什么需要统一工作流", "证据与严格事实门禁", "串行保存和版本一致性", "发布快照与人工终审", "落地建议"],
    "bodyMarkdown": "# AI 原生内容工作流\n\n公众号内容生产不只是生成文字，还需要让来源、编辑、审校与发布保持一致。\n\n## 证据先行\n\n严格事实模式会检查可核验来源；证据不足时暂停，而不是继续编造。\n\n## 保存与发布一致\n\n同一文章的保存请求严格串行。发布前冻结 revision 和正文指纹，微信回执也绑定到该快照。\n\n## 结果\n\n用户看到的预览、人工终审的版本和最终同步内容保持一致。",
    "summary": "用来源证据、串行保存、版本指纹和不可变发布快照，构建可靠的公众号 AI 内容工作流。",
    "coverDataUrl": "",
    "review": [
        {"label": "事实与来源", "status": "passed", "message": "关键陈述均可追溯到来源快照。"},
        {"label": "结构与可读性", "status": "passed", "message": "标题、摘要、层级与段落结构完整。"},
        {"label": "发布风险", "status": "warning", "message": "同步正式公众号前仍需人工核对封面与账号权限。"},
    ],
    "reviewFingerprint": "abc123",
    "reviewApproved": True,
    "reviewRevision": 8,
    "reviewedAt": NOW,
    "publishStatus": "not_synced",
    "publishRemoteId": "",
    "publishedRevision": 0,
    "publishFingerprint": "",
    "publishPreviewHash": "",
    "revision": 8,
    "createdAt": NOW,
    "updatedAt": NOW,
    "sources": [
        {
            "title": "内容工作流安全与一致性研究",
            "finalUrl": "https://example.com/research/article",
            "publisher": "示例研究资料",
            "fetchedAt": NOW,
            "contentHash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "extractionMethod": "html-main-content",
            "preview": "本文讨论来源证据、并发编辑、人工终审和发布快照的一致性设计。",
        }
    ],
}
TASK = {
    "id": "task-demo",
    "projectId": PROJECT["id"],
    "projectTitle": PROJECT["title"],
    "status": "succeeded",
    "currentStep": "completed",
    "progress": 100,
    "message": "统一工作流已完成",
    "errorCode": "",
    "updatedAt": NOW,
    "events": [
        {"step": "source", "message": "来源快照已固定", "detail": {}},
        {"step": "research", "message": "证据门禁通过", "detail": {}},
        {"step": "outline", "message": "文章框架已生成", "detail": {}},
        {"step": "draft", "message": "正文已生成", "detail": {}},
        {"step": "review", "message": "自动审校完成", "detail": {}},
        {"step": "completed", "message": "统一工作流已完成", "detail": {}},
    ],
}
PREVIEW = {
    "revision": 8,
    "bodyFingerprint": "abc123",
    "previewHash": "preview123",
    "html": "<h1>AI 原生内容工作流</h1><p>公众号内容生产不只是生成文字，还需要让来源、编辑、审校与发布保持一致。</p><h2>证据先行</h2><p>严格事实模式会检查可核验来源；证据不足时暂停，而不是继续编造。</p><h2>保存与发布一致</h2><p>同一文章的保存请求严格串行。发布前冻结 revision 和正文指纹，微信回执也绑定到该快照。</p>",
}


def mock_script() -> str:
    fixtures = {
        "bootstrap": {
            "version": "2.1.3",
            "projects": [PROJECT],
            "tasks": [TASK],
            "settings": {
                "ai": {"baseUrl": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "temperature": 0.4, "autoReview": True, "apiKeySet": True, "apiKeyHint": "••••1234"},
                "general": {"defaultLength": 1800, "strictFacts": True, "allowNetwork": True},
                "wechat": {"accountName": "演示公众号", "appId": "wx-demo", "appSecretSet": True, "appSecretHint": "••••5678", "thumbMediaId": "demo-media"},
            },
        },
        "health": {"ok": True, "version": "2.1.3", "database": {"ok": True}, "ai": {"configured": True, "reachable": True, "verifiedAt": NOW}, "wechat": {"configured": True, "reachable": True, "verifiedAt": NOW}},
        "project": PROJECT,
        "task": TASK,
        "preview": PREVIEW,
    }
    data = json.dumps(fixtures, ensure_ascii=False)
    return f"""
window.__fixtures = {data};
window.fetch = async function(input, options={{}}) {{
  const path = typeof input === 'string' ? input : input.url;
  let value;
  if (path === '/api/v2/bootstrap') value = window.__fixtures.bootstrap;
  else if (path === '/api/v2/health') value = window.__fixtures.health;
  else if (path.startsWith('/api/v2/projects/demo-project/preview')) value = window.__fixtures.preview;
  else if (path.startsWith('/api/v2/projects/demo-project/versions')) value = {{items:[{{revision:7, reason:'autosave', createdAt:'{NOW}', snapshot:window.__fixtures.project}}]}};
  else if (path.startsWith('/api/v2/projects/demo-project')) value = window.__fixtures.project;
  else if (path.startsWith('/api/v2/tasks/task-demo')) value = window.__fixtures.task;
  else if (path.startsWith('/api/v2/tasks')) value = {{items:[window.__fixtures.task]}};
  else if (path.startsWith('/api/v2/projects')) value = {{items:[window.__fixtures.project]}};
  else value = {{}};
  return new Response(JSON.stringify(value), {{status:200, headers:{{'content-type':'application/json'}}}});
}};
window.confirm = () => true;
"""


def prepare(page, viewport: dict[str, int]) -> None:
    page.set_viewport_size(viewport)
    page.set_content('<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>公众号 AI Studio</title></head><body><div id="app"></div><div id="toast-root" class="toast-root"></div></body></html>')
    page.add_style_tag(content=(ROOT / "web" / "styles.css").read_text(encoding="utf-8"))
    page.add_script_tag(content=mock_script())
    page.add_script_tag(content=(ROOT / "web" / "app.js").read_text(encoding="utf-8"))
    page.wait_for_selector(".app-shell")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH") or "/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        prepare(page, {"width": 1440, "height": 1000})
        page.screenshot(path=str(OUT / "2.1.3_唯一创作入口.png"), full_page=True)
        page.evaluate("location.hash='#/workspace?project=demo-project&task=task-demo'")
        page.wait_for_selector("#project-body")
        page.screenshot(path=str(OUT / "2.1.3_单页工作区.png"), full_page=True)
        page.close()

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        prepare(mobile, {"width": 390, "height": 844})
        mobile.screenshot(path=str(OUT / "2.1.3_移动端.png"), full_page=True)
        mobile.close()
        browser.close()
    print("screenshot_render: OK")


if __name__ == "__main__":
    main()
