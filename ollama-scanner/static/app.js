async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.href = '/login.html';
    throw new Error('unauthenticated');
  }
  return res;
}

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

// ---------- Hosts ----------

async function fetchHosts() {
  const res = await apiFetch('/api/hosts');
  const hosts = await res.json();
  renderHosts(hosts);
}

function renderHosts(hosts) {
  hostList.innerHTML = '';
  hostEmpty.style.display = hosts.length ? 'none' : 'block';
  hosts.forEach((host) => {
    const { url, enabled, favorite } = host;
    const li = document.createElement('li');
    li.className = `host-item${enabled ? '' : ' host-item--disabled'}`;
    li.innerHTML = `
      <button class="host-item__star${favorite ? ' is-lit' : ''}" title="${favorite ? '取消收藏' : '收藏此地址'}" aria-pressed="${favorite}">★</button>
      <span class="host-item__url">${escapeHtml(url)}</span>
      <label class="host-item__toggle" title="${enabled ? '启用中，参与扫描' : '已禁用，不参与扫描'}">
        <input type="checkbox" ${enabled ? 'checked' : ''} aria-label="启用 ${escapeHtml(url)}" />
        <span class="host-item__toggle-track"><span class="host-item__toggle-thumb"></span></span>
      </label>
      <button class="host-item__remove" title="移除" aria-label="移除 ${escapeHtml(url)}">×</button>
    `;
    li.querySelector('.host-item__star').addEventListener('click', () => patchHost(url, { favorite: !favorite }));
    li.querySelector('.host-item__toggle input').addEventListener('change', (e) => patchHost(url, { enabled: e.target.checked }));
    li.querySelector('.host-item__remove').addEventListener('click', () => removeHost(url));
    hostList.appendChild(li);
  });
}

async function patchHost(url, changes) {
  const res = await apiFetch('/api/hosts', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, ...changes }),
  });
  if (res.ok) {
    const hosts = await res.json();
    renderHosts(hosts);
  }
}

async function addHost(url) {
  const res = await apiFetch('/api/hosts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || '添加失败');
    return;
  }
  const hosts = await res.json();
  renderHosts(hosts);
}

async function removeHost(url) {
  const res = await apiFetch('/api/hosts', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (res.ok) {
    const hosts = await res.json();
    renderHosts(hosts);
  }
}

hostForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const val = hostInput.value.trim();
  if (!val) return;
  addHost(val);
  hostInput.value = '';
});

// ---------- Scan control ----------

async function startScan() {
  const concurrency = clampConcurrency(concurrencyNumber.value);
  const res = await apiFetch('/api/scan/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ concurrency }),
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

function appendLogs(logs) {
  if (!logs || !logs.length) return;
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logs.forEach((l) => {
    lastSeq = Math.max(lastSeq, l.seq);
    const div = document.createElement('div');
    div.className = 'log__line';
    div.innerHTML = `<span class="log__ts">${l.ts}</span>${escapeHtml(l.text)}`;
    logEl.appendChild(div);
  });
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
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `lb-host-item${host.url === lbActiveHost ? ' is-active' : ''}`;
    btn.textContent = host.url + (host.enabled ? '' : ' (已禁用)');
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
    btn.innerHTML = `
      <span class="status-dot ${dotClass}" data-role="dot"></span>
      <span class="lb-model-item__name">${escapeHtml(m.model)}</span>
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

// ---------- Leaderboard table ----------

async function refreshLeaderboardTable() {
  try {
    const res = await apiFetch('/api/leaderboard');
    const data = await res.json();
    renderLbTable(lbRanked, data.ranked, true);
    renderLbTable(lbFailed, data.failed, false);
  } catch (e) {
    // ignore, apiFetch already handles auth redirect
  }
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
    row.innerHTML = `
      <span class="lb-row__rank">${ranked ? '#' + entry.rank : '✘'}</span>
      <span class="lb-row__info">
        <div class="lb-row__model">${escapeHtml(entry.model)}</div>
        <div class="lb-row__host">@ ${escapeHtml(entry.host)}</div>
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
