async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.href = '/login.html';
    throw new Error('unauthenticated');
  }
  return res;
}

// ---------- 主题切换（深色/浅色，偏好存在浏览器本地，只影响这个浏览器） ----------
(function initTheme() {
  const saved = window.localStorage.getItem('ollama-scanner-theme');
  if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
})();

document.getElementById('themeToggle')?.addEventListener('click', () => {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  if (isLight) {
    document.documentElement.removeAttribute('data-theme');
    window.localStorage.setItem('ollama-scanner-theme', 'dark');
  } else {
    document.documentElement.setAttribute('data-theme', 'light');
    window.localStorage.setItem('ollama-scanner-theme', 'light');
  }
});

const hostForm = document.getElementById('hostForm');
const hostInput = document.getElementById('hostInput');
const hostList = document.getElementById('hostList');
const hostEmpty = document.getElementById('hostEmpty');

const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const statusPill = document.getElementById('statusPill');
const logEl = document.getElementById('log');
const resultsBody = document.getElementById('resultsBody');
const radar = document.getElementById('radar');

const concurrencyRange = document.getElementById('concurrencyRange');
const concurrencyNumber = document.getElementById('concurrencyNumber');
const concurrencyValue = document.getElementById('concurrencyValue');
const modelConcurrencyRange = document.getElementById('modelConcurrencyRange');
const modelConcurrencyNumber = document.getElementById('modelConcurrencyNumber');
const modelConcurrencyValue = document.getElementById('modelConcurrencyValue');

const logoutBtn = document.getElementById('logoutBtn');
const leaderboardToggle = document.getElementById('leaderboardToggle');
const leaderboardPanel = document.getElementById('leaderboardPanel');
const leaderboardRefresh = document.getElementById('leaderboardRefresh');
const lbHostList = document.getElementById('lbHostList');
const lbHostEmpty = document.getElementById('lbHostEmpty');
const lbModelPanel = document.getElementById('lbModelPanel');
const lbModelHostLabel = document.getElementById('lbModelHostLabel');
const lbModelList = document.getElementById('lbModelList');
const lbModelEmpty = document.getElementById('lbModelEmpty');
const lbRanked = document.getElementById('lbRanked');
const lbFailed = document.getElementById('lbFailed');

let pollTimer = null;
let lastSeq = 0;
let wasRunning = false;
let lbActiveHost = null;

// ---------- Logout ----------

logoutBtn.addEventListener('click', async () => {
  await apiFetch('/api/logout', { method: 'POST' });
  window.location.href = '/login.html';
});

// ---------- Leaderboard panel toggle ----------

leaderboardToggle.addEventListener('click', () => {
  const isHidden = leaderboardPanel.hasAttribute('hidden');
  if (isHidden) {
    leaderboardPanel.removeAttribute('hidden');
    leaderboardToggle.classList.add('is-lit');
    refreshLeaderboardSidebar();
    refreshLeaderboardTable();
  } else {
    leaderboardPanel.setAttribute('hidden', '');
  }
});

leaderboardRefresh.addEventListener('click', () => {
  refreshLeaderboardSidebar();
  refreshLeaderboardTable();
});

const lbExportCsv = document.getElementById('lbExportCsv');
const lbExportMd = document.getElementById('lbExportMd');

function downloadLeaderboard(fmt) {
  // 直接跳转即可触发浏览器下载（后端用 Content-Disposition: attachment），
  // 走的是当前登录会话的 cookie，不需要额外处理。
  window.location.href = `/api/leaderboard/export?fmt=${fmt}`;
}
lbExportCsv.addEventListener('click', () => downloadLeaderboard('csv'));
lbExportMd.addEventListener('click', () => downloadLeaderboard('md'));

// ---------- Audit log panel ----------

const auditToggle = document.getElementById('auditToggle');
const auditPanel = document.getElementById('auditPanel');
const auditRefresh = document.getElementById('auditRefresh');
const auditList = document.getElementById('auditList');
const auditEmpty = document.getElementById('auditEmpty');

const AUDIT_ACTION_LABELS = {
  add_host: '新增主机',
  patch_host: '修改主机',
  delete_host: '删除主机',
  login_success: '登录成功',
  login_failed: '登录失败',
  update_settings: '更新设置',
  notify_test: '测试通知',
  notify_failed: '通知发送失败',
};

auditToggle.addEventListener('click', () => {
  const isHidden = auditPanel.hasAttribute('hidden');
  if (isHidden) {
    auditPanel.removeAttribute('hidden');
    auditToggle.classList.add('is-lit');
    refreshAuditLog();
  } else {
    auditPanel.setAttribute('hidden', '');
    auditToggle.classList.remove('is-lit');
  }
});
auditRefresh.addEventListener('click', refreshAuditLog);

async function refreshAuditLog() {
  try {
    const res = await apiFetch('/api/audit-log');
    const logs = await res.json();
    renderAuditLog(logs);
  } catch (e) {
    // ignore, apiFetch already handles auth redirect
  }
}

function renderAuditLog(logs) {
  auditEmpty.style.display = logs.length ? 'none' : 'block';
  auditList.innerHTML = logs.map((l) => `
    <div class="audit-row">
      <span class="audit-row__ts">${escapeHtml(l.ts)}</span>
      <span class="audit-row__action">${escapeHtml(AUDIT_ACTION_LABELS[l.action] || l.action)}</span>
      <span class="audit-row__ip">${escapeHtml(l.ip)}</span>
      <span class="audit-row__detail">${escapeHtml(l.detail || '')}</span>
    </div>
  `).join('');
}

// ---------- Concurrency control ----------

function clampConcurrency(v) {
  v = parseInt(v, 10);
  if (isNaN(v)) v = 3;
  return Math.min(100, Math.max(1, v));
}

function setConcurrency(v) {
  v = clampConcurrency(v);
  concurrencyRange.value = v;
  concurrencyNumber.value = v;
  concurrencyValue.textContent = v;
  try { localStorage.setItem('ollama-scanner-concurrency', String(v)); } catch (e) {}
}

concurrencyRange.addEventListener('input', () => setConcurrency(concurrencyRange.value));
concurrencyNumber.addEventListener('input', () => setConcurrency(concurrencyNumber.value));

(function initConcurrency() {
  let saved = 3;
  try {
    const stored = localStorage.getItem('ollama-scanner-concurrency');
    if (stored) saved = clampConcurrency(stored);
  } catch (e) {}
  setConcurrency(saved);
})();

// ---------- Model concurrency control (每个主机内并发测试几个模型) ----------

function clampModelConcurrency(v) {
  v = parseInt(v, 10);
  if (isNaN(v)) v = 4;
  return Math.min(20, Math.max(1, v));
}

function setModelConcurrency(v) {
  v = clampModelConcurrency(v);
  modelConcurrencyRange.value = v;
  modelConcurrencyNumber.value = v;
  modelConcurrencyValue.textContent = v;
  try { localStorage.setItem('ollama-scanner-model-concurrency', String(v)); } catch (e) {}
}

modelConcurrencyRange.addEventListener('input', () => setModelConcurrency(modelConcurrencyRange.value));
modelConcurrencyNumber.addEventListener('input', () => setModelConcurrency(modelConcurrencyNumber.value));

(function initModelConcurrency() {
  let saved = 4;
  try {
    const stored = localStorage.getItem('ollama-scanner-model-concurrency');
    if (stored) saved = clampModelConcurrency(stored);
  } catch (e) {}
  setModelConcurrency(saved);
})();

// ---------- Hosts ----------

let allHostsCache = [];
let hostTagFilter = '';

async function fetchHosts() {
  const res = await apiFetch('/api/hosts');
  const hosts = await res.json();
  allHostsCache = hosts;
  renderHosts(filterHostsByTag(hosts));
}

function filterHostsByTag(hosts) {
  const q = hostTagFilter.trim().toLowerCase();
  if (!q) return hosts;
  return hosts.filter(
    (h) => (h.tags || []).some((t) => t.toLowerCase().includes(q)) || (h.group || '').toLowerCase().includes(q)
  );
}

const hostTagFilterInput = document.getElementById('hostTagFilter');
hostTagFilterInput.addEventListener('input', () => {
  hostTagFilter = hostTagFilterInput.value;
  renderHosts(filterHostsByTag(allHostsCache));
});

const hostFailedZone = document.getElementById('hostFailedZone');
const hostFailedList = document.getElementById('hostFailedList');

function isHostFailed(host) {
  return host.last_scan_status === 'unreachable' || host.last_scan_status === 'all_down';
}

function renderHosts(hosts) {
  const normalHosts = hosts.filter((h) => !isHostFailed(h));
  const failedHosts = hosts.filter((h) => isHostFailed(h));

  hostList.innerHTML = '';
  hostEmpty.style.display = normalHosts.length ? 'none' : 'block';
  normalHosts.forEach((host) => hostList.appendChild(buildHostItem(host)));

  hostFailedZone.hidden = failedHosts.length === 0;
  hostFailedList.innerHTML = '';
  failedHosts.forEach((host) => hostFailedList.appendChild(buildHostItem(host)));
}

function buildHostItem(host) {
  const { url, enabled, favorite, tags, group, last_scan_status } = host;
  const tagList = tags || [];
  const failed = isHostFailed(host);
  const li = document.createElement('li');
  li.className = `host-item${enabled ? '' : ' host-item--disabled'}${failed ? ' host-item--failed' : ''}`;
  const statusLabel = last_scan_status === 'unreachable'
    ? '⛔ 上次扫描整链接不可达'
    : last_scan_status === 'all_down'
      ? '⛔ 上次扫描全部模型不通'
      : '';
  li.innerHTML = `
    <button class="host-item__star${favorite ? ' is-lit' : ''}" title="${favorite ? '取消收藏' : '收藏此地址'}" aria-pressed="${favorite}">★</button>
    <span class="host-item__main">
      <span class="host-item__url">${escapeHtml(url)}</span>
      ${statusLabel ? `<span class="host-tag host-tag--fail">${statusLabel}</span>` : ''}
      ${group ? `<span class="host-tag host-tag--group">📁 ${escapeHtml(group)}</span>` : ''}
      ${tagList.length ? `<span class="host-item__tags">${tagList.map((t) => `<span class="host-tag">${escapeHtml(t)}</span>`).join('')}</span>` : ''}
    </span>
    <button class="host-item__tagbtn" title="编辑分组">📁</button>
    <button class="host-item__tagbtn" title="编辑标签">🏷</button>
    <label class="host-item__toggle" title="${enabled ? '启用中，参与扫描' : '已禁用，不参与扫描'}">
      <input type="checkbox" ${enabled ? 'checked' : ''} aria-label="启用 ${escapeHtml(url)}" />
      <span class="host-item__toggle-track"><span class="host-item__toggle-thumb"></span></span>
    </label>
    <button class="host-item__remove" title="移除" aria-label="移除 ${escapeHtml(url)}">×</button>
  `;
  const [groupBtn, tagBtn] = li.querySelectorAll('.host-item__tagbtn');
  li.querySelector('.host-item__star').addEventListener('click', () => patchHost(url, { favorite: !favorite }));
  groupBtn.addEventListener('click', () => editHostGroup(url, group));
  tagBtn.addEventListener('click', () => editHostTags(url, tagList));
  li.querySelector('.host-item__toggle input').addEventListener('change', (e) => patchHost(url, { enabled: e.target.checked }));
  li.querySelector('.host-item__remove').addEventListener('click', () => removeHost(url));
  return li;
}

document.getElementById('hostFailedDeleteAllBtn').addEventListener('click', async () => {
  if (!confirm('确定要一键删除"失败区"里的全部链接吗？此操作不可撤销。')) return;
  try {
    const res = await apiFetch('/api/hosts/failed', { method: 'DELETE' });
    const data = await res.json();
    allHostsCache = data.hosts;
    renderHosts(filterHostsByTag(allHostsCache));
  } catch (e) {
    // ignore
  }
});

function editHostGroup(url, currentGroup) {
  const input = window.prompt('给这台主机设置一个分组名（比如：机房A / 项目X），留空清除分组', currentGroup || '');
  if (input === null) return; // 取消
  patchHost(url, { group: input.trim() });
}

function editHostTags(url, currentTags) {
  const input = window.prompt('用逗号分隔多个标签，例如：机房A,GPU', (currentTags || []).join(','));
  if (input === null) return; // 取消
  const tags = input.split(',').map((t) => t.trim()).filter(Boolean);
  patchHost(url, { tags });
}

async function patchHost(url, changes) {
  const res = await apiFetch('/api/hosts', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, ...changes }),
  });
  if (res.ok) {
    const hosts = await res.json();
    allHostsCache = hosts;
    renderHosts(filterHostsByTag(hosts));
  }
}

async function addHost(url, force) {
  const res = await apiFetch('/api/hosts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, force: !!force }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail = err.detail;
    // 探活失败：后端返回 {code: 'unreachable', message: ...}，弹二次确认后带 force 重新提交
    if (detail && typeof detail === 'object' && detail.code === 'unreachable') {
      if (window.confirm(`${detail.message}\n\n仍然要添加这个地址吗？`)) {
        return addHost(url, true);
      }
      return;
    }
    alert((typeof detail === 'string' ? detail : detail?.message) || '添加失败');
    return;
  }
  const hosts = await res.json();
  allHostsCache = hosts;
  renderHosts(filterHostsByTag(hosts));
}

async function removeHost(url) {
  const res = await apiFetch('/api/hosts', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (res.ok) {
    const hosts = await res.json();
    allHostsCache = hosts;
    renderHosts(filterHostsByTag(hosts));
  }
}

hostForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const val = hostInput.value.trim();
  if (!val) return;
  addHost(val);
  hostInput.value = '';
});

// ---------- Batch paste parsing ----------

const batchToggle = document.getElementById('batchToggle');
const batchPanel = document.getElementById('batchPanel');
const batchInput = document.getElementById('batchInput');
const batchParseBtn = document.getElementById('batchParseBtn');
const batchCancelBtn = document.getElementById('batchCancelBtn');
const batchPreview = document.getElementById('batchPreview');

batchToggle.addEventListener('click', () => {
  const showing = !batchPanel.hidden;
  batchPanel.hidden = showing;
  if (!showing) batchInput.focus();
});
batchCancelBtn.addEventListener('click', () => {
  batchPanel.hidden = true;
  batchInput.value = '';
  batchPreview.innerHTML = '';
});

// Matches ip[:port], with or without a leading http(s)://
const IP_PORT_RE = /(?:https?:\/\/)?(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?/g;
const DEFAULT_PORT = '11434';

function isPrivateIp(ip) {
  const parts = ip.split('.').map(Number);
  if (parts.length !== 4 || parts.some((n) => Number.isNaN(n) || n < 0 || n > 255)) return false;
  const [a, b] = parts;
  if (a === 10) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 127) return true; // loopback
  if (a === 169 && b === 254) return true; // link-local
  return false;
}

function parseAddresses(text) {
  const seen = new Set();
  const results = [];
  let match;
  IP_PORT_RE.lastIndex = 0;
  while ((match = IP_PORT_RE.exec(text)) !== null) {
    const ip = match[1];
    const port = match[2] || DEFAULT_PORT;
    const url = `http://${ip}:${port}`;
    if (seen.has(url)) continue;
    seen.add(url);
    results.push({ url, ip, private: isPrivateIp(ip) });
  }
  return results;
}

batchParseBtn.addEventListener('click', async () => {
  const parsed = parseAddresses(batchInput.value);
  if (!parsed.length) {
    batchPreview.innerHTML = '<li>没有识别到任何 ip:port 地址。</li>';
    return;
  }

  const privateOnes = parsed.filter((p) => p.private);
  const publicOnes = parsed.filter((p) => !p.private);

  if (publicOnes.length) {
    const list = publicOnes.map((p) => `  • ${p.url}`).join('\n');
    const confirmed = confirm(
      `以下 ${publicOnes.length} 个地址不属于内网网段（10.x / 172.16-31.x / 192.168.x）：\n\n${list}\n\n` +
      `请确认这些都是你自己拥有或已获得明确授权测试的主机，再继续添加。\n点击"确定"添加全部，"取消"仅添加内网地址。`
    );
    if (!confirmed) {
      publicOnes.length = 0; // drop them, keep only private
    }
  }

  const toAdd = [...privateOnes, ...(publicOnes.length ? publicOnes : [])];
  batchPreview.innerHTML = toAdd
    .map((p) => `<li><span class="${p.private ? 'tag-private' : 'tag-public'}">${p.private ? '内网' : '公网'}</span> ${escapeHtml(p.url)}</li>`)
    .join('');

  for (const p of toAdd) {
    await addHost(p.url);
  }

  if (toAdd.length) {
    batchInput.value = '';
  }
});

// ---------- Scan control ----------

async function startScan() {
  const concurrency = clampConcurrency(concurrencyNumber.value);
  const model_concurrency = clampModelConcurrency(modelConcurrencyNumber.value);
  const res = await apiFetch('/api/scan/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ concurrency, model_concurrency }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || '无法开始扫描');
    return;
  }
  logEl.innerHTML = '';
  lastSeq = 0;
  setRunningUI(true);
  poll();
}

async function stopScan() {
  await apiFetch('/api/scan/stop', { method: 'POST' });
  setStatusPill('stopping', '停止中…');
  stopBtn.disabled = true;
}

startBtn.addEventListener('click', startScan);
stopBtn.addEventListener('click', stopScan);

function setRunningUI(running) {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
  concurrencyRange.disabled = running;
  concurrencyNumber.disabled = running;
  modelConcurrencyRange.disabled = running;
  modelConcurrencyNumber.disabled = running;
  radar.classList.toggle('is-active', running);
  if (running) setStatusPill('running', '扫描中…');
}

function setStatusPill(kind, text) {
  statusPill.className = `status-pill status-pill--${kind}`;
  statusPill.textContent = text;
}

// ---------- Polling ----------

async function poll() {
  clearTimeout(pollTimer);
  try {
    const res = await apiFetch(`/api/scan/status?since=${lastSeq}`);
    const data = await res.json();

    appendLogs(data.logs);
    setRunningUI(data.running);

    if (!data.running) {
      if (data.results) {
        setStatusPill('done', '已完成');
        renderResults(data.results);
      } else if (wasRunning) {
        setStatusPill('idle', '待机');
      }
    }
    wasRunning = data.running;

    if (data.running) {
      pollTimer = setTimeout(poll, 1200);
    }
  } catch (e) {
    pollTimer = setTimeout(poll, 2000);
  }
}

const MAX_LOG_LINES = 3000; // 超长扫描（几十台主机跑几小时）避免日志把DOM撑爆导致页面变卡

function appendLogs(logs) {
  if (!logs || !logs.length) return;
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  const frag = document.createDocumentFragment();
  logs.forEach((l) => {
    lastSeq = Math.max(lastSeq, l.seq);
    const div = document.createElement('div');
    div.className = 'log__line';
    div.innerHTML = `<span class="log__ts">${l.ts}</span>${escapeHtml(l.text)}`;
    frag.appendChild(div);
  });
  logEl.appendChild(frag);
  while (logEl.children.length > MAX_LOG_LINES) {
    logEl.removeChild(logEl.firstChild);
  }
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}

// ---------- Results ----------

function renderResults(results) {
  if (!results || !results.advanced || Object.keys(results.advanced).length === 0) {
    resultsBody.innerHTML = '<p class="results-empty">本次扫描没有可用模型进入高级测试阶段。</p>';
    return;
  }

  resultsBody.innerHTML = '';
  Object.entries(results.advanced).forEach(([key, tests]) => {
    const [host, model] = key.split('|');
    const passCount = tests.filter((t) => t.status === 'PASS').length;
    const allPass = tests.length > 0 && passCount === tests.length;

    const card = document.createElement('div');
    card.className = 'result-card';
    card.innerHTML = `
      <div class="result-card__head">
        <span>${escapeHtml(model)} <span style="color:var(--dim)">@ ${escapeHtml(host)}</span></span>
        <span class="result-card__badge ${allPass ? 'result-card__badge--pass' : 'result-card__badge--fail'}">
          ${passCount}/${tests.length} 通过
        </span>
      </div>
      <div class="result-card__tests">
        ${tests.map((t) => `
          <div class="result-test">
            <span>${escapeHtml(t.test)}</span>
            <span class="result-test__status--${t.status}">${t.status} (${t.elapsed.toFixed(1)}s)</span>
          </div>
        `).join('')}
      </div>
    `;
    resultsBody.appendChild(card);
  });
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ---------- Leaderboard sidebar: hosts -> models connectivity ----------

async function refreshLeaderboardSidebar() {
  try {
    const res = await apiFetch('/api/hosts/status');
    const hosts = await res.json();
    renderLbHostList(hosts);
  } catch (e) {
    // ignore, apiFetch already handles auth redirect
  }
}

function renderLbHostList(hosts) {
  lbHostList.innerHTML = '';
  lbHostEmpty.style.display = hosts.length ? 'none' : 'block';

  hosts.forEach((host) => {
    const failed = host.last_scan_status === 'unreachable' || host.last_scan_status === 'all_down';
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `lb-host-item${host.url === lbActiveHost ? ' is-active' : ''}${failed ? ' lb-host-item--failed' : ''}`;
    btn.textContent = host.url + (host.enabled ? '' : ' (已禁用)') + (failed ? ' ⛔' : '');
    btn.addEventListener('click', () => {
      lbActiveHost = host.url;
      renderLbHostList(hosts);
      renderLbModelPanel(host);
    });
    li.appendChild(btn);
    lbHostList.appendChild(li);
  });

  if (lbActiveHost) {
    const active = hosts.find((h) => h.url === lbActiveHost);
    if (active) renderLbModelPanel(active);
  }
}

function renderLbModelPanel(host) {
  lbModelPanel.hidden = false;
  lbModelHostLabel.textContent = host.url;
  lbModelList.innerHTML = '';
  lbModelEmpty.style.display = host.models.length ? 'none' : 'block';

  host.models.forEach((m) => {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lb-model-item';
    const dotClass = m.ok === true ? 'status-dot--ok' : m.ok === false ? 'status-dot--fail' : 'status-dot--unknown';
    const uptimeHtml = m.uptime_pct != null ? `<span class="lb-model-item__uptime">在线率 ${m.uptime_pct}%</span>` : '';
    btn.innerHTML = `
      <span class="status-dot ${dotClass}" data-role="dot"></span>
      <span class="lb-model-item__name">${escapeHtml(m.model)}</span>
      ${uptimeHtml}
    `;
    btn.title = '点击发送「你好」测试连通性';
    btn.addEventListener('click', () => pingModel(host.url, m.model, btn));
    li.appendChild(btn);
    lbModelList.appendChild(li);
  });
}

async function pingModel(hostUrl, model, btnEl) {
  const dot = btnEl.querySelector('[data-role="dot"]');
  dot.className = 'status-dot status-dot--checking';
  btnEl.disabled = true;
  try {
    const res = await apiFetch('/api/ping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: hostUrl, model }),
    });
    const data = await res.json();
    dot.className = `status-dot ${data.ok ? 'status-dot--ok' : 'status-dot--fail'}`;
    btnEl.title = data.ok
      ? `正常 (${data.elapsed}s)`
      : `失败: ${(data.error || '无响应').slice(0, 80)}`;
  } catch (e) {
    dot.className = 'status-dot status-dot--fail';
  } finally {
    btnEl.disabled = false;
  }
}

// ---------- Leaderboard table (三分类: core / control / language) ----------

let lbActiveCategory = 'core';
let lbLastData = null;
let lbMode = 'deep'; // 'deep' | 'quick'
let lbQuickData = null;

const lbTabs = document.querySelectorAll('.lb-tabs .lb-tab');
const lbModeButtons = document.querySelectorAll('.lb-mode-toggle .lb-tab');
const lbDeepOnlyEls = [document.getElementById('lbDeepOnly'), document.getElementById('lbTrendSection')];
const lbRankedTitle = document.getElementById('lbRankedTitle');

lbModeButtons.forEach((btn) => {
  btn.addEventListener('click', () => {
    lbMode = btn.dataset.mode;
    lbModeButtons.forEach((b) => b.classList.toggle('is-active', b === btn));
    lbDeepOnlyEls.forEach((el) => { if (el) el.style.display = lbMode === 'quick' ? 'none' : ''; });
    refreshLeaderboardTable();
  });
});

lbTabs.forEach((tab) => {
  tab.addEventListener('click', () => {
    lbActiveCategory = tab.dataset.cat;
    lbTabs.forEach((t) => t.classList.toggle('is-active', t === tab));
    renderActiveCategory();
    loadTrendChart();
  });
});

const trendRangeSelect = document.getElementById('trendRangeSelect');
const trendChartEl = document.getElementById('trendChart');
trendRangeSelect.addEventListener('change', loadTrendChart);

const TREND_METRIC_COLORS = {
  ranked_count: '#2dd4bf',
  avg_elapsed: '#facc15',
};

async function loadTrendChart() {
  try {
    const days = trendRangeSelect.value;
    const res = await apiFetch(`/api/history?days=${days}`);
    const records = await res.json();
    renderTrendChart(records);
  } catch (e) {
    // apiFetch 已处理鉴权跳转
  }
}

function renderTrendChart(records) {
  const points = (records || [])
    .map((r) => ({ ts: r.ts, cat: (r.categories || {})[lbActiveCategory] }))
    .filter((p) => p.cat);

  if (!points.length) {
    trendChartEl.innerHTML = '<p class="results-empty">暂无历史趋势数据，跑几次扫描后会自动出现。</p>';
    return;
  }

  const width = 640;
  const height = 160;
  const padding = 24;
  const maxRanked = Math.max(1, ...points.map((p) => p.cat.ranked_count || 0));
  const maxElapsed = Math.max(1, ...points.map((p) => p.cat.avg_elapsed || 0));

  function pathFor(getVal, maxVal) {
    return points
      .map((p, i) => {
        const x = padding + (i / Math.max(1, points.length - 1)) * (width - padding * 2);
        const v = getVal(p.cat) || 0;
        const y = height - padding - (v / maxVal) * (height - padding * 2);
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }

  const rankedPath = pathFor((c) => c.ranked_count, maxRanked);
  const elapsedPath = pathFor((c) => c.avg_elapsed, maxElapsed);

  trendChartEl.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="历史趋势图">
      <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="var(--line)" stroke-width="1" />
      <path d="${rankedPath}" fill="none" stroke="${TREND_METRIC_COLORS.ranked_count}" stroke-width="2" />
      <path d="${elapsedPath}" fill="none" stroke="${TREND_METRIC_COLORS.avg_elapsed}" stroke-width="2" />
    </svg>
    <div class="trend-legend">
      <span><i style="background:${TREND_METRIC_COLORS.ranked_count}"></i>上榜数量（最高 ${maxRanked}）</span>
      <span><i style="background:${TREND_METRIC_COLORS.avg_elapsed}"></i>平均耗时秒（最高 ${maxElapsed.toFixed(1)}s）</span>
      <span>${points[0].ts.slice(0, 10)} ~ ${points[points.length - 1].ts.slice(0, 10)}，共 ${points.length} 个点</span>
    </div>
  `;
}

async function refreshLeaderboardTable() {
  try {
    if (lbMode === 'quick') {
      const res = await apiFetch('/api/leaderboard/quick');
      lbQuickData = await res.json();
      renderActiveCategory();
    } else {
      const res = await apiFetch('/api/leaderboard');
      lbLastData = await res.json();
      renderActiveCategory();
      loadTrendChart();
    }
  } catch (e) {
    // ignore, apiFetch already handles auth redirect
  }
}

function renderActiveCategory() {
  if (lbMode === 'quick') {
    if (!lbQuickData) return;
    lbRankedTitle.textContent = `⚡ ${lbQuickData.label}（在线优先，按响应速度排序）`;
    renderQuickTable(lbRanked, lbQuickData.ranked, true);
    renderQuickTable(lbFailed, lbQuickData.failed, false);
    return;
  }
  if (!lbLastData) return;
  const catData = lbLastData[lbActiveCategory] || { label: '', ranked: [], failed: [] };
  lbRankedTitle.textContent = `排行榜（${catData.label || ''} · 全部通过）`;
  renderLbTable(lbRanked, catData.ranked, true);
  renderLbTable(lbFailed, catData.failed, false);
}

function renderQuickTable(container, entries, ranked) {
  if (!entries || !entries.length) {
    container.innerHTML = `<p class="results-empty">${ranked ? '暂无在线模型。' : '暂无离线记录。'}</p>`;
    return;
  }
  container.innerHTML = '';
  entries.forEach((entry) => {
    const row = document.createElement('div');
    row.className = `lb-row${ranked ? '' : ' lb-row--fail'}`;
    const statsHtml = ranked ? `<strong>${entry.elapsed ?? '?'}s</strong> 响应耗时` : '离线/无响应';
    const familyHint = entry.family_hint
      ? `<div class="lb-row__family">参考系列: ${escapeHtml(entry.family_hint)}</div>`
      : '';
    row.innerHTML = `
      <span class="lb-row__rank">${ranked ? '#' + entry.rank : '✘'}</span>
      <span class="lb-row__info">
        <div class="lb-row__model">${escapeHtml(entry.model)}</div>
        <div class="lb-row__host">@ ${escapeHtml(entry.host)}</div>
        ${familyHint}
      </span>
      <span class="lb-row__stats">${statsHtml}</span>
    `;
    container.appendChild(row);
  });
}

function renderLbTable(container, entries, ranked) {
  if (!entries || !entries.length) {
    container.innerHTML = `<p class="results-empty">${ranked ? '暂无排名数据。' : '暂无失败记录。'}</p>`;
    return;
  }
  container.innerHTML = '';
  entries.forEach((entry) => {
    const row = document.createElement('div');
    row.className = `lb-row${ranked ? '' : ' lb-row--fail'}`;
    const statsHtml = ranked
      ? `<strong>${entry.elapsed_total}s</strong> 总耗时 · ${entry.passed}/${entry.total} 通过`
      : `${entry.passed || 0}/${entry.total || 0} 通过${entry.error ? ' · ' + escapeHtml(entry.error) : ''}`;
    const familyHint = entry.family_hint
      ? `<div class="lb-row__family">参考系列: ${escapeHtml(entry.family_hint)}</div>`
      : '';
    row.innerHTML = `
      <span class="lb-row__rank">${ranked ? '#' + entry.rank : '✘'}</span>
      <span class="lb-row__info">
        <div class="lb-row__model">${escapeHtml(entry.model)}</div>
        <div class="lb-row__host">@ ${escapeHtml(entry.host)}</div>
        ${familyHint}
      </span>
      <span class="lb-row__stats">${statsHtml}</span>
      <button type="button" class="lb-row__retest">↻ 重新测试</button>
    `;
    row.querySelector('.lb-row__retest').addEventListener('click', (e) => retestLeaderboardEntry(entry, e.target));
    container.appendChild(row);
  });
}

async function retestLeaderboardEntry(entry, btnEl) {
  btnEl.disabled = true;
  const originalText = btnEl.textContent;
  btnEl.textContent = '测试中…';
  try {
    await apiFetch('/api/leaderboard/retest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: entry.host, model: entry.model }),
    });
    await refreshLeaderboardTable();
  } catch (e) {
    btnEl.disabled = false;
    btnEl.textContent = originalText;
  }
}

// ---------- Settings panel: 定时扫描 + 异常通知 ----------

const settingsToggle = document.getElementById('settingsToggle');
const settingsPanel = document.getElementById('settingsPanel');
const settingsSaveBtn = document.getElementById('settingsSaveBtn');
const notifyTestBtn = document.getElementById('notifyTestBtn');
const settingsStatus = document.getElementById('settingsStatus');

settingsToggle.addEventListener('click', () => {
  const isHidden = settingsPanel.hasAttribute('hidden');
  if (isHidden) {
    settingsPanel.removeAttribute('hidden');
    settingsToggle.classList.add('is-lit');
    loadSettingsIntoForm();
  } else {
    settingsPanel.setAttribute('hidden', '');
    settingsToggle.classList.remove('is-lit');
  }
});

async function loadSettingsIntoForm() {
  try {
    const res = await apiFetch('/api/settings');
    const s = await res.json();

    document.getElementById('schedEnabled').checked = !!s.schedule.enabled;
    document.getElementById('schedTime').value = s.schedule.time || '09:00';
    document.getElementById('schedConcurrency').value = s.schedule.concurrency ?? 3;
    document.getElementById('schedModelConcurrency').value = s.schedule.model_concurrency ?? 4;

    document.getElementById('notifyWecomEnabled').checked = !!s.notify.wecom.enabled;
    document.getElementById('notifyWecomUrl').value = s.notify.wecom.webhook_url || '';

    document.getElementById('notifyTelegramEnabled').checked = !!s.notify.telegram.enabled;
    document.getElementById('notifyTelegramToken').value = s.notify.telegram.bot_token || '';
    document.getElementById('notifyTelegramChatId').value = s.notify.telegram.chat_id || '';

    document.getElementById('notifyBarkEnabled').checked = !!s.notify.bark.enabled;
    document.getElementById('notifyBarkKey').value = s.notify.bark.key || '';
    document.getElementById('notifyBarkServer').value = s.notify.bark.server || 'https://api.day.app';

    document.getElementById('notifyEmailEnabled').checked = !!s.notify.email.enabled;
    document.getElementById('notifyEmailHost').value = s.notify.email.smtp_host || '';
    document.getElementById('notifyEmailPort').value = s.notify.email.smtp_port ?? 587;
    document.getElementById('notifyEmailUser').value = s.notify.email.username || '';
    document.getElementById('notifyEmailPass').value = s.notify.email.password || '';
    document.getElementById('notifyEmailFrom').value = s.notify.email.from_addr || '';
    document.getElementById('notifyEmailTo').value = s.notify.email.to_addr || '';
    document.getElementById('notifyEmailTls').checked = s.notify.email.use_tls !== false;

    const hist = s.history || {};
    document.getElementById('histRetentionDays').value = hist.retention_days ?? 180;
    document.getElementById('histMaxSizeMb').value = hist.max_size_mb ?? 50;
    document.getElementById('histAutoCleanup').checked = hist.auto_cleanup_enabled !== false;
  } catch (e) {
    // ignore, apiFetch already handles auth redirect
  }
  refreshHistoryStats();
  refreshShareStatus();
  refreshTotpStatus();
  refreshMetricsStatus();
  refreshCustomTests();
  refreshCoreTests();
}

function buildSettingsPayload() {
  return {
    schedule: {
      enabled: document.getElementById('schedEnabled').checked,
      time: document.getElementById('schedTime').value || '09:00',
      concurrency: clampConcurrency(document.getElementById('schedConcurrency').value),
      model_concurrency: clampModelConcurrency(document.getElementById('schedModelConcurrency').value),
    },
    notify: {
      wecom: {
        enabled: document.getElementById('notifyWecomEnabled').checked,
        webhook_url: document.getElementById('notifyWecomUrl').value.trim(),
      },
      telegram: {
        enabled: document.getElementById('notifyTelegramEnabled').checked,
        bot_token: document.getElementById('notifyTelegramToken').value.trim(),
        chat_id: document.getElementById('notifyTelegramChatId').value.trim(),
      },
      bark: {
        enabled: document.getElementById('notifyBarkEnabled').checked,
        key: document.getElementById('notifyBarkKey').value.trim(),
        server: document.getElementById('notifyBarkServer').value.trim() || 'https://api.day.app',
      },
      email: {
        enabled: document.getElementById('notifyEmailEnabled').checked,
        smtp_host: document.getElementById('notifyEmailHost').value.trim(),
        smtp_port: parseInt(document.getElementById('notifyEmailPort').value, 10) || 587,
        username: document.getElementById('notifyEmailUser').value.trim(),
        password: document.getElementById('notifyEmailPass').value,
        from_addr: document.getElementById('notifyEmailFrom').value.trim(),
        to_addr: document.getElementById('notifyEmailTo').value.trim(),
        use_tls: document.getElementById('notifyEmailTls').checked,
      },
    },
    history: {
      retention_days: parseInt(document.getElementById('histRetentionDays').value, 10) || 180,
      max_size_mb: parseInt(document.getElementById('histMaxSizeMb').value, 10) || 50,
      auto_cleanup_enabled: document.getElementById('histAutoCleanup').checked,
    },
  };
}

settingsSaveBtn.addEventListener('click', async () => {
  settingsStatus.textContent = '保存中…';
  try {
    const res = await apiFetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildSettingsPayload()),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      settingsStatus.textContent = `保存失败：${err.detail || res.status}`;
      return;
    }
    settingsStatus.textContent = '已保存 ✓';
    setTimeout(() => { settingsStatus.textContent = ''; }, 3000);
  } catch (e) {
    settingsStatus.textContent = '保存失败，请检查网络';
  }
});

// ---------- 历史趋势数据：文件大小/条数展示 + 按天数删除 ----------

const histStatsText = document.getElementById('histStatsText');

async function refreshHistoryStats() {
  try {
    const res = await apiFetch('/api/history/stats');
    const s = await res.json();
    const sizeKb = (s.size_bytes / 1024).toFixed(1);
    histStatsText.textContent = `共 ${s.count} 条记录，占用 ${sizeKb} KB`;
  } catch (e) {
    histStatsText.textContent = '';
  }
}

async function deleteHistory(mode, days) {
  const label = mode === 'all' ? '全部历史记录' : `${days} 天前的历史记录`;
  if (!confirm(`确定要删除${label}吗？此操作不可撤销。`)) return;
  try {
    await apiFetch('/api/history', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mode === 'all' ? { mode: 'all' } : { mode: 'days', days }),
    });
    await refreshHistoryStats();
    await loadTrendChart();
  } catch (e) {
    // apiFetch 已处理鉴权跳转
  }
}

document.getElementById('histDelete30Btn').addEventListener('click', () => deleteHistory('days', 30));
document.getElementById('histDelete60Btn').addEventListener('click', () => deleteHistory('days', 60));
document.getElementById('histDelete180Btn').addEventListener('click', () => deleteHistory('days', 180));
document.getElementById('histDelete365Btn').addEventListener('click', () => deleteHistory('days', 365));
document.getElementById('histDeleteAllBtn').addEventListener('click', () => deleteHistory('all'));

// ---------- 只读分享链接（多 token，各自可命名/设过期时间/单独吊销） ----------

const shareEnabledEl = document.getElementById('shareEnabled');
const shareTokenList = document.getElementById('shareTokenList');

async function refreshShareStatus() {
  try {
    const res = await apiFetch('/api/share/settings');
    const s = await res.json();
    shareEnabledEl.checked = !!s.enabled;
    renderShareTokenList(s.tokens || []);
  } catch (e) {
    // ignore
  }
}

function renderShareTokenList(tokens) {
  if (!tokens.length) {
    shareTokenList.innerHTML = '<p class="results-empty">还没有生成任何分享链接。</p>';
    return;
  }
  shareTokenList.innerHTML = '';
  tokens.forEach((t) => {
    const url = `${window.location.origin}/share.html?token=${t.token}`;
    const row = document.createElement('div');
    row.className = 'share-token-row';
    row.innerHTML = `
      <span class="share-token-row__label">${escapeHtml(t.label || '(未命名)')}</span>
      <input type="text" class="settings-input" readonly value="${escapeHtml(url)}" />
      <span class="panel__hint">${t.expires_at ? '过期于 ' + escapeHtml(t.expires_at.slice(0, 10)) : '永久有效'}</span>
      <button class="btn" data-act="copy">📋 复制</button>
      <button class="btn btn--danger" data-act="revoke">🗑 吊销</button>
    `;
    row.querySelector('[data-act="copy"]').addEventListener('click', () => {
      navigator.clipboard?.writeText(url);
    });
    row.querySelector('[data-act="revoke"]').addEventListener('click', async () => {
      if (!confirm(`确定要吊销"${t.label || '(未命名)'}"这个链接吗？`)) return;
      await apiFetch(`/api/share/tokens/${encodeURIComponent(t.token)}`, { method: 'DELETE' });
      refreshShareStatus();
    });
    shareTokenList.appendChild(row);
  });
}

shareEnabledEl.addEventListener('change', async () => {
  await apiFetch('/api/share/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: shareEnabledEl.checked }),
  });
  refreshShareStatus();
});

document.getElementById('shareCreateBtn').addEventListener('click', async () => {
  const label = document.getElementById('shareTokenLabel').value.trim();
  const expireDaysRaw = document.getElementById('shareTokenExpireDays').value.trim();
  const expires_days = expireDaysRaw ? parseInt(expireDaysRaw, 10) : null;
  try {
    await apiFetch('/api/share/tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, expires_days }),
    });
    document.getElementById('shareTokenLabel').value = '';
    document.getElementById('shareTokenExpireDays').value = '';
    refreshShareStatus();
  } catch (e) {
    // ignore
  }
});

// ---------- 两步验证 (TOTP) ----------

const totpStatusText = document.getElementById('totpStatusText');
const totpSetupBtn = document.getElementById('totpSetupBtn');
const totpDisableBtn = document.getElementById('totpDisableBtn');
const totpSetupPanel = document.getElementById('totpSetupPanel');
const totpSecretText = document.getElementById('totpSecretText');

async function refreshTotpStatus() {
  try {
    const res = await apiFetch('/api/totp/status');
    const s = await res.json();
    totpStatusText.textContent = s.enabled ? '当前状态：已开启' : '当前状态：未开启';
    totpSetupBtn.style.display = s.enabled ? 'none' : '';
    totpDisableBtn.style.display = s.enabled ? '' : 'none';
    if (s.enabled) totpSetupPanel.style.display = 'none';
  } catch (e) {
    // ignore
  }
}

totpSetupBtn.addEventListener('click', async () => {
  try {
    const res = await apiFetch('/api/totp/setup', { method: 'POST' });
    const data = await res.json();
    totpSecretText.value = data.secret;
    totpSetupPanel.style.display = 'flex';
  } catch (e) {
    // ignore
  }
});

document.getElementById('totpConfirmBtn').addEventListener('click', async () => {
  const code = document.getElementById('totpConfirmCode').value.trim();
  if (!code) return;
  try {
    const res = await apiFetch('/api/totp/enable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    if (res.ok) {
      settingsStatus.textContent = '两步验证已开启';
      setTimeout(() => { settingsStatus.textContent = ''; }, 2000);
      refreshTotpStatus();
    } else {
      const err = await res.json().catch(() => ({}));
      settingsStatus.textContent = err.detail || '验证码不正确';
    }
  } catch (e) {
    // ignore
  }
});

totpDisableBtn.addEventListener('click', async () => {
  const password = window.prompt('请输入当前登录密码以确认关闭两步验证：');
  if (!password) return;
  try {
    const res = await apiFetch('/api/totp/disable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (res.ok) {
      refreshTotpStatus();
    } else {
      const err = await res.json().catch(() => ({}));
      settingsStatus.textContent = err.detail || '密码不正确';
    }
  } catch (e) {
    // ignore
  }
});

// ---------- Prometheus 指标 ----------

const metricsEnabledEl = document.getElementById('metricsEnabled');
const metricsUrlText = document.getElementById('metricsUrlText');

async function refreshMetricsStatus() {
  try {
    const res = await apiFetch('/api/metrics/settings');
    const s = await res.json();
    metricsEnabledEl.checked = !!s.enabled;
    metricsUrlText.value = s.enabled && s.has_token ? '已启用（点击"重新生成 token"以查看完整地址）' : '';
  } catch (e) {
    // ignore
  }
}

metricsEnabledEl.addEventListener('change', async () => {
  try {
    const res = await apiFetch('/api/metrics/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: metricsEnabledEl.checked }),
    });
    const data = await res.json();
    metricsUrlText.value = data.token ? `${window.location.origin}/api/metrics?token=${data.token}` : '';
  } catch (e) {
    // ignore
  }
});

document.getElementById('metricsRegenerateBtn').addEventListener('click', async () => {
  if (!confirm('重新生成后，旧的抓取 token 会立即失效，确定继续吗？')) return;
  try {
    const res = await apiFetch('/api/metrics/regenerate', { method: 'POST' });
    const data = await res.json();
    metricsUrlText.value = data.token ? `${window.location.origin}/api/metrics?token=${data.token}` : '';
  } catch (e) {
    // ignore
  }
});

// ---------- 自定义语言性测试用例 ----------

let pendingCustomRules = [];

const customRuleType = document.getElementById('customRuleType');
const customRuleWord = document.getElementById('customRuleWord');
const customRuleWords = document.getElementById('customRuleWords');
const customRuleList = document.getElementById('customRuleList');
const customTestList = document.getElementById('customTestList');

customRuleType.addEventListener('change', () => {
  customRuleWord.style.display = customRuleType.value === 'keyword_count' ? '' : 'none';
  customRuleWords.style.display = customRuleType.value === 'forbidden_words' ? '' : 'none';
});

document.getElementById('customRuleAddBtn').addEventListener('click', () => {
  const type = customRuleType.value;
  const n = parseInt(document.getElementById('customRuleN').value, 10);
  let rule = { type };
  if (type === 'keyword_count') {
    rule.word = customRuleWord.value.trim();
    rule.count = n || 0;
  } else if (type === 'forbidden_words') {
    rule.words = customRuleWords.value.split(',').map((w) => w.trim()).filter(Boolean);
  } else {
    rule.n = n || 0;
  }
  pendingCustomRules.push(rule);
  renderPendingRules();
});

function renderPendingRules() {
  customRuleList.textContent = pendingCustomRules.length
    ? '待保存的规则：' + pendingCustomRules.map((r) => JSON.stringify(r)).join(' | ')
    : '';
}

document.getElementById('customTestSaveBtn').addEventListener('click', async () => {
  const name = document.getElementById('customTestName').value.trim();
  const prompt = document.getElementById('customTestPrompt').value.trim();
  if (!prompt) {
    settingsStatus.textContent = 'prompt 不能为空';
    return;
  }
  try {
    await apiFetch('/api/custom-tests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, prompt, rules: pendingCustomRules }),
    });
    document.getElementById('customTestName').value = '';
    document.getElementById('customTestPrompt').value = '';
    pendingCustomRules = [];
    renderPendingRules();
    refreshCustomTests();
  } catch (e) {
    // ignore
  }
});

async function refreshCustomTests() {
  try {
    const res = await apiFetch('/api/custom-tests');
    const tests = await res.json();
    if (!tests.length) {
      customTestList.innerHTML = '<p class="results-empty">还没有自定义用例。</p>';
      return;
    }
    customTestList.innerHTML = '';
    tests.forEach((t) => {
      const row = document.createElement('div');
      row.className = 'share-token-row';
      row.innerHTML = `
        <span class="share-token-row__label">${escapeHtml(t.name)}</span>
        <span class="panel__hint">${escapeHtml(t.prompt).slice(0, 60)}${t.prompt.length > 60 ? '…' : ''}</span>
        <button class="btn btn--danger" data-act="del">🗑 删除</button>
      `;
      row.querySelector('[data-act="del"]').addEventListener('click', async () => {
        if (!confirm(`确定要删除用例"${t.name}"吗？`)) return;
        await apiFetch(`/api/custom-tests/${encodeURIComponent(t.id)}`, { method: 'DELETE' });
        refreshCustomTests();
      });
      customTestList.appendChild(row);
    });
  } catch (e) {
    // ignore
  }
}

// ---------- 自定义核心测试用例（高风险：需要密码二次确认） ----------

const coreTestList = document.getElementById('coreTestList');

document.getElementById('coreTestSaveBtn').addEventListener('click', async () => {
  const name = document.getElementById('coreTestName').value.trim();
  const prompt = document.getElementById('coreTestPrompt').value.trim();
  const harness = document.getElementById('coreTestHarness').value;
  const password = document.getElementById('coreTestPassword').value;
  if (!prompt || !harness.trim()) {
    settingsStatus.textContent = 'prompt 和 harness 不能为空';
    return;
  }
  if (!password) {
    settingsStatus.textContent = '请输入密码以确认';
    return;
  }
  try {
    const res = await apiFetch('/api/custom-tests/core', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, prompt, harness, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      settingsStatus.textContent = err.detail || '保存失败，请检查密码是否正确';
      return;
    }
    document.getElementById('coreTestName').value = '';
    document.getElementById('coreTestPrompt').value = '';
    document.getElementById('coreTestHarness').value = '';
    document.getElementById('coreTestPassword').value = '';
    refreshCoreTests();
  } catch (e) {
    // ignore
  }
});

async function refreshCoreTests() {
  try {
    const res = await apiFetch('/api/custom-tests/core');
    const tests = await res.json();
    if (!tests.length) {
      coreTestList.innerHTML = '<p class="results-empty">还没有自定义核心测试用例。</p>';
      return;
    }
    coreTestList.innerHTML = '';
    tests.forEach((t) => {
      const row = document.createElement('div');
      row.className = 'share-token-row';
      row.innerHTML = `
        <span class="share-token-row__label">⚠️ ${escapeHtml(t.name)}</span>
        <span class="panel__hint">${escapeHtml(t.prompt).slice(0, 60)}${t.prompt.length > 60 ? '…' : ''}</span>
        <button class="btn btn--danger" data-act="del">🗑 删除</button>
      `;
      row.querySelector('[data-act="del"]').addEventListener('click', async () => {
        if (!confirm(`确定要删除核心测试用例"${t.name}"吗？`)) return;
        await apiFetch(`/api/custom-tests/core/${encodeURIComponent(t.id)}`, { method: 'DELETE' });
        refreshCoreTests();
      });
      coreTestList.appendChild(row);
    });
  } catch (e) {
    // ignore
  }
}

// ---------- 审计日志导出 ----------

document.getElementById('auditExportBtn').addEventListener('click', async () => {
  try {
    const res = await apiFetch('/api/audit-log/export');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'audit_log.csv';
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    // ignore
  }
});

notifyTestBtn.addEventListener('click', async () => {
  settingsStatus.textContent = '发送中…';
  try {
    const res = await apiFetch('/api/notify/test', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      settingsStatus.textContent = `发送失败：${data.detail || res.status}`;
      return;
    }
    settingsStatus.textContent = `已发送到：${(data.channels || []).join(', ')}`;
  } catch (e) {
    settingsStatus.textContent = '发送失败，请检查网络';
  }
});

// ---------- Init ----------

async function init() {
  await fetchHosts();
  try {
    const res = await apiFetch('/api/scan/status?since=0');
    const data = await res.json();
    appendLogs(data.logs);
    setRunningUI(data.running);
    if (data.results) renderResults(data.results);
    if (data.running) poll();
  } catch (e) {
    // backend not reachable yet
  }
}

init();
