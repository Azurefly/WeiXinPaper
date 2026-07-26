const app = document.getElementById('app');
const toastRoot = document.getElementById('toast-root');
const DRAFT_STORAGE_KEY = 'studio-form-drafts';

const state = {
  loading: true,
  fatal: '',
  version: '2.1.3',
  projects: [],
  projectCounts: { active: 0, all: 0, deleted: 0 },
  articlePage: 0,
  articlePageSize: 50,
  articleTotal: 0,
  articleSearchTimer: null,
  tasks: [],
  settings: { ai: {}, general: {}, wechat: {} },
  currentProject: null,
  currentTask: null,
  preview: null,
  versions: [],
  showVersions: false,
  saveState: 'idle',
  conflict: null,
  search: '',
  showArchived: false,
  showDeleted: false,
  mobileOpen: false,
  health: null,
  pollTimer: null,
  saveTimers: new Map(),
  pendingSaves: new Map(),
  saveChains: new Map(),
  dirtyProjects: new Set(),
  aiDraft: null,
  generalDraft: null,
  wechatDraft: null,
  logs: [],
  logsFilter: { level: 'ALL', q: '' },
  logsAutoRefresh: true,
  logsPollTimer: null,
  bodyMode: 'edit', // 'edit' | 'preview'
};

const ROUTES = {
  create: { label: '创作', icon: '✦', title: '开始创作' },
  articles: { label: '文章', icon: '▤', title: '文章中心' },
  ai: { label: 'AI', icon: '◎', title: 'AI 能力' },
  logs: { label: '日志', icon: '📋', title: '系统日志中心' },
  settings: { label: '设置', icon: '⚙', title: '设置' },
};

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date);
}

function routeInfo() {
  const raw = location.hash.replace(/^#\/?/, '') || 'create';
  const [path, query = ''] = raw.split('?');
  return { path, params: new URLSearchParams(query) };
}

function loadStoredDrafts() {
  try {
    const raw = window.sessionStorage.getItem(DRAFT_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    state.aiDraft = parsed.aiDraft || null;
    state.generalDraft = parsed.generalDraft || null;
    state.wechatDraft = parsed.wechatDraft || null;
  } catch {
    state.aiDraft = null;
    state.generalDraft = null;
    state.wechatDraft = null;
  }
}

function persistDraft(key, value) {
  try {
    const raw = window.sessionStorage.getItem(DRAFT_STORAGE_KEY);
    const drafts = raw ? JSON.parse(raw) : {};
    if (value) drafts[key] = value;
    else delete drafts[key];
    window.sessionStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(drafts));
  } catch {
    // Ignore sessionStorage failures and keep in-memory draft protection.
  }
}

async function navigate(path, params = {}) {
  syncAiDraftFromDom();
  syncGeneralDraftFromDom();
  syncWechatDraftFromDom();
  const current = state.currentProject?.id;
  if (current) {
    const ok = await flushProjectSave(current);
    if (!ok) return;
  }
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value));
  location.hash = `#/${path}${query.toString() ? `?${query}` : ''}`;
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  let body = options.body;
  if (body && typeof body !== 'string' && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { ...options, body, headers });
  } catch (error) {
    const failure = new Error(`无法连接本地服务：${error.message}`);
    failure.code = 'network_error';
    throw failure;
  }
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const problem = data?.error || {};
    const error = new Error(problem.message || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = problem.code || 'request_failed';
    error.detail = problem.detail;
    throw error;
  }
  return data;
}

function toast(message, type = '') {
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.textContent = message;
  node.setAttribute('role', 'status');
  toastRoot.append(node);
  setTimeout(() => node.remove(), 4200);
}

function statusPill(status) {
  const labels = {
    queued: ['排队中', 'running'], running: ['执行中', 'running'], succeeded: ['已完成', 'success'],
    failed: ['失败', 'danger'], blocked: ['等待处理', 'warning'], cancelled: ['已取消', 'warning'],
    timeout: ['已超时', 'danger'], draft: ['草稿', ''], working: ['生成中', 'running'],
    synced: ['已同步', 'success'], not_synced: ['未同步', ''], stale: ['旧版本已同步', 'warning'],
  };
  const [label, cls] = labels[status] || [status || '未知', ''];
  return `<span class="pill ${cls}">${escapeHtml(label)}</span>`;
}

function setProjectInState(project) {
  if (!project) return;
  const index = state.projects.findIndex((item) => item.id === project.id);
  if (index >= 0) state.projects[index] = project;
  else state.projects.unshift(project);
  if (state.currentProject?.id === project.id) state.currentProject = project;
}

async function bootstrap() {
  state.loading = true;
  render();
  try {
    const data = await api('/api/v2/bootstrap');
    state.version = data.version;
    state.projects = data.projects || [];
    state.projectCounts = data.projectCounts || state.projectCounts;
    state.articleTotal = Number(data.projectTotal || state.projects.length);
    state.tasks = data.tasks || [];
    state.settings = data.settings || state.settings;
    state.fatal = '';
    await refreshHealth();
  } catch (error) {
    state.fatal = error.message;
  } finally {
    state.loading = false;
    render();
    startPolling();
  }
}

async function refreshHealth() {
  try {
    state.health = await api('/api/v2/health');
  } catch {
    state.health = null;
  }
}

function startPolling() {
  clearTimeout(state.pollTimer);
  const active = state.tasks.some((task) => ['queued', 'running'].includes(task.status));
  state.pollTimer = setTimeout(async () => {
    try {
      const data = await api('/api/v2/tasks?limit=100');
      state.tasks = data.items || [];
      const { path, params } = routeInfo();
      let shouldRender = false;
      if (path === 'workspace') {
        const projectId = params.get('project');
        const taskId = params.get('task');
        if (taskId) state.currentTask = await api(`/api/v2/tasks/${encodeURIComponent(taskId)}`);
        if (projectId && !state.dirtyProjects.has(projectId) && !state.saveChains.has(projectId)) {
          state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(projectId)}`);
          await refreshPreview(false);
          shouldRender = true;
        }
      } else if (path === 'tasks') {
        if (params.get('task')) {
          state.currentTask = await api(`/api/v2/tasks/${encodeURIComponent(params.get('task'))}`);
        }
        shouldRender = true;
      }
      if (shouldRender) render();
      else updateTaskProgressOnly();
    } catch (error) {
      console.warn(error);
    }
    startPolling();
  }, active ? 1800 : 12000);
}

async function loadRouteData() {
  const { path, params } = routeInfo();
  try {
    if (path === 'workspace') {
      const projectId = params.get('project');
      const taskId = params.get('task');
      state.currentProject = projectId ? await api(`/api/v2/projects/${encodeURIComponent(projectId)}`) : null;
      state.currentTask = taskId ? await api(`/api/v2/tasks/${encodeURIComponent(taskId)}`) : null;
      state.conflict = null;
      state.versions = [];
      state.showVersions = false;
      await refreshPreview(false);
    } else if (path === 'articles') {
      await reloadProjects();
    } else if (path === 'tasks') {
      const taskId = params.get('task');
      state.currentTask = taskId ? await api(`/api/v2/tasks/${encodeURIComponent(taskId)}`) : null;
    }
  } catch (error) {
    toast(error.message, 'error');
  }
  render();
}

function appShell(content, activeRoute) {
  const current = ROUTES[activeRoute] || { title: activeRoute === 'workspace' ? '文章工作区' : '任务诊断' };
  const health = state.health;
  return `
    <div class="app-shell">
      <aside class="sidebar ${state.mobileOpen ? 'open' : ''}" aria-label="主导航">
        <div class="brand"><div class="brand-mark">✦</div><div class="brand-text"><strong>公众号 AI Studio</strong><span>AI 原生内容工作台</span></div></div>
        <nav class="nav">
          ${Object.entries(ROUTES).map(([key, item]) => `<button data-nav="${key}" class="${activeRoute === key ? 'active' : ''}" aria-label="${item.label}"><span class="nav-icon">${item.icon}</span><span class="nav-label">${item.label}</span></button>`).join('')}
        </nav>
        <div class="sidebar-foot">2.1.3 Audit Repair<br>本地 SQLite · 回环安全模式</div>
      </aside>
      ${state.mobileOpen ? '<button class="mobile-overlay" id="mobile-overlay" aria-label="关闭菜单"></button>' : ''}
      <main class="main">
        <header class="topbar">
          <div class="top-actions"><button class="icon-btn mobile-menu" id="mobile-menu" aria-label="打开菜单">☰</button><h1>${escapeHtml(current.title)}</h1></div>
          <div class="top-actions">
            <span class="pill ${health?.ok ? 'success' : 'danger'} desktop-only">${health?.ok ? '● 服务正常' : '● 服务异常'}</span>
            <button class="icon-btn" id="refresh-all" aria-label="刷新">↻</button>
          </div>
        </header>
        <div class="content">${content}</div>
      </main>
    </div>`;
}

function renderCreate() {
  const activeCount = state.tasks.filter((task) => ['queued', 'running'].includes(task.status)).length;
  const finishedCount = state.tasks.filter((task) => task.status === 'succeeded').length;
  return `
    <section class="card hero">
      <div class="eyebrow">唯一创作入口</div>
      <h2>粘贴来源，或直接说你想写什么</h2>
      <p>输入网页、GitHub 地址或自然语言主题。严格事实模式下，没有可核验来源的主题任务会暂停，不会伪造证据继续生成。</p>
      <form id="create-form" class="create-box">
        <div class="field">
          <label for="source-input">来源或创作目标</label>
          <input id="source-input" class="input create-input" maxlength="4000" autocomplete="off" placeholder="例如：https://github.com/... 或 写一篇关于 Spring Boot 新版本的公众号文章" required />
        </div>
        <button class="btn btn-primary" type="submit" id="create-button">开始创作 →</button>
      </form>
      <details style="margin-top:16px">
        <summary class="helper">高级设置</summary>
        <label class="checkline" style="margin-top:12px"><input type="checkbox" id="create-auto-review" ${state.settings.ai?.autoReview !== false ? 'checked' : ''}><span><strong>生成后自动审校</strong><br><span class="helper">关闭后时间线会明确显示“已跳过”，不会伪装成已执行。</span></span></label>
      </details>
    </section>
    <div class="grid grid-3 stats">
      <div class="card stat"><strong>${state.projectCounts.active ?? state.projects.filter((p) => !p.archived && !p.deleted).length}</strong><span>当前文章</span></div>
      <div class="card stat"><strong>${activeCount}</strong><span>正在执行</span></div>
      <div class="card stat"><strong>${finishedCount}</strong><span>成功任务</span></div>
    </div>`;
}

function timelineHtml(task) {
  if (!task) return '<div class="empty">没有任务信息</div>';
  const steps = [
    ['source', '读取来源'], ['research', '理解目标'], ['outline', '文章框架'],
    ['draft', '正文写作'], ['cover', '封面生成'], ['review', '自动审校'], ['completed', '生成完成'],
  ];
  const order = ['queued', 'source', 'research', 'outline', 'draft', 'cover', 'review', 'completed'];
  const currentIndex = order.indexOf(task.currentStep || 'queued');
  const events = task.events || [];
  const skipped = new Map(events.filter((event) => event.detail?.skipped).map((event) => [event.step, event.message]));
  const executed = new Set(events.filter((event) => !event.detail?.skipped).map((event) => event.step));
  return `<div class="timeline">${steps.map(([key, label]) => {
    const index = order.indexOf(key);
    const wasSkipped = skipped.has(key);
    const done = !wasSkipped && (executed.has(key) || task.status === 'succeeded' || (index < currentIndex && ['queued', 'running'].includes(task.status)));
    const active = key === task.currentStep && ['queued', 'running'].includes(task.status);
    const error = key === task.currentStep && ['failed', 'timeout', 'blocked', 'cancelled'].includes(task.status);
    const cls = wasSkipped ? 'skipped' : done ? 'done' : active ? 'active' : error ? 'error' : '';
    const symbol = wasSkipped ? '–' : done ? '✓' : active ? '•' : error ? '!' : '';
    const message = wasSkipped ? skipped.get(key) : active || error ? task.message : done ? '已真实执行' : index < currentIndex ? '已处理' : '等待执行';
    return `<div class="timeline-item"><div class="timeline-dot ${cls}">${symbol}</div><div class="timeline-content"><strong>${label}</strong><span>${escapeHtml(message)}</span></div></div>`;
  }).join('')}</div>`;
}

function conflictField(label, id, value, textarea = false) {
  const tag = textarea
    ? `<textarea id="${id}" style="min-height:${id.includes('body') ? '240px' : '82px'}">${escapeHtml(value || '')}</textarea>`
    : `<input class="input" id="${id}" value="${escapeHtml(value || '')}">`;
  return `<div class="field"><label for="${id}">${label}</label>${tag}</div>`;
}

function conflictHtml() {
  if (!state.conflict) return '';
  const { server, pendingFields } = state.conflict;
  return `<section class="card card-pad conflict">
    <div class="section-title"><div><h3>检测到多字段编辑冲突</h3><p>标题、摘要、正文和封面都保留在本地，不会被静默丢弃。</p></div>${statusPill('warning')}</div>
    <div class="grid grid-2">
      <div class="source-card"><strong>服务端 revision ${server.revision}</strong><p class="helper">${escapeHtml(server.title)}</p><p class="helper">${escapeHtml(server.summary)}</p></div>
      <div class="source-card"><strong>本地待保存字段</strong><p class="helper">${escapeHtml(Object.keys(pendingFields).join('、') || '正文')}</p></div>
    </div>
    <div class="form-grid" style="margin-top:14px">
      ${conflictField('本地标题', 'conflict-title', pendingFields.title ?? server.title)}
      ${conflictField('本地摘要', 'conflict-summary', pendingFields.summary ?? server.summary, true)}
      <div class="wide">${conflictField('本地正文', 'conflict-body', pendingFields.bodyMarkdown ?? server.bodyMarkdown, true)}</div>
    </div>
    <div class="top-actions" style="margin-top:14px">
      <button class="btn btn-ghost" id="conflict-use-server">采用服务端</button>
      <button class="btn btn-secondary" id="conflict-merge">合并正文并保留本地信息</button>
      <button class="btn btn-primary" id="conflict-overwrite">用本地字段覆盖</button>
    </div>
  </section>`;
}

function versionsHtml() {
  if (!state.showVersions) return '';
  return `<section class="card card-pad">
    <div class="section-title"><div><h3>版本历史</h3><p>恢复前会自动保存当前版本，审校与发布状态会重新失效。</p></div><button class="icon-btn" id="close-versions" aria-label="关闭版本历史">×</button></div>
    <div class="article-list">${state.versions.length ? state.versions.map((item) => `
      <div class="source-card"><strong>revision ${item.revision} · ${escapeHtml(item.reason)}</strong><div class="source-meta"><span>${formatTime(item.createdAt)}</span><span>${escapeHtml(item.snapshot?.title || '')}</span></div><button class="btn btn-secondary" data-restore-version="${item.revision}" style="margin-top:10px">恢复此版本</button></div>`).join('') : '<div class="empty">暂无历史版本</div>'}</div>
  </section>`;
}

function renderWorkspace() {
  const project = state.currentProject;
  const task = state.currentTask;
  if (!project) return '<div class="card empty"><strong>尚未选择文章</strong><span>请从创作入口或文章中心打开文章。</span></div>';
  const sources = project.sources || [];
  const review = project.review || [];
  const blockedBySave = state.dirtyProjects.has(project.id) || state.saveChains.has(project.id) || Boolean(state.conflict);
  const reviewCurrent = project.reviewApproved && project.reviewRevision === project.revision;
  const previewCurrent = state.preview?.revision === project.revision;
  const canPublish = reviewCurrent && previewCurrent && project.bodyMarkdown && !blockedBySave;
  const taskActions = task && ['failed', 'blocked', 'timeout', 'cancelled'].includes(task.status)
    ? `<select id="retry-mode" aria-label="重试范围"><option value="review_only">仅重做审校</option><option value="preserve_body">保留正文，重做框架与审校</option><option value="from_outline">从现有框架重做正文</option><option value="full">全部重做</option></select><button class="btn btn-secondary" id="task-retry">按范围重试</button>` : '';
  return `
    <div class="page-head"><div><h2>${escapeHtml(project.title || '未命名文章')}</h2><p>保存完成后生成不可变预览，再终审当前 revision，最后同步该快照。</p></div><div class="top-actions">${statusPill(project.status)} ${statusPill(project.publishStatus)}</div></div>
    ${conflictHtml()}
    ${versionsHtml()}
    <div class="workspace-layout">
      <div class="workspace-main">
        <section class="card card-pad">
          <div class="section-title"><div><h3>AI 执行</h3><p>${task ? `任务 ${escapeHtml(task.id)}` : '该文章没有关联任务'}</p></div><div class="top-actions">${task ? statusPill(task.status) : ''}${taskActions}</div></div>
          ${task ? `${timelineHtml(task)}<div class="progress"><i style="width:${Math.max(0, Math.min(100, task.progress || 0))}%"></i></div><div class="top-actions" style="margin-top:14px"><button class="btn btn-ghost" id="open-task">查看任务诊断</button>${['queued', 'running'].includes(task.status) ? '<button class="btn btn-danger" id="task-cancel">取消任务</button>' : ''}${task.status === 'blocked' ? '<button class="btn btn-primary" data-nav="ai">配置 AI</button>' : ''}</div>` : '<div class="empty">没有任务信息</div>'}
        </section>
        <section class="card card-pad">
          <div class="section-title"><div><h3>文章信息</h3><p>同一文章的保存请求严格串行；保存期间继续输入会进入下一批。</p></div><span class="pill ${state.saveState === 'saved' ? 'success' : state.saveState === 'error' ? 'danger' : state.saveState === 'saving' ? 'running' : ''}" id="save-state">${{ idle: '已保存', saving: '保存中…', saved: '已保存', error: '保存失败' }[state.saveState] || '已保存'}</span></div>
          <div class="field"><label for="project-title">标题</label><input class="input autosave" id="project-title" data-field="title" maxlength="120" value="${escapeHtml(project.title)}"></div>
          <div class="field" style="margin-top:14px"><label for="project-summary">摘要</label><textarea class="autosave" id="project-summary" data-field="summary" maxlength="300" style="min-height:92px">${escapeHtml(project.summary)}</textarea></div>
        </section>
        <section class="card card-pad">
          <div class="section-title"><div><h3>文章框架</h3><p>重试时可选择复用框架或重新生成。</p></div><button class="btn btn-ghost" id="show-versions">版本历史</button></div>
          ${project.outline?.length ? `<ol class="outline-list">${project.outline.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ol>` : '<div class="empty"><strong>尚无框架</strong><span>任务完成后会显示文章框架。</span></div>'}
        </section>
        <section class="card card-pad">
          <div class="section-title"><div><h3>正文编辑</h3><p>标题、摘要、正文和封面任一变化都会使终审失效。</p></div><div class="body-mode-switch"><span class="helper" style="margin-right:8px">revision ${project.revision}</span><button class="btn btn-ghost body-mode-btn ${state.bodyMode === 'edit' ? 'active' : ''}" id="body-mode-edit">编辑</button><button class="btn btn-ghost body-mode-btn ${state.bodyMode === 'preview' ? 'active' : ''}" id="body-mode-preview">预览</button></div></div>
          ${state.bodyMode === 'edit'
            ? `<textarea class="editor autosave" id="project-body" data-field="bodyMarkdown" maxlength="500000" placeholder="正文将在这里生成，也可以直接手工写作。">${escapeHtml(project.bodyMarkdown)}</textarea>`
            : `<div class="body-preview-wrap">${previewCurrent ? `<div class="rich-preview body-preview-content">${state.preview.html}</div>` : '<div class="empty"><strong>暂无预览</strong><span>保存后点击"预览"按钮查看渲染效果。</span></div>'}</div>`
          }
        </section>
        <section class="card card-pad">
          <div class="section-title"><div><h3>发布前审校</h3><p>只能终审已完成保存且与服务端指纹一致的当前 revision。</p></div></div>
          ${review.length ? review.map((item) => `<div class="review-item"><div class="review-symbol ${escapeHtml(item.status)}">${item.status === 'passed' ? '✓' : item.status === 'failed' ? '×' : '!'}</div><div><strong>${escapeHtml(item.label)}</strong><p>${escapeHtml(item.message)}</p></div></div>`).join('') : '<div class="alert info">自动审校未执行或尚无结果。人工终审仍会绑定当前正文指纹。</div>'}
          ${blockedBySave ? '<div class="alert warning" style="margin-top:14px">仍有内容未保存或存在冲突，终审和发布已禁用。</div>' : ''}
          <label class="checkline" style="margin-top:14px"><input type="checkbox" id="review-approved" ${reviewCurrent ? 'checked' : ''} ${blockedBySave || !project.bodyMarkdown ? 'disabled' : ''}><span><strong>我已逐项核对事实、结构、标题、摘要和封面</strong><br><span class="helper">终审记录绑定 revision 与正文 SHA-256；任何编辑都会自动失效。</span></span></label>
        </section>
      </div>
      <aside class="workspace-side">
        <section class="card card-pad">
          <div class="section-title"><div><h3>来源快照</h3><p>按来源身份与内容哈希保存，不覆盖共享快照。</p></div>${project.sourceKind === 'url' ? '<button class="icon-btn" id="refresh-source" aria-label="重新读取来源">↻</button>' : ''}</div>
          ${sources.length ? sources.map((source) => `<div class="source-card"><strong>${escapeHtml(source.title || source.finalUrl)}</strong><div class="source-meta"><span>${escapeHtml(source.publisher || '未知发布方')}</span><span>${formatTime(source.fetchedAt)}</span></div><div class="source-meta" style="margin-top:8px"><span>SHA-256 ${escapeHtml(source.contentHash.slice(0, 16))}…</span><span>${escapeHtml(source.extractionMethod)}</span></div><p class="source-preview">${escapeHtml(source.preview)}</p></div>`).join('') : `<div class="empty"><strong>${project.sourceKind === 'topic' ? '主题创作' : '尚无来源快照'}</strong><span>${project.sourceKind === 'topic' ? '严格事实模式下，该任务会因缺少证据暂停。' : '来源读取成功后会显示。'}</span></div>`}
        </section>
        <section class="card card-pad">
          <div class="section-title"><div><h3>公众号发布快照</h3><p>这里展示的 HTML 与提交给微信的 HTML 完全一致。</p></div><button class="icon-btn" id="refresh-preview" aria-label="刷新发布预览">↻</button></div>
          <div class="preview-phone"><div class="preview-bar"></div><div class="preview-content">${project.coverDataUrl ? `<img class="cover-preview" src="${escapeHtml(project.coverDataUrl)}" alt="文章封面">` : ''}<h1>${escapeHtml(project.title)}</h1><div class="digest">${escapeHtml(project.summary || '尚未填写摘要')}</div><div class="preview-body rich-preview" id="publish-preview">${previewCurrent ? state.preview.html : '<p>保存后点击刷新预览。</p>'}</div></div></div>
          <div class="field" style="margin-top:16px"><label for="cover-file">封面图片（PNG/JPEG/WEBP/GIF，解码后小于 2MB）</label><input class="input" type="file" id="cover-file" accept="image/png,image/jpeg,image/webp,image/gif"></div>
          ${project.coverDataUrl ? '<button class="btn btn-ghost" id="remove-cover" style="width:100%;margin-top:10px">移除封面</button>' : ''}
          <button class="btn btn-primary" id="publish-button" style="width:100%;margin-top:14px" ${canPublish ? '' : 'disabled'}>同步当前快照到公众号草稿</button>
          <p class="helper" style="margin-top:10px">预览 revision：${state.preview?.revision ?? '—'}；终审 revision：${project.reviewRevision || '—'}</p>
        </section>
      </aside>
    </div>`;
}

function renderArticles() {
  const totalPages = Math.max(1, Math.ceil(state.articleTotal / state.articlePageSize));
  const currentPage = Math.min(state.articlePage + 1, totalPages);
  return `
    <div class="page-head"><div><h2>${state.showDeleted ? '回收站' : '文章中心'}</h2><p>服务端搜索与分页，支持万级文章库；生命周期包含归档、软删除、恢复、永久删除、复制和导出。</p></div><span class="pill">共 ${state.articleTotal} 篇</span></div>
    <section class="card card-pad">
      <div class="searchbar"><input class="input" id="article-search" placeholder="搜索标题或摘要" value="${escapeHtml(state.search)}"><label class="checkline"><input type="checkbox" id="show-archived" ${state.showArchived ? 'checked' : ''} ${state.showDeleted ? 'disabled' : ''}><span>显示归档</span></label><label class="checkline"><input type="checkbox" id="show-deleted" ${state.showDeleted ? 'checked' : ''}><span>回收站</span></label></div>
      <div class="article-list">${state.projects.length ? state.projects.map((project) => `
        <article class="card article-row"><div><h3>${escapeHtml(project.title)}</h3><p>${escapeHtml(project.summary || '暂无摘要')} · revision ${project.revision} · ${formatTime(project.updatedAt)}</p></div><div class="article-actions">
          ${project.deleted ? `<button class="btn btn-secondary" data-restore-deleted="${project.id}">恢复</button><button class="btn btn-danger" data-purge-project="${project.id}">永久删除</button>` : `<button class="btn btn-primary" data-open-project="${project.id}">打开</button><button class="btn btn-ghost" data-export-project="${project.id}">导出</button><button class="btn btn-ghost" data-copy-project="${project.id}">复制</button><button class="btn btn-ghost" data-archive-project="${project.id}" data-archived="${project.archived}">${project.archived ? '取消归档' : '归档'}</button><button class="btn btn-danger" data-delete-project="${project.id}">删除</button>`}
        </div></article>`).join('') : `<div class="empty"><strong>${state.showDeleted ? '回收站为空' : state.search ? '没有匹配文章' : '暂无文章'}</strong><span>${state.showDeleted ? '删除的文章会显示在这里。' : state.search ? '请调整搜索词。' : '从唯一创作入口开始。'}</span></div>`}</div>
      <div class="pagination" aria-label="文章分页">
        <button class="btn btn-ghost" id="article-prev" ${state.articlePage <= 0 ? 'disabled' : ''}>上一页</button>
        <span>第 ${currentPage} / ${totalPages} 页</span>
        <button class="btn btn-ghost" id="article-next" ${(state.articlePage + 1) * state.articlePageSize >= state.articleTotal ? 'disabled' : ''}>下一页</button>
      </div>
    </section>`;
}

function renderAi() {
  const savedAi = state.settings.ai || {};
  const apiKeyHint = savedAi.apiKeyHint || '';
  const ai = state.aiDraft || {
    providerId: savedAi.providerId || 'openai-compatible',
    baseUrl: savedAi.baseUrl || 'https://api.openai.com/v1',
    apiKey: '',
    model: savedAi.model || '',
    temperature: savedAi.temperature ?? 0.4,
    autoReview: savedAi.autoReview !== false,
  };
  const health = state.health?.ai || {};
  return `
    <div class="page-head"><div><h2>AI 能力</h2><p>配置、可连接、最近验证成功是三个独立状态。</p></div></div>
    <div class="grid grid-2">
      <section class="card card-pad"><div class="section-title"><div><h3>OpenAI 兼容模型</h3><p>请求固定到已验证公网 IP，禁止重定向携带 Authorization。</p></div></div>
        <form id="ai-form" class="setting-group">
          <div class="field"><label for="ai-base-url">Base URL</label><input class="input" id="ai-base-url" value="${escapeHtml(ai.baseUrl || 'https://api.openai.com/v1')}"></div>
          <div class="field"><label for="ai-key">API Key ${apiKeyHint ? `（已保存 ${escapeHtml(apiKeyHint)}）` : ''}</label><input class="input" type="password" id="ai-key" value="${escapeHtml(ai.apiKey || '')}" placeholder="留空表示保持原值"></div>
          <div class="field"><label for="ai-model">模型</label><input class="input" id="ai-model" value="${escapeHtml(ai.model || '')}"></div>
          <div class="field"><label for="ai-temp">温度</label><input class="input" id="ai-temp" type="number" min="0" max="2" step="0.1" value="${escapeHtml(ai.temperature ?? 0.4)}"></div>
          <label class="checkline"><input type="checkbox" id="ai-auto-review" ${ai.autoReview !== false ? 'checked' : ''}><span><strong>自动审校</strong><br><span class="helper">关闭后服务端会记录明确 skipped 事件。</span></span></label>
          <div class="top-actions"><button class="btn btn-primary" type="submit">保存设置</button><button class="btn btn-secondary" type="button" id="verify-ai">验证真实连接</button></div>
        </form>
      </section>
      <section class="card card-pad"><div class="section-title"><div><h3>状态</h3><p>最近验证结果不会被“已保存密钥”替代。</p></div></div>
        <div class="stack"><div class="source-card"><strong>已配置</strong><p class="helper">${health.configured ? '是' : '否'}</p></div><div class="source-card"><strong>可连接</strong><p class="helper">${health.reachable ? '是' : '否'}</p></div><div class="source-card"><strong>最近验证</strong><p class="helper">${formatTime(health.verifiedAt)} · ${escapeHtml(health.message || '尚未验证')}</p></div></div>
      </section>
    </div>`;
}

function renderSettings() {
  const savedGeneral = state.settings.general || {};
  const savedWechat = state.settings.wechat || {};
  const general = state.generalDraft || {
    defaultLength: savedGeneral.defaultLength || 1800,
    strictFacts: Boolean(savedGeneral.strictFacts),
    allowNetwork: savedGeneral.allowNetwork !== false,
  };
  const wechat = state.wechatDraft || {
    accountName: savedWechat.accountName || '',
    appId: savedWechat.appId || '',
    appSecret: '',
    thumbMediaId: savedWechat.thumbMediaId || '',
  };
  const appSecretHint = savedWechat.appSecretHint || '';
  const health = state.health?.wechat || {};
  return `
    <div class="page-head"><div><h2>设置</h2><p>输入类型和范围由服务端再次校验，字符串“false”不会被当成 true。</p></div></div>
    <div class="grid grid-2">
      <section class="card card-pad"><div class="section-title"><div><h3>创作策略</h3><p>严格事实模式要求可核验网页来源和来源编号。</p></div></div>
        <form id="general-form" class="setting-group">
          <div class="field"><label for="default-length">默认字数</label><input class="input" id="default-length" type="number" min="300" max="20000" value="${escapeHtml(general.defaultLength || 1800)}"></div>
          <label class="checkline"><input type="checkbox" id="strict-facts" ${general.strictFacts ? 'checked' : ''}><span><strong>严格事实模式</strong><br><span class="helper">主题创作缺少来源时会暂停，不会继续无证据生成。</span></span></label>
          <label class="checkline"><input type="checkbox" id="allow-network" ${general.allowNetwork !== false ? 'checked' : ''}><span><strong>允许联网</strong><br><span class="helper">关闭后来源刷新和外部发布会被阻止。</span></span></label>
          <button class="btn btn-primary" type="submit">保存通用设置</button>
        </form>
      </section>
      <section class="card card-pad"><div class="section-title"><div><h3>微信公众号</h3><p>先用临时凭证验证成功，再原子替换已保存配置。</p></div>${statusPill(health.reachable ? 'succeeded' : 'blocked')}</div>
        <form id="wechat-form" class="setting-group">
          <div class="field"><label for="wechat-name">公众号名称</label><input class="input" id="wechat-name" maxlength="120" value="${escapeHtml(wechat.accountName || '')}"></div>
          <div class="field"><label for="wechat-appid">AppID</label><input class="input" id="wechat-appid" maxlength="128" value="${escapeHtml(wechat.appId || '')}"></div>
          <div class="field"><label for="wechat-secret">AppSecret ${appSecretHint ? `（已保存 ${escapeHtml(appSecretHint)}）` : ''}</label><input class="input" type="password" id="wechat-secret" value="${escapeHtml(wechat.appSecret || '')}" placeholder="留空保持原值"></div>
          <div class="field"><label for="wechat-thumb">默认封面 Media ID（未上传本地封面时使用）</label><input class="input" id="wechat-thumb" maxlength="256" value="${escapeHtml(wechat.thumbMediaId || '')}"></div>
          <button class="btn btn-primary" type="submit">验证并保存</button>
        </form>
      </section>
    </div>`;
}

function renderLogRows() {
  const logs = state.logs || [];
  const levelClass = (level) => {
    if (level === 'ERROR') return 'log-row-error';
    if (level === 'WARNING' || level === 'WARN') return 'log-row-warn';
    if (level === 'INFO') return 'log-row-info';
    return 'log-row-debug';
  };
  const MAX_MSG_LEN = 120;
  if (!logs.length) return '<div class="empty">暂无日志记录</div>';
  return logs.map((log, i) => {
    const hasStack = log.stack && log.stack.length > 0;
    const fullMsg = log.message || '';
    const isLong = fullMsg.length > MAX_MSG_LEN || hasStack;
    const shortMsg = isLong ? fullMsg.substring(0, MAX_MSG_LEN) + '…' : fullMsg;
    return `<div class="log-row ${levelClass(log.level)}${isLong ? ' log-row-clickable' : ''}" data-log-index="${i}">
      <span class="log-time">${escapeHtml(log.timestamp.substring(11, 23))}</span>
      <span class="log-level log-level-${log.level.toLowerCase()}">${escapeHtml(log.level)}</span>
      <span class="log-module">${escapeHtml(log.module)}</span>
      ${log.task_id && log.task_id !== '-' ? `<span class="log-task">${escapeHtml(log.task_id.substring(0, 16))}</span>` : ''}
      <span class="log-msg">${escapeHtml(shortMsg)}</span>
      ${isLong ? '<span class="log-expand-hint">点击查看详情</span>' : ''}
    </div>`;
  }).join('');
}

function renderLogs() {
  const filter = state.logsFilter || { level: 'ALL', q: '' };
  return `
    <div class="page-head"><div><h2>系统日志中心</h2><p>全链路系统日志，支持级别过滤与关键字搜索。日志保存在内存中，服务重启后清空。点击长日志可查看完整内容。</p></div></div>
    <section class="card card-pad">
      <div class="log-toolbar">
        <select id="log-level-filter" class="input" style="width:auto">
          <option value="ALL" ${filter.level === 'ALL' ? 'selected' : ''}>全部级别</option>
          <option value="INFO" ${filter.level === 'INFO' ? 'selected' : ''}>INFO</option>
          <option value="WARN" ${filter.level === 'WARN' ? 'selected' : ''}>WARN</option>
          <option value="ERROR" ${filter.level === 'ERROR' ? 'selected' : ''}>ERROR</option>
        </select>
        <input id="log-search" class="input" style="flex:1" placeholder="搜索关键字…" value="${escapeHtml(filter.q || '')}">
        <label class="checkline" style="white-space:nowrap"><input type="checkbox" id="log-autorefresh" ${state.logsAutoRefresh ? 'checked' : ''}><span>自动刷新</span></label>
        <button class="btn btn-secondary" id="log-refresh">刷新</button>
        <button class="btn btn-ghost" id="log-export">导出</button>
      </div>
      <div class="log-viewer">${renderLogRows()}</div>
    </section>
    <div class="log-modal-overlay" id="log-modal">
      <div class="log-modal">
        <div class="log-modal-header">
          <span class="log-modal-title" id="log-modal-title">日志详情</span>
          <button class="log-modal-close" id="log-modal-close">&times;</button>
        </div>
        <div class="log-modal-meta" id="log-modal-meta"></div>
        <pre class="log-modal-body" id="log-modal-body"></pre>
        <div class="log-modal-footer">
          <button class="btn btn-ghost" id="log-modal-copy">复制内容</button>
        </div>
      </div>
    </div>`;
}

async function fetchLogs() {
  const filter = state.logsFilter || { level: 'ALL', q: '' };
  const params = new URLSearchParams();
  if (filter.level && filter.level !== 'ALL') params.set('level', filter.level);
  if (filter.q) params.set('q', filter.q);
  params.set('limit', '200');
  try {
    const data = await api(`/api/v2/logs?${params}`);
    state.logs = data.logs || [];
  } catch (error) {
    state.logs = [];
  }
}

function bindLogs() {
  const levelSelect = document.getElementById('log-level-filter');
  const searchInput = document.getElementById('log-search');
  const refreshBtn = document.getElementById('log-refresh');
  const autoRefresh = document.getElementById('log-autorefresh');
  const exportBtn = document.getElementById('log-export');
  let searchTimer = null;
  levelSelect?.addEventListener('change', () => {
    state.logsFilter.level = levelSelect.value;
    fetchLogs().then(() => render());
  });
  searchInput?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.logsFilter.q = searchInput.value.trim();
      fetchLogs().then(() => render());
    }, 350);
  });
  refreshBtn?.addEventListener('click', () => fetchLogs().then(() => render()));
  autoRefresh?.addEventListener('change', () => {
    state.logsAutoRefresh = autoRefresh.checked;
    setupLogPolling();
  });
  exportBtn?.addEventListener('click', () => {
    const logs = state.logs || [];
    const text = logs.map((l) => `[${l.timestamp}] [${l.level}] [${l.module}] [task:${l.task_id}] ${l.message}${l.stack ? '\n' + l.stack : ''}`).join('\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `studio-logs-${new Date().toISOString().substring(0, 19).replace(/[:T]/g, '-')}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  });
  // 日志详情弹窗
  const modal = document.getElementById('log-modal');
  const modalTitle = document.getElementById('log-modal-title');
  const modalMeta = document.getElementById('log-modal-meta');
  const modalBody = document.getElementById('log-modal-body');
  const modalClose = document.getElementById('log-modal-close');
  const modalCopy = document.getElementById('log-modal-copy');
  let currentLogText = '';

  function openLogModal(log) {
    modalTitle.textContent = `${log.level} · ${log.module}`;
    const metaParts = [
      `<span class="log-modal-meta-item"><strong>时间</strong> ${escapeHtml(log.timestamp)}</span>`,
      `<span class="log-modal-meta-item"><strong>级别</strong> ${escapeHtml(log.level)}</span>`,
      `<span class="log-modal-meta-item"><strong>模块</strong> ${escapeHtml(log.module)}</span>`,
    ];
    if (log.task_id && log.task_id !== '-') {
      metaParts.push(`<span class="log-modal-meta-item"><strong>任务</strong> ${escapeHtml(log.task_id)}</span>`);
    }
    modalMeta.innerHTML = metaParts.join('');
    let body = log.message || '';
    if (log.stack) {
      body += '\n\n--- 堆栈追踪 ---\n' + log.stack;
    }
    currentLogText = body;
    modalBody.textContent = body;
    modal.classList.add('open');
  }

  function closeLogModal() {
    modal.classList.remove('open');
    currentLogText = '';
  }

  modalClose?.addEventListener('click', closeLogModal);
  modal?.addEventListener('click', (e) => {
    if (e.target === modal) closeLogModal();
  });
  if (state._logKeydownHandler) {
    document.removeEventListener('keydown', state._logKeydownHandler);
  }
  state._logKeydownHandler = (e) => {
    if (e.key === 'Escape' && modal && modal.classList.contains('open')) closeLogModal();
  };
  document.addEventListener('keydown', state._logKeydownHandler);
  modalCopy?.addEventListener('click', () => {
    if (!currentLogText) return;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(currentLogText).then(() => {
        modalCopy.textContent = '已复制';
        setTimeout(() => { modalCopy.textContent = '复制内容'; }, 1500);
      });
    } else {
      const ta = document.createElement('textarea');
      ta.value = currentLogText;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      modalCopy.textContent = '已复制';
      setTimeout(() => { modalCopy.textContent = '复制内容'; }, 1500);
    }
  });
  // 点击日志行打开弹窗（使用事件委托避免重新渲染后监听器丢失）
  const viewer = document.querySelector('.log-viewer');
  viewer?.addEventListener('click', (e) => {
    const row = e.target.closest('.log-row-clickable');
    if (!row) return;
    const idx = parseInt(row.dataset.logIndex, 10);
    const log = (state.logs || [])[idx];
    if (log) openLogModal(log);
  });
  setupLogPolling();
}

function setupLogPolling() {
  clearTimeout(state.logsPollTimer);
  if (!state.logsAutoRefresh) return;
  const { path } = routeInfo();
  if (path !== 'logs') return;
  state.logsPollTimer = setTimeout(async () => {
    await fetchLogs();
    const { path: currentPath } = routeInfo();
    if (currentPath === 'logs') {
      // 只更新日志行内容，不重建整个页面
      // 这样弹窗状态（打开/关闭）得以保留，事件委托仍然有效
      const viewer = document.querySelector('.log-viewer');
      if (viewer) {
        viewer.innerHTML = renderLogRows();
      }
    }
    setupLogPolling();
  }, 5000);
}

function renderTasks() {
  const selected = state.currentTask;
  return `
    <div class="page-head"><div><h2>任务诊断</h2><p>任务显示文章标题、状态、重试范围与真实事件；跳过步骤不会标记为完成。</p></div></div>
    <div class="grid grid-2">
      <section class="card card-pad"><div class="article-list">${state.tasks.length ? state.tasks.map((task) => `<button class="source-card task-select" data-open-task="${task.id}"><strong>${escapeHtml(task.projectTitle || task.projectId)}</strong><div class="source-meta"><span>${escapeHtml(task.status)}</span><span>${formatTime(task.updatedAt)}</span><span>${task.progress}%</span></div><p class="helper">${escapeHtml(task.message)}</p></button>`).join('') : '<div class="empty">暂无任务</div>'}</div></section>
      <section class="card card-pad">${selected ? `<div class="stack"><div class="section-title"><div><h3>${escapeHtml(selected.projectTitle || '任务详情')}</h3><p>${escapeHtml(selected.id)}</p></div>${statusPill(selected.status)}</div>${selected.errorCode ? `<div class="alert error"><strong>${escapeHtml(selected.errorCode)}</strong><br>${escapeHtml(selected.errorDetail || selected.message)}</div>` : ''}${timelineHtml(selected)}<div>${(selected.events || []).map((event) => `<div class="task-event ${escapeHtml(event.level)}"><strong>${formatTime(event.createdAt)} · ${escapeHtml(event.step)}${event.detail?.skipped ? ' · 已跳过' : ''}</strong><p>${escapeHtml(event.message)}</p></div>`).join('')}</div><div class="top-actions">${['queued', 'running'].includes(selected.status) ? '<button class="btn btn-danger" id="diag-cancel">取消</button>' : ''}${['failed', 'blocked', 'timeout', 'cancelled'].includes(selected.status) ? '<select id="diag-retry-mode"><option value="review_only">仅审校</option><option value="preserve_body">保留正文</option><option value="from_outline">从框架重做</option><option value="full">全部重做</option></select><button class="btn btn-secondary" id="diag-retry">重试</button>' : ''}${selected.projectId ? '<button class="btn btn-primary" id="diag-open-project">打开文章</button>' : ''}</div></div>` : '<div class="empty">从左侧选择任务查看详情</div>'}</section>
    </div>`;
}

function render() {
  if (state.loading) {
    app.innerHTML = '<div class="loading-screen"><div><div class="spinner"></div>正在加载工作台…</div></div>';
    return;
  }
  if (state.fatal) {
    app.innerHTML = `<div class="loading-screen"><div class="card card-pad"><div class="alert error"><strong>无法启动工作台</strong><br>${escapeHtml(state.fatal)}</div><br><button class="btn btn-primary" id="fatal-retry">重新连接</button></div></div>`;
    document.getElementById('fatal-retry')?.addEventListener('click', bootstrap);
    return;
  }
  const { path } = routeInfo();
  let content;
  if (path === 'create') content = renderCreate();
  else if (path === 'articles') content = renderArticles();
  else if (path === 'workspace') content = renderWorkspace();
  else if (path === 'ai') content = renderAi();
  else if (path === 'settings') content = renderSettings();
  else if (path === 'logs') content = renderLogs();
  else if (path === 'tasks') content = renderTasks();
  else content = '<div class="card empty">页面不存在</div>';
  app.innerHTML = appShell(content, path);
  bindCommon();
  if (path === 'create') bindCreate();
  if (path === 'workspace') bindWorkspace();
  if (path === 'articles') bindArticles();
  if (path === 'ai') bindAi();
  if (path === 'settings') bindSettings();
  if (path === 'logs') bindLogs();
  if (path === 'tasks') bindTasks();
}

function bindCommon() {
  document.querySelectorAll('[data-nav]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.nav)));
  document.getElementById('mobile-menu')?.addEventListener('click', () => { state.mobileOpen = !state.mobileOpen; render(); });
  document.getElementById('mobile-overlay')?.addEventListener('click', () => { state.mobileOpen = false; render(); });
  document.getElementById('refresh-all')?.addEventListener('click', bootstrap);
}

function bindCreate() {
  document.getElementById('create-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = document.getElementById('source-input');
    const button = document.getElementById('create-button');
    const sourceInput = input.value.trim();
    if (!sourceInput) return;
    button.disabled = true;
    button.textContent = '正在创建…';
    try {
      const result = await api('/api/v2/workflows', {
        method: 'POST',
        body: { sourceInput, autoReview: document.getElementById('create-auto-review').checked },
      });
      state.projects.unshift(result.project);
      state.projectCounts.active = Number(state.projectCounts.active || 0) + 1;
      state.projectCounts.all = Number(state.projectCounts.all || 0) + 1;
      state.articleTotal = Number(state.articleTotal || 0) + 1;
      state.tasks.unshift(result.task);
      state.currentProject = result.project;
      state.currentTask = result.task;
      await navigate('workspace', { project: result.project.id, task: result.task.id });
    } catch (error) {
      toast(error.message, 'error');
      button.disabled = false;
      button.textContent = '开始创作 →';
    }
  });
}

function updateTaskProgressOnly() {
  if (!state.currentTask) return;
  const bar = document.querySelector('.progress > i');
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, state.currentTask.progress || 0))}%`;
  const badge = document.querySelector('.workspace-main .section-title .pill');
  if (badge) badge.outerHTML = statusPill(state.currentTask.status);
}

function scheduleProjectSave(projectId, field, value) {
  const pending = { ...(state.pendingSaves.get(projectId) || {}), [field]: value };
  state.pendingSaves.set(projectId, pending);
  state.dirtyProjects.add(projectId);
  state.saveState = 'saving';
  clearTimeout(state.saveTimers.get(projectId));
  state.saveTimers.set(projectId, setTimeout(() => flushProjectSave(projectId), 700));
  updateSaveBadge();
}

function updateSaveBadge() {
  const badge = document.getElementById('save-state');
  if (!badge) return;
  const labels = { idle: '已保存', saving: '保存中…', saved: '已保存', error: '保存失败' };
  badge.textContent = labels[state.saveState] || '已保存';
  badge.className = `pill ${state.saveState === 'saved' ? 'success' : state.saveState === 'error' ? 'danger' : state.saveState === 'saving' ? 'running' : ''}`;
}

async function flushProjectSave(projectId) {
  clearTimeout(state.saveTimers.get(projectId));
  state.saveTimers.delete(projectId);
  if (state.saveChains.has(projectId)) {
    await state.saveChains.get(projectId);
    return !state.conflict || state.conflict.projectId !== projectId;
  }
  if (!state.pendingSaves.has(projectId)) return true;

  const chain = (async () => {
    while (state.pendingSaves.has(projectId)) {
      const fields = state.pendingSaves.get(projectId);
      state.pendingSaves.delete(projectId);
      const project = state.currentProject?.id === projectId
        ? state.currentProject
        : state.projects.find((item) => item.id === projectId);
      if (!project) break;
      state.saveState = 'saving';
      updateSaveBadge();
      try {
        const updated = await api(`/api/v2/projects/${encodeURIComponent(projectId)}`, {
          method: 'PATCH',
          headers: { 'If-Match': String(project.revision) },
          body: fields,
        });
        const optimistic = state.pendingSaves.get(projectId) || {};
        setProjectInState({ ...updated, ...optimistic });
        state.saveState = 'saved';
        state.preview = null;
      } catch (error) {
        state.saveState = 'error';
        state.pendingSaves.set(projectId, { ...fields, ...(state.pendingSaves.get(projectId) || {}) });
        if (error.code === 'revision_conflict' && error.detail?.server) {
          const allPending = state.pendingSaves.get(projectId) || {};
          state.conflict = { server: error.detail.server, pendingFields: allPending, projectId };
          state.currentProject = { ...state.currentProject, ...allPending };
          render();
        } else {
          toast(error.message, 'error');
          updateSaveBadge();
        }
        return;
      }
    }
    if (!state.pendingSaves.has(projectId) && (!state.conflict || state.conflict.projectId !== projectId)) {
      state.dirtyProjects.delete(projectId);
      state.saveState = 'saved';
      updateSaveBadge();
      await refreshPreview(false);
    }
  })();
  state.saveChains.set(projectId, chain);
  try {
    await chain;
  } finally {
    state.saveChains.delete(projectId);
  }
  return !state.conflict || state.conflict.projectId !== projectId;
}

async function refreshPreview(shouldRender = true) {
  const project = state.currentProject;
  if (!project || project.deleted) return;
  try {
    state.preview = await api(`/api/v2/projects/${encodeURIComponent(project.id)}/preview`);
    if (shouldRender) render();
    else {
      const preview = document.getElementById('publish-preview');
      if (preview) preview.innerHTML = state.preview.html;
    }
  } catch (error) {
    if (shouldRender) toast(error.message, 'error');
  }
}

function bindWorkspace() {
  const boundProjectId=state.currentProject?.id;
  document.querySelectorAll('.autosave').forEach((element) => {
    element.addEventListener('input', () => {
      const field = element.dataset.field;
      scheduleProjectSave(boundProjectId,field,element.value);
    });
  });
  document.getElementById('review-approved')?.addEventListener('change', async (event) => {
    const approved = event.target.checked;
    event.target.disabled = true;
    try {
      const ok = await flushProjectSave(boundProjectId);
      if (!ok) throw new Error('请先处理编辑冲突');
      await refreshPreview(false);
      state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(boundProjectId)}/review`, {
        method: 'POST',
        body: {
          approved,
          revision: state.currentProject.revision,
          bodyFingerprint: state.preview.bodyFingerprint,
        },
      });
      setProjectInState(state.currentProject);
      await refreshPreview(false);
      toast(approved ? '当前 revision 已完成人工终审' : '已取消人工终审', 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
      event.target.checked = !approved;
      event.target.disabled = false;
    }
  });
  document.getElementById('refresh-source')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const ok = await flushProjectSave(boundProjectId);
      if (!ok) throw new Error('请先处理编辑冲突');
      const result = await api(`/api/v2/projects/${encodeURIComponent(boundProjectId)}/refresh-source`, {
        method: 'POST', headers: { 'If-Match': String(state.currentProject.revision) }, body: {},
      });
      state.currentProject = result.project;
      setProjectInState(result.project);
      state.preview = null;
      toast(result.changed ? '来源变化，标题、摘要、框架、正文、审校和发布状态均已失效' : '来源内容没有变化', result.changed ? '' : 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
      event.currentTarget.disabled = false;
    }
  });
  document.getElementById('open-task')?.addEventListener('click', () => navigate('tasks', { task: state.currentTask.id }));
  document.getElementById('task-cancel')?.addEventListener('click', () => taskAction('cancel'));
  document.getElementById('task-retry')?.addEventListener('click', () => taskAction('retry', document.getElementById('retry-mode')?.value));
  document.getElementById('publish-button')?.addEventListener('click', publishProject);
  document.getElementById('refresh-preview')?.addEventListener('click', async () => {
    const ok = await flushProjectSave(boundProjectId);
    if (ok) await refreshPreview(true);
  });
  document.getElementById('body-mode-edit')?.addEventListener('click', () => {
    if (state.bodyMode !== 'edit') { state.bodyMode = 'edit'; render(); }
  });
  document.getElementById('body-mode-preview')?.addEventListener('click', async () => {
    if (state.bodyMode === 'preview') return;
    state.bodyMode = 'preview';
    render();
    // 切到预览时自动刷新一次，确保内容最新
    const ok = await flushProjectSave(boundProjectId);
    if (ok) await refreshPreview(true);
  });
  document.getElementById('cover-file')?.addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 2_000_000) { toast('封面文件必须小于 2MB', 'error'); return; }
    const reader = new FileReader();
    reader.onload = () => scheduleProjectSave(boundProjectId, 'coverDataUrl', String(reader.result || ''));
    reader.onerror = () => toast('无法读取封面文件', 'error');
    reader.readAsDataURL(file);
  });
  document.getElementById('remove-cover')?.addEventListener('click', () => scheduleProjectSave(boundProjectId, 'coverDataUrl', ''));
  document.getElementById('show-versions')?.addEventListener('click', async () => {
    try {
      const ok = await flushProjectSave(boundProjectId);
      if (!ok) return;
      const result = await api(`/api/v2/projects/${encodeURIComponent(boundProjectId)}/versions`);
      state.versions = result.items || [];
      state.showVersions = true;
      render();
    } catch (error) { toast(error.message, 'error'); }
  });
  document.getElementById('close-versions')?.addEventListener('click', () => { state.showVersions = false; render(); });
  document.querySelectorAll('[data-restore-version]').forEach((button) => button.addEventListener('click', async () => {
    if (!confirm(`确认恢复 revision ${button.dataset.restoreVersion}？当前内容会先自动保存为历史版本。`)) return;
    try {
      state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(boundProjectId)}/versions/${button.dataset.restoreVersion}/restore`, { method: 'POST', body: {} });
      setProjectInState(state.currentProject);
      state.showVersions = false;
      state.preview = null;
      await refreshPreview(false);
      toast('历史版本已恢复，终审和发布状态已失效', 'success');
      render();
    } catch (error) { toast(error.message, 'error'); }
  }));
  bindConflict();
}

function conflictLocalFields() {
  return {
    title: document.getElementById('conflict-title')?.value || '',
    summary: document.getElementById('conflict-summary')?.value || '',
    bodyMarkdown: document.getElementById('conflict-body')?.value || '',
    ...(state.conflict?.pendingFields?.coverDataUrl !== undefined ? { coverDataUrl: state.conflict.pendingFields.coverDataUrl } : {}),
  };
}

function bindConflict() {
  if (!state.conflict) return;
  document.getElementById('conflict-use-server')?.addEventListener('click', () => {
    const { server, projectId } = state.conflict;
    state.pendingSaves.delete(projectId);
    state.dirtyProjects.delete(projectId);
    state.currentProject = server;
    setProjectInState(server);
    state.conflict = null;
    state.saveState = 'saved';
    state.preview = null;
    render();
  });
  document.getElementById('conflict-overwrite')?.addEventListener('click', async () => resolveConflict(false));
  document.getElementById('conflict-merge')?.addEventListener('click', async () => resolveConflict(true));
}

async function resolveConflict(mergeBody) {
  const conflict = state.conflict;
  if (!conflict) return;
  const local = conflictLocalFields();
  if (mergeBody && local.bodyMarkdown !== conflict.server.bodyMarkdown) {
    local.bodyMarkdown = `${conflict.server.bodyMarkdown}\n\n---\n\n${local.bodyMarkdown}`;
  }
  state.pendingSaves.delete(conflict.projectId);
  state.currentProject = conflict.server;
  setProjectInState(conflict.server);
  state.conflict = null;
  state.pendingSaves.set(conflict.projectId, local);
  state.dirtyProjects.add(conflict.projectId);
  const ok = await flushProjectSave(conflict.projectId);
  if (ok) {
    toast(mergeBody ? '冲突内容已合并并保存' : '本地字段已覆盖服务端', 'success');
    render();
  }
}

async function taskAction(action, retryMode = 'review_only') {
  if (!state.currentTask) return;
  try {
    if (action === 'retry' && state.currentProject?.id) {
      const ok = await flushProjectSave(state.currentProject.id);
      if (!ok) return;
    }
    const result = await api(`/api/v2/tasks/${encodeURIComponent(state.currentTask.id)}/${action}`, {
      method: 'POST', body: action === 'retry' ? { retryMode } : {},
    });
    if (action === 'retry') {
      state.currentTask = result.task;
      state.currentProject = result.project;
      setProjectInState(result.project);
      await navigate('workspace', { project: result.project.id, task: result.task.id });
    } else state.currentTask = result;
    toast(action === 'retry' ? '已按所选范围创建重试任务' : '已请求取消', 'success');
    render();
  } catch (error) { toast(error.message, 'error'); }
}

async function publishProject(event) {
  event.currentTarget.disabled = true;
  event.currentTarget.textContent = '正在同步冻结快照…';
  try {
    const projectId = state.currentProject.id;
    const ok = await flushProjectSave(projectId);
    if (!ok) throw new Error('请先处理编辑冲突');
    await refreshPreview(false);
    const result = await api(`/api/v2/projects/${encodeURIComponent(projectId)}/publish`, {
      method: 'POST',
      body: {
        revision: state.currentProject.revision,
        bodyFingerprint: state.preview.bodyFingerprint,
        previewHash: state.preview.previewHash,
      },
    });
    state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(projectId)}`);
    setProjectInState(state.currentProject);
    const message = result.status === 'current'
      ? `当前 revision 已同步：${result.remoteId}`
      : `revision ${result.revision} 已同步，但文章随后发生变化，当前文章未标记为已同步`;
    toast(message, result.status === 'current' ? 'success' : '');
    render();
  } catch (error) {
    toast(error.message, 'error');
    event.currentTarget.disabled = false;
    event.currentTarget.textContent = '同步当前快照到公众号草稿';
  }
}

async function reloadProjects({ resetPage = false } = {}) {
  if (resetPage) state.articlePage = 0;
  const params = new URLSearchParams({
    includeDeleted: state.showDeleted ? 'true' : 'false',
    deletedOnly: state.showDeleted ? 'true' : 'false',
    includeArchived: state.showDeleted || state.showArchived ? 'true' : 'false',
    q: state.search.trim(),
    limit: String(state.articlePageSize),
    offset: String(state.articlePage * state.articlePageSize),
  });
  const result = await api(`/api/v2/projects?${params}`);
  if (state.articlePage > 0 && !result.items?.length && result.total > 0) {
    state.articlePage = Math.max(0, Math.ceil(result.total / state.articlePageSize) - 1);
    return reloadProjects();
  }
  state.projects = result.items || [];
  state.articleTotal = Number(result.total || 0);
  state.projectCounts = {
    ...state.projectCounts,
    ...(state.showDeleted ? { deleted: state.articleTotal } : state.showArchived ? { all: state.articleTotal } : { active: state.articleTotal }),
  };
}

function bindArticles() {
  document.getElementById('article-search')?.addEventListener('input', (event) => {
    state.search = event.target.value;
    clearTimeout(state.articleSearchTimer);
    state.articleSearchTimer = setTimeout(async () => {
      try { await reloadProjects({ resetPage: true }); render(); document.getElementById('article-search')?.focus(); }
      catch (error) { toast(error.message, 'error'); }
    }, 300);
  });
  document.getElementById('show-archived')?.addEventListener('change', async (event) => {
    state.showArchived = event.target.checked;
    try { await reloadProjects({ resetPage: true }); render(); } catch (error) { toast(error.message, 'error'); }
  });
  document.getElementById('show-deleted')?.addEventListener('change', async (event) => {
    state.showDeleted = event.target.checked;
    try { await reloadProjects({ resetPage: true }); render(); } catch (error) { toast(error.message, 'error'); }
  });
  document.getElementById('article-prev')?.addEventListener('click', async () => {
    if (state.articlePage <= 0) return;
    state.articlePage -= 1;
    try { await reloadProjects(); render(); } catch (error) { toast(error.message, 'error'); }
  });
  document.getElementById('article-next')?.addEventListener('click', async () => {
    if ((state.articlePage + 1) * state.articlePageSize >= state.articleTotal) return;
    state.articlePage += 1;
    try { await reloadProjects(); render(); } catch (error) { toast(error.message, 'error'); }
  });
  document.querySelectorAll('[data-open-project]').forEach((button) => button.addEventListener('click', () => {
    const project = state.projects.find((item) => item.id === button.dataset.openProject);
    const task = state.tasks.find((item) => item.projectId === project.id);
    navigate('workspace', { project: project.id, task: task?.id || '' });
  }));
  document.querySelectorAll('[data-export-project]').forEach((button) => button.addEventListener('click', () => { location.href = `/api/v2/projects/${button.dataset.exportProject}/export`; }));
  document.querySelectorAll('[data-copy-project]').forEach((button) => button.addEventListener('click', async () => {
    try { await api(`/api/v2/projects/${button.dataset.copyProject}/copy`, { method: 'POST', body: {} }); await reloadProjects({ resetPage: true }); toast('文章已复制', 'success'); render(); } catch (error) { toast(error.message, 'error'); }
  }));
  document.querySelectorAll('[data-archive-project]').forEach((button) => button.addEventListener('click', async () => {
    const action = button.dataset.archived === 'true' ? 'restore' : 'archive';
    try { await api(`/api/v2/projects/${button.dataset.archiveProject}/${action}`, { method: 'POST', body: {} }); await reloadProjects(); toast(action === 'archive' ? '已归档' : '已恢复', 'success'); render(); } catch (error) { toast(error.message, 'error'); }
  }));
  document.querySelectorAll('[data-delete-project]').forEach((button) => button.addEventListener('click', async () => {
    if (!confirm('确认将这篇文章移入回收站？')) return;
    try { await api(`/api/v2/projects/${button.dataset.deleteProject}`, { method: 'DELETE' }); await reloadProjects(); toast('文章已移入回收站', 'success'); render(); } catch (error) { toast(error.message, 'error'); }
  }));
  document.querySelectorAll('[data-restore-deleted]').forEach((button) => button.addEventListener('click', async () => {
    try { await api(`/api/v2/projects/${button.dataset.restoreDeleted}/restore`, { method: 'POST', body: {} }); await reloadProjects({ resetPage: true }); toast('文章已从回收站恢复', 'success'); render(); } catch (error) { toast(error.message, 'error'); }
  }));
  document.querySelectorAll('[data-purge-project]').forEach((button) => button.addEventListener('click', async () => {
    if (!confirm('永久删除后无法恢复，确认继续？')) return;
    try { await api(`/api/v2/projects/${button.dataset.purgeProject}/purge`, { method: 'DELETE', body: {} }); await reloadProjects(); toast('文章已永久删除', 'success'); render(); } catch (error) { toast(error.message, 'error'); }
  }));
}

function aiFormValue() {
  return {
    providerId: 'openai-compatible',
    baseUrl: document.getElementById('ai-base-url').value.trim(),
    apiKey: document.getElementById('ai-key').value.trim(),
    model: document.getElementById('ai-model').value.trim(),
    temperature: Number(document.getElementById('ai-temp').value),
    autoReview: document.getElementById('ai-auto-review').checked,
  };
}

function syncAiDraftFromDom() {
  const form = document.getElementById('ai-form');
  if (!form) return null;
  state.aiDraft = aiFormValue();
  persistDraft('aiDraft', state.aiDraft);
  return state.aiDraft;
}

function bindAi() {
  document.querySelectorAll('#ai-form input').forEach((element) => {
    const eventName = element.type === 'checkbox' ? 'change' : 'input';
    element.addEventListener(eventName, syncAiDraftFromDom);
  });
  document.getElementById('ai-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const draft = syncAiDraftFromDom() || aiFormValue();
      state.settings = await api('/api/v2/settings', { method: 'PATCH', body: { ai: draft } });
      state.aiDraft = null;
      persistDraft('aiDraft', null);
      await refreshHealth();
      toast('AI 设置已保存', 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  document.getElementById('verify-ai')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const draft = syncAiDraftFromDom() || aiFormValue();
      const result = await api('/api/v2/settings/ai/verify', { method: 'POST', body: draft });
      await refreshHealth();
      toast(result.message, 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
      event.currentTarget.disabled = false;
    }
  });
}

function generalFormValue() {
  return {
    defaultLength: Number(document.getElementById('default-length').value),
    strictFacts: document.getElementById('strict-facts').checked,
    allowNetwork: document.getElementById('allow-network').checked,
  };
}

function syncGeneralDraftFromDom() {
  const form = document.getElementById('general-form');
  if (!form) return null;
  state.generalDraft = generalFormValue();
  persistDraft('generalDraft', state.generalDraft);
  return state.generalDraft;
}

function wechatFormValue() {
  return {
    accountName: document.getElementById('wechat-name').value.trim(),
    appId: document.getElementById('wechat-appid').value.trim(),
    appSecret: document.getElementById('wechat-secret').value.trim(),
    thumbMediaId: document.getElementById('wechat-thumb').value.trim(),
  };
}

function syncWechatDraftFromDom() {
  const form = document.getElementById('wechat-form');
  if (!form) return null;
  state.wechatDraft = wechatFormValue();
  persistDraft('wechatDraft', state.wechatDraft);
  return state.wechatDraft;
}

function bindSettings() {
  document.querySelectorAll('#general-form input').forEach((element) => {
    const eventName = element.type === 'checkbox' ? 'change' : 'input';
    element.addEventListener(eventName, syncGeneralDraftFromDom);
  });
  document.querySelectorAll('#wechat-form input').forEach((element) => {
    const eventName = element.type === 'checkbox' ? 'change' : 'input';
    element.addEventListener(eventName, syncWechatDraftFromDom);
  });
  document.getElementById('general-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const previous = JSON.parse(JSON.stringify(state.settings));
    try {
      const draft = syncGeneralDraftFromDom() || generalFormValue();
      state.settings = await api('/api/v2/settings', {
        method: 'PATCH',
        body: { general: draft },
      });
      state.generalDraft = null;
      persistDraft('generalDraft', null);
      toast('通用设置已保存', 'success');
      render();
    } catch (error) {
      state.settings = previous;
      toast(error.message, 'error');
      render();
    }
  });
  document.getElementById('wechat-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const draft = syncWechatDraftFromDom() || wechatFormValue();
      const result = await api('/api/v2/settings/wechat/verify-and-save', {
        method: 'POST',
        body: draft,
      });
      state.settings.wechat = result;
      state.wechatDraft = null;
      persistDraft('wechatDraft', null);
      await refreshHealth();
      toast('公众号凭证验证成功并已保存', 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
      button.disabled = false;
    }
  });
}

function bindTasks() {
  document.querySelectorAll('[data-open-task]').forEach((button) => button.addEventListener('click', () => navigate('tasks', { task: button.dataset.openTask })));
  document.getElementById('diag-cancel')?.addEventListener('click', () => taskAction('cancel'));
  document.getElementById('diag-retry')?.addEventListener('click', () => taskAction('retry', document.getElementById('diag-retry-mode')?.value));
  document.getElementById('diag-open-project')?.addEventListener('click', () => navigate('workspace', { project: state.currentTask.projectId, task: state.currentTask.id }));
}

window.addEventListener('hashchange', async () => { state.mobileOpen = false; await loadRouteData(); });
window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && state.mobileOpen) { state.mobileOpen = false; render(); }
});
window.addEventListener('beforeunload', (event) => {
  clearTimeout(state.pollTimer);
  if (state.dirtyProjects.size || state.saveChains.size || state.pendingSaves.size) {
    event.preventDefault();
    event.returnValue = '';
  }
});

loadStoredDrafts();
bootstrap();
