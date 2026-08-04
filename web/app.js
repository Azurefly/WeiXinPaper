const app = document.getElementById('app');
const toastRoot = document.getElementById('toast-root');
const DRAFT_STORAGE_KEY = 'studio-form-drafts';

const state = {
  loading: true,
  fatal: '',
  auth: { authenticated: false, mustChangePassword: false, username: '', csrfToken: '' },
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
  wsTab: 'write', // 'write' | 'review' | 'publish'
  wsLeftCollapsed: false,
  wsRightCollapsed: false,
  wsPreviewExpanded: false,    // P1: 公众号预览默认折叠
  wsTimelineExpanded: false,   // P4: 终态Timeline展开
  expandedSources: new Set(),  // P2: 展开的来源快照ID
  articleMenuId: null,         // P3: 打开下拉菜单的文章ID
  aiStatusExpanded: false,     // P5: AI状态详情展开
  taskEventsExpanded: false,   // P6: 任务事件全部展开
  wechatConfigExpanded: false, // P7: 微信配置展开
  // ---- 审计修复新增状态 ----
  sourcePreview: null,           // U3: 来源预览结果 {title, author, publisher, preview, contentHash, error}
  sourcePreviewLoading: false,   // U3: 来源预览加载中
  selectedArticleIds: new Set(), // U4: 文章批量选中的 ID
  publishStale: null,            // D1: 发布 stale 状态 {projectId, revision, remoteId}
  mergePreview: null,            // U2: 合并预览段落列表
  diffVersions: null,            // U1: 版本对比数据 {oldText, newText, oldLabel, newLabel}
  diffViewMode: 'side',          // U1: 版本对比视图 'side' | 'unified'
  aiBackupExpanded: false,       // A5: 备用模型配置展开
  logsRev: 0,                    // P1: 日志版本号，用于 preserve 判定
  sse: null,                     // P2: EventSource 实例
  reviewFilter: 'all',           // #048: 审校结果筛选 'all'|'passed'|'warning'|'failed'
  previewLoading: false,         // #039: 预览加载状态
  online: navigator.onLine,      // #076: 网络状态
  // ---- P2 批量修复新增状态 ----
  sseRetryCount: 0,              // #077: SSE 重连次数
  sseRetryTimer: null,           // #077: SSE 重连定时器
  previewDevice: 'desktop',      // #042: 预览设备 'desktop'|'mobile'
  splitPreview: false,           // #043: 分屏编辑+预览
  findReplaceOpen: false,        // #006: 查找替换面板
  findQuery: '',                 // #006: 查找内容
  replaceQuery: '',              // #006: 替换内容
  findMatchCount: 0,             // #006: 匹配数
  findMatchIndex: 0,             // #006: 当前匹配索引
  publishSuccess: null,          // #058: 发布成功引导 {remoteId, revision}
  preserveStaleWarned: false,    // #035: preserve 过时警告已显示
  // ---- 200点审查 P0/P1 修复新增状态 ----
  darkMode: false,               // #117: 暗色模式
  saveRetryCount: 0,             // #029: 保存重试次数
  saveRetryTimer: null,          // #029: 保存重试定时器
  sessionWarningShown: false,    // #091: 会话过期警告已显示
  sessionHeartbeatTimer: null,   // #091: 会话心跳定时器
  publishConfirmOpen: false,     // #065: 发布确认对话框
  publishLoading: false,         // #065: 发布加载状态
  locatedReviewIdx: null,        // #052: 定位的审校项索引
  // ---- 200点审查第二轮修复新增状态 ----
  sidebarPreviewTimer: null,     // #049: 侧栏预览更新防抖
  sensitiveWordsFound: [],       // #122: 检测到的敏感词
  mobileKeyboardOpen: false,     // #162: 移动端虚拟键盘状态
  domEventListeners: [],         // #109: DOM 事件监听器追踪
  // ---- 200点审查第三轮修复新增状态 ----
  sseFailNotified: false,         // #088: SSE失败已通知
  sseEventIds: new Set(),         // #034: SSE事件去重
};

// P1: 渲染批处理句柄（requestAnimationFrame）
let _pendingRender = null;

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

// #154 CSRF：读取双重提交令牌。优先使用登录后缓存的令牌，回退到 csrf_token Cookie。
function getCsrfToken() {
  if (state.auth && state.auth.csrfToken) return state.auth.csrfToken;
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : '';
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  let body = options.body;
  if (body && typeof body !== 'string' && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  // #154 CSRF：写操作需携带 X-CSRF-Token，与服务端 Cookie 中的 csrf_token 做双重提交校验
  const method = (options.method || 'GET').toUpperCase();
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    const csrfToken = getCsrfToken();
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  }
  // #185: AbortController 超时保护 — 默认 30s，AI 相关请求 120s
  const timeoutMs = options.timeout || (path.includes('/tasks/') || path.includes('/publish') ? 120000 : 30000);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, { ...options, body, headers, signal: controller.signal });
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      const failure = new Error(`请求超时（${timeoutMs / 1000}s）`);
      failure.code = 'timeout';
      throw failure;
    }
    const failure = new Error(`无法连接本地服务：${error.message}`);
    failure.code = 'network_error';
    throw failure;
  }
  clearTimeout(timeoutId);
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    // #184: 兼容多种错误响应格式 { error: { code, message } } | { detail: "..." } | { message: "..." } | 纯文本
    const problem = data?.error || (typeof data === 'object' && data ? { message: data.detail || data.message } : { message: typeof data === 'string' ? data : '' });
    // #082: 401 时先将编辑内容写入 localStorage，避免登录过期导致数据丢失
    if (response.status === 401 || problem.code === 'session_expired' || problem.code === 'unauthenticated') {
      _saveDraftToLocal();
      state.auth.authenticated = false;
      state.auth.mustChangePassword = false;
      state.auth.csrfToken = '';
      render();
    }
    const error = new Error(problem.message || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = problem.code || 'request_failed';
    error.detail = problem.detail;
    throw error;
  }
  return data;
}

// #078: Toast 管理 — 同类去重，最多 3 条
const _toastDedup = new Map();
function toast(message, type = '') {
  // 同类消息去重 — 2 秒内相同消息+类型不重复显示
  const key = `${type}:${message}`;
  const now = Date.now();
  if (_toastDedup.has(key) && now - _toastDedup.get(key) < 2000) return;
  _toastDedup.set(key, now);
  // 限制同时显示 3 条 toast
  const existing = toastRoot.querySelectorAll('.toast');
  if (existing.length >= 3) existing[0].remove();
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.textContent = message;
  // #096: 错误 toast 使用 role="alert"，其他使用 role="status"
  node.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toastRoot.append(node);
  setTimeout(() => { node.remove(); _toastDedup.delete(key); }, 4200);
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

function _scoreBadgeHtml(score) {
  if (typeof score !== 'number' || Number.isNaN(score)) return '';
  const cls = score >= 80 ? 'success' : score >= 60 ? 'warning' : 'danger';
  return `<span class="pill ${cls}" style="margin-left:8px" title="AI 辅助评分，仅供人工复核参考">AI 参考分 ${score}</span>`;
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
    // 先检查会话状态
    const sessionInfo = await api('/api/v2/auth/session');
    state.auth = {
      authenticated: !!sessionInfo.authenticated,
      mustChangePassword: !!sessionInfo.mustChangePassword,
      username: sessionInfo.username || '',
      csrfToken: sessionInfo.csrfToken || state.auth.csrfToken || '',
    };
    // #117: 初始化暗色模式（从 localStorage 读取）
    try {
      const darkMode = localStorage.getItem('studio-dark-mode');
      if (darkMode === '1') {
        state.darkMode = true;
        document.documentElement.setAttribute('data-theme', 'dark');
      }
    } catch { /* noop */ }
    // #009: 启动 localStorage 草稿定期同步
    _startLocalDraftSync();
    // #091: 启动会话心跳检测（每 5 分钟检查一次）
    _startSessionHeartbeat();
    if (!state.auth.authenticated) {
      state.loading = false;
      render();
      return;
    }
    if (state.auth.mustChangePassword) {
      state.loading = false;
      render();
      return;
    }
    const data = await api('/api/v2/bootstrap');
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
    if (state.auth.authenticated && !state.auth.mustChangePassword) {
      startPolling();
    }
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
  // P2: 有活跃任务时尝试启用 SSE 实时推送；连接失败会自动回退到下面的轮询
  if (active && !state.sse) _startSSE();
  state.pollTimer = setTimeout(async () => {
    try {
      const data = await api('/api/v2/tasks?limit=100');
      state.tasks = data.items || [];
      const { path, params } = routeInfo();
      let shouldRender = false;
      if (path === 'workspace') {
        const projectId = params.get('project');
        const taskId = params.get('task');
        let refreshWorkspace = false;
        if (taskId) {
          const wasActive = ['queued', 'running'].includes(state.currentTask?.status);
          state.currentTask = await api(`/api/v2/tasks/${encodeURIComponent(taskId)}`);
          refreshWorkspace = wasActive || ['queued', 'running'].includes(state.currentTask?.status);
        }
        if (refreshWorkspace && projectId && !state.dirtyProjects.has(projectId) && !state.saveChains.has(projectId)) {
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
      // P1: 使用 rAF 批处理渲染，合并同一帧内的多次更新；任务进度仍走轻量更新
      if (shouldRender) _scheduleRender();
      else updateTaskProgressOnly();
    } catch (error) {
      console.warn(error);
    }
    startPolling();
  }, active ? 1800 : 60000);
}

async function loadRouteData() {
  const { path, params } = routeInfo();
  const loadingBar = document.getElementById('route-loading');
  if (loadingBar) { loadingBar.classList.remove('done'); loadingBar.classList.add('active'); }
  try {
    if (path === 'workspace') {
      const projectId = params.get('project');
      const taskId = params.get('task');
      state.currentProject = projectId ? await api(`/api/v2/projects/${encodeURIComponent(projectId)}`) : null;
      state.currentTask = taskId ? await api(`/api/v2/tasks/${encodeURIComponent(taskId)}`) : null;
      state.conflict = null;
      state.mergePreview = null;     // U2
      state.diffVersions = null;     // U1
      state.publishStale = null;     // D1
      state.versions = [];
      state.showVersions = false;
      await refreshPreview(false);
      // #009: 检查 localStorage 中是否有未保存的本地草稿
      if (projectId && state.currentProject) {
        const draft = _loadDraftFromLocal(projectId);
        if (draft && draft.savedAt && draft.revision === state.currentProject.revision) {
          // 草稿 revision 与当前一致，检查内容是否不同
          if (draft.bodyMarkdown && draft.bodyMarkdown !== state.currentProject.bodyMarkdown) {
            toast('检测到本地草稿，内容已恢复', 'warning');
            state.currentProject = { ...state.currentProject, bodyMarkdown: draft.bodyMarkdown };
            if (draft.title) state.currentProject.title = draft.title;
            if (draft.summary) state.currentProject.summary = draft.summary;
          }
        } else if (draft && draft.savedAt > Date.now() - 3600000) {
          // 1小时内的草稿，revision 不匹配但仍提示
          toast('检测到旧版本草稿（可能已过时），未自动恢复', '');
        }
      }
    } else if (path === 'articles') {
      await reloadProjects();
    } else if (path === 'tasks') {
      const taskId = params.get('task');
      state.currentTask = taskId ? await api(`/api/v2/tasks/${encodeURIComponent(taskId)}`) : null;
    }
  } catch (error) {
    toast(error.message, 'error');
  }
  if (loadingBar) {
    loadingBar.classList.add('done');
    setTimeout(() => { loadingBar.classList.remove('active', 'done'); }, 500);
  }
  render();
}

function appShell(content, activeRoute) {
  const current = ROUTES[activeRoute] || { title: activeRoute === 'workspace' ? '文章工作区' : '任务诊断' };
  const health = state.health;
  // #091: 会话过期警告横幅
  const sessionWarning = state.sessionWarningShown ? `
    <div class="session-warning-banner">
      <span>会话即将过期，请保存当前内容</span>
      <button id="session-renew">续期会话</button>
    </div>` : '';
  return `
    ${sessionWarning}
    <div class="app-shell" ${state.darkMode ? 'data-theme="dark"' : ''}>
      <aside class="sidebar ${state.mobileOpen ? 'open' : ''}" aria-label="主导航">
        <div class="brand"><div class="brand-mark">✦</div><div class="brand-text"><strong>公众号 AI Studio</strong><span>AI 原生内容工作台</span></div></div>
        <nav class="nav">
          ${Object.entries(ROUTES).map(([key, item]) => `<button data-nav="${key}" class="${activeRoute === key ? 'active' : ''}" aria-label="${item.label}"><span class="nav-icon">${item.icon}</span><span class="nav-label">${item.label}</span></button>`).join('')}
        </nav>
        <div class="sidebar-foot">
          <div class="sidebar-user">
            <span class="sidebar-user-icon">👤</span>
            <span class="sidebar-user-name">${escapeHtml(state.auth.username || 'admin')}</span>
          </div>
          <button class="sidebar-logout" id="sidebar-logout" title="退出登录">退出登录</button>
          <div class="sidebar-version">2.1.3 本地安全版<br>本地 SQLite · 回环安全模式</div>
        </div>
      </aside>
      ${state.mobileOpen ? '<button class="mobile-overlay" id="mobile-overlay" aria-label="关闭菜单"></button>' : ''}
      <main class="main">
        <header class="topbar">
          <div class="top-actions"><button class="icon-btn mobile-menu" id="mobile-menu" aria-label="打开菜单">☰</button><h1>${escapeHtml(current.title)}</h1></div>
          <div class="top-actions">
            <span class="pill ${health?.ok ? 'success' : 'danger'} desktop-only">${health?.ok ? '● 服务正常' : '● 服务异常'}</span>
            <button class="theme-toggle" id="theme-toggle" title="${state.darkMode ? '切换到亮色模式' : '切换到暗色模式'}" aria-label="切换主题">${state.darkMode ? '☀' : '☾'}</button>
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
      <p>输入网页、GitHub 地址或自然语言主题。强制引用模式下，没有可核验来源的主题任务会暂停，不会伪造证据继续生成。</p>
      <form id="create-form">
        <div class="create-box">
          <div class="field">
            <label for="source-input">来源或创作目标</label>
            <input id="source-input" class="input create-input" maxlength="4000" autocomplete="off" placeholder="例如：https://github.com/... 或 写一篇关于 Spring Boot 新版本的公众号文章" required />
          </div>
          <div class="top-actions" style="gap:8px">
            <button class="btn btn-secondary" type="button" id="preview-source-btn">预览来源</button>
            <button class="btn btn-primary" type="submit" id="create-button">开始创作 →</button>
          </div>
        </div>
        <div class="field create-requirements-field">
          <label for="requirements-input">文章生成要求<span class="helper">（可选，不填则由 AI 自主发挥）</span></label>
          <textarea id="requirements-input" class="input create-requirements" maxlength="2000" rows="2" placeholder="例如：风格正式、约 800 字、重点突出新特性、面向开发者读者…"></textarea>
        </div>
        ${state.sourcePreviewLoading ? '<div class="alert info" style="margin-top:14px">正在获取来源预览…</div>' : ''}
        ${state.sourcePreview ? `
        <div class="alert ${state.sourcePreview.error ? 'error' : 'info'}" style="margin-top:14px">
          ${state.sourcePreview.error
            ? `<strong>来源预览失败</strong><br>${escapeHtml(state.sourcePreview.error)}<br><span class="helper">你仍可直接提交工作流继续创作。</span>`
            : `<strong>来源预览</strong>确认无误后再提交工作流。<div class="source-meta" style="margin-top:6px"><span>${escapeHtml(state.sourcePreview.title || '无标题')}</span><span>${escapeHtml(state.sourcePreview.publisher || '未知发布方')}</span><span>${escapeHtml(state.sourcePreview.author || '未知作者')}</span></div>${state.sourcePreview.preview ? `<p class="helper" style="margin-top:6px">${escapeHtml(state.sourcePreview.preview)}</p>` : ''}${state.sourcePreview.contentHash ? `<p class="helper" style="margin-top:4px">SHA-256 ${escapeHtml(String(state.sourcePreview.contentHash).slice(0, 16))}…</p>` : ''}`}
        </div>` : ''}
      </form>
      <details style="margin-top:16px">
        <summary class="helper">高级设置</summary>
        <label class="checkline" style="margin-top:12px"><input type="checkbox" id="create-auto-review" ${state.settings.ai?.autoReview !== false ? 'checked' : ''}><span><strong>生成后自动审校</strong><br><span class="helper">关闭后时间线会明确显示“已跳过”，不会伪装成已执行。</span></span></label>
      </details>
    </section>
    <details class="create-stats-collapse" style="margin-top:16px">
      <summary class="helper">工作台概览</summary>
      <div class="grid grid-3 stats" style="margin-top:12px">
        <div class="card stat"><strong>${state.projectCounts.active ?? state.projects.filter((p) => !p.archived && !p.deleted).length}</strong><span>当前文章</span></div>
        <div class="card stat"><strong>${activeCount}</strong><span>正在执行</span></div>
        <div class="card stat"><strong>${finishedCount}</strong><span>成功任务</span></div>
      </div>
    </details>`;
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

function miniTimelineHtml(task) {
  if (!task) return '';
  const steps = [
    ['source', '来源'], ['research', '目标'], ['outline', '框架'],
    ['draft', '正文'], ['cover', '封面'], ['review', '审校'],
  ];
  const order = ['queued', 'source', 'research', 'outline', 'draft', 'cover', 'review', 'completed'];
  const currentIndex = order.indexOf(task.currentStep || 'queued');
  const events = task.events || [];
  const skipped = new Set(events.filter((event) => event.detail?.skipped).map((event) => event.step));
  const executed = new Set(events.filter((event) => !event.detail?.skipped).map((event) => event.step));
  return `<div class="mini-timeline">${steps.map(([key, label]) => {
    const index = order.indexOf(key);
    const wasSkipped = skipped.has(key);
    const done = !wasSkipped && (executed.has(key) || task.status === 'succeeded' || (index < currentIndex && ['queued', 'running'].includes(task.status)));
    const active = key === task.currentStep && ['queued', 'running'].includes(task.status);
    const error = key === task.currentStep && ['failed', 'timeout', 'blocked', 'cancelled'].includes(task.status);
    const dotCls = wasSkipped ? 'skipped' : done ? 'done' : active ? 'active' : error ? 'error' : 'pending';
    const symbol = wasSkipped ? '–' : done ? '✓' : active ? '•' : error ? '!' : '';
    const connCls = done ? 'done' : '';
    return `<div class="mini-step"><div class="mini-dot ${dotCls}">${symbol}</div><span>${label}</span></div><div class="mini-connector ${connCls}"></div>`;
  }).join('').replace(/<div class="mini-connector[^"]*"><\/div>$/, '')}</div>`;
}

function conflictField(label, id, value, textarea = false) {
  const tag = textarea
    ? `<textarea id="${id}" style="min-height:${id.includes('body') ? '240px' : '82px'}">${escapeHtml(value || '')}</textarea>`
    : `<input class="input" id="${id}" value="${escapeHtml(value || '')}">`;
  return `<div class="field"><label for="${id}">${label}</label>${tag}</div>`;
}

const FIELD_LABELS = { title: '标题', summary: '摘要', bodyMarkdown: '正文', coverDataUrl: '封面' };

function conflictHtml() {
  if (!state.conflict) return '';
  const { server, pendingFields } = state.conflict;
  const pendingLabels = Object.keys(pendingFields).map((k) => FIELD_LABELS[k] || k).join('、') || '正文';
  const hasCover = (pendingFields.coverDataUrl !== undefined ? pendingFields.coverDataUrl : server.coverDataUrl) || '';
  return `<section class="card card-pad conflict">
    <div class="section-title"><div><h3>检测到多字段编辑冲突</h3><p>标题、摘要、正文和封面都保留在本地，不会被静默丢弃。</p></div>${statusPill('warning')}</div>
    <div class="grid grid-2">
      <div class="source-card"><strong>服务端 revision ${server.revision}</strong><p class="helper">${escapeHtml(server.title)}</p><p class="helper">${escapeHtml(server.summary)}</p></div>
      <div class="source-card"><strong>本地待保存字段</strong><p class="helper">${escapeHtml(pendingLabels)}</p></div>
    </div>
    <div class="form-grid" style="margin-top:14px">
      ${conflictField('本地标题', 'conflict-title', pendingFields.title ?? server.title)}
      ${conflictField('本地摘要', 'conflict-summary', pendingFields.summary ?? server.summary, true)}
      <div class="wide">${conflictField('本地正文', 'conflict-body', pendingFields.bodyMarkdown ?? server.bodyMarkdown, true)}</div>
    </div>
    ${hasCover ? `<div class="form-grid" style="margin-top:14px"><div class="wide"><label class="field"><span class="helper">本地封面（已保留，将随覆盖/合并写入）</span></label><img class="cover-preview" src="${escapeHtml(hasCover)}" alt="本地封面" style="max-width:200px;border-radius:10px"></div></div>` : ''}
    ${state.mergePreview ? _mergePreviewHtml() : ''}
    <div class="alert warning" style="margin-top:14px">合并会按段落对比服务端与本地内容；请逐段勾选需要保留的内容。封面保留本地版本。</div>
    <div class="top-actions" style="margin-top:14px">
      <button class="btn btn-ghost" id="conflict-use-server">采用服务端</button>
      <button class="btn btn-secondary" id="conflict-merge">${state.mergePreview ? '重新生成合并预览' : '合并正文并保留本地信息'}</button>
      <button class="btn btn-primary" id="conflict-overwrite">用本地字段覆盖</button>
    </div>
  </section>`;
}

// U2: 段落级三方合并预览 HTML
function _mergePreviewHtml() {
  const segs = state.mergePreview || [];
  if (!segs.length) return '';
  // #114: 合并预览冲突标记说明
  const legend = `<div class="merge-legend" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;padding:10px 14px;background:var(--surface-2);border-radius:10px;font-size:13px">
    <span><span class="pill" style="margin-right:4px">相同段落</span>两端一致，默认保留</span>
    <span><span class="pill warning" style="margin-right:4px">仅服务端</span>服务端有而本地没有</span>
    <span><span class="pill success" style="margin-right:4px">仅本地</span>本地有而服务端没有</span>
    <span><span class="pill danger" style="margin-right:4px">两端不同</span>同一段落内容不同，需选择保留方式</span>
  </div>`;
  const row = segs.map((seg, i) => {
    const short = (t) => escapeHtml((t || '').replace(/\n/g, ' ').slice(0, 100)) + ((t || '').length > 100 ? '…' : '');
    if (seg.type === 'same') {
      return `<div class="merge-seg" style="border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px"><label class="checkline"><input type="checkbox" data-merge-keep="${i}" ${seg.keep ? 'checked' : ''}><span><span class="pill" style="margin-right:6px">相同段落</span>${short(seg.serverText)}</span></label></div>`;
    }
    if (seg.type === 'server') {
      return `<div class="merge-seg" style="border:1px solid #efd9a9;border-radius:8px;padding:10px;margin-bottom:8px;background:#fff9ed"><label class="checkline"><input type="checkbox" data-merge-keep="${i}" ${seg.keep ? 'checked' : ''}><span><span class="pill warning" style="margin-right:6px">仅服务端</span>${short(seg.serverText)}</span></label></div>`;
    }
    if (seg.type === 'local') {
      return `<div class="merge-seg" style="border:1px solid #c9e4e2;border-radius:8px;padding:10px;margin-bottom:8px;background:#edf7f7"><label class="checkline"><input type="checkbox" data-merge-keep="${i}" ${seg.keep ? 'checked' : ''}><span><span class="pill success" style="margin-right:6px">仅本地</span>${short(seg.localText)}</span></label></div>`;
    }
    // conflict：两端都有但不同
    return `<div class="merge-seg" style="border:1px solid #efc7ca;border-radius:8px;padding:10px;margin-bottom:8px;background:#fff4f4">
      <div style="margin-bottom:6px;font-size:13px"><span class="pill danger" style="margin-right:6px">两端不同</span>
        <label style="margin-right:10px"><input type="radio" name="merge-choice-${i}" value="server" data-merge-choice="${i}" ${seg.choice === 'server' ? 'checked' : ''}>仅服务端</label>
        <label style="margin-right:10px"><input type="radio" name="merge-choice-${i}" value="local" data-merge-choice="${i}" ${seg.choice === 'local' ? 'checked' : ''}>仅本地</label>
        <label><input type="radio" name="merge-choice-${i}" value="both" data-merge-choice="${i}" ${seg.choice === 'both' ? 'checked' : ''}>两者都保留</label>
      </div>
      <div class="grid grid-2">
        <div><strong style="font-size:12px;color:var(--muted)">服务端</strong><pre style="white-space:pre-wrap;background:var(--surface-2);padding:8px;border-radius:6px;max-height:140px;overflow:auto;margin:4px 0 0;font-size:13px">${escapeHtml(seg.serverText)}</pre></div>
        <div><strong style="font-size:12px;color:var(--muted)">本地</strong><pre style="white-space:pre-wrap;background:var(--surface-2);padding:8px;border-radius:6px;max-height:140px;overflow:auto;margin:4px 0 0;font-size:13px">${escapeHtml(seg.localText)}</pre></div>
      </div>
    </div>`;
  }).join('');
  return `<div class="alert info" style="margin-top:14px"><strong>正文合并预览</strong><br>已按段落（双换行分隔）对比服务端与本地正文，相同段落、仅服务端、仅本地、两端不同的段落已分别标记，请勾选需要保留的段落。</div>
  ${legend}
  <div class="merge-preview" style="margin-top:12px">${row}</div>
  <div class="top-actions" style="margin-top:12px">
    <button class="btn btn-primary" id="merge-confirm">确认合并并保存</button>
    <button class="btn btn-ghost" id="merge-cancel">取消合并</button>
  </div>`;
}

function versionsHtml() {
  if (!state.showVersions) return '';
  return `<section class="card card-pad">
    <div class="section-title"><div><h3>版本历史</h3><p>恢复前会自动保存当前版本，审校与发布状态会重新失效。</p></div><button class="icon-btn" id="close-versions" aria-label="关闭版本历史">×</button></div>
    <div class="article-list">${state.versions.length ? state.versions.map((item) => `
      <div class="source-card"><strong>revision ${item.revision} · ${escapeHtml(item.reason)}</strong><div class="source-meta"><span>${formatTime(item.createdAt)}</span><span>${escapeHtml(item.snapshot?.title || '')}</span></div><div class="top-actions" style="margin-top:10px"><button class="btn btn-secondary" data-diff-version="${item.revision}">对比当前</button><button class="btn btn-ghost" data-restore-version="${item.revision}">恢复此版本</button></div></div>`).join('') : '<div class="empty">暂无历史版本</div>'}</div>
  </section>`;
}

// #047: 预览 HTML XSS 二次清洗 — 前端防御性清洗，移除危险元素和属性
function _sanitizePreviewHtml(html) {
  if (!html) return '';
  try {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    // 移除 script / style / iframe / object / embed / link / meta 标签
    doc.querySelectorAll('script, style, iframe, object, embed, link, meta').forEach((el) => el.remove());
    // #157: 移除 SVG 中的 foreignObject（可嵌入 HTML+JS）
    doc.querySelectorAll('foreignObject').forEach((el) => el.remove());
    // 移除所有 on* 事件属性
    doc.querySelectorAll('*').forEach((el) => {
      Array.from(el.attributes).forEach((attr) => {
        const name = attr.name.toLowerCase();
        const val = attr.value.trim().toLowerCase();
        if (name.startsWith('on')) el.removeAttribute(attr.name);
        // #157: 移除 javascript:/vbscript:/data:text-html 协议的 href/src
        if ((name === 'href' || name === 'src' || name === 'xlink:href') &&
            (val.startsWith('javascript:') || val.startsWith('vbscript:') || val.startsWith('data:text/html'))) {
          el.removeAttribute(attr.name);
        }
        // #157: 移除 data: 协议的非图片资源
        if (name === 'src' && val.startsWith('data:') && !val.startsWith('data:image/')) {
          el.removeAttribute(attr.name);
        }
      });
    });
    return doc.body.innerHTML;
  } catch {
    return html; // 解析失败时返回原始 HTML（后端已清洗）
  }
}

// #009/#082: 将当前编辑内容保存到 localStorage 作为草稿备份
function _saveDraftToLocal() {
  try {
    const project = state.currentProject;
    if (!project) return;
    const draft = {
      projectId: project.id,
      title: project.title,
      summary: project.summary,
      bodyMarkdown: project.bodyMarkdown,
      revision: project.revision,
      savedAt: Date.now(),
    };
    localStorage.setItem(`studio-draft-${project.id}`, JSON.stringify(draft));
  } catch { /* localStorage 不可用时静默 */ }
}

// #009: 从 localStorage 恢复草稿
function _loadDraftFromLocal(projectId) {
  try {
    const raw = localStorage.getItem(`studio-draft-${projectId}`);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch { return null; }
}

// #009: 清除 localStorage 草稿
function _clearDraftFromLocal(projectId) {
  try { localStorage.removeItem(`studio-draft-${projectId}`); } catch { /* noop */ }
}

// #009: 定期保存正文到 localStorage（每 5 秒）
let _localDraftTimer = null;
function _startLocalDraftSync() {
  if (_localDraftTimer) clearInterval(_localDraftTimer);
  _localDraftTimer = setInterval(() => {
    if (state.currentProject && state.dirtyProjects.has(state.currentProject.id)) {
      _saveDraftToLocal();
    }
  }, 5000);
}

// #083: 带重试的 API 调用包装器（用于关键操作）
async function apiWithRetry(path, options = {}, maxRetries = 3) {
  let lastError;
  for (let i = 0; i <= maxRetries; i++) {
    try {
      return await api(path, options);
    } catch (error) {
      lastError = error;
      if (error.code === 'revision_conflict' || error.status === 401 || error.status === 403) throw error;
      if (i < maxRetries) {
        const delay = Math.min(1000 * Math.pow(2, i), 8000);
        await new Promise(r => setTimeout(r, delay));
      }
    }
  }
  throw lastError;
}

// #091: 会话心跳检测 — 每 5 分钟检查会话状态
function _startSessionHeartbeat() {
  if (state.sessionHeartbeatTimer) clearInterval(state.sessionHeartbeatTimer);
  state.sessionHeartbeatTimer = setInterval(async () => {
    if (!state.auth.authenticated) return;
    try {
      const info = await api('/api/v2/auth/session');
      if (!info.authenticated) {
        _saveDraftToLocal();
        state.auth.authenticated = false;
        toast('会话已过期，编辑内容已保存到本地', 'error');
        render();
      } else if (info.expiresIn && info.expiresIn < 300) {
        // 会话将在 5 分钟内过期
        if (!state.sessionWarningShown) {
          state.sessionWarningShown = true;
          render();
        }
      }
    } catch {
      // 心跳失败不强制登出，可能是网络波动
    }
  }, 300000); // 5 分钟
}

// #093: 焦点陷阱 — 用于模态对话框
function _trapFocus(modal) {
  if (!modal) return;
  const focusable = modal.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])');
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  first.focus();
  modal.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus(); }
    } else {
      if (document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
}

// #071/#156: 封面文件验证 — 检查 MIME 类型和扩展名
function _validateCoverFile(file) {
  const allowedMimes = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
  const allowedExts = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];
  const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
  if (!allowedMimes.includes(file.type)) return { ok: false, reason: '文件类型不支持，仅支持 PNG/JPEG/WEBP/GIF' };
  if (!allowedExts.includes(ext)) return { ok: false, reason: '文件扩展名不支持' };
  if (file.size > 2 * 1024 * 1024) return { ok: false, reason: '文件大小超过 2MB 限制' };
  // #156: 拒绝 SVG（可能包含恶意脚本）
  if (file.type === 'image/svg+xml' || ext === '.svg') return { ok: false, reason: 'SVG 格式不允许' };
  return { ok: true };
}

// #002/#003: 字数统计辅助函数
function _wordCount(text) {
  if (!text) return { chars: 0, words: 0 };
  const chars = text.length;
  const chinese = text.match(/[\u4e00-\u9fff]/g)?.length || 0;
  const english = text.match(/[a-zA-Z]+/g)?.length || 0;
  return { chars, words: chinese + english };
}

// #015: 阅读时长估算
function _readingTime(words) {
  const minutes = Math.ceil(words / 300);
  return minutes <= 1 ? '约 1 分钟' : `约 ${minutes} 分钟`;
}

// #023: 输入框字数计数 HTML
function _charCounter(value, max) {
  const len = (value || '').length;
  const cls = len > max * 0.9 ? 'warning' : '';
  return `<span class="char-counter ${cls}">${len}/${max}</span>`;
}

// #042 (P1): 客户端 Markdown 渲染 — 离线预览回退方案
function _renderMarkdownClientSide(md) {
  if (!md) return '<p style="color:var(--muted)">暂无内容</p>';
  let html = escapeHtml(md);
  // 标题
  html = html.replace(/^######\s+(.+)$/gm, '<h6>$1</h6>');
  html = html.replace(/^#####\s+(.+)$/gm, '<h5>$1</h5>');
  html = html.replace(/^####\s+(.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');
  // 分隔线
  html = html.replace(/^---+\s*$/gm, '<hr>');
  html = html.replace(/^\*\*\*\s*$/gm, '<hr>');
  // 引用块
  html = html.replace(/^&gt;\s+(.+)$/gm, '<blockquote>$1</blockquote>');
  // 代码块
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // 图片
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img alt="$1" src="$2">');
  // 链接
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // 加粗
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  // 斜体
  html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
  // 删除线
  html = html.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  // 无序列表
  const lines = html.split('\n');
  const result = [];
  let inUl = false, inOl = false;
  for (const line of lines) {
    const ulMatch = line.match(/^\s*[-*+]\s+(.+)/);
    const olMatch = line.match(/^\s*(\d+)\.\s+(.+)/);
    if (ulMatch) {
      if (!inUl) { result.push('<ul>'); inUl = true; }
      if (inOl) { result.push('</ol>'); inOl = false; }
      result.push(`<li>${ulMatch[1]}</li>`);
    } else if (olMatch) {
      if (!inOl) { result.push('<ol>'); inOl = true; }
      if (inUl) { result.push('</ul>'); inUl = false; }
      result.push(`<li>${olMatch[2]}</li>`);
    } else {
      if (inUl) { result.push('</ul>'); inUl = false; }
      if (inOl) { result.push('</ol>'); inOl = false; }
      if (line.trim()) result.push(`<p>${line}</p>`);
    }
  }
  if (inUl) result.push('</ul>');
  if (inOl) result.push('</ol>');
  return _sanitizePreviewHtml(result.join('\n'));
}

// #122 (P1): 敏感词检测 — 检查正文中是否包含常见敏感词
const _SENSITIVE_WORDS = [
  '习近平', '毛泽东', '邓小平', '江泽民', '胡锦涛', '温家宝', '李克强', '李强',
  '六四', '天安门', '法轮功', '达赖', '藏独', '疆独', '台独', '港独',
  '暴恐', '恐怖袭击', '炸弹制作', '杀人方法', '自杀方法',
  '色情', '裸聊', '赌博网站', '毒品交易', '买卖枪支',
  '反动', '颠覆', '煽动', '分裂国家',
  '传销', '非法集资', '电信诈骗', '洗钱',
];
function _detectSensitiveWords(text) {
  if (!text) return [];
  const found = [];
  const lowerText = text.toLowerCase();
  for (const word of _SENSITIVE_WORDS) {
    if (lowerText.includes(word.toLowerCase())) {
      found.push(word);
    }
  }
  return found;
}

// #085 (P1): 冲突解决后数据完整性校验
function _validateConflictResolution(serverData, mergedFields) {
  const issues = [];
  // 检查必填字段是否存在
  if (!serverData && !mergedFields) {
    issues.push('服务端数据和合并字段均为空');
    return { valid: false, issues };
  }
  const data = { ...(serverData || {}), ...(mergedFields || {}) };
  // 检查标题非空
  if (data.title !== undefined && (!data.title || data.title.trim().length === 0)) {
    issues.push('标题为空');
  }
  // 检查标题长度
  if (data.title && data.title.length > 120) {
    issues.push('标题超过 120 字符');
  }
  // 检查摘要长度
  if (data.summary && data.summary.length > 300) {
    issues.push('摘要超过 300 字符');
  }
  // 检查正文字符限制
  if (data.bodyMarkdown && data.bodyMarkdown.length > 500000) {
    issues.push('正文超过 500000 字符');
  }
  // 检查 revision 一致性
  if (serverData?.revision !== undefined && mergedFields?.revision !== undefined &&
      serverData.revision !== mergedFields.revision) {
    issues.push(`revision 不一致: 服务端 ${serverData.revision} vs 合并 ${mergedFields.revision}`);
  }
  return { valid: issues.length === 0, issues };
}

// #109 (P1): 安全添加事件监听器 — 追踪以便后续清理
function _addTrackedListener(target, type, handler, options) {
  target.addEventListener(type, handler, options);
  state.domEventListeners.push({ target, type, handler, options });
}

// #109 (P1): 清理所有被追踪的事件监听器
function _cleanupTrackedListeners() {
  for (const { target, type, handler, options } of state.domEventListeners) {
    try { target.removeEventListener(type, handler, options); } catch { /* noop */ }
  }
  state.domEventListeners = [];
}

// #102 (P0): 定向 DOM 更新 — 只更新变化的元素而不全量 re-render
function _targetedUpdate(field, value) {
  switch (field) {
    case 'saveState': {
      const labels = { idle: '✓ 已保存', saving: '⏳ 保存中…', saved: '✓ 已保存', error: '⚠ 保存失败' };
      const text = labels[value] || '✓ 已保存';
      const cls = value === 'saved' ? 'success' : value === 'error' ? 'danger' : value === 'saving' ? 'running' : '';
      document.querySelectorAll('.save-state-badge').forEach((badge) => {
        badge.textContent = text;
        badge.className = `pill ${cls} save-state-badge`;
      });
      // 保存失败时显示重试按钮
      const retryBtn = document.querySelector('.save-retry-btn');
      if (value === 'error' && !retryBtn) {
        const footer = document.querySelector('.editor-footer');
        if (footer) {
          const btn = document.createElement('button');
          btn.className = 'btn btn-secondary save-retry-btn';
          btn.id = 'save-retry';
          btn.textContent = '重试保存';
          btn.addEventListener('click', () => {
            if (state.currentProject) flushProjectSave(state.currentProject.id);
          });
          footer.appendChild(btn);
        }
      } else if (value !== 'error' && retryBtn) {
        retryBtn.remove();
      }
      return true;
    }
    case 'wordCount': {
      const stats = _wordCount(value);
      const targetWords = state.settings.general?.defaultLength || 1800;
      const wcEl = document.querySelector('.editor-footer .word-count');
      const ccEl = document.querySelector('.editor-footer .char-count');
      if (wcEl) {
        wcEl.textContent = `字数: ${stats.words}/${targetWords} (目标±20%) · ${_readingTime(stats.words)}`;
        wcEl.className = `word-count ${stats.words > targetWords * 1.2 ? 'warning' : ''}`;
      }
      if (ccEl) {
        const charCls = stats.chars > 450000 ? 'near-limit' : stats.chars >= 500000 ? 'at-limit' : '';
        ccEl.textContent = `字符数: ${stats.chars}/500000`;
        ccEl.className = `char-count ${charCls}`;
      }
      return true;
    }
    case 'previewHtml': {
      const previewEl = document.getElementById('publish-preview');
      if (previewEl) previewEl.innerHTML = _sanitizePreviewHtml(value);
      const splitPreview = document.querySelector('.split-preview .body-preview-content');
      if (splitPreview) splitPreview.innerHTML = _sanitizePreviewHtml(value);
      return true;
    }
  }
  return false;
}

// #049 (P1): 侧栏预览防抖更新 — 正文变化时更新公众号侧栏预览
function _scheduleSidebarPreviewUpdate() {
  if (state.sidebarPreviewTimer) clearTimeout(state.sidebarPreviewTimer);
  state.sidebarPreviewTimer = setTimeout(() => {
    const previewEl = document.getElementById('publish-preview');
    if (!previewEl || !state.currentProject) return;
    // 如果有服务端预览且 revision 匹配，使用服务端预览
    if (state.preview?.revision === state.currentProject.revision && state.preview?.html) {
      previewEl.innerHTML = _sanitizePreviewHtml(state.preview.html);
    } else {
      // #042: 离线时使用客户端 Markdown 渲染
      previewEl.innerHTML = _renderMarkdownClientSide(state.currentProject.bodyMarkdown);
    }
  }, 800);
}

function renderWorkspace() {
  const project = state.currentProject;
  const task = state.currentTask;
  if (!project) return '<div class="card empty"><strong>尚未选择文章</strong><span>请从创作入口或文章中心打开文章。</span></div>';
  const sources = project.sources || [];
  const review = project.review || [];
  // R3: 检测封面生成失败（任务事件含 cover_failed 标记，或 project.coverFailed）
  const coverFailed = Boolean(project.coverFailed) || !!((task?.events || []).some((e) => e.step === 'cover' && (e.level === 'error' || e.detail?.coverFailed)));
  // A4: 审校综合评分（兼容多种字段命名）
  const overallScore = (typeof project.reviewScore === 'number' ? project.reviewScore
    : typeof project.overallScore === 'number' ? project.overallScore
    : typeof project.reviewOverallScore === 'number' ? project.reviewOverallScore
    : typeof project.reviewMeta?.overall_score === 'number' ? project.reviewMeta.overall_score
    : null);
  const blockedBySave = state.dirtyProjects.has(project.id) || state.saveChains.has(project.id) || Boolean(state.conflict);
  const reviewCurrent = project.reviewApproved && project.reviewRevision === project.revision;
  const previewCurrent = state.preview?.revision === project.revision;
  const publishAccount = state.settings.wechat || {};
  const wechatReady = Boolean(publishAccount.accountName && publishAccount.appId && state.health?.wechat?.reachable);
  // #168: 空正文发布阻断 — trim() 检查防止纯空白字符通过
  const hasBody = project.bodyMarkdown && project.bodyMarkdown.trim().length > 0;
  const canPublish = reviewCurrent && previewCurrent && hasBody && !blockedBySave && wechatReady;
  const taskActions = task && ['failed', 'blocked', 'timeout', 'cancelled'].includes(task.status)
    ? `<select id="retry-mode" aria-label="重试范围"><option value="review_only">仅重做审校</option><option value="preserve_body">保留正文，重做框架与审校</option><option value="from_outline">从现有框架重做正文</option><option value="full">全部重做</option></select><button class="btn btn-secondary" id="task-retry">按范围重试</button>${task.errorCode === 'server_restarted' ? '<button class="btn btn-primary" id="task-resume" style="margin-left:8px">恢复任务</button>' : ''}` : '';
  const saveLabel = { idle: '✓ 已保存', saving: '⏳ 保存中…', saved: '✓ 已保存', error: '⚠ 保存失败' }[state.saveState] || '✓ 已保存';
  const saveCls = state.saveState === 'saved' ? 'success' : state.saveState === 'error' ? 'danger' : state.saveState === 'saving' ? 'running' : '';
  const tab = state.wsTab || 'write';
  const leftCol = state.wsLeftCollapsed ? '' : 'ws-col-open';
  const rightCol = state.wsRightCollapsed ? '' : 'ws-col-open';
  const gridCls = `ws-three-col ${state.wsLeftCollapsed ? 'left-collapsed' : ''} ${state.wsRightCollapsed ? 'right-collapsed' : ''}`;
  return `
    ${!state.online ? '<div class="network-banner">网络已断开 — 编辑内容会在网络恢复后自动保存</div>' : ''}
    <div class="page-head"><div><h2>${escapeHtml(project.title || '未命名文章')}</h2><p>保存完成后生成不可变预览，再终审当前 revision，最后同步该快照。</p></div><div class="top-actions">${statusPill(project.status)} ${statusPill(project.publishStatus)}</div></div>
    ${conflictHtml()}
    ${versionsHtml()}
    <div class="ws-shell card">
      <div class="ws-status-strip">
        <span class="ws-strip-label">AI 执行</span>
        ${task ? `<span class="ws-strip-taskid">${escapeHtml(task.id)}</span>${statusPill(task.status)}` : '<span class="helper">该文章没有关联任务</span>'}
        <span class="ws-strip-sep"></span>
        <span class="ws-strip-label">保存</span>
        <span class="pill ${saveCls} save-state-badge">${saveLabel}</span>
        <span class="ws-strip-sep"></span>
        <span class="ws-strip-label">来源</span>
        <span class="pill">${project.sourceKind === 'url' ? 'URL' : '主题'}</span>
        <div class="ws-strip-actions">${taskActions}<button class="btn btn-ghost ws-strip-btn" id="open-task">诊断</button>${['queued', 'running'].includes(task?.status) ? '<button class="btn btn-danger ws-strip-btn" id="task-cancel">取消</button>' : ''}${task?.status === 'blocked' ? '<button class="btn btn-primary ws-strip-btn" data-nav="ai">配置 AI</button>' : ''}</div>
      </div>
      ${task && ['queued', 'running'].includes(task.status) ? `<div class="ws-timeline-row">${miniTimelineHtml(task)}</div><div class="ws-progress"><div class="progress" role="progressbar" aria-valuenow="${Math.max(0, Math.min(100, task.progress || 0))}" aria-valuemin="0" aria-valuemax="100" aria-label="任务进度"><i style="width:${Math.max(0, Math.min(100, task.progress || 0))}%"></i></div></div>` : ''}
      ${task && !['queued', 'running'].includes(task.status) ? (state.wsTimelineExpanded ? `<div class="ws-timeline-row">${miniTimelineHtml(task)}<button class="btn btn-ghost ws-mini-btn" id="toggle-timeline" style="margin-left:12px">收起</button></div>` : `<div class="ws-timeline-collapsed"><span class="helper">执行轨迹：${escapeHtml(task.currentStep || task.status)}</span><button class="btn btn-ghost ws-mini-btn" id="toggle-timeline">展开轨迹</button></div>`) : ''}
      <div class="ws-tab-bar" role="tablist" aria-label="工作区标签">
        <button class="ws-tab-btn ${tab === 'write' ? 'active' : ''}" data-ws-tab="write" role="tab" aria-selected="${tab === 'write'}">写作</button>
        <button class="ws-tab-btn ${tab === 'review' ? 'active' : ''}" data-ws-tab="review" role="tab" aria-selected="${tab === 'review'}">审校${review.length ? `<span class="ws-tab-badge">${review.length}</span>` : ''}</button>
        <button class="ws-tab-btn ${tab === 'publish' ? 'active' : ''}" data-ws-tab="publish" role="tab" aria-selected="${tab === 'publish'}">发布</button>
      </div>
      <div class="${gridCls}">
        <!-- LEFT: 文章信息与框架 (collapsible) -->
        ${!state.wsLeftCollapsed ? `
        <div class="ws-col-left ${leftCol}">
          <div class="ws-panel">
            <div class="ws-panel-head">
              <h3 class="ws-panel-title">文章信息与框架</h3>
              <div class="ws-panel-actions"><span class="pill ${saveCls} save-state-badge" id="save-state">${saveLabel}</span><button class="ws-collapse-btn" id="collapse-left" title="收起左栏" aria-label="收起左栏" aria-expanded="${!state.wsLeftCollapsed}">‹</button></div>
            </div>
            <div class="ws-panel-body">
              <div class="field"><label for="project-title" style="display:flex;justify-content:space-between;align-items:baseline">标题 ${_charCounter(project.title, 120)}</label><input class="input autosave" id="project-title" data-field="title" maxlength="120" value="${escapeHtml(project.title)}"></div>
              <div class="field" style="margin-top:12px"><label for="project-summary" style="display:flex;justify-content:space-between;align-items:baseline">摘要 ${_charCounter(project.summary, 300)}</label><textarea class="autosave" id="project-summary" data-field="summary" maxlength="300" style="min-height:72px">${escapeHtml(project.summary)}</textarea></div>
              <div class="ws-divider"></div>
              <div class="ws-section-label">文章框架<button class="btn btn-ghost ws-mini-btn" id="show-versions">版本历史</button></div>
              ${project.outline?.length ? `<ol class="outline-list">${project.outline.map((item, idx) => `<li data-outline-idx="${idx}" title="点击跳转到正文对应位置">${escapeHtml(item)}</li>`).join('')}</ol>` : '<div class="empty"><strong>尚无框架</strong><span>任务完成后会显示文章框架。</span></div>'}
              ${project.requirements ? `<div class="ws-divider"></div><div class="ws-section-label">文章生成要求</div><div class="ws-requirements-box">${escapeHtml(project.requirements)}</div>` : ''}
            </div>
          </div>
        </div>` : '<div class="ws-col-collapsed"><button class="ws-expand-tab" id="expand-left" aria-label="展开信息与框架"><span class="ws-expand-icon">›</span>信息与框架</button></div>'}

        <!-- CENTER: 正文编辑 / 审校 / 发布 -->
        <div class="ws-col-center">
          ${tab === 'write' ? `
          <div class="ws-panel">
            <div class="ws-panel-head">
              <div><h3 class="ws-panel-title">正文编辑</h3><p class="ws-panel-sub">revision ${project.revision} · 标题、摘要、正文和封面任一变化都会使终审失效</p></div>
              <div class="body-mode-switch">
                <button class="btn btn-ghost body-mode-btn ${state.bodyMode === 'edit' ? 'active' : ''}" id="body-mode-edit" aria-label="编辑模式">编辑</button>
                <button class="btn btn-ghost body-mode-btn ${state.bodyMode === 'preview' ? 'active' : ''}" id="body-mode-preview" aria-label="预览模式">预览</button>
                <button class="btn btn-ghost body-mode-btn ${state.splitPreview ? 'active' : ''}" id="body-mode-split" title="编辑+预览分屏" aria-label="分屏模式">⇆</button>
                ${state.bodyMode === 'preview' || state.splitPreview ? `<button class="btn btn-ghost body-mode-btn ${state.previewDevice === 'mobile' ? 'active' : ''}" id="preview-device-toggle" title="切换${state.previewDevice === 'mobile' ? '桌面' : '移动'}预览" aria-label="切换预览设备">${state.previewDevice === 'mobile' ? '🖥' : '📱'}</button>` : ''}
              </div>
            </div>
            <div class="ws-panel-body">
              ${state.splitPreview
                ? (() => {
                    // #043: 分屏编辑+预览模式
                    const wordStats = _wordCount(project.bodyMarkdown);
                    const targetWords = state.settings.general?.defaultLength || 1800;
                    const isTaskActive = task && ['queued', 'running'].includes(task.status);
                    const editorLocked = isTaskActive && !['review_only', 'preserve_body'].includes(task.retryMode);
                    const lockedOverlay = editorLocked ? `<div class="editor-locked-overlay"><div class="locked-msg">AI 任务执行中<span>编辑器已锁定</span></div></div>` : '';
                    return `<div class="split-view">
                      <div class="split-editor">
                        <textarea class="editor autosave" id="project-body" data-field="bodyMarkdown" data-preserve="true" data-preserve-key="project-body" data-preserve-rev="${project.revision}" maxlength="500000" aria-label="正文编辑器" placeholder="编辑正文...">${escapeHtml(project.bodyMarkdown)}</textarea>
                        ${lockedOverlay}
                        <div class="editor-footer"><div class="word-count ${wordStats.words > targetWords * 1.2 ? 'warning' : ''}">字数: ${wordStats.words}/${targetWords}</div><div class="char-count">字符: ${wordStats.chars}/500000</div></div>
                      </div>
                      <div class="split-preview ${state.previewDevice === 'mobile' ? 'preview-mobile' : ''}">
                        ${state.previewLoading ? '<div class="preview-loading"><div class="spinner"></div><span>生成预览...</span></div>' : previewCurrent ? `<div class="rich-preview body-preview-content">${_sanitizePreviewHtml(state.preview.html)}</div>` : '<div class="empty"><strong>暂无预览</strong><span>保存后查看预览</span></div>'}
                      </div>
                    </div>`;
                  })()
                : state.bodyMode === 'edit'
                ? (() => {
                    // #001: 编辑器工具栏 — 含撤销/重做、全屏模式
                    const toolbar = `
                      <div class="editor-toolbar">
                        <button class="editor-tool-btn" data-action="undo" title="撤销 (Ctrl+Z)" aria-label="撤销">↶</button>
                        <button class="editor-tool-btn" data-action="redo" title="重做 (Ctrl+Y)" aria-label="重做">↷</button>
                        <div class="editor-tool-sep"></div>
                        <button class="editor-tool-btn" data-insert="bold" title="加粗 (Ctrl+B)" aria-label="加粗"><b>B</b></button>
                        <button class="editor-tool-btn" data-insert="italic" title="斜体 (Ctrl+I)" aria-label="斜体"><i>I</i></button>
                        <button class="editor-tool-btn" data-insert="heading" title="标题" aria-label="标题">H</button>
                        <button class="editor-tool-btn" data-insert="list" title="列表" aria-label="列表">•</button>
                        <button class="editor-tool-btn" data-insert="link" title="链接 (Ctrl+K)" aria-label="插入链接">🔗</button>
                        <button class="editor-tool-btn" data-insert="image" title="图片" aria-label="插入图片">🖼</button>
                        <div class="editor-tool-sep"></div>
                        <button class="editor-tool-btn" data-insert="hr" title="分隔线" aria-label="分隔线">―</button>
                        <button class="editor-tool-btn" data-insert="quote" title="引用" aria-label="引用">"</button>
                        <button class="editor-tool-btn" data-insert="code" title="代码块" aria-label="代码块">{ }</button>
                        <button class="editor-tool-btn" data-action="table" title="插入表格" aria-label="插入表格">⊞</button>
                        <div class="editor-tool-sep"></div>
                        <button class="editor-tool-btn" data-action="paste-plain" title="粘贴为纯文本 (Ctrl+Shift+V)" aria-label="粘贴纯文本">T</button>
                        <button class="editor-tool-btn" data-action="find-replace" title="查找替换 (Ctrl+F)" aria-label="查找替换">🔍</button>
                        <button class="editor-tool-btn" data-action="focus-mode" title="专注模式 (F11)" aria-label="专注模式">⛶</button>
                      </div>`;
                    // #006: 查找替换面板
                    const findReplacePanel = state.findReplaceOpen ? `
                      <div class="find-replace-panel">
                        <input type="text" class="find-input" id="find-query" placeholder="查找..." value="${escapeHtml(state.findQuery)}" aria-label="查找内容">
                        <input type="text" class="replace-input" id="replace-query" placeholder="替换为..." value="${escapeHtml(state.replaceQuery)}" aria-label="替换内容">
                        <button class="btn btn-ghost ws-mini-btn" id="find-next" aria-label="查找下一个">↓</button>
                        <button class="btn btn-ghost ws-mini-btn" id="replace-one" aria-label="替换">替换</button>
                        <button class="btn btn-ghost ws-mini-btn" id="replace-all" aria-label="全部替换">全部</button>
                        <span class="find-count" id="find-count">${state.findMatchCount} 个匹配</span>
                        <button class="btn btn-ghost ws-mini-btn" id="find-close" aria-label="关闭查找替换">×</button>
                      </div>` : '';
                    // #002/#003: 字数统计
                    const wordStats = _wordCount(project.bodyMarkdown);
                    const targetWords = state.settings.general?.defaultLength || 1800;
                    const wordCountCls = wordStats.words > targetWords * 1.2 ? 'warning' : '';
                    const charCls = wordStats.chars > 450000 ? 'near-limit' : wordStats.chars >= 500000 ? 'at-limit' : '';
                    // #027: 保存失败重试按钮
                    const saveRetryBtn = state.saveState === 'error' ? `<button class="btn btn-secondary save-retry-btn" id="save-retry">重试保存</button>` : '';
                    // #026: 编辑器锁定逻辑
                    const isTaskActive = task && ['queued', 'running'].includes(task.status);
                    const editorLocked = isTaskActive && !['review_only', 'preserve_body'].includes(task.retryMode);
                    // #080: 任务终态时编辑器显示明确状态
                    const taskTerminal = task && ['failed', 'blocked', 'timeout', 'cancelled'].includes(task.status);
                    const lockedOverlay = editorLocked ? `
                      <div class="editor-locked-overlay">
                        <div class="locked-msg">
                          AI 任务执行中
                          <span>当前正在${task.currentStep === 'draft' ? '生成正文' : '执行' + (task.currentStep || '')}，编辑器已锁定</span>
                        </div>
                      </div>` : '';
                    const terminalBanner = taskTerminal ? `<div class="alert ${task.status === 'blocked' ? 'warning' : 'error'}" style="margin-bottom:8px;padding:8px 12px;font-size:13px"><strong>任务${task.status === 'failed' ? '失败' : task.status === 'blocked' ? '阻塞' : task.status === 'timeout' ? '超时' : '已取消'}</strong> · 编辑器已解锁，可手动编辑正文。点击上方"诊断"查看详情或重试。</div>` : '';
                    // #122: 敏感词检测警告
                    const sensitiveWarn = state.sensitiveWordsFound.length > 0
                      ? `<div class="alert warning sensitive-warn" style="margin-bottom:8px;padding:8px 12px;font-size:13px"><strong>⚠ 检测到敏感词</strong> · ${escapeHtml(state.sensitiveWordsFound.join('、'))} · 发布前请确认内容合规</div>`
                      : '';
                    return `${terminalBanner}${sensitiveWarn}${toolbar}${findReplacePanel}
                      <div class="editor-wrap" style="position:relative">
                        <textarea class="editor autosave" id="project-body" data-field="bodyMarkdown" data-preserve="true" data-preserve-key="project-body" data-preserve-rev="${project.revision}" maxlength="500000" aria-label="正文编辑器" aria-describedby="editor-hint" placeholder="正文将在这里生成，也可以直接手工写作。支持 Markdown 语法，使用上方工具栏快速插入格式。">${escapeHtml(project.bodyMarkdown)}</textarea>
                        ${lockedOverlay}
                      </div>
                      <div class="editor-footer">
                        <div class="word-count ${wordCountCls}">字数: ${wordStats.words}/${targetWords} (目标±20%) · ${_readingTime(wordStats.words)}</div>
                        <div class="char-count ${charCls}">字符数: ${wordStats.chars}/500000</div>
                      </div>
                      ${saveRetryBtn}`;
                  })()
                : `<div class="body-preview-wrap ${state.previewDevice === 'mobile' ? 'preview-mobile-wrap' : ''}">${state.previewLoading ? `
                    <div class="preview-loading">
                      <div class="spinner"></div>
                      <span>正在生成预览...</span>
                    </div>` : previewCurrent ? `<div class="${state.previewDevice === 'mobile' ? 'preview-phone-frame' : ''}"><div class="rich-preview body-preview-content">${_sanitizePreviewHtml(state.preview.html)}</div></div>` : `
                    <div class="empty">
                      <strong>暂无预览</strong>
                      <span>保存后点击"预览"按钮查看渲染效果。</span>
                      <button class="btn btn-secondary preview-empty-action" id="preview-empty-save">立即保存并预览</button>
                    </div>`}</div>`
              }
            </div>
          </div>` : ''}
          ${tab === 'review' ? `
          <div class="ws-panel">
            <div class="ws-panel-head">
              <div><h3 class="ws-panel-title">发布前审校</h3><p class="ws-panel-sub">只能终审已完成保存且与服务端指纹一致的当前 revision</p></div>
            </div>
            <div class="ws-panel-body">
              ${overallScore !== null ? `<div class="alert ${overallScore >= 80 ? 'info' : overallScore >= 60 ? 'warning' : 'error'}" style="margin-bottom:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap"><strong>AI 综合参考分</strong><span style="font-size:24px;font-weight:700;color:${overallScore >= 80 ? 'var(--success)' : overallScore >= 60 ? 'var(--warning)' : 'var(--danger)'}">${overallScore}</span><span class="helper">仅供人工复核参考，不代表事实、版权或平台合规认证。</span></div>` : ''}
              ${coverFailed ? '<div class="alert error" style="margin-bottom:14px"><strong>封面自动生成失败</strong><br>请前往「发布」标签手动上传封面图片。</div>' : ''}
              ${review.length ? (() => {
                // #048: 审校结果筛选栏
                const filterBar = `
                  <div class="review-filter-bar" role="group" aria-label="审校结果筛选">
                    <button class="review-filter-btn ${state.reviewFilter === 'all' ? 'active' : ''}" data-review-filter="all" aria-pressed="${state.reviewFilter === 'all'}">全部 <span class="filter-count">${review.length}</span></button>
                    <button class="review-filter-btn ${state.reviewFilter === 'passed' ? 'active' : ''}" data-review-filter="passed" aria-pressed="${state.reviewFilter === 'passed'}">通过 <span class="filter-count">${review.filter(r => r.status === 'passed').length}</span></button>
                    <button class="review-filter-btn ${state.reviewFilter === 'warning' ? 'active' : ''}" data-review-filter="warning" aria-pressed="${state.reviewFilter === 'warning'}">需注意 <span class="filter-count">${review.filter(r => r.status === 'warning').length}</span></button>
                    <button class="review-filter-btn ${state.reviewFilter === 'failed' ? 'active' : ''}" data-review-filter="failed" aria-pressed="${state.reviewFilter === 'failed'}">不通过 <span class="filter-count">${review.filter(r => r.status === 'failed').length}</span></button>
                  </div>`;
                const filtered = state.reviewFilter === 'all' ? review : review.filter(r => r.status === state.reviewFilter);
                const items = filtered.map((item, idx) => `<div class="review-item ${state.locatedReviewIdx === idx ? 'review-located' : ''}" data-review-idx="${idx}" role="listitem" title="点击定位到正文"><div class="review-symbol ${escapeHtml(item.status)}">${item.status === 'passed' ? '✓' : item.status === 'failed' ? '×' : '!'}</div><div><strong>${escapeHtml(item.label)}</strong>${typeof item.score === 'number' ? _scoreBadgeHtml(item.score) : ''}<p>${escapeHtml(item.message)}</p></div></div>`).join('');
                return `<div role="list">${filterBar}${items}</div>`;
              })() : '<div class="alert info">自动审校未执行或尚无结果。人工终审仍会绑定当前正文指纹。</div>'}
              ${blockedBySave ? '<div class="alert warning" style="margin-top:14px">仍有内容未保存或存在冲突，终审和发布已禁用。</div>' : ''}
              <label class="checkline" style="margin-top:14px"><input type="checkbox" id="review-approved" ${reviewCurrent ? 'checked' : ''} ${blockedBySave || !project.bodyMarkdown ? 'disabled' : ''}><span><strong>我确认内容无误，可以发布</strong><br><span class="helper">终审绑定 revision 与正文 SHA-256；标题、摘要、正文或封面任一变化都会自动失效。</span></span></label>
              ${!reviewCurrent && project.bodyMarkdown ? '<button class="btn btn-secondary" id="re-review" style="margin-top:10px">重新审校</button>' : ''}
            </div>
          </div>` : ''}
          ${tab === 'publish' ? `
          <div class="ws-panel">
            <div class="ws-panel-head">
              <div><h3 class="ws-panel-title">封面图片</h3><p class="ws-panel-sub">PNG/JPEG/WEBP/GIF，解码后小于 2MB</p></div>
            </div>
            <div class="ws-panel-body">
              ${coverFailed ? '<div class="alert error cover-fail-alert" style="margin-bottom:12px"><strong>⚠ 封面自动生成失败</strong><br>请手动上传封面图片。</div>' : ''}
              <div class="field"><label for="cover-file">封面图片</label><input class="input" type="file" id="cover-file" accept="image/png,image/jpeg,image/webp,image/gif"></div>
              <div class="cover-drop-zone" id="cover-drop-zone" tabindex="0" role="button" aria-label="拖拽图片到此处上传封面">
                <span class="helper">或将图片拖拽到此处上传封面（PNG/JPEG/WEBP/GIF，小于 2MB）</span>
              </div>
              ${project.coverDataUrl ? '<button class="btn btn-ghost" id="remove-cover" style="width:100%;margin-top:10px">移除封面</button>' : ''}
            </div>
          </div>
          <div class="ws-panel" style="margin-top:16px">
            <div class="ws-panel-head">
              <div><h3 class="ws-panel-title">发布状态</h3><p class="ws-panel-sub">预览 revision：${state.preview?.revision ?? '—'}；终审 revision：${project.reviewRevision || '—'}</p></div>
              ${statusPill(project.publishStatus)}
            </div>
            <div class="ws-panel-body">
              ${(() => {
                const accountLabel = publishAccount.accountName || '未配置发布账号';
                const appIdHint = publishAccount.appId ? `…${publishAccount.appId.slice(-6)}` : '未配置 AppID';
                const targetCard = `<div class="source-card" style="margin-bottom:14px"><strong>发布目标：${escapeHtml(accountLabel)}</strong><p class="helper" style="margin:6px 0 0">AppID ${escapeHtml(appIdHint)} · ${wechatReady ? '凭证已验证' : '尚未通过真实连接验证'} · 将创建一份新草稿</p></div>`;
                // #056: 发布前检查清单 — 未通过项显示红色 × 和"点击修复"链接
                const checks = [
                  { label: '正文已保存', ok: !blockedBySave && hasBody, fixTab: 'write', fixHint: '前往编辑' },
                  { label: '预览已刷新', ok: previewCurrent, fixTab: 'write', fixHint: '刷新预览' },
                  { label: '人工终审通过', ok: reviewCurrent, fixTab: 'review', fixHint: '前往审校' },
                  { label: '发布账号已验证', ok: wechatReady, fixTab: 'publish', fixHint: '前往设置', settingsLink: true },
                  // #064: 封面图片检查（非阻断，仅警告）
                  { label: '封面图片已设置', ok: Boolean(project.coverDataUrl), fixTab: 'publish', fixHint: '上传封面', warnOnly: true },
                ];
                const checklistHtml = checks.map((c) => {
                  if (c.ok) return `<div class="publish-check ok"><span class="publish-check-icon">✓</span>${c.label}</div>`;
                  if (c.warnOnly) return `<div class="publish-check pending"><span class="publish-check-icon" style="color:var(--warning)">⚠</span>${c.label} <button class="btn btn-ghost ws-mini-btn publish-fix-btn" data-fix-tab="${c.fixTab}" style="margin-left:6px">${c.fixHint}</button></div>`;
                  if (c.settingsLink) return `<div class="publish-check pending"><span class="publish-check-icon" style="color:var(--danger)">✕</span>${c.label} <a class="btn btn-ghost ws-mini-btn" href="#/settings" style="margin-left:6px">${c.fixHint}</a></div>`;
                  return `<div class="publish-check pending"><span class="publish-check-icon" style="color:var(--danger)">✕</span>${c.label} <button class="btn btn-ghost ws-mini-btn publish-fix-btn" data-fix-tab="${c.fixTab}" style="margin-left:6px">${c.fixHint}</button></div>`;
                }).join('');
                // #057: 发布按钮 disabled 时显示 tooltip
                const blockingChecks = checks.filter(c => !c.ok && !c.warnOnly);
                const disabledReason = !canPublish ? ` title="无法发布：${blockingChecks.map(c => c.label).join('、')}"` : '';
                // #065: 发布确认对话框
                const publishConfirm = state.publishConfirmOpen ? `
                  <div class="publish-confirm-dialog" id="publish-confirm-overlay">
                    <div class="publish-confirm-content">
                      <h3>确认发布</h3>
                      <p>即将同步 revision ${project.revision} 到「${escapeHtml(accountLabel)}」的草稿箱。此操作将创建一份新草稿，确认继续？</p>
                      <div class="publish-confirm-actions">
                        <button class="btn btn-ghost" id="publish-confirm-cancel">取消</button>
                        <button class="btn btn-primary" id="publish-confirm-ok">确认同步</button>
                      </div>
                    </div>
                  </div>` : '';
                // #065: 发布加载遮罩
                const publishLoadingOverlay = state.publishLoading ? `
                  <div class="publish-loading-overlay">
                    <div class="publish-loading-content">
                      <div class="spinner"></div>
                      <p>正在同步到公众号草稿...</p>
                    </div>
                  </div>` : '';
                return `${publishConfirm}${publishLoadingOverlay}${targetCard}<div class="publish-checklist">${checklistHtml}</div>${canPublish ? '<div class="alert info" style="margin-top:14px">所有条件已满足，可同步到目标公众号草稿。</div>' : ''}<button class="btn btn-primary" id="publish-button" style="width:100%;margin-top:14px" ${canPublish ? '' : 'disabled'}${disabledReason}>同步 revision ${project.revision} 到「${escapeHtml(accountLabel)}」</button>`;
              })()}
              ${state.publishStale && state.publishStale.projectId === project.id ? `
              <div class="alert warning" style="margin-top:14px"><strong>发布状态需要确认</strong>本地未标记为已同步，但远程草稿已存在${state.publishStale.revision ? `（revision ${escapeHtml(String(state.publishStale.revision))}` : ''}${state.publishStale.remoteId ? `，远程 ID ${escapeHtml(String(state.publishStale.remoteId))}` : ''}）。请选择标记为已同步，或撤回远程草稿。</div>
              <div class="top-actions" style="margin-top:10px">
                <button class="btn btn-secondary" id="publish-confirm-sync">标记为已同步</button>
                <button class="btn btn-danger" id="publish-delete-remote">撤回远程草稿</button>
              </div>` : ''}
              ${state.publishSuccess ? `
              <div class="alert success publish-success-banner" style="margin-top:14px">
                <strong>已成功同步到公众号草稿</strong>
                <p style="margin:6px 0 0">远程草稿 ID：${escapeHtml(state.publishSuccess.remoteId || '—')} · revision ${escapeHtml(String(state.publishSuccess.revision || '—'))}</p>
                <p style="margin:4px 0 0" class="helper">下一步：<a href="https://mp.weixin.qq.com" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:underline">前往微信公众平台</a> → 草稿箱 → 编辑/预览 → 群发。如需修改内容，编辑后会自动失效当前同步状态。</p>
                <button class="btn btn-ghost ws-mini-btn" id="dismiss-publish-success" style="margin-top:8px">我知道了</button>
              </div>` : ''}
            </div>
          </div>` : ''}
        </div>

        <!-- RIGHT: 侧栏 (collapsible) -->
        ${!state.wsRightCollapsed ? `
        <div class="ws-col-right ${rightCol}">
          <div class="ws-panel">
            <div class="ws-panel-head">
              <h3 class="ws-panel-title">侧栏</h3>
              <div class="ws-panel-actions">${project.sourceKind === 'url' ? '<button class="icon-btn" id="refresh-source" aria-label="重新读取来源">↻</button>' : ''}<button class="ws-collapse-btn" id="collapse-right" title="收起右栏" aria-label="收起右栏" aria-expanded="${!state.wsRightCollapsed}">›</button></div>
            </div>
            <div class="ws-panel-body">
              <div class="ws-section-label">来源快照</div>
              ${sources.length ? sources.map((source) => {
                const sid = source.id || source.contentHash?.slice(0, 12) || '';
                const expanded = state.expandedSources.has(sid);
                return `<div class="source-card">
                  <strong>${escapeHtml(source.title || source.finalUrl)}</strong>
                  <div class="source-meta"><span>${escapeHtml(source.publisher || '未知发布方')}</span><span>${formatTime(source.fetchedAt)}</span></div>
                  ${expanded ? `<div class="source-meta" style="margin-top:8px"><span>SHA-256 ${escapeHtml(source.contentHash.slice(0, 16))}…</span><span>${escapeHtml(source.extractionMethod)}</span></div><p class="source-preview">${escapeHtml(source.preview)}</p><button class="btn btn-ghost ws-mini-btn source-toggle-btn" data-source-toggle="${escapeHtml(sid)}">收起详情</button>` : `<button class="btn btn-ghost ws-mini-btn source-toggle-btn" data-source-toggle="${escapeHtml(sid)}">查看详情</button>`}
                </div>`;
              }).join('') : `<div class="empty"><strong>${project.sourceKind === 'topic' ? '主题创作' : '尚无来源快照'}</strong><span>${project.sourceKind === 'topic' ? (state.settings.general?.strictFacts ? '强制引用模式已开启，该任务会因缺少证据暂停。' : '本任务按主题直接创作，没有外部来源快照。') : '来源读取成功后会显示。'}</span></div>`}
              <div class="ws-divider"></div>
              <div class="ws-section-label">公众号预览<button class="icon-btn" id="refresh-preview" aria-label="刷新发布预览">↻</button></div>
              ${state.wsPreviewExpanded
                ? `<div class="preview-phone"><div class="preview-bar"></div><div class="preview-content">${project.coverDataUrl ? `<img class="cover-preview" src="${escapeHtml(project.coverDataUrl)}" alt="文章封面">` : ''}<h1>${escapeHtml(project.title)}</h1><div class="digest">${escapeHtml(project.summary || '尚未填写摘要')}</div><div class="preview-body rich-preview" id="publish-preview">${previewCurrent ? state.preview.html : '<p>保存后点击刷新预览。</p>'}</div></div></div><button class="btn btn-ghost ws-mini-btn" id="toggle-preview" style="width:100%;margin-top:10px">收起预览</button>`
                : `<button class="btn btn-secondary" id="toggle-preview" style="width:100%">查看公众号预览</button>`
              }
            </div>
          </div>
        </div>` : '<div class="ws-col-collapsed"><button class="ws-expand-tab" id="expand-right" aria-label="展开侧栏"><span class="ws-expand-icon">‹</span>侧栏</button></div>'}
      </div>
    </div>
    ${state.diffVersions ? `
    <div class="log-modal-overlay open" id="diff-modal-overlay">
      <div class="log-modal" style="max-width:1100px;max-height:85vh">
        <div class="log-modal-header">
          <span class="log-modal-title">版本对比 · ${escapeHtml(state.diffVersions.oldLabel)} → ${escapeHtml(state.diffVersions.newLabel)}</span>
          <button class="log-modal-close" id="diff-modal-close" aria-label="关闭">&times;</button>
        </div>
        <div class="log-modal-meta">
          <button class="btn btn-ghost ws-mini-btn" id="diff-toggle-view">${state.diffViewMode === 'side' ? '切换为统一视图' : '切换为并排视图'}</button>
          <span class="helper">绿色为新增行，红色为删除行，灰色为未变化行。</span>
        </div>
        <div class="log-modal-body" style="display:block">
          ${state.diffViewMode === 'side'
            ? _sideBySideDiffHtml(state.diffVersions.oldText, state.diffVersions.newText)
            : `<div style="font-family:'SFMono-Regular',Consolas,monospace;font-size:13px;line-height:1.7;background:#151a1d;border:1px solid #2e383b;border-radius:8px;padding:10px;overflow:auto;max-height:60vh">${_lineDiff(state.diffVersions.oldText, state.diffVersions.newText)}</div>`}
        </div>
        <div class="log-modal-footer">
          <button class="btn btn-ghost" id="diff-modal-close-btn">关闭</button>
        </div>
      </div>
    </div>` : ''}`;
}

function renderArticles() {
  const totalPages = Math.max(1, Math.ceil(state.articleTotal / state.articlePageSize));
  const currentPage = Math.min(state.articlePage + 1, totalPages);
  return `
    <div class="page-head"><div><h2>${state.showDeleted ? '回收站' : '文章中心'}</h2><p>服务端搜索与分页，支持万级文章库；生命周期包含归档、软删除、恢复、永久删除、复制和导出。</p></div><span class="pill">共 ${state.articleTotal} 篇</span></div>
    <section class="card card-pad">
      <div class="searchbar"><input class="input" id="article-search" placeholder="搜索标题或摘要" value="${escapeHtml(state.search)}"><label class="checkline"><input type="checkbox" id="show-archived" ${state.showArchived ? 'checked' : ''} ${state.showDeleted ? 'disabled' : ''}><span>显示归档</span></label><label class="checkline"><input type="checkbox" id="show-deleted" ${state.showDeleted ? 'checked' : ''}><span>回收站</span></label></div>
      ${state.selectedArticleIds.size > 0 ? `
      <div class="alert info" style="margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <strong>批量操作：已选 ${state.selectedArticleIds.size} 篇</strong>
        <button class="btn btn-ghost ws-mini-btn" id="batch-select-all">全选当前页</button>
        <button class="btn btn-ghost ws-mini-btn" id="batch-clear">取消选择</button>
        <button class="btn btn-secondary" id="batch-archive">批量归档</button>
        <button class="btn btn-secondary" id="batch-restore">批量恢复</button>
        <button class="btn btn-danger" id="batch-delete">批量删除（移入回收站）</button>
      </div>` : ''}
      <div class="article-list">${state.projects.length ? state.projects.map((project) => `
        <article class="card article-row"><div style="display:flex;gap:12px;align-items:flex-start"><input type="checkbox" class="batch-checkbox" data-batch-id="${project.id}" ${state.selectedArticleIds.has(project.id) ? 'checked' : ''} aria-label="选择此文章" style="margin-top:5px"><div><h3>${escapeHtml(project.title)}</h3><p>${escapeHtml(project.summary || '暂无摘要')} · revision ${project.revision} · ${formatTime(project.updatedAt)}</p></div></div><div class="article-actions">
          ${project.deleted ? `<button class="btn btn-secondary" data-restore-deleted="${project.id}">恢复</button><button class="btn btn-danger" data-purge-project="${project.id}">永久删除</button>` : `<button class="btn btn-primary" data-open-project="${project.id}">打开</button><div class="article-menu-wrap"><button class="icon-btn article-menu-btn" data-article-menu="${project.id}" aria-label="更多操作">⋯</button>${state.articleMenuId === project.id ? `<div class="article-menu"><button class="article-menu-item" data-export-project="${project.id}">导出</button><button class="article-menu-item" data-copy-project="${project.id}">复制</button><button class="article-menu-item" data-archive-project="${project.id}" data-archived="${project.archived}">${project.archived ? '取消归档' : '归档'}</button><button class="article-menu-item article-menu-danger" data-delete-project="${project.id}">删除</button></div>` : ''}</div>`}
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
    maxTokens: savedAi.maxTokens ?? 4096,
    autoReview: savedAi.autoReview !== false,
    backup: savedAi.backup || { baseUrl: '', apiKey: '', model: '' },
  };
  const backup = ai.backup || {};
  const health = state.health?.ai || {};
  const statusSummary = `${health.configured ? '✓ 已配置' : '✗ 未配置'} · ${health.reachable ? '✓ 可连接' : '✗ 不可连接'} · 最近验证 ${formatTime(health.verifiedAt)}`;
  return `
    <div class="page-head"><div><h2>AI 能力</h2><p>配置、可连接、最近验证成功是三个独立状态。</p></div></div>
    <div class="ai-status-bar">
      <span class="helper">${escapeHtml(statusSummary)}</span>
      <button class="btn btn-ghost ws-mini-btn" id="toggle-ai-status">${state.aiStatusExpanded ? '收起详情' : '查看详情'}</button>
    </div>
    ${state.aiStatusExpanded ? `<section class="card card-pad" style="margin-bottom:18px"><div class="stack"><div class="source-card"><strong>已配置</strong><p class="helper">${health.configured ? '是' : '否'}</p></div><div class="source-card"><strong>可连接</strong><p class="helper">${health.reachable ? '是' : '否'}</p></div><div class="source-card"><strong>最近验证</strong><p class="helper">${formatTime(health.verifiedAt)} · ${escapeHtml(health.message || '尚未验证')}</p></div></div></section>` : ''}
    <div>
      <section class="card card-pad"><div class="section-title"><div><h3>OpenAI 兼容模型</h3><p>请求固定到已验证公网 IP，禁止重定向携带 Authorization。</p></div></div>
        <form id="ai-form" class="setting-group">
          <div class="field"><label for="ai-base-url">Base URL</label><input class="input" id="ai-base-url" value="${escapeHtml(ai.baseUrl || 'https://api.openai.com/v1')}"></div>
          <div class="field"><label for="ai-key">API Key ${apiKeyHint ? `（已保存 ${escapeHtml(apiKeyHint)}）` : ''}</label><input class="input" type="password" id="ai-key" value="${escapeHtml(ai.apiKey || '')}" placeholder="留空表示保持原值"></div>
          <div class="field"><label for="ai-model">模型</label><input class="input" id="ai-model" value="${escapeHtml(ai.model || '')}"></div>
          <div class="field"><label for="ai-temp">温度</label><input class="input" id="ai-temp" type="number" min="0" max="2" step="0.1" value="${escapeHtml(ai.temperature ?? 0.4)}"></div>
          <div class="field"><label for="ai-max-tokens">最大 Tokens</label><input class="input" id="ai-max-tokens" type="number" min="1024" max="16384" step="256" value="${escapeHtml(ai.maxTokens ?? 4096)}"><span class="helper">控制单次 AI 回复的最大长度（1024–16384）</span></div>
          <label class="checkline"><input type="checkbox" id="ai-auto-review" ${ai.autoReview !== false ? 'checked' : ''}><span><strong>自动审校</strong><br><span class="helper">关闭后服务端会记录明确 skipped 事件。</span></span></label>
          <div class="ws-divider" style="margin:14px 0"></div>
          <div class="section-title" id="toggle-ai-backup" style="cursor:pointer"><div><h3 style="font-size:16px;margin:0">备用模型配置</h3><p class="helper">当主模型连续失败时自动切换到备用模型</p></div><button class="ws-collapse-btn" type="button" aria-label="展开备用配置">${state.aiBackupExpanded ? '‹' : '›'}</button></div>
          ${state.aiBackupExpanded ? `<div class="setting-group" style="margin-top:10px">
            <div class="field"><label for="ai-backup-base-url">备用 Base URL</label><input class="input" id="ai-backup-base-url" value="${escapeHtml(backup.baseUrl || '')}" placeholder="例如：https://backup.example.com/v1"></div>
            <div class="field"><label for="ai-backup-key">备用 API Key ${backup.apiKeyHint ? `（已保存 ${escapeHtml(backup.apiKeyHint)}）` : ''}</label><input class="input" type="password" id="ai-backup-key" value="${escapeHtml(backup.apiKey || '')}" placeholder="留空表示保持原值"></div>
            <div class="field"><label for="ai-backup-model">备用模型名称</label><input class="input" id="ai-backup-model" value="${escapeHtml(backup.model || '')}"></div>
          </div>` : ''}
          <div class="top-actions"><button class="btn btn-primary" type="submit">保存设置</button><button class="btn btn-secondary" type="button" id="verify-ai">验证真实连接</button></div>
        </form>
      </section>
    </div>`;
}

function renderDataManagement() {
  return `<section class="card card-pad" style="margin-top:24px">
      <div class="section-title"><div><h3>数据管理</h3><p>导出全部文章、版本历史、任务日志和通用设置为 JSON 备份文件；从备份文件恢复数据。</p></div></div>
      <div class="data-mgmt-grid">
        <div class="data-mgmt-block">
          <div class="data-mgmt-label">
            <strong>导出数据</strong>
            <span class="helper">包含全部项目、版本历史、任务日志、来源快照、发布回执和通用设置（不含 API 密钥等敏感信息）。</span>
          </div>
          <button class="btn btn-secondary" id="data-export-btn" type="button">导出备份</button>
        </div>
        <div class="data-mgmt-block">
          <div class="data-mgmt-label">
            <strong>导入数据</strong>
            <span class="helper">从备份文件恢复数据。合并模式跳过已存在的项目，覆盖模式替换同名项目。</span>
          </div>
          <div class="data-mgmt-import-row">
            <label class="btn btn-secondary file-upload-label" for="data-import-file">选择文件</label>
            <input type="file" id="data-import-file" accept="application/json,.json" hidden>
            <span id="data-import-filename" class="helper">未选择文件</span>
          </div>
          <div class="data-mgmt-import-row" id="data-import-options" style="display:none">
            <select class="input" id="data-import-mode" style="width:auto">
              <option value="merge" selected>合并模式（跳过已存在）</option>
              <option value="replace">覆盖模式（替换同名项目）</option>
            </select>
            <button class="btn btn-primary" id="data-import-btn" type="button">开始导入</button>
          </div>
        </div>
      </div>
    </section>`;
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
    <div class="page-head"><div><h2>设置</h2><p>管理创作策略、发布账号与数据备份。所有输入均由服务端二次校验。</p></div></div>
    <div class="grid grid-2">
      <section class="card card-pad"><div class="section-title"><div><h3>创作策略</h3><p>强制引用模式要求可核验网页来源和段落引用标记。</p></div></div>
        <form id="general-form" class="setting-group">
          <div class="field"><label for="default-length">默认字数</label><input class="input" id="default-length" type="number" min="300" max="20000" value="${escapeHtml(general.defaultLength || 1800)}"></div>
          <label class="checkline"><input type="checkbox" id="strict-facts" ${general.strictFacts ? 'checked' : ''}><span><strong>强制引用模式</strong><br><span class="helper">仅允许使用已抓取来源中的信息，并要求 [来源N] 标记；纯主题任务会暂停。这不等同于第三方事实核查。</span></span></label>
          <label class="checkline"><input type="checkbox" id="allow-network" ${general.allowNetwork !== false ? 'checked' : ''}><span><strong>允许联网</strong><br><span class="helper">关闭后来源刷新和外部发布会被阻止。</span></span></label>
          <button class="btn btn-primary" type="submit">保存通用设置</button>
        </form>
      </section>
      <section class="card"><div class="section-title wechat-header" id="toggle-wechat"><div><h3>微信公众号</h3><p>${escapeHtml(wechat.accountName || '未配置')} · ${escapeHtml(health.message || (health.reachable ? '已验证' : '未验证'))}</p></div>${statusPill(health.reachable ? 'succeeded' : 'blocked')}<button class="ws-collapse-btn" aria-label="展开配置">${state.wechatConfigExpanded ? '‹' : '›'}</button></div>
        ${state.wechatConfigExpanded ? `<div class="card-pad"><form id="wechat-form" class="setting-group">
          <div class="field"><label for="wechat-name">公众号名称</label><input class="input" id="wechat-name" maxlength="120" value="${escapeHtml(wechat.accountName || '')}"></div>
          <div class="field"><label for="wechat-appid">AppID</label><input class="input" id="wechat-appid" maxlength="128" value="${escapeHtml(wechat.appId || '')}"></div>
          <div class="field"><label for="wechat-secret">AppSecret ${appSecretHint ? `（已保存 ${escapeHtml(appSecretHint)}）` : ''}</label><input class="input" type="password" id="wechat-secret" value="${escapeHtml(wechat.appSecret || '')}" placeholder="留空保持原值"></div>
          <div class="field"><label for="wechat-thumb">默认封面 Media ID（未上传本地封面时使用）</label><input class="input" id="wechat-thumb" maxlength="256" value="${escapeHtml(wechat.thumbMediaId || '')}"></div>
          <button class="btn btn-primary" type="submit">验证并保存</button>
        </form></div>` : ''}
      </section>
    </div>
    ${renderDataManagement()}`;
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
      <div class="log-viewer" data-preserve="true" data-preserve-key="log-viewer" data-preserve-rev="${state.logsRev}">${renderLogRows()}</div>
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
    state.logsRev = (state.logsRev || 0) + 1; // P1: 日志刷新后递增版本号，使下次全量渲染不再回退旧节点
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
  if (viewer && viewer.dataset.bound !== '1') {
    // P1: 保留的日志节点已有委托监听器，避免重复绑定
    viewer.dataset.bound = '1';
    viewer.addEventListener('click', (e) => {
      const row = e.target.closest('.log-row-clickable');
      if (!row) return;
      const idx = parseInt(row.dataset.logIndex, 10);
      const log = (state.logs || [])[idx];
      if (log) openLogModal(log);
    });
  }
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
      <section class="card card-pad">${selected ? (() => {
        const allEvents = selected.events || [];
        const errorEvents = allEvents.filter((e) => e.level === 'error');
        const recentEvents = allEvents.slice(-5);
        const visibleEvents = state.taskEventsExpanded ? allEvents : [...new Set([...errorEvents, ...recentEvents])];
        const eventList = visibleEvents.map((event) => `<div class="task-event ${escapeHtml(event.level)}"><strong>${formatTime(event.createdAt)} · ${escapeHtml(event.step)}${event.detail?.skipped ? ' · 已跳过' : ''}</strong><p>${escapeHtml(event.message)}</p></div>`).join('');
        const eventToggle = allEvents.length > 5 ? `<button class="btn btn-ghost ws-mini-btn" id="toggle-task-events" style="margin-top:10px">${state.taskEventsExpanded ? '只显示最近 5 条' : `显示全部 ${allEvents.length} 条事件`}</button>` : '';
        return `<div class="stack"><div class="section-title"><div><h3>${escapeHtml(selected.projectTitle || '任务详情')}</h3><p>${escapeHtml(selected.id)}</p></div>${statusPill(selected.status)}</div>${selected.errorCode ? `<div class="alert error"><strong>${escapeHtml(selected.errorCode)}</strong><br>${escapeHtml(selected.errorDetail || selected.message)}</div>` : ''}${timelineHtml(selected)}<div>${eventList}${eventToggle}</div><div class="top-actions">${['queued', 'running'].includes(selected.status) ? '<button class="btn btn-danger" id="diag-cancel">取消</button>' : ''}${['failed', 'blocked', 'timeout', 'cancelled'].includes(selected.status) ? '<select id="diag-retry-mode"><option value="review_only">仅审校</option><option value="preserve_body">保留正文</option><option value="from_outline">从框架重做</option><option value="full">全部重做</option></select><button class="btn btn-secondary" id="diag-retry">重试</button>' : ''}${selected.projectId ? '<button class="btn btn-primary" id="diag-open-project">打开文章</button>' : ''}</div></div>`;
      })() : '<div class="empty">从左侧选择任务查看详情</div>'}</section>
    </div>`;
}

// ---------------------------------------------------------------------------
// 认证页面：登录 & 修改密码
// ---------------------------------------------------------------------------

function renderLogin() {
  return `
    <div class="auth-page">
      <div class="auth-card">
        <div class="auth-brand">
          <div class="auth-logo">✦</div>
          <h1>公众号 AI Studio</h1>
          <p>AI 原生内容工作台</p>
        </div>
        <form id="login-form" class="auth-form" autocomplete="off">
          <div class="auth-field">
            <label for="login-username">用户名</label>
            <input type="text" id="login-username" name="username" required autocomplete="username"
                   placeholder="请输入用户名" value="admin" />
          </div>
          <div class="auth-field">
            <label for="login-password">密码</label>
            <input type="password" id="login-password" name="password" required autocomplete="current-password"
                   placeholder="请输入密码" />
          </div>
          <button type="submit" class="btn btn-primary auth-submit" id="login-button">登录</button>
        </form>
        <div class="auth-hint" id="login-hint"></div>
      </div>
    </div>`;
}

function bindLogin() {
  const form = document.getElementById('login-form');
  const button = document.getElementById('login-button');
  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    const hint = document.getElementById('login-hint');
    if (!username || !password) {
      hint.textContent = '请输入用户名和密码';
      hint.className = 'auth-hint error';
      return;
    }
    button.disabled = true;
    button.textContent = '登录中…';
    hint.textContent = '';
    hint.className = 'auth-hint';
    try {
      const result = await api('/api/v2/auth/login', {
        method: 'POST',
        body: { username, password },
      });
      state.auth = {
        authenticated: true,
        mustChangePassword: !!result.mustChangePassword,
        username: result.username || username,
        csrfToken: result.csrfToken || '',
      };
      if (state.auth.mustChangePassword) {
        render();
      } else {
        await bootstrap();
      }
    } catch (error) {
      hint.textContent = error.message || '登录失败';
      hint.className = 'auth-hint error';
      button.disabled = false;
      button.textContent = '登录';
    }
  });
}

function renderChangePassword() {
  return `
    <div class="auth-page">
      <div class="auth-card">
        <div class="auth-brand">
          <div class="auth-logo">🔐</div>
          <h1>修改初始密码</h1>
          <p>首次登录需要修改密码后才能使用系统</p>
        </div>
        <form id="change-password-form" class="auth-form" autocomplete="off">
          <div class="auth-field">
            <label for="cp-old">当前密码</label>
            <input type="password" id="cp-old" name="oldPassword" required autocomplete="current-password"
                   placeholder="请输入当前密码" />
          </div>
          <div class="auth-field">
            <label for="cp-new">新密码</label>
            <input type="password" id="cp-new" name="newPassword" required autocomplete="new-password"
                   placeholder="至少 8 位，含大小写字母和数字" />
          </div>
          <div class="auth-field">
            <label for="cp-confirm">确认新密码</label>
            <input type="password" id="cp-confirm" name="confirmPassword" required autocomplete="new-password"
                   placeholder="请再次输入新密码" />
          </div>
          <button type="submit" class="btn btn-primary auth-submit" id="cp-button">修改密码</button>
        </form>
        <div class="auth-hint" id="cp-hint"></div>
        <div class="auth-rules">
          <p>密码要求：</p>
          <ul>
            <li>长度至少 8 位</li>
            <li>必须包含大写字母、小写字母和数字</li>
            <li>不能与当前密码相同</li>
          </ul>
        </div>
      </div>
    </div>`;
}

function bindChangePassword() {
  const form = document.getElementById('change-password-form');
  const button = document.getElementById('cp-button');
  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const oldPassword = document.getElementById('cp-old').value;
    const newPassword = document.getElementById('cp-new').value;
    const confirmPassword = document.getElementById('cp-confirm').value;
    const hint = document.getElementById('cp-hint');
    if (newPassword !== confirmPassword) {
      hint.textContent = '两次输入的新密码不一致';
      hint.className = 'auth-hint error';
      return;
    }
    button.disabled = true;
    button.textContent = '修改中…';
    hint.textContent = '';
    hint.className = 'auth-hint';
    try {
      await api('/api/v2/auth/change-password', {
        method: 'POST',
        body: { oldPassword, newPassword, confirmPassword },
      });
      state.auth.mustChangePassword = false;
      toast('密码修改成功，正在加载工作台…', 'success');
      await bootstrap();
    } catch (error) {
      hint.textContent = error.message || '密码修改失败';
      hint.className = 'auth-hint error';
      button.disabled = false;
      button.textContent = '修改密码';
    }
  });
}

async function handleLogout() {
  try {
    await api('/api/v2/auth/logout', { method: 'POST', body: {} });
  } catch {
    // 忽略登出请求失败
  }
  state.auth = { authenticated: false, mustChangePassword: false, username: '', csrfToken: '' };
  state.projects = [];
  state.tasks = [];
  state.currentProject = null;
  state.currentTask = null;
  clearTimeout(state.pollTimer);
  _stopSSE(); // P2: 登出时关闭 SSE
  render();
}

// ---------------------------------------------------------------------------
// P1: 渲染批处理 —— 多次调用合并到同一帧执行，避免 innerHTML 全量重渲染抖动
// ---------------------------------------------------------------------------
function _scheduleRender() {
  if (_pendingRender) cancelAnimationFrame(_pendingRender);
  _pendingRender = requestAnimationFrame(() => {
    _pendingRender = null;
    render();
  });
}

// ---------------------------------------------------------------------------
// P2: SSE 实时推送客户端
// ---------------------------------------------------------------------------
function _startSSE() {
  if (state.sse) return;
  if (typeof window.EventSource !== 'function') return; // 浏览器不支持，回退轮询
  try {
    // EventSource 会自动携带同源会话 Cookie。不将 CSRF token 写入 URL，
    // 避免凭据进入访问日志、历史记录或代理层。
    const es = new EventSource('/api/v2/tasks/events/stream');
    state.sse = es;
    es.onopen = () => {
      // #077: 连接成功后重置重连计数
      if (state.sseRetryCount > 0) {
        toast('实时连接已恢复', 'success');
      }
      state.sseRetryCount = 0;
      if (state.sseRetryTimer) { clearTimeout(state.sseRetryTimer); state.sseRetryTimer = null; }
    };
    es.onmessage = (evt) => {
      let data;
      try { data = JSON.parse(evt.data); } catch { return; }
      _handleSSEEvent(data);
    };
    es.onerror = () => {
      // #077: 指数退避自动重连 — 1s, 2s, 4s, 8s, 16s, 最大 30s
      _stopSSE();
      if (!state.pollTimer) startPolling();
      // #088: 5 次重连失败后通知用户
      if (state.sseRetryCount >= 5) {
        _notifySSEFailure();
        return;
      }
      const delay = Math.min(1000 * Math.pow(2, state.sseRetryCount), 30000);
      state.sseRetryCount++;
      state.sseRetryTimer = setTimeout(() => {
        state.sseRetryTimer = null;
        _startSSE();
      }, delay);
    };
  } catch {
    state.sse = null;
  }
}

function _stopSSE() {
  if (state.sseRetryTimer) { clearTimeout(state.sseRetryTimer); state.sseRetryTimer = null; }
  if (state.sse) {
    try { state.sse.close(); } catch { /* noop */ }
    state.sse = null;
  }
}

function _handleSSEEvent(data) {
  if (!data || !data.taskId) return; // 心跳或无关事件
  // #034: SSE 事件去重 — 通过 eventId 防止重复处理
  if (_isDuplicateSSEEvent(data.eventId || data._id)) return;
  const idx = state.tasks.findIndex((t) => t.id === data.taskId);
  if (idx >= 0) state.tasks[idx] = { ...state.tasks[idx], ...data };
  if (state.currentTask?.id === data.taskId) {
    state.currentTask = { ...state.currentTask, ...data };
  }
  const { path } = routeInfo();
  if (path === 'workspace' || path === 'tasks') {
    updateTaskProgressOnly();
    _scheduleRender();
  }
  // 任务进入终态后，若无活跃任务则关闭 SSE
  const terminal = ['succeeded', 'failed', 'cancelled', 'timeout', 'blocked'];
  if (terminal.includes(data.status)) {
    const { path, params } = routeInfo();
    if (path === 'workspace' && params.get('task') === data.taskId) {
      loadRouteData().catch((error) => toast(error.message, 'error'));
    }
    if (!state.tasks.some((t) => ['queued', 'running'].includes(t.status))) _stopSSE();
  }
}

// ---------------------------------------------------------------------------
// U1: 行级 Diff 工具
// ---------------------------------------------------------------------------
// 计算行级 LCS diff，返回段数组：{type:'same'|'add'|'del', text}
function _computeLineDiff(oldText, newText) {
  const a = String(oldText ?? '').split('\n');
  const b = String(newText ?? '').split('\n');
  // 限制规模，避免超大文本冻结 UI
  const MAX = 2000;
  const aTrim = a.length > MAX ? a.slice(0, MAX) : a;
  const bTrim = b.length > MAX ? b.slice(0, MAX) : b;
  const n = aTrim.length, m = bTrim.length;
  // #103: 使用 Int32Array 替代普通数组减少内存占用（4 bytes vs 8 bytes per element）
  // 并使用滚动数组优化空间至 O(2 * min(n,m))
  if (n === 0) return bTrim.map(text => ({ type: 'add', text }));
  if (m === 0) return aTrim.map(text => ({ type: 'del', text }));
  // 确保 n >= m 以最小化滚动数组宽度
  let aa = aTrim, bb = bTrim;
  let nn = n, mm = m;
  let swapped = false;
  if (n < m) { [aa, bb] = [bTrim, aTrim]; [nn, mm] = [m, n]; swapped = true; }
  // 使用两行滚动数组
  let prev = new Int32Array(mm + 1);
  let curr = new Int32Array(mm + 1);
  // 需要完整 DP 表来回溯，但对小规模文本直接用 Map 存储
  // 对于大规模文本（>500行），使用分块策略
  if (nn * mm > 500000) {
    // 大文本降级：简单的行对行匹配
    const segs = [];
    const setA = new Set(aa);
    const setB = new Set(bb);
    let i = 0, j = 0;
    while (i < nn || j < mm) {
      if (i < nn && j < mm && aa[i] === bb[j]) {
        segs.push({ type: 'same', text: aa[i] }); i++; j++;
      } else if (i < nn && !setB.has(aa[i])) {
        segs.push({ type: 'del', text: aa[i] }); i++;
      } else if (j < mm && !setA.has(bb[j])) {
        segs.push({ type: 'add', text: bb[j] }); j++;
      } else if (i < nn) {
        segs.push({ type: 'del', text: aa[i] }); i++;
      } else if (j < mm) {
        segs.push({ type: 'add', text: bb[j] }); j++;
      }
    }
    return swapped ? segs.map(s => s.type === 'add' ? { type: 'del', text: s.text } : s.type === 'del' ? { type: 'add', text: s.text } : s) : segs;
  }
  // 小规模文本使用完整 DP
  const dp = Array.from({ length: nn + 1 }, () => new Int32Array(mm + 1));
  for (let i = nn - 1; i >= 0; i--) {
    for (let j = mm - 1; j >= 0; j--) {
      dp[i][j] = aa[i] === bb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const segs = [];
  let i = 0, j = 0;
  while (i < nn && j < mm) {
    if (aa[i] === bb[j]) { segs.push({ type: 'same', text: aa[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { segs.push({ type: 'del', text: aa[i] }); i++; }
    else { segs.push({ type: 'add', text: bb[j] }); j++; }
  }
  while (i < nn) { segs.push({ type: 'del', text: aa[i] }); i++; }
  while (j < mm) { segs.push({ type: 'add', text: bb[j] }); j++; }
  return swapped ? segs.map(s => s.type === 'add' ? { type: 'del', text: s.text } : s.type === 'del' ? { type: 'add', text: s.text } : s) : segs;
}

// 返回统一 diff HTML：相同行灰色 / 新增行绿色 / 删除行红色
function _lineDiff(oldText, newText) {
  const segs = _computeLineDiff(oldText, newText);
  const color = { same: '#7a8a8c', add: '#1a6b3a', del: '#8a2a2a' };
  const bg = { same: 'transparent', add: '#e6f9ec', del: '#fdecea' };
  return segs.map((s) => {
    const prefix = s.type === 'add' ? '+ ' : s.type === 'del' ? '- ' : '  ';
    return `<div style="background:${bg[s.type]};color:${color[s.type]};padding:2px 8px;white-space:pre-wrap;word-break:break-word;border-radius:4px">${escapeHtml(prefix + s.text)}</div>`;
  }).join('');
}

// 返回 side-by-side HTML 字符串：左列旧版本（删除高亮），右列新版本（新增高亮）
function _sideBySideDiffHtml(oldText, newText) {
  const segs = _computeLineDiff(oldText, newText);
  const left = [];
  const right = [];
  for (const s of segs) {
    if (s.type === 'same') {
      left.push(`<div style="color:#7a8a8c;padding:2px 8px;white-space:pre-wrap;word-break:break-word">${escapeHtml('  ' + s.text)}</div>`);
      right.push(`<div style="color:#7a8a8c;padding:2px 8px;white-space:pre-wrap;word-break:break-word">${escapeHtml('  ' + s.text)}</div>`);
    } else if (s.type === 'del') {
      left.push(`<div style="background:#fdecea;color:#8a2a2a;padding:2px 8px;white-space:pre-wrap;word-break:break-word">${escapeHtml('- ' + s.text)}</div>`);
      right.push(`<div style="padding:2px 8px;white-space:pre-wrap;word-break:break-word">&nbsp;</div>`);
    } else {
      left.push(`<div style="padding:2px 8px;white-space:pre-wrap;word-break:break-word">&nbsp;</div>`);
      right.push(`<div style="background:#e6f9ec;color:#1a6b3a;padding:2px 8px;white-space:pre-wrap;word-break:break-word">${escapeHtml('+ ' + s.text)}</div>`);
    }
  }
  const paneStyle = 'flex:1;min-width:0;font-family:"SFMono-Regular",Consolas,monospace;font-size:13px;line-height:1.6;border:1px solid #2e383b;border-radius:8px;overflow:auto;max-height:55vh;background:#151a1d;color:#d4e4e6';
  return `<div style="display:flex;gap:12px">
    <div style="${paneStyle}"><div style="padding:8px 10px;border-bottom:1px solid #2e383b;color:#8a9b9d;font-weight:700">旧版本</div>${left.join('')}</div>
    <div style="${paneStyle}"><div style="padding:8px 10px;border-bottom:1px solid #2e383b;color:#8a9b9d;font-weight:700">当前版本</div>${right.join('')}</div>
  </div>`;
}

// ---------------------------------------------------------------------------
// U2: 段落级三方合并工具
// ---------------------------------------------------------------------------
// 按双换行拆分段落，返回段落对比结构供预览使用
function _buildMergeSegments(serverText, localText) {
  const serverParas = String(serverText ?? '').split(/\n\s*\n/);
  const localParas = String(localText ?? '').split(/\n\s*\n/);
  const n = serverParas.length, m = localParas.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = serverParas[i] === localParas[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const segs = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (serverParas[i] === localParas[j]) {
      segs.push({ type: 'same', serverText: serverParas[i], localText: localParas[j], keep: true, choice: 'both' });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      segs.push({ type: 'server', serverText: serverParas[i], localText: '', keep: true, choice: 'server' });
      i++;
    } else {
      segs.push({ type: 'local', serverText: '', localText: localParas[j], keep: true, choice: 'local' });
      j++;
    }
  }
  while (i < n) { segs.push({ type: 'server', serverText: serverParas[i++], localText: '', keep: true, choice: 'server' }); }
  while (j < m) { segs.push({ type: 'local', serverText: '', localText: localParas[j++], keep: true, choice: 'local' }); }
  // 把「两端都有但不同」的相邻 server+local 段合并为 conflict 段（便于用户选择）
  const merged = [];
  for (let k = 0; k < segs.length; k++) {
    const s = segs[k];
    if (s.type === 'server' && k + 1 < segs.length && segs[k + 1].type === 'local') {
      merged.push({ type: 'conflict', serverText: s.serverText, localText: segs[k + 1].localText, keep: true, choice: 'both' });
      k++;
    } else {
      merged.push(s);
    }
  }
  return merged;
}

function _applyMergeSegments(segments) {
  const out = [];
  for (const s of segments) {
    if (!s.keep) continue;
    if (s.type === 'same') out.push(s.serverText || s.localText);
    else if (s.type === 'server') out.push(s.serverText);
    else if (s.type === 'local') out.push(s.localText);
    else if (s.type === 'conflict') {
      if (s.choice === 'server') out.push(s.serverText);
      else if (s.choice === 'local') out.push(s.localText);
      else { out.push(s.serverText); out.push(s.localText); } // both
    }
  }
  return out.join('\n\n');
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
  // 未认证 → 显示登录页
  if (!state.auth.authenticated) {
    app.innerHTML = renderLogin();
    bindLogin();
    return;
  }
  // 首次登录需修改密码
  if (state.auth.mustChangePassword) {
    app.innerHTML = renderChangePassword();
    bindChangePassword();
    return;
  }
  const { path } = routeInfo();
  // P1: 重新渲染前捕获带 data-preserve 标记的区域（编辑器/日志），
  // 避免全量 innerHTML 重置丢失用户正在输入的内容、光标位置与滚动状态。
  const preserved = {};
  document.querySelectorAll('[data-preserve="true"]').forEach((el) => {
    const key = el.getAttribute('data-preserve-key');
    if (!key) return;
    preserved[key] = {
      node: el,
      scrollTop: el.scrollTop || 0,
      scrollLeft: el.scrollLeft || 0,
      selectionStart: el.selectionStart,
      selectionEnd: el.selectionEnd,
      focused: document.activeElement === el,
      rev: el.getAttribute('data-preserve-rev') || '',
    };
  });
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
  // P1: 还原被保留的区域 —— 仅当新 DOM 中存在同 key 占位且 revision 一致时替换，
  // 内容已变化（保存后 revision 变更、日志已刷新等）时不回退到旧节点，保证数据正确。
  // #033: 额外检查父容器结构是否一致，避免模式切换后旧节点不适配新布局
  Object.keys(preserved).forEach((key) => {
    const placeholder = document.querySelector(`[data-preserve-key="${key}"]`);
    if (!placeholder) return;
    const saved = preserved[key];
    const newRev = placeholder.getAttribute('data-preserve-rev') || '';
    if (newRev && saved.rev && newRev !== saved.rev) return;
    // #033: 检查父容器 class 是否一致（如从编辑模式切换到分屏模式，父容器结构不同）
    const savedParentClass = saved.node.parentElement?.className || '';
    const newParentClass = placeholder.parentElement?.className || '';
    if (savedParentClass && newParentClass && savedParentClass !== newParentClass) return;
    // #033: 检查标签名一致（避免 div 替换 textarea 等不匹配情况）
    if (saved.node.tagName !== placeholder.tagName) return;
    // #033: 检查 data-field 一致（避免不同字段的节点互相替换）
    const savedField = saved.node.getAttribute('data-field') || '';
    const newField = placeholder.getAttribute('data-field') || '';
    if (savedField && newField && savedField !== newField) return;
    placeholder.replaceWith(saved.node);
    try {
      saved.node.scrollTop = saved.scrollTop;
      saved.node.scrollLeft = saved.scrollLeft;
      if (typeof saved.selectionStart === 'number' && saved.selectionStart != null) {
        saved.node.setSelectionRange(saved.selectionStart, saved.selectionEnd);
      }
    } catch { /* noop */ }
    if (saved.focused) {
      try { saved.node.focus(); } catch { /* noop */ }
    }
  });
  // #069: 动态更新浏览器标题
  if (path === 'workspace' && state.currentProject?.title) {
    document.title = `${state.currentProject.title} - 公众号 AI Studio`;
  } else {
    const route = ROUTES[path];
    document.title = route ? `${route.title} - 公众号 AI Studio` : '公众号 AI Studio';
  }
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
  document.getElementById('sidebar-logout')?.addEventListener('click', handleLogout);
  // #117: 暗色模式切换
  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    state.darkMode = !state.darkMode;
    document.documentElement.setAttribute('data-theme', state.darkMode ? 'dark' : 'light');
    try { localStorage.setItem('studio-dark-mode', state.darkMode ? '1' : '0'); } catch { /* noop */ }
    render();
  });
  // #091: 会话续期
  document.getElementById('session-renew')?.addEventListener('click', async () => {
    try {
      await api('/api/v2/auth/session', { method: 'POST' });
      state.sessionWarningShown = false;
      toast('会话已续期', 'success');
      render();
    } catch (error) {
      toast('会话续期失败，请重新登录', 'error');
    }
  });
}

function bindCreate() {
  // U3: 预览来源 —— 先调用预览 API 展示标题/作者/摘要，失败仍允许继续提交
  document.getElementById('preview-source-btn')?.addEventListener('click', async () => {
    const input = document.getElementById('source-input');
    const url = (input?.value || '').trim();
    if (!url) { toast('请先输入来源链接', 'error'); input?.focus(); return; }
    if (!/^https?:\/\//i.test(url)) { toast('预览来源仅支持 http(s) 链接；主题创作可直接提交', 'error'); return; }
    const btn = document.getElementById('preview-source-btn');
    if (btn) { btn.disabled = true; btn.textContent = '预览中…'; }
    state.sourcePreviewLoading = true;
    state.sourcePreview = null;
    render();
    try {
      const result = await api('/api/v2/source/preview', { method: 'POST', body: { url } });
      state.sourcePreview = result;
    } catch (error) {
      state.sourcePreview = { error: error.message || '来源预览失败' };
    } finally {
      state.sourcePreviewLoading = false;
      render();
    }
  });
  document.getElementById('create-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = document.getElementById('source-input');
    const button = document.getElementById('create-button');
    const sourceInput = input.value.trim();
    if (!sourceInput) {
      toast('请输入来源链接或创作目标', 'error');
      input.focus();
      return;
    }
    const requirements = (document.getElementById('requirements-input')?.value || '').trim();
    button.disabled = true;
    button.textContent = '正在创建…';
    try {
      const result = await api('/api/v2/workflows', {
        method: 'POST',
        body: { sourceInput, autoReview: document.getElementById('create-auto-review').checked, requirements },
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
      if (error.code === 'network_error') {
        toast('本地服务不可用，请确认服务已启动后重试。输入内容已保留。', 'error');
      } else {
        toast(error.message || '创建失败，请检查输入后重试。输入内容已保留。', 'error');
      }
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
  // #009: 输入时写入 localStorage 草稿
  _saveDraftToLocal();
  updateSaveBadge();
}

function updateSaveBadge() {
  // #102: 使用定向 DOM 更新替代全量 re-render
  if (_targetedUpdate('saveState', state.saveState)) return;
  const labels = { idle: '已保存', saving: '保存中…', saved: '已保存', error: '保存失败' };
  const text = labels[state.saveState] || '已保存';
  const cls = `pill ${state.saveState === 'saved' ? 'success' : state.saveState === 'error' ? 'danger' : state.saveState === 'saving' ? 'running' : ''}`;
  document.querySelectorAll('.save-state-badge').forEach((badge) => {
    badge.textContent = text;
    badge.className = `${cls} save-state-badge`;
  });
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
    // #031: 保存链超时保护 — 最大 10 次迭代，单次 30s 超时
    let chainIterations = 0;
    const chainStartTime = Date.now();
    while (state.pendingSaves.has(projectId)) {
      if (++chainIterations > 10) {
        state.saveState = 'error';
        toast('保存操作过多，请稍后重试', 'error');
        updateSaveBadge();
        break;
      }
      if (Date.now() - chainStartTime > 30000) {
        state.saveState = 'error';
        toast('保存超时，请检查网络后重试', 'error');
        updateSaveBadge();
        break;
      }
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
          timeout: 30000,
        });
        // #039: 保存成功后直接使用服务端返回数据，不做 optimistic 合并
        const newPending = state.pendingSaves.get(projectId);
        if (newPending) {
          // 有新的 pending 字段 → 合并到服务端数据后触发下一轮保存
          setProjectInState({ ...updated, ...newPending });
        } else {
          setProjectInState(updated);
        }
        state.saveState = 'saved';
        state.preview = null;
        // #009: 保存成功后清除 localStorage 草稿
        _clearDraftFromLocal(projectId);
        // #029: 保存成功后重置重试计数
        state.saveRetryCount = 0;
        if (state.saveRetryTimer) { clearTimeout(state.saveRetryTimer); state.saveRetryTimer = null; }
      } catch (error) {
        state.saveState = 'error';
        state.pendingSaves.set(projectId, { ...fields, ...(state.pendingSaves.get(projectId) || {}) });
        if (error.code === 'revision_conflict' && error.detail?.server) {
          const allPending = state.pendingSaves.get(projectId) || {};
          state.conflict = { server: error.detail.server, pendingFields: allPending, projectId };
          state.mergePreview = null; // U2: 新冲突出现时清理旧合并预览
          state.currentProject = { ...state.currentProject, ...allPending };
          // #085: 冲突解决后数据完整性校验
          const validation = _validateConflictResolution(error.detail.server, allPending);
          if (!validation.valid) {
            console.warn('[冲突校验] 数据完整性问题:', validation.issues);
            toast(`冲突数据校验: ${validation.issues.join('；')}`, 'warning');
          }
          render();
        } else if (error.code === 'network_error' || error.code === 'timeout') {
          // #029: 网络错误/超时时自动指数退避重试 — 2s/4s/8s/16s，最多 4 次
          if (state.saveRetryCount < 4) {
            const delay = Math.min(2000 * Math.pow(2, state.saveRetryCount), 16000);
            state.saveRetryCount++;
            toast(`保存失败，${delay / 1000}s 后自动重试（第 ${state.saveRetryCount}/4 次）`, '');
            updateSaveBadge();
            state.saveRetryTimer = setTimeout(() => {
              state.saveRetryTimer = null;
              flushProjectSave(projectId);
            }, delay);
          } else {
            toast('自动重试已用完，请手动点击"重试保存"', 'error');
            updateSaveBadge();
          }
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
  state.previewLoading = true;
  if (shouldRender) render();
  try {
    state.preview = await api(`/api/v2/projects/${encodeURIComponent(project.id)}/preview`);
    state.previewLoading = false;
    if (shouldRender) render();
    else {
      // #102: 使用定向 DOM 更新替代手动 DOM 操作
      if (!_targetedUpdate('previewHtml', state.preview.html)) {
        const preview = document.getElementById('publish-preview');
        if (preview) preview.innerHTML = _sanitizePreviewHtml(state.preview.html);
      }
    }
  } catch (error) {
    state.previewLoading = false;
    // #042: API 不可用时使用客户端 Markdown 渲染作为回退
    if (error.code === 'network_error' || error.code === 'timeout') {
      const clientHtml = _renderMarkdownClientSide(project.bodyMarkdown);
      state.preview = { html: clientHtml, revision: project.revision };
      if (shouldRender) render();
      else _targetedUpdate('previewHtml', clientHtml);
      toast('网络不可用，已使用本地预览', '');
    } else if (shouldRender) { toast(error.message, 'error'); render(); }
  }
}

function bindWorkspace() {
  const boundProjectId=state.currentProject?.id;
  // #066: Tab 切换记忆滚动位置
  if (!state.tabScrollPositions) state.tabScrollPositions = {};
  document.querySelectorAll('[data-ws-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      // 保存当前 tab 的滚动位置
      const center = document.querySelector('.ws-col-center');
      if (center) state.tabScrollPositions[state.wsTab] = center.scrollTop;
      state.wsTab = btn.dataset.wsTab;
      render();
      // 恢复目标 tab 的滚动位置
      requestAnimationFrame(() => {
        const newCenter = document.querySelector('.ws-col-center');
        if (newCenter && state.tabScrollPositions[state.wsTab]) {
          newCenter.scrollTop = state.tabScrollPositions[state.wsTab];
        }
      });
    });
    // #083: ARIA tablist 方向键导航
    btn.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const tabs = Array.from(document.querySelectorAll('[data-ws-tab]'));
      const idx = tabs.indexOf(btn);
      const next = e.key === 'ArrowRight' ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
      tabs[next]?.focus();
      tabs[next]?.click();
    });
  });
  document.getElementById('collapse-left')?.addEventListener('click', () => {
    state.wsLeftCollapsed = true; render();
  });
  document.getElementById('expand-left')?.addEventListener('click', () => {
    state.wsLeftCollapsed = false; render();
  });
  document.getElementById('collapse-right')?.addEventListener('click', () => {
    state.wsRightCollapsed = true; render();
  });
  document.getElementById('expand-right')?.addEventListener('click', () => {
    state.wsRightCollapsed = false; render();
  });
  document.getElementById('toggle-preview')?.addEventListener('click', () => {
    state.wsPreviewExpanded = !state.wsPreviewExpanded; render();
  });
  document.getElementById('toggle-timeline')?.addEventListener('click', () => {
    state.wsTimelineExpanded = !state.wsTimelineExpanded; render();
  });
  document.querySelectorAll('[data-source-toggle]')?.forEach((btn) => {
    btn.addEventListener('click', () => {
      const sid = btn.dataset.sourceToggle;
      // #068: 同时只允许展开一个来源快照
      if (state.expandedSources.has(sid)) {
        state.expandedSources.delete(sid);
      } else {
        state.expandedSources.clear();
        state.expandedSources.add(sid);
      }
      render();
    });
  });
  document.querySelectorAll('.autosave').forEach((element) => {
    // P1: 被 data-preserve 保留的节点已有监听器，跳过避免重复绑定
    if (element.dataset.bound === '1') return;
    element.dataset.bound = '1';
    // #092: 字数统计 debounce — 避免每次按键都执行正则匹配
    let wordCountTimer = null;
    element.addEventListener('input', () => {
      const field = element.dataset.field;
      scheduleProjectSave(boundProjectId,field,element.value);
      // #023: 实时更新标题/摘要字数计数（标题/摘要量小，无需 debounce）
      if (field === 'title' || field === 'summary') {
        const max = field === 'title' ? 120 : 300;
        const counter = element.closest('.field')?.querySelector('.char-counter');
        if (counter) {
          const len = element.value.length;
          counter.textContent = `${len}/${max}`;
          counter.className = `char-counter ${len > max * 0.9 ? 'warning' : ''}`;
        }
      }
      // #092: 正文字数统计 300ms debounce
      if (field === 'bodyMarkdown') {
        clearTimeout(wordCountTimer);
        wordCountTimer = setTimeout(() => {
          // #102: 使用定向 DOM 更新替代手动 DOM 操作
          _targetedUpdate('wordCount', element.value);
          // #049: 更新侧栏预览
          _scheduleSidebarPreviewUpdate();
          // #122: 敏感词检测
          const found = _detectSensitiveWords(element.value);
          if (found.length > 0 || state.sensitiveWordsFound.length > 0) {
            const prev = state.sensitiveWordsFound.sort().join(',');
            const curr = found.sort().join(',');
            if (prev !== curr) {
              state.sensitiveWordsFound = found;
              render();
            }
          }
        }, 300);
      }
    });
  });

  // #001/#020: 编辑器工具栏 — 在光标处插入 Markdown 语法
  document.querySelectorAll('.editor-tool-btn[data-insert]')?.forEach((btn) => {
    btn.addEventListener('mousedown', (e) => e.preventDefault()); // #020: 阻止编辑器失焦
    btn.addEventListener('click', () => {
      const editor = document.getElementById('project-body');
      if (!editor) return;
      const type = btn.dataset.insert;
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      const selected = editor.value.substring(start, end);
      const before = editor.value.substring(0, start);
      const after = editor.value.substring(end);
      let insert = '', cursorOffset = 0, selectLen = 0;
      switch (type) {
        case 'bold': insert = `**${selected || '加粗文本'}**`; selectLen = selected.length || 4; break;
        case 'italic': insert = `*${selected || '斜体文本'}*`; selectLen = selected.length || 4; break;
        case 'heading': insert = `## ${selected || '标题'}`; selectLen = selected.length || 2; break;
        case 'list': insert = `- ${selected || '列表项'}`; selectLen = selected.length || 3; break;
        case 'link': insert = `[${selected || '链接文本'}](https://)`; selectLen = selected.length || 4; break;
        case 'image': insert = `![${selected || '图片描述'}](https://)`; selectLen = selected.length || 4; break;
        case 'hr': insert = `\n---\n`; break;
        case 'quote': insert = `> ${selected || '引用文本'}`; selectLen = selected.length || 4; break;
        case 'code': insert = `\n\`\`\`\n${selected || '代码块'}\n\`\`\`\n`; selectLen = selected.length || 3; break;
      }
      editor.value = before + insert + after;
      editor.focus();
      const newStart = start + insert.length - (selected ? selected.length : 0) - (selected ? 0 : selectLen);
      editor.setSelectionRange(start + (type === 'hr' || type === 'code' ? insert.length : (selected ? 0 : (insert.length - selectLen))), start + insert.length);
      editor.dispatchEvent(new Event('input'));
    });
  });

  // #007/#010/#012: 编辑器工具栏 action 按钮 — 撤销/重做/粘贴纯文本/专注模式
  document.querySelectorAll('.editor-tool-btn[data-action]')?.forEach((btn) => {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      const editor = document.getElementById('project-body');
      if (!editor && action !== 'focus-mode') return;
      if (action === 'undo') { editor.focus(); document.execCommand('undo'); }
      else if (action === 'redo') { editor.focus(); document.execCommand('redo'); }
      else if (action === 'paste-plain') {
        // #010: 粘贴为纯文本 — 从剪贴板读取并清除格式
        navigator.clipboard?.readText().then((text) => {
          if (!text) return;
          const start = editor.selectionStart;
          const end = editor.selectionEnd;
          editor.value = editor.value.substring(0, start) + text + editor.value.substring(end);
          editor.setSelectionRange(start + text.length, start + text.length);
          editor.dispatchEvent(new Event('input'));
          toast('已粘贴为纯文本', 'success');
        }).catch(() => toast('无法读取剪贴板，请用 Ctrl+Shift+V', 'error'));
      } else if (action === 'focus-mode') {
        // #012: 专注写作模式 — 切换全屏编辑
        state.focusMode = !state.focusMode;
        document.body.classList.toggle('focus-mode-active', state.focusMode);
        render();
      } else if (action === 'find-replace') {
        // #006: 查找替换面板
        state.findReplaceOpen = !state.findReplaceOpen;
        render();
        if (state.findReplaceOpen) {
          requestAnimationFrame(() => document.getElementById('find-query')?.focus());
        }
      }
    });
  });

  // #053/#058: 重新审校按钮 — 触发 review_only 重试，带 loading 状态
  document.getElementById('re-review')?.addEventListener('click', async () => {
    if (!state.currentTask) {
      toast('当前文章没有关联任务，无法重新审校', 'error');
      return;
    }
    // #058: 按钮 loading 状态
    const btn = document.getElementById('re-review');
    if (btn) { btn.disabled = true; btn.textContent = '审校中...'; }
    try {
      await flushProjectSave(boundProjectId);
      await taskAction('retry', 'review_only');
    } catch (error) {
      toast(error.message || '重新审校失败', 'error');
    } finally {
      // 按钮会在下次 render 时恢复
      if (btn) { btn.disabled = false; btn.textContent = '重新审校'; }
    }
  });

  // #016/#017/#004: 编辑器键盘快捷键
  const editor = document.getElementById('project-body');
  if (editor && editor.dataset.keysBound !== '1') {
    editor.dataset.keysBound = '1';
    editor.addEventListener('keydown', (e) => {
      // #016: Ctrl+S 保存
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        flushProjectSave(boundProjectId).then(ok => {
          toast(ok ? '已保存' : '保存失败，请重试', ok ? 'success' : 'error');
        });
        return;
      }
      // #017: Ctrl+B 加粗 / Ctrl+I 斜体
      if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
        e.preventDefault();
        const btn = document.querySelector('.editor-tool-btn[data-insert="bold"]');
        btn?.click();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'i') {
        e.preventDefault();
        const btn = document.querySelector('.editor-tool-btn[data-insert="italic"]');
        btn?.click();
        return;
      }
      // #019: Ctrl+K 插入链接
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        const btn = document.querySelector('.editor-tool-btn[data-insert="link"]');
        btn?.click();
        return;
      }
      // #018: Ctrl+Shift+P 切换编辑/预览
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'P') {
        e.preventDefault();
        state.bodyMode = state.bodyMode === 'edit' ? 'preview' : 'edit';
        state.splitPreview = false;
        render();
        return;
      }
      // #006: Ctrl+F 查找替换
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        state.findReplaceOpen = true;
        render();
        requestAnimationFrame(() => document.getElementById('find-query')?.focus());
        return;
      }
      // #008: 自动配对补全 — () [] {} `` **
      if (!e.ctrlKey && !e.metaKey && !e.altKey) {
        const pairs = { '(': ')', '[': ']', '{': '}', '`': '`' };
        if (pairs[e.key]) {
          const start = editor.selectionStart;
          const end = editor.selectionEnd;
          const selected = editor.value.substring(start, end);
          if (selected) {
            e.preventDefault();
            editor.value = editor.value.substring(0, start) + e.key + selected + pairs[e.key] + editor.value.substring(end);
            editor.setSelectionRange(start + 1, start + 1 + selected.length);
            editor.dispatchEvent(new Event('input'));
          } else {
            e.preventDefault();
            editor.value = editor.value.substring(0, start) + e.key + pairs[e.key] + editor.value.substring(end);
            editor.setSelectionRange(start + 1, start + 1);
            editor.dispatchEvent(new Event('input'));
          }
          return;
        }
      }
      // #004: Tab 键缩进
      if (e.key === 'Tab') {
        e.preventDefault();
        const start = editor.selectionStart;
        const end = editor.selectionEnd;
        if (e.shiftKey) {
          // Shift+Tab: 删除行首缩进
          const lineStart = editor.value.lastIndexOf('\n', start - 1) + 1;
          if (editor.value.substring(lineStart, lineStart + 4) === '    ') {
            editor.value = editor.value.substring(0, lineStart) + editor.value.substring(lineStart + 4);
            editor.setSelectionRange(Math.max(lineStart, start - 4), Math.max(lineStart, end - 4));
          }
        } else {
          // Tab: 插入 4 空格
          editor.value = editor.value.substring(0, start) + '    ' + editor.value.substring(end);
          editor.setSelectionRange(start + 4, start + 4);
        }
        editor.dispatchEvent(new Event('input'));
      }
    });
  }

  const mdEditor = document.getElementById('project-body');

  // #162: 移动端虚拟键盘适配 — 使用 visualViewport API
  if (window.visualViewport) {
    _addTrackedListener(window.visualViewport, 'resize', () => {
      const keyboardOpen = window.visualViewport.height < window.innerHeight * 0.75;
      if (keyboardOpen !== state.mobileKeyboardOpen) {
        state.mobileKeyboardOpen = keyboardOpen;
        document.body.classList.toggle('mobile-keyboard-open', keyboardOpen);
        // 滚动编辑器到可见区域
        if (keyboardOpen && mdEditor) {
          requestAnimationFrame(() => {
            mdEditor.scrollIntoView({ behavior: 'smooth', block: 'center' });
          });
        }
      }
    });
  }

  // #027: 保存失败重试
  document.getElementById('save-retry')?.addEventListener('click', () => {
    flushProjectSave(boundProjectId).then(ok => {
      if (ok) toast('保存成功', 'success');
    });
  });

  // #006: 查找替换功能
  const findInput = document.getElementById('find-query');
  const replaceInput = document.getElementById('replace-query');
  const findCountEl = document.getElementById('find-count');
  function _updateFindCount() {
    const editor = document.getElementById('project-body');
    if (!editor || !state.findQuery) { state.findMatchCount = 0; state.findMatchIndex = 0; }
    else {
      const text = editor.value;
      const matches = [];
      let idx = 0;
      while ((idx = text.indexOf(state.findQuery, idx)) !== -1) { matches.push(idx); idx += state.findQuery.length; }
      state.findMatchCount = matches.length;
      state.findMatchIndex = 0;
    }
    if (findCountEl) findCountEl.textContent = `${state.findMatchCount} 个匹配`;
  }
  findInput?.addEventListener('input', () => { state.findQuery = findInput.value; _updateFindCount(); });
  replaceInput?.addEventListener('input', () => { state.replaceQuery = replaceInput.value; });
  document.getElementById('find-next')?.addEventListener('click', () => {
    const editor = document.getElementById('project-body');
    if (!editor || !state.findQuery) return;
    const text = editor.value;
    const from = editor.selectionEnd;
    let pos = text.indexOf(state.findQuery, from);
    if (pos === -1) pos = text.indexOf(state.findQuery, 0); // wrap around
    if (pos >= 0) {
      editor.focus();
      editor.setSelectionRange(pos, pos + state.findQuery.length);
      state.findMatchIndex++;
      if (findCountEl) findCountEl.textContent = `${state.findMatchCount} 个匹配 (第 ${((state.findMatchIndex - 1) % Math.max(1, state.findMatchCount)) + 1}/${state.findMatchCount})`;
    } else {
      toast('未找到匹配内容', '');
    }
  });
  document.getElementById('replace-one')?.addEventListener('click', () => {
    const editor = document.getElementById('project-body');
    if (!editor || !state.findQuery) return;
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    if (editor.value.substring(start, end) === state.findQuery) {
      editor.value = editor.value.substring(0, start) + state.replaceQuery + editor.value.substring(end);
      editor.setSelectionRange(start, start + state.replaceQuery.length);
      editor.dispatchEvent(new Event('input'));
      _updateFindCount();
    }
    document.getElementById('find-next')?.click();
  });
  document.getElementById('replace-all')?.addEventListener('click', () => {
    const editor = document.getElementById('project-body');
    if (!editor || !state.findQuery) return;
    const text = editor.value;
    const count = text.split(state.findQuery).length - 1;
    if (count === 0) { toast('未找到匹配内容', ''); return; }
    editor.value = text.split(state.findQuery).join(state.replaceQuery);
    editor.dispatchEvent(new Event('input'));
    _updateFindCount();
    toast(`已替换 ${count} 处`, 'success');
  });
  document.getElementById('find-close')?.addEventListener('click', () => {
    state.findReplaceOpen = false;
    render();
  });

  // #038: 预览空状态 — 立即保存并预览
  document.getElementById('preview-empty-save')?.addEventListener('click', async () => {
    state.previewLoading = true;
    render();
    const ok = await flushProjectSave(boundProjectId);
    if (ok) await refreshPreview(true);
    state.previewLoading = false;
    render();
  });

  // #048: 审校结果筛选
  document.querySelectorAll('[data-review-filter]')?.forEach((btn) => {
    btn.addEventListener('click', () => {
      state.reviewFilter = btn.dataset.reviewFilter;
      render();
    });
  });
  // #052: 审校结果点击定位 — 滚动到正文对应位置
  document.querySelectorAll('[data-review-idx]')?.forEach((item) => {
    item.addEventListener('click', () => {
      const idx = parseInt(item.dataset.reviewIdx, 10);
      const review = state.currentProject?.review || [];
      const reviewItem = review[idx];
      if (!reviewItem) return;
      state.locatedReviewIdx = idx;
      state.wsTab = 'write';
      state.bodyMode = 'edit';
      render();
      // 尝试在编辑器中搜索审校关键词并高亮
      requestAnimationFrame(() => {
        const editor = document.getElementById('project-body');
        if (!editor) return;
        // 使用审校消息中的关键词进行搜索
        const keywords = reviewItem.message?.match(/[\u4e00-\u9fff a-zA-Z]{2,}/g);
        if (keywords && keywords.length > 0) {
          const keyword = keywords.sort((a, b) => b.length - a.length)[0]; // 取最长的关键词
          const pos = editor.value.indexOf(keyword);
          if (pos >= 0) {
            editor.focus();
            editor.setSelectionRange(pos, pos + keyword.length);
            // 滚动到匹配位置
            const lines = editor.value.substring(0, pos).split('\n').length;
            const lineHeight = parseInt(getComputedStyle(editor).lineHeight, 10) || 24;
            editor.scrollTop = (lines - 1) * lineHeight;
          }
        }
      });
    });
  });
  // #011: 大纲导航 — 点击大纲项跳转到正文对应位置
  document.querySelectorAll('[data-outline-idx]')?.forEach((item) => {
    item.addEventListener('click', () => {
      const idx = parseInt(item.dataset.outlineIdx, 10);
      const outline = state.currentProject?.outline || [];
      const heading = outline[idx];
      if (!heading) return;
      const editor = document.getElementById('project-body');
      if (!editor) return;
      // 在正文中搜索对应的标题文本
      const pos = editor.value.indexOf(heading);
      if (pos >= 0) {
        editor.focus();
        const lineStart = editor.value.lastIndexOf('\n', pos) + 1;
        editor.setSelectionRange(lineStart, pos + heading.length);
        const lines = editor.value.substring(0, pos).split('\n').length;
        const lineHeight = parseInt(getComputedStyle(editor).lineHeight, 10) || 24;
        editor.scrollTop = (lines - 1) * lineHeight;
      } else {
        toast('未在正文中找到对应标题', '');
      }
    });
  });
  // #065: 发布确认对话框
  document.getElementById('publish-button')?.addEventListener('click', (e) => {
    if (e.currentTarget.disabled) return;
    e.preventDefault();
    state.publishConfirmOpen = true;
    render();
  });
  document.getElementById('publish-confirm-cancel')?.addEventListener('click', () => {
    state.publishConfirmOpen = false;
    render();
  });
  document.getElementById('publish-confirm-ok')?.addEventListener('click', async () => {
    state.publishConfirmOpen = false;
    state.publishLoading = true;
    render();
    await publishProject({ currentTarget: document.getElementById('publish-button') });
    state.publishLoading = false;
  });
  // #006: 表格插入按钮
  document.querySelectorAll('.editor-tool-btn[data-action="table"]')?.forEach((btn) => {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
    btn.addEventListener('click', () => {
      const editor = document.getElementById('project-body');
      if (!editor) return;
      const rows = parseInt(prompt('行数（1-10）', '3'), 10) || 3;
      const cols = parseInt(prompt('列数（1-8）', '3'), 10) || 3;
      if (rows < 1 || rows > 10 || cols < 1 || cols > 8) { toast('行列数超出范围（行1-10，列1-8）', 'error'); return; }
      // 生成 Markdown 表格
      const header = `| ${Array(cols).fill('列1').map((_, i) => `列${i + 1}`).join(' | ')} |`;
      const separator = `| ${Array(cols).fill('---').join(' | ')} |`;
      const dataRows = Array(rows).fill(0).map((_, r) => `| ${Array(cols).fill('').map((_, c) => `R${r + 1}C${c + 1}`).join(' | ')} |`);
      const table = `\n${header}\n${separator}\n${dataRows.join('\n')}\n`;
      // 在光标处插入
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      editor.value = editor.value.substring(0, start) + table + editor.value.substring(end);
      editor.focus();
      editor.setSelectionRange(start + table.length, start + table.length);
      editor.dispatchEvent(new Event('input'));
      toast('已插入表格', 'success');
    });
  });
  // #015: 粘贴图片自动上传 — 监听 paste 事件
  const bodyEditor = document.getElementById('project-body');
  if (bodyEditor && bodyEditor.dataset.pasteBound !== '1') {
    bodyEditor.dataset.pasteBound = '1';
    bodyEditor.addEventListener('paste', (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      for (const item of items) {
        if (item.type.startsWith('image/')) {
          e.preventDefault();
          const file = item.getAsFile();
          if (!file) continue;
          // 检查文件类型 (#156: 拒绝 SVG)
          const allowedTypes = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
          if (!allowedTypes.includes(file.type)) {
            toast('不支持的图片格式，仅支持 PNG/JPEG/WEBP/GIF', 'error');
            return;
          }
          if (file.size > 2 * 1024 * 1024) {
            toast('图片大小超过 2MB 限制', 'error');
            return;
          }
          // 读取为 data URL 并插入 Markdown
          const reader = new FileReader();
          reader.onload = () => {
            const dataUrl = reader.result;
            const insertText = `\n![粘贴图片](${dataUrl})\n`;
            const start = bodyEditor.selectionStart;
            const end = bodyEditor.selectionEnd;
            bodyEditor.value = bodyEditor.value.substring(0, start) + insertText + bodyEditor.value.substring(end);
            bodyEditor.setSelectionRange(start + insertText.length, start + insertText.length);
            bodyEditor.dispatchEvent(new Event('input'));
            toast('图片已粘贴插入', 'success');
          };
          reader.readAsDataURL(file);
          break;
        }
      }
    });
  }
  // #056: 发布检查清单 — 点击修复跳转到对应 tab
  document.querySelectorAll('[data-fix-tab]')?.forEach((btn) => {
    btn.addEventListener('click', () => {
      state.wsTab = btn.dataset.fixTab;
      render();
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
      // D2 修复：审校通过后自动刷新预览，确保 previewCurrent 为 true，发布按钮可立即使用
      await refreshPreview(false);
      // #050: 审校通过后自动跳转到发布 tab
      if (approved) {
        state.wsTab = 'publish';
        toast('审校已通过，预览已自动刷新，已跳转到发布页', 'success');
      } else {
        toast('已取消人工终审', 'success');
      }
      render();
    } catch (error) {
      toast(error.message, 'error');
      event.target.checked = !approved;
      event.target.disabled = false;
    }
  });
  document.getElementById('refresh-source')?.addEventListener('click', async () => {
    const btn = document.getElementById('refresh-source');
    if (!btn) return;
    btn.disabled = true;
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
      const btn2 = document.getElementById('refresh-source');
      if (btn2) btn2.disabled = false;
    }
  });
  document.getElementById('open-task')?.addEventListener('click', () => navigate('tasks', { task: state.currentTask.id }));
  document.getElementById('task-cancel')?.addEventListener('click', () => taskAction('cancel'));
  document.getElementById('task-retry')?.addEventListener('click', () => taskAction('retry', document.getElementById('retry-mode')?.value));
  // R1: 恢复因服务重启中断的任务
  document.getElementById('task-resume')?.addEventListener('click', async () => {
    if (!state.currentTask) return;
    try {
      const result = await api(`/api/v2/tasks/${encodeURIComponent(state.currentTask.id)}/resume`, { method: 'POST', body: {} });
      state.currentProject = result.project;
      setProjectInState(result.project);
      state.currentTask = result.task;
      toast('任务已恢复，正在重新执行', 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  // #065: 发布按钮已在上方绑定到确认对话框流程，此处不再重复绑定
  // #058: 关闭发布成功引导
  document.getElementById('dismiss-publish-success')?.addEventListener('click', dismissPublishSuccess);
  // D1: 并发发布 stale 状态处理 —— 标记为已同步 / 撤回远程草稿
  document.getElementById('publish-confirm-sync')?.addEventListener('click', async () => {
    const btn = document.getElementById('publish-confirm-sync');
    if (btn) btn.disabled = true;
    try {
      const projectId = state.currentProject.id;
      const revision = state.publishStale?.revision ?? state.currentProject.revision;
      state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(projectId)}/publish/confirm-sync`, { method: 'POST', body: { revision } });
      setProjectInState(state.currentProject);
      state.publishStale = null;
      toast('已标记为已同步', 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
      if (btn) btn.disabled = false;
    }
  });
  document.getElementById('publish-delete-remote')?.addEventListener('click', async () => {
    if (!confirm('确认撤回远程草稿？撤回后远程草稿将被删除。')) return;
    const btn = document.getElementById('publish-delete-remote');
    if (btn) btn.disabled = true;
    try {
      const projectId = state.currentProject.id;
      const remoteId = state.publishStale?.remoteId || '';
      const result = await api(`/api/v2/projects/${encodeURIComponent(projectId)}/publish/delete-remote`, { method: 'POST', body: { remoteId } });
      state.publishStale = null;
      toast(result.message || '远程草稿已撤回', result.deleted ? 'success' : '');
      state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(projectId)}`);
      setProjectInState(state.currentProject);
      render();
    } catch (error) {
      toast(error.message, 'error');
      if (btn) btn.disabled = false;
    }
  });
  document.getElementById('refresh-preview')?.addEventListener('click', async () => {
    const ok = await flushProjectSave(boundProjectId);
    if (ok) await refreshPreview(true);
  });
  document.getElementById('body-mode-edit')?.addEventListener('click', () => {
    if (state.bodyMode !== 'edit' || state.splitPreview) { state.bodyMode = 'edit'; state.splitPreview = false; render(); }
  });
  document.getElementById('body-mode-preview')?.addEventListener('click', async () => {
    if (state.bodyMode === 'preview') return;
    state.bodyMode = 'preview';
    state.splitPreview = false;
    // #039: 如果没有当前预览，立即显示 loading 状态避免空状态闪烁
    const needsRefresh = !state.preview || state.preview?.revision !== state.currentProject?.revision;
    if (needsRefresh) state.previewLoading = true;
    render();
    // 切到预览时自动刷新一次，确保内容最新
    const ok = await flushProjectSave(boundProjectId);
    if (ok) await refreshPreview(true);
  });
  // #043: 分屏编辑+预览
  document.getElementById('body-mode-split')?.addEventListener('click', async () => {
    state.splitPreview = !state.splitPreview;
    if (state.splitPreview) {
      state.bodyMode = 'edit';
      const needsRefresh = !state.preview || state.preview?.revision !== state.currentProject?.revision;
      if (needsRefresh) state.previewLoading = true;
    }
    render();
    if (state.splitPreview) {
      const ok = await flushProjectSave(boundProjectId);
      if (ok) await refreshPreview(true);
    }
  });
  // #042: 预览设备切换（桌面/移动）
  document.getElementById('preview-device-toggle')?.addEventListener('click', () => {
    state.previewDevice = state.previewDevice === 'desktop' ? 'mobile' : 'desktop';
    render();
  });
  document.getElementById('cover-file')?.addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    // #071/#156: 使用统一验证函数检查文件类型和大小
    const validation = _validateCoverFile(file);
    if (!validation.ok) { toast(validation.reason, 'error'); return; }
    // #156: 上传后通过 Image 对象验证可正常解码
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => scheduleProjectSave(boundProjectId, 'coverDataUrl', String(reader.result || ''));
      img.onerror = () => toast('文件无法解码为有效图片，可能已损坏', 'error');
      img.src = reader.result;
    };
    reader.onerror = () => toast('无法读取封面文件', 'error');
    reader.readAsDataURL(file);
  });
  // #009: 拖拽图片上传封面
  const dropZone = document.getElementById('cover-drop-zone');
  if (dropZone) {
    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', async (e) => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      // #071/#156: 使用统一验证函数检查文件类型和大小
      const validation = _validateCoverFile(file);
      if (!validation.ok) { toast(validation.reason, 'error'); return; }
      const reader = new FileReader();
      reader.onload = () => {
        // #156: 通过 Image 对象验证可正常解码
        const img = new Image();
        img.onload = () => { scheduleProjectSave(boundProjectId, 'coverDataUrl', String(reader.result || '')); toast('封面已上传', 'success'); };
        img.onerror = () => toast('文件无法解码为有效图片，可能已损坏', 'error');
        img.src = reader.result;
      };
      reader.onerror = () => toast('无法读取封面文件', 'error');
      reader.readAsDataURL(file);
    });
    dropZone.addEventListener('click', () => document.getElementById('cover-file')?.click());
    dropZone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); document.getElementById('cover-file')?.click(); } });
  }
  document.getElementById('remove-cover')?.addEventListener('click', () => {
    // #062: 移除封面前确认
    if (!confirm('确认移除封面图片？')) return;
    scheduleProjectSave(boundProjectId, 'coverDataUrl', '');
    toast('封面已移除，终审状态已自动失效', 'success');
  });
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
  document.getElementById('close-versions')?.addEventListener('click', () => { state.showVersions = false; state.diffVersions = null; render(); });
  document.querySelectorAll('[data-restore-version]').forEach((button) => button.addEventListener('click', async () => {
    const rev = button.dataset.restoreVersion;
    const version = state.versions.find((v) => String(v.revision) === String(rev));
    // #110: 版本恢复前显示 diff 对比确认
    if (version?.snapshot?.bodyMarkdown) {
      state.diffVersions = {
        oldText: version.snapshot.bodyMarkdown,
        newText: state.currentProject?.bodyMarkdown || '',
        oldLabel: `revision ${rev}（将恢复）`,
        newLabel: `当前 revision ${state.currentProject?.revision ?? '—'}（将被覆盖）`,
      };
      state.showVersions = false;
      render();
      // 延迟显示确认框，让用户先看到 diff
      const confirmed = confirm(`确认恢复 revision ${rev}？\n\n当前内容会先自动保存为历史版本。\n点击"确定"查看差异后再次确认，点击"取消"放弃恢复。`);
      if (!confirmed) { state.diffVersions = null; state.showVersions = true; render(); return; }
      // 第二次确认
      const confirmed2 = confirm(`已查看差异。确认恢复到 revision ${rev}？此操作不可撤销。`);
      if (!confirmed2) { state.diffVersions = null; state.showVersions = true; render(); return; }
      state.diffVersions = null;
    } else {
      if (!confirm(`确认恢复 revision ${rev}？当前内容会先自动保存为历史版本。`)) return;
    }
    try {
      state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(boundProjectId)}/versions/${rev}/restore`, { method: 'POST', body: {} });
      setProjectInState(state.currentProject);
      state.showVersions = false;
      state.preview = null;
      await refreshPreview(false);
      toast('历史版本已恢复，终审和发布状态已失效', 'success');
      render();
    } catch (error) { toast(error.message, 'error'); }
  }));
  // U1: 版本对比 —— 选中版本正文与当前正文做 side-by-side diff
  document.querySelectorAll('[data-diff-version]').forEach((button) => button.addEventListener('click', () => {
    const rev = button.dataset.diffVersion;
    const version = state.versions.find((v) => String(v.revision) === String(rev));
    if (!version) { toast('未找到该历史版本', 'error'); return; }
    state.diffVersions = {
      oldText: version.snapshot?.bodyMarkdown || '',
      newText: state.currentProject?.bodyMarkdown || '',
      oldLabel: `revision ${rev}`,
      newLabel: `当前 revision ${state.currentProject?.revision ?? '—'}`,
    };
    render();
  }));
  document.getElementById('diff-modal-close')?.addEventListener('click', () => { state.diffVersions = null; render(); });
  document.getElementById('diff-modal-close-btn')?.addEventListener('click', () => { state.diffVersions = null; render(); });
  document.getElementById('diff-toggle-view')?.addEventListener('click', () => {
    state.diffViewMode = state.diffViewMode === 'side' ? 'unified' : 'side';
    render();
  });
  document.getElementById('diff-modal-overlay')?.addEventListener('click', (e) => {
    if (e.target.id === 'diff-modal-overlay') { state.diffVersions = null; render(); }
  });
  // #086/#093: 弹窗焦点管理 — 打开时聚焦关闭按钮并启用焦点陷阱
  if (state.diffVersions) {
    requestAnimationFrame(() => {
      const modal = document.querySelector('.log-modal');
      if (modal) _trapFocus(modal);
      else document.getElementById('diff-modal-close')?.focus();
    });
  }
  // #065: 发布确认对话框焦点陷阱
  if (state.publishConfirmOpen) {
    requestAnimationFrame(() => {
      const modal = document.querySelector('.publish-confirm-content');
      if (modal) _trapFocus(modal);
    });
  }
  if (state.showVersions && !state.diffVersions) {
    requestAnimationFrame(() => document.getElementById('close-versions')?.focus());
  }
  // #035: preserve 过时警告 — 检查保留的内容是否可能过时
  const preservedEditor = document.getElementById('project-body');
  if (preservedEditor && preservedEditor.dataset.preserveRev && state.currentProject) {
    const preserveRev = parseInt(preservedEditor.dataset.preserveRev, 10);
    const currentRev = parseInt(state.currentProject.revision, 10);
    if (preserveRev < currentRev && !state.preserveStaleWarned) {
      state.preserveStaleWarned = true;
      toast('编辑器内容可能已过时（服务端 revision 已更新），请检查是否有冲突', 'warning');
    }
  }
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
    state.mergePreview = null; // U2: 清理合并预览
    state.saveState = 'saved';
    state.preview = null;
    render();
  });
  document.getElementById('conflict-overwrite')?.addEventListener('click', async () => { state.mergePreview = null; await resolveConflict(false); });
  // U2: 合并按钮改为生成段落级三方合并预览，不再直接简单拼接
  document.getElementById('conflict-merge')?.addEventListener('click', () => {
    const { server } = state.conflict;
    const localBody = document.getElementById('conflict-body')?.value || '';
    state.mergePreview = _buildMergeSegments(server.bodyMarkdown || '', localBody);
    render();
  });
  document.getElementById('merge-cancel')?.addEventListener('click', () => { state.mergePreview = null; render(); });
  document.getElementById('merge-confirm')?.addEventListener('click', async () => {
    const conflict = state.conflict;
    if (!conflict || !state.mergePreview) return;
    const local = conflictLocalFields(); // 读取标题/摘要/封面；正文下面会被合并结果覆盖
    const segments = state.mergePreview.map((seg, i) => {
      const keepEl = document.querySelector(`[data-merge-keep="${i}"]`);
      const keep = keepEl ? keepEl.checked : seg.keep;
      let choice = seg.choice;
      const choices = document.querySelectorAll(`[data-merge-choice="${i}"]`);
      if (choices.length) {
        const checked = Array.from(choices).find((r) => r.checked);
        if (checked) choice = checked.value;
      }
      return { ...seg, keep, choice };
    });
    local.bodyMarkdown = _applyMergeSegments(segments);
    state.mergePreview = null;
    state.pendingSaves.delete(conflict.projectId);
    state.currentProject = conflict.server;
    setProjectInState(conflict.server);
    state.conflict = null;
    state.pendingSaves.set(conflict.projectId, local);
    state.dirtyProjects.add(conflict.projectId);
    const ok = await flushProjectSave(conflict.projectId);
    if (ok) {
      toast('正文已按段落合并并保存，请检查结果', 'success');
      render();
    }
  });
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
  const btn = event?.currentTarget || document.getElementById('publish-button');
  if (btn) { btn.disabled = true; }
  try {
    const projectId = state.currentProject.id;
    const ok = await flushProjectSave(projectId);
    if (!ok) throw new Error('请先处理编辑冲突');
    await refreshPreview(false);
    // #083: 使用带重试的 API 调用发布
    const result = await apiWithRetry(`/api/v2/projects/${encodeURIComponent(projectId)}/publish`, {
      method: 'POST',
      body: {
        revision: state.currentProject.revision,
        bodyFingerprint: state.preview.bodyFingerprint,
        previewHash: state.preview.previewHash,
      },
    }, 2);
    state.currentProject = await api(`/api/v2/projects/${encodeURIComponent(projectId)}`);
    setProjectInState(state.currentProject);
    // D1: 发布返回 stale 时记录状态矛盾，触发「标记为已同步 / 撤回远程草稿」处理 UI
    if (result.status === 'current') {
      state.publishStale = null;
      // #058: 发布成功后显示后续引导
      state.publishSuccess = { remoteId: result.remoteId, revision: state.currentProject.revision };
      toast(`当前 revision 已同步：${result.remoteId}`, 'success');
    } else {
      state.publishStale = { projectId, revision: result.revision, remoteId: result.remoteId };
      state.publishSuccess = null;
      toast(`revision ${result.revision} 已同步，但文章随后发生变化，当前文章未标记为已同步。状态矛盾：本地未同步但远程有草稿，请在下方选择处理方式。`, '');
    }
    state.publishLoading = false;
    render();
  } catch (error) {
    toast(error.message, 'error');
    state.publishLoading = false;
    if (btn) { btn.disabled = false; }
    render();
  }
}

// #058: 关闭发布成功引导
function dismissPublishSuccess() {
  state.publishSuccess = null;
  render();
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

// U4: 执行批量操作（归档/删除/恢复），通过 /api/v2/projects/batch 一次性提交
async function _runBatchAction(action) {
  const ids = Array.from(state.selectedArticleIds);
  if (!ids.length) { toast('请先选择文章', 'error'); return; }
  const labels = { archive: '归档', delete: '移入回收站', restore: '恢复' };
  if (!confirm(`确认对选中的 ${ids.length} 篇文章执行「${labels[action] || action}」操作？`)) return;
  try {
    const result = await api('/api/v2/projects/batch', { method: 'POST', body: { action, ids } });
    toast(`批量操作完成，共更新 ${result?.updated ?? ids.length} 篇`, 'success');
    state.selectedArticleIds.clear();
    await reloadProjects();
    render();
  } catch (error) {
    toast(error.message || '批量操作失败', 'error');
  }
}

function bindArticles() {
  // U4: 批量选择与批量操作
  document.querySelectorAll('.batch-checkbox').forEach((cb) => {
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', () => {
      const id = cb.dataset.batchId;
      if (cb.checked) state.selectedArticleIds.add(id);
      else state.selectedArticleIds.delete(id);
      render();
    });
  });
  document.getElementById('batch-select-all')?.addEventListener('click', () => {
    state.projects.forEach((p) => state.selectedArticleIds.add(p.id));
    render();
  });
  document.getElementById('batch-clear')?.addEventListener('click', () => {
    state.selectedArticleIds.clear();
    render();
  });
  document.getElementById('batch-archive')?.addEventListener('click', () => _runBatchAction('archive'));
  document.getElementById('batch-restore')?.addEventListener('click', () => _runBatchAction('restore'));
  document.getElementById('batch-delete')?.addEventListener('click', () => _runBatchAction('delete'));
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
  document.querySelectorAll('[data-article-menu]').forEach((button) => button.addEventListener('click', (e) => {
    e.stopPropagation();
    const id = button.dataset.articleMenu;
    state.articleMenuId = state.articleMenuId === id ? null : id;
    render();
  }));
  document.addEventListener('click', () => {
    if (state.articleMenuId) { state.articleMenuId = null; render(); }
  }, { once: true });
}

function aiFormValue() {
  // A5: 备用模型字段在折叠状态下不在 DOM，此时从已有 draft/settings 继承，避免清空
  const prevBackup = state.aiDraft?.backup || state.settings.ai?.backup || {};
  const bBaseUrl = document.getElementById('ai-backup-base-url');
  const bKey = document.getElementById('ai-backup-key');
  const bModel = document.getElementById('ai-backup-model');
  const backup = {
    baseUrl: bBaseUrl ? bBaseUrl.value.trim() : (prevBackup.baseUrl || ''),
    apiKey: bKey ? bKey.value.trim() : (prevBackup.apiKey || ''),
    model: bModel ? bModel.value.trim() : (prevBackup.model || ''),
  };
  if (prevBackup.apiKeyHint) backup.apiKeyHint = prevBackup.apiKeyHint;
  return {
    providerId: 'openai-compatible',
    baseUrl: document.getElementById('ai-base-url').value.trim(),
    apiKey: document.getElementById('ai-key').value.trim(),
    model: document.getElementById('ai-model').value.trim(),
    temperature: Number(document.getElementById('ai-temp').value),
    maxTokens: Number(document.getElementById('ai-max-tokens').value),
    autoReview: document.getElementById('ai-auto-review').checked,
    backup,
  };
}

function syncAiDraftFromDom() {
  const form = document.getElementById('ai-form');
  if (!form) return null;
  state.aiDraft = aiFormValue();
  persistDraft('aiDraft', state.aiDraft);
  return state.aiDraft;
}

function validateAiTemperature() {
  const tempInput = document.getElementById('ai-temp');
  if (tempInput) {
    const tempVal = Number(tempInput.value);
    if (Number.isNaN(tempVal) || tempVal < 0 || tempVal > 2) {
      toast('温度必须在 0 ~ 2 之间', 'error');
      tempInput.focus();
      return false;
    }
  }
  return true;
}

function bindAi() {
  document.getElementById('toggle-ai-status')?.addEventListener('click', () => {
    state.aiStatusExpanded = !state.aiStatusExpanded; render();
  });
  // A5: 备用模型配置折叠面板
  document.getElementById('toggle-ai-backup')?.addEventListener('click', () => {
    state.aiBackupExpanded = !state.aiBackupExpanded; render();
  });
  document.querySelectorAll('#ai-form input').forEach((element) => {
    const eventName = element.type === 'checkbox' ? 'change' : 'input';
    element.addEventListener(eventName, syncAiDraftFromDom);
  });
  document.getElementById('ai-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!validateAiTemperature()) return;
    try {
      const draft = syncAiDraftFromDom() || aiFormValue();
      state.settings = await api('/api/v2/settings', { method: 'PATCH', body: { ai: draft } });
      state.aiDraft = null;
      persistDraft('aiDraft', null);
      // #155: API Key 保存后立即从内存中清除明文副本
      if (draft.apiKey) draft.apiKey = '';
      await refreshHealth();
      toast('AI 设置已保存', 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  document.getElementById('verify-ai')?.addEventListener('click', async () => {
    const btn = document.getElementById('verify-ai');
    if (!btn) return;
    if (!validateAiTemperature()) return;
    btn.disabled = true;
    try {
      const draft = syncAiDraftFromDom() || aiFormValue();
      const result = await api('/api/v2/settings/ai/verify', { method: 'POST', body: draft });
      await refreshHealth();
      toast(result.message, 'success');
      render();
    } catch (error) {
      toast(error.message, 'error');
      const btn2 = document.getElementById('verify-ai');
      if (btn2) btn2.disabled = false;
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
  document.getElementById('toggle-wechat')?.addEventListener('click', () => {
    state.wechatConfigExpanded = !state.wechatConfigExpanded; render();
  });
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

  // 数据导出
  document.getElementById('data-export-btn')?.addEventListener('click', () => {
    location.href = '/api/v2/data/export';
  });

  // 数据导入：文件选择
  let importFileData = null;
  document.getElementById('data-import-file')?.addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    const filenameSpan = document.getElementById('data-import-filename');
    const optionsDiv = document.getElementById('data-import-options');
    if (!file) {
      importFileData = null;
      filenameSpan.textContent = '未选择文件';
      optionsDiv.style.display = 'none';
      return;
    }
    if (file.size > 100_000_000) {
      toast('文件过大（超过 100MB）', 'error');
      event.target.value = '';
      return;
    }
    try {
      const text = await file.text();
      importFileData = JSON.parse(text);
      if (importFileData.format !== 'studio-backup') {
        toast('文件格式不正确：缺少 studio-backup 标识', 'error');
        importFileData = null;
        event.target.value = '';
        return;
      }
      filenameSpan.textContent = file.name;
      optionsDiv.style.display = 'flex';
    } catch (err) {
      toast('文件解析失败：' + (err.message || '无效的 JSON'), 'error');
      importFileData = null;
      event.target.value = '';
    }
  });

  // 数据导入：执行导入
  document.getElementById('data-import-btn')?.addEventListener('click', async () => {
    if (!importFileData) {
      toast('请先选择备份文件', 'error');
      return;
    }
    const btn = document.getElementById('data-import-btn');
    const mode = document.getElementById('data-import-mode')?.value || 'merge';
    btn.disabled = true;
    btn.textContent = '导入中…';
    try {
      const result = await api('/api/v2/data/import', {
        method: 'POST',
        body: { data: importFileData, mode },
      });
      const c = result.imported || {};
      const parts = [];
      if (c.projects) parts.push(`项目 ${c.projects}`);
      if (c.skipped) parts.push(`跳过 ${c.skipped}`);
      if (c.versions) parts.push(`版本 ${c.versions}`);
      if (c.tasks) parts.push(`任务 ${c.tasks}`);
      if (c.sources) parts.push(`来源 ${c.sources}`);
      if (c.receipts) parts.push(`回执 ${c.receipts}`);
      if (c.settings) parts.push('通用设置');
      toast(parts.length ? `导入完成：${parts.join('，')}` : '无新数据需要导入', 'success');
      // 重新加载数据
      await loadRouteData();
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '开始导入';
    }
  });
}

function bindTasks() {
  document.querySelectorAll('[data-open-task]').forEach((button) => button.addEventListener('click', () => navigate('tasks', { task: button.dataset.openTask })));
  document.getElementById('diag-cancel')?.addEventListener('click', () => taskAction('cancel'));
  document.getElementById('diag-retry')?.addEventListener('click', () => taskAction('retry', document.getElementById('diag-retry-mode')?.value));
  document.getElementById('diag-open-project')?.addEventListener('click', () => navigate('workspace', { project: state.currentTask.projectId, task: state.currentTask.id }));
  document.getElementById('toggle-task-events')?.addEventListener('click', () => { state.taskEventsExpanded = !state.taskEventsExpanded; render(); });
}

// #172: hashchange 防循环 — 100ms 内的重复触发将被忽略
let _lastHashChange = 0;
window.addEventListener('hashchange', async () => {
  const now = Date.now();
  if (now - _lastHashChange < 100) return;
  _lastHashChange = now;
  state.mobileOpen = false;
  await loadRouteData();
});
// #021: 统一 Esc 键处理 — 关闭弹窗/退出预览/关闭移动菜单
window.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  // #006: 关闭查找替换面板
  if (state.findReplaceOpen) { state.findReplaceOpen = false; render(); return; }
  // 优先关闭弹窗
  if (state.diffVersions) { state.diffVersions = null; render(); return; }
  if (state.showVersions) { state.showVersions = false; render(); return; }
  if (state.conflict) { return; } // 冲突弹窗不允许 Esc 关闭
  // 退出预览模式
  if (state.bodyMode === 'preview') { state.bodyMode = 'edit'; render(); return; }
  if (state.splitPreview) { state.splitPreview = false; state.bodyMode = 'edit'; render(); return; }
  // 关闭移动菜单
  if (state.mobileOpen) { state.mobileOpen = false; render(); return; }
  // 只在正文编辑器聚焦时响应编辑快捷键
  const inEditor = event.target?.dataset?.field === 'bodyMarkdown' || event.target?.id === 'project-title' || event.target?.id === 'project-summary';
  // #017: Ctrl+D — 重复行（仅在编辑器内）
  if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key === 'd' && inEditor) {
    event.preventDefault();
    _duplicateLine();
    return;
  }
  // #018: Alt+Up/Down — 移动行（仅在编辑器内）
  if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown') && inEditor) {
    event.preventDefault();
    _moveLine(event.key === 'ArrowUp' ? 'up' : 'down');
    return;
  }
  // #019: Ctrl+/ — 注释切换（仅在编辑器内）
  if ((event.ctrlKey || event.metaKey) && event.key === '/' && inEditor) {
    event.preventDefault();
    _toggleComment();
    return;
  }
});
// #028: 离开页面警告 — 检查所有未保存状态
window.addEventListener('beforeunload', (event) => {
  // P1-25: 清理所有定时器与 SSE 连接，避免页面卸载后遗留后台轮询
  if (state.pollTimer) clearTimeout(state.pollTimer);
  if (state.logsPollTimer) clearTimeout(state.logsPollTimer);
  if (state.articleSearchTimer) clearTimeout(state.articleSearchTimer);
  if (state.saveTimers && state.saveTimers.size > 0) {
    state.saveTimers.forEach((t) => clearTimeout(t));
    state.saveTimers.clear();
  }
  if (state.sse) { try { state.sse.close(); } catch { /* noop */ } state.sse = null; }
  if (state.sseRetryTimer) { clearTimeout(state.sseRetryTimer); state.sseRetryTimer = null; }
  // #109: 清理所有被追踪的事件监听器
  _cleanupTrackedListeners();
  // #049: 清理侧栏预览定时器
  if (state.sidebarPreviewTimer) { clearTimeout(state.sidebarPreviewTimer); state.sidebarPreviewTimer = null; }
  // #091: 清理会话心跳定时器
  if (state.sessionHeartbeatTimer) { clearInterval(state.sessionHeartbeatTimer); state.sessionHeartbeatTimer = null; }
  // #009: 将未保存内容写入 localStorage 作为双保险
  if (state.dirtyProjects.size || state.pendingSaves.size) {
    _saveDraftToLocal();
  }
  // 不在卸载阶段发送无法携带 CSRF 头的 sendBeacon。草稿已写入
  // localStorage，浏览器也会通过下方确认框阻止误离开。
  // 有未保存内容时阻止离开
  if (state.dirtyProjects.size || state.saveChains.size || state.pendingSaves.size) {
    event.preventDefault();
    event.returnValue = '';
    return event.returnValue;
  }
});
// #076: 网络状态监听
window.addEventListener('offline', () => { state.online = false; render(); });
// #089: 网络恢复后自动重连 SSE
window.addEventListener('online', () => {
  state.online = true;
  _reconnectSSEOnOnline();
  render();
});
// #017: Ctrl+D 重复行
function _duplicateLine() {
  const textarea = document.querySelector('[data-field="bodyMarkdown"]');
  if (!textarea) return;
  const start = textarea.selectionStart;
  const value = textarea.value;
  const lineStart = value.lastIndexOf('\n', start - 1) + 1;
  const lineEnd = value.indexOf('\n', start);
  const line = value.slice(lineStart, lineEnd === -1 ? value.length : lineEnd);
  textarea.value = value.slice(0, lineStart) + line + '\n' + value.slice(lineStart);
  textarea.selectionStart = textarea.selectionEnd = start + line.length + 1;
  scheduleProjectSave(state.currentProject.id, 'bodyMarkdown', textarea.value);
}

// #018: Alt+Up/Down 移动行
function _moveLine(direction) {
  const textarea = document.querySelector('[data-field="bodyMarkdown"]');
  if (!textarea) return;
  const value = textarea.value;
  const pos = textarea.selectionStart;
  const lineStart = value.lastIndexOf('\n', pos - 1) + 1;
  let lineEnd = value.indexOf('\n', pos);
  if (lineEnd === -1) lineEnd = value.length;
  const line = value.slice(lineStart, lineEnd);
  if (direction === 'up' && lineStart === 0) return;
  if (direction === 'down' && lineEnd === value.length) return;
  if (direction === 'up') {
    const prevEnd = lineStart - 1;
    const prevStart = value.lastIndexOf('\n', prevEnd - 1) + 1;
    const prevLine = value.slice(prevStart, prevEnd);
    textarea.value = value.slice(0, prevStart) + line + '\n' + prevLine + value.slice(lineEnd);
    textarea.selectionStart = textarea.selectionEnd = prevStart + (pos - lineStart);
  } else {
    const nextStart = lineEnd + 1;
    let nextEnd = value.indexOf('\n', nextStart);
    if (nextEnd === -1) nextEnd = value.length;
    const nextLine = value.slice(nextStart, nextEnd);
    textarea.value = value.slice(0, lineStart) + nextLine + '\n' + line + value.slice(nextEnd);
    textarea.selectionStart = textarea.selectionEnd = lineStart + nextLine.length + 1 + (pos - lineStart);
  }
  scheduleProjectSave(state.currentProject.id, 'bodyMarkdown', textarea.value);
}

// #019: Ctrl+/ 注释切换（Markdown HTML 注释）
function _toggleComment() {
  const textarea = document.querySelector('[data-field="bodyMarkdown"]');
  if (!textarea) return;
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const selected = textarea.value.substring(start, end);
  if (selected.startsWith('<!-- ') && selected.endsWith(' -->')) {
    textarea.value = textarea.value.slice(0, start) + selected.slice(5, -4) + textarea.value.slice(end);
    textarea.selectionStart = start; textarea.selectionEnd = end - 9;
  } else {
    textarea.value = textarea.value.slice(0, start) + '<!-- ' + selected + ' -->' + textarea.value.slice(end);
    textarea.selectionStart = start; textarea.selectionEnd = end + 9;
  }
  scheduleProjectSave(state.currentProject.id, 'bodyMarkdown', textarea.value);
}

// #034: SSE 事件去重
function _isDuplicateSSEEvent(eventId) {
  if (!eventId) return false;
  if (state.sseEventIds.has(eventId)) return true;
  state.sseEventIds.add(eventId);
  if (state.sseEventIds.size > 100) {
    const arr = Array.from(state.sseEventIds);
    state.sseEventIds = new Set(arr.slice(-50));
  }
  return false;
}

// #088: SSE 失败通知
function _notifySSEFailure() {
  if (state.sseFailNotified) return;
  state.sseFailNotified = true;
  toast('实时更新连接失败，已切换到轮询模式', 'error');
}

// #089: 网络恢复后重连 SSE
function _reconnectSSEOnOnline() {
  if (state.online && state.sseRetryCount < 5) {
    _startSSE();
    state.sseFailNotified = false;
    toast('实时更新已恢复', 'success');
  }
}

loadStoredDrafts();
bootstrap();
