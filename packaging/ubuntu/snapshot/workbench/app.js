"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, snapshot: null, snapshotReceivedAt: 0, view: "overview", refreshSequence: 0, dispatchPaused: null };
state.serviceActivations = {};
let insightActivationPosting = false;
let serviceActivationReadSequence = 0;
let flowRefreshTimer = null;
let overviewRefreshTimer = null;
let snapshotRefreshInFlight = null;
let projectMaterialCounts = null;
let projectMaterialLoading = false;
let projectActions = new Map();
let projectActionsPending = new Map();
let projectActionsTimer = null;
const OVERVIEW_REFRESH_MS = 5000;
let dispatchPolicyDirty = false;
let dispatchPolicySaving = false;
let activationUpdating = false;
let activationPosting = false;
let activationSignature = null;
const labels = {
  status: { queued: "排队中", running: "执行中", succeeded: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消" },
  kind: { image: "图片生成", video: "视频生成", quote: "视频报价", sync: "账号同步", session_import: "登录信息导入", check: "状态检查" },
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[character]);
}

function dateTime(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "时间未知";
  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 16 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function kindLabel(kind) { return labels.kind[kind] || (kind ? `其他：${kind}` : "未标注类型"); }
function statusLabel(status) { return labels.status[status] || (status ? `未知：${status}` : "状态未知"); }
function statusBadge(status) {
  const known = Object.prototype.hasOwnProperty.call(labels.status, status) ? status : "unknown";
  return `<span class="task-badge ${known}">${esc(statusLabel(status))}</span>`;
}

function empty(title, description = "") {
  return `<div class="empty"><strong>${esc(title)}</strong>${description ? `<p>${esc(description)}</p>` : ""}</div>`;
}

// Read-only official evidence. Login and the production sender are separate states.
state.channelStatus = null;
let channelRead = null;
let channelReadFailed = false;
const channelLabels = {succeeded:'最近调用成功', verification_required:'待额外核验', processing:'调用处理中', failed:'最近调用失败', unconfirmed:'调用结果未确认', untested:'未实测'};
function officialAccount(account) {
  const key = String(account.key || '').replace(/^flow-account-/, '');
  const data = !channelReadFailed && state.channelStatus?.available ? state.channelStatus.accounts.find(a => a.account_key === key) : null;
  const call = data?.latest_call;
  const stale = !!call && Date.now()/1000 - call.created > 86400;
  const paused = data?.paused === true || account.gemini_enabled === false;
  const outcome = call?.outcome || 'untested';
  const label = channelReadFailed || !state.channelStatus?.available ? '实测状态未读取'
    : call ? channelLabels[outcome] || '状态未知' : paused ? '未实测 · 自动派发暂停' : '未实测';
  return {data, call, outcome, paused, stale, label,
    attention: !!data && !paused && !stale && ['verification_required','failed'].includes(outcome)};
}
function officialSummary() {
  if (channelReadFailed || !state.channelStatus?.available) return '官方实测记录暂不可读取；不据此判断账号故障。';
  const accounts = state.snapshot?.real_accounts?.items || [];
  const records = accounts.map(officialAccount);
  const count = kind => records.filter(r => r.outcome === kind).length;
  return `当前可派发 ${records.filter(r=>r.data?.dispatch_eligible).length} / ${records.length} · 手动暂停 ${records.filter(r=>r.paused).length}。最近调用记录：成功 ${count('succeeded')} · 待核验 ${count('verification_required')} · 未实测 ${count('untested')}${['processing','failed','unconfirmed'].filter(k=>count(k)).map(k=>' · '+channelLabels[k]+' '+count(k)).join('')}`;
}
function officialDetails(record) {
  const call = record.call;
  return `<p>通道：官方 Antigravity CLI · ${state.channelStatus?.production_send_available ? '已接入工作台账号池' : '暂不可用'}。</p><p>登录授权：${record.data?.authorization === 'saved_verified_identity' ? '本地已保存且账号身份一致；不保证下一次官方请求一定通过' : '本地授权未就绪'}。</p><p>额外核验：${call?.verification === 'not_required_for_this_call' ? '该次调用未受拦截' : call?.verification === 'page_error_500' ? '记录中的官方核验页返回 500，不能据此认定账号失效' : call?.verification === 'required' ? '该次调用要求额外核验' : '未单独读取'}</p><p>最近真实调用：${esc(channelLabels[record.outcome] || '未实测')}${call ? ` · ${esc(dateTime(call.created))}` : ''}。${record.stale ? '记录超过 24 小时，仅作历史参考。' : '仅证明该次结果，不保证下次可用。'}</p><p>来源：已保存的官方实测请求${state.channelStatus?.read_at ? ` · 读取于 ${esc(dateTime(state.channelStatus.read_at))}` : '（暂未读取）'}。</p>`;
}
async function refreshChannelStatus() {
  if (channelRead) return channelRead;
  channelRead = (async () => {
    try {
      const data = await getJson('/api/channel-status', {timeoutMs:8000});
      if (data.schema_version !== 1 || data.available !== true || !Array.isArray(data.accounts)) throw new Error('unknown');
      state.channelStatus = data; channelReadFailed = false;
    } catch (_) { channelReadFailed = true; }
    renderConnections();
    if (state.snapshot) {
      renderAccountInsights(state.snapshot, $('overviewInsights'));
      renderAccounts();
    }
    if (currentConversation) syncConversationControls(true);
  })().finally(() => {channelRead = null;});
  return channelRead;
}
function officialConversation(item) {
  return ['official_antigravity_cli_isolated','official_antigravity_cli'].includes(state.channelStatus?.conversations?.[item.id]);
}
function safeRecordedNote(value) {
  return String(value || '').replace(/https?:\/\/[^\s<>"']+/gi, '（记录中的链接已隐藏）')
    .replace(/Bearer\s+\S+/gi, 'Bearer [已隐藏]')
    .replace(/(?:access_token|refresh_token|authorization_code|code_verifier|cookie)\s*[:=]\s*[^\s,;]+/gi, '[授权信息已隐藏]');
}
function renderGeminiReply(record) {
  if (['queued','dispatching','running'].includes(record.status)) return `<p class="subtle">${record.status==='queued' ? '排队中，等待并发空位。' : '官方调用处理中。'} 不会重复提交。</p>`;
  const text=record.response ? `<p>${esc(record.response).replace(/\n/g,'<br>')}</p>` : '';
  const images=(record.images || []).filter(url=>/^\/api\/gemini-images\/[0-9a-f-]{36}\/[0-3]$/.test(url)).map(url=>`<a href="${esc(url)}" target="_blank" rel="noopener"><img src="${esc(url)}" alt="历史生成图片" style="max-width:100%">查看或下载图片</a>`).join('');
  const status=record.status==='unknown' ? '<p class="subtle">结果未确认，记录保留，不会自动重试或换号。</p>' : record.status==='failed' ? `<p class="subtle">${esc(safeRecordedNote(record.note || '本轮未成功，已停止。'))}</p>` : '';
  return text+images+status;
}
// Each issue reflects recorded state only; it does not change account eligibility.
function accountIssues(account) {
  const issues = [];
  const add = (key, title, impact, action) => issues.push({key, title, impact, action});
  if (account.flow_risk_paused === true || account.flow_health === 'paused')
    add('flow_paused', 'Flow 已暂停，需核查', '该账号的 Flow 暂不能用于生成；Gemini 状态单独判断。', '核对原账号状态后，再决定是否恢复 Flow。');
  else if (account.flow_cooling === true || account.flow_health === 'cooling')
    add('flow_cooling', 'Flow 冷却中', '该账号的 Flow 暂时等待；Gemini 状态单独判断。', '等待冷却结束后核对状态。');
  else if (account.flow_health === 'empty')
    add('flow_empty', 'Flow 积分不足', '该账号暂不能用于需要积分的 Flow 任务。', '核对积分余额。');
  else if (['attention', 'unknown'].includes(account.flow_health))
    add('flow_unknown', 'Flow 状态待核对', '目前无法确认该账号的 Flow 是否可用。', '查看账号详情并核对 Flow 状态。');

  const official = officialAccount(account);
  if (official.attention) add('official_call', official.label,
    '这是该账号最近一次官方独立调用的记录；Flow 单独判断。', '查看详情中的时间与来源，不会自动重试。');
  return issues;
}

function geminiAccountAvailability(account) {
  if (account.gemini_channel === 'official_antigravity_cli') {
    const item = officialAccount(account).data;
    const permitted = !channelReadFailed && state.channelStatus?.production_send_available === true && item?.dispatch_eligible === true;
    const reasons = {dispatch_paused:'已手动暂停', unresolved_request:'官方通道有结果待核对', official_call_blocked:'官方授权或调用受限', authorization_not_ready:'官方授权未就绪'};
    return {manual:permitted, auto:permitted, reason:permitted ? (account.gemini_busy ? '可排队 · 官方调用' : '官方授权就绪') : reasons[item?.restriction] || '官方调用暂不可用'};
  }
  if (account.gemini_enabled === false) return {manual: false, auto: false, reason: '已暂停 Gemini'};
  if (account.gemini_busy) return {manual: false, auto: false, reason: '正在处理，请稍候'};
  if (account.gemini_unresolved > 0) return {manual: false, auto: false, reason: '有结果未确认，需先核对'};
  if (account.gemini_auto_state === 'browser_unknown') return {manual: false, auto: false, reason: '浏览器状态待核对'};
  if (account.gemini_auto_state === 'browser_unavailable') return {manual: false, auto: false, reason: '账号浏览器未就绪'};
  if (account.gemini !== 'verified') {
    const browserPaused = /浏览器未确认运行/.test(account.keepalive_note || '');
    return {manual: false, auto: false, reason: browserPaused ? '账号浏览器待核对' : '登录或保活待核对'};
  }
  const automatic = ['eligible', 'refresh_needed'].includes(account.gemini_auto_state);
  const reason = account.gemini_auto_state === 'refresh_needed' ? '可选择，发送前核对用量'
    : account.gemini_auto_state === 'usage_unknown' ? '可手选，自动调度用量待核对'
    : account.gemini_auto_state === 'limit_reached' ? '可手选，已达到自动调度上限' : '上次登录核对通过';
  return {manual: true, auto: automatic, reason};
}

function geminiSelectionReady(value) {
  const data = state.snapshot?.real_accounts;
  if (!data?.available || !Array.isArray(data.items)) return false;
  if (value === 'auto') return data.items.some((item) => geminiAccountAvailability(item).auto);
  const account = data.items.find((item) => item.key === 'flow-account-' + value);
  return !!account && geminiAccountAvailability(account).manual;
}

function renderGeminiAccountOptions(select, accounts) {
  const selected = select.value;
  const autoCount = accounts.filter((item) => geminiAccountAvailability(item).auto).length;
  select.innerHTML = `<option value="auto"${autoCount ? '' : ' disabled'}>轮询分配账号（${autoCount ? autoCount + ' 个可参与' : '暂无符合条件账号'}）</option>`
    + accounts.map((item) => {
      const availability = geminiAccountAvailability(item);
      return `<option value="${esc(item.key.slice('flow-account-'.length))}"${availability.manual ? '' : ' disabled'}>${esc(item.email || item.label || item.profile)} · ${esc(availability.reason)}</option>`;
    }).join('');
  select.value = [...select.options].some((option) => option.value === selected) ? selected : 'auto';
}

function syncQuickGeminiControls() {
  $('geminiSend').disabled = true; // Legacy Gemini sender removed.
}

function setView(view) {
  if (!["overview", "accounts", "projects", "tasks", "conversations", "library", "api"].includes(view)) view = "overview";
  state.view = view;
  for (const name of ["overview", "accounts", "projects", "tasks", "conversations", "library", "api"]) $(name + "View").hidden = name !== view;
  const display = { overview: "总览", accounts: "账号", projects: "项目", tasks: "任务", conversations: "对话", library: "素材库", api: "API 文档" };
  $("breadcrumb").textContent = display[view];
  document.querySelectorAll(".navigation [data-view]").forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  history.replaceState(null, "", view === "overview" ? "/" : `/#${view}`);
  window.scrollTo({ top: 0, behavior: "auto" });
  if (view === "accounts") { refreshChannelStatus(); renderAccounts(); refreshOfficialQuota(); refreshGeminiActivity(); refreshActivation(); }
  if (view === "conversations") { refreshChannelStatus(); loadConversations(); }
  if (view === "library") loadLibrary();
  if (view === "projects") { loadProjectMaterialCounts(); loadProjectActions(); }
  if (view === "api") loadApiKeys();
  scheduleFlowRefresh();
  scheduleOverviewRefresh();
  if (view === 'overview' && state.snapshotReceivedAt) refresh({background: true});
}

function scheduleOverviewRefresh() {
  if (overviewRefreshTimer) clearTimeout(overviewRefreshTimer);
  overviewRefreshTimer = null;
  if (state.view === 'overview' && !document.hidden && !snapshotRefreshInFlight) {
    overviewRefreshTimer = setTimeout(() => {
      overviewRefreshTimer = null;
      refresh({background: true});
    }, OVERVIEW_REFRESH_MS);
  }
}

document.addEventListener('visibilitychange', () => {
  scheduleOverviewRefresh();
  if (!document.hidden && state.view === 'overview') refresh({background: true});
});
window.addEventListener('focus', () => {
  if (!document.hidden && state.view === 'overview') refresh({background: true});
});

function scheduleFlowRefresh() {
  if (flowRefreshTimer) clearTimeout(flowRefreshTimer);
  flowRefreshTimer = null;
  if (state.view === "tasks" && state.snapshot?.tasks?.some((task) => ["queued", "running"].includes(task.status))) {
    flowRefreshTimer = setTimeout(() => { flowRefreshTimer = null; refresh(); }, 10000);
  }
}

function renderConnections() {
  for (const service of ["flow", "gemini"]) {
    const data = state.status?.[service];
    const pill = $(service + "Status");
    const description = $(service + "StatusText");
    if (service === "gemini") {
      $('geminiName').textContent = 'Gemini · 官方独立通道';
      pill.className = 'status-pill neutral';
      pill.textContent = state.channelStatus?.production_send_available ? '官方调用已接入' : '官方调用暂不可用';
      description.textContent = officialSummary();
      continue;
    }
    if (!data) {
      pill.className = "status-pill neutral";
      pill.textContent = "状态未知";
      description.textContent = "暂时无法检查连接";
    } else if (data.connected) {
      pill.className = "status-pill online";
      pill.textContent = "已连接";
      description.textContent = service === "flow" ? "管理后台可响应" : (data.description || "Gemini 后台可响应");
    } else {
      pill.className = "status-pill offline";
      pill.textContent = "未连接";
      description.textContent = data.message || "服务没有返回正常状态";
    }
  }
}

function renderOverview() {
  const snapshot = state.snapshot;
  renderOverviewCharts(snapshot);
  renderAccountInsights(snapshot, $('overviewInsights'));
  const dataNotice = snapshot?.flow_state?.simulated ? "当前来源不是正式后台，不能作为真实账号或任务使用。" : "Flow 保留原后台；Gemini 使用官方调用。新对话轮询分配，多轮固定账号。";
  $("sourceNotice").textContent = dataNotice + (snapshot?.flow_state?.skipped_projects || snapshot?.flow_state?.skipped_tasks ? " 有格式异常的记录未显示，请在原后台查看。" : "");
  const readable = snapshot?.flow_state?.available === true;
  const projects = readable ? snapshot.projects : [];
  const tasks = readable ? snapshot.tasks : [];
  const library = snapshot?.library;
  $("projectCount").textContent = readable ? projects.length.toLocaleString("zh-CN") : "—";
  $("taskCount").textContent = readable ? tasks.length.toLocaleString("zh-CN") : "—";
  $("materialCount").textContent = library?.available && Number.isInteger(library.counts?.total) ? library.counts.total.toLocaleString("zh-CN") : "—";
  $("materialBreakdown").textContent = library?.available && library.counts ? `图片 ${library.counts.images ?? "未知"} · 视频 ${library.counts.videos ?? "未知"}` : (library?.message || "暂时无法读取素材库");
  if (!readable) {
    const reason = snapshot?.flow_state?.message || "Flow 后台暂时无法读取";
    $("recentProjects").innerHTML = empty("项目暂不可读取", reason);
    $("recentTasks").innerHTML = empty("任务暂不可读取", reason);
    return;
  }
  $("recentProjects").innerHTML = projects.length ? projects.slice(0, 5).map((item) => `
    <div class="compact-row"><span class="row-icon" aria-hidden="true">▤</span><div class="row-info"><span class="row-title" title="${esc(item.title)}">${esc(item.title)}</span><span class="row-sub">Flow · ${esc(item.source_profile)}</span></div><span class="row-end">${esc(dateTime(item.checked).split(" ")[0])}</span></div>`).join("") : empty("还没有已登记项目", "原后台读取或登记后会显示在这里。");
  $("recentTasks").innerHTML = tasks.length ? tasks.slice(0, 5).map((item) => `
    <div class="compact-row"><span class="row-icon" aria-hidden="true">◷</span><div class="row-info"><span class="row-title" title="${esc(item.project_title)}">${esc(item.project_title)}</span><span class="row-sub">${esc(kindLabel(item.kind))} · ${esc(dateTime(item.created))}</span>${taskFailureMarkup(item)}</div>${statusBadge(item.status)}</div>`).join("") : empty("暂时没有任务记录", "新任务仍通过原后台创建。");
}

function overviewDistribution(target, title, caption, parts) {
  const known = parts.every(part => Number.isInteger(part.value) && part.value >= 0);
  const total = known ? parts.reduce((sum, part) => sum + part.value, 0) : 0;
  const list = known ? parts.map(part => `<div><span><i class="chart-dot ${part.color}" aria-hidden="true"></i>${esc(part.label)}</span><strong>${part.value.toLocaleString('zh-CN')}</strong></div>`).join('') : '<p class="chart-unavailable">暂时无法读取数据</p>';
  const bar = total ? parts.map(part => `<i class="${part.color}" style="width:${(part.value / total * 100).toFixed(2)}%"></i>`).join('') : '';
  target.innerHTML = `<div class="overview-chart-header"><div><span class="section-kicker">${esc(title === '任务分布' ? 'TASKS' : 'LIBRARY')}</span><h2>${esc(title)}</h2></div><strong>${known ? total.toLocaleString('zh-CN') : '—'}</strong></div><p>${esc(caption)}</p><div class="overview-chart-track ${total ? '' : 'is-empty'}" aria-hidden="true">${bar}</div><div class="overview-chart-legend">${list}</div>`;
}

function renderOverviewCharts(snapshot) {
  const available = snapshot?.flow_state?.available === true && Array.isArray(snapshot?.tasks);
  const tasks = available ? snapshot.tasks : [];
  overviewDistribution($('overviewTaskChart'), '任务分布', '原后台最近记录，含所有任务类型。', [
    {label:'已完成', color:'chart-green', value: available ? tasks.filter(x => x.status === 'succeeded').length : null},
    {label:'失败', color:'chart-rust', value: available ? tasks.filter(x => x.status === 'failed').length : null},
    {label:'其他状态', color:'chart-slate', value: available ? tasks.filter(x => !['succeeded','failed'].includes(x.status)).length : null},
  ]);
  const counts = snapshot?.library?.available ? snapshot.library.counts : null;
  const complete = counts && Number.isInteger(counts.total) && Number.isInteger(counts.images) && Number.isInteger(counts.videos) && counts.total >= counts.images + counts.videos;
  overviewDistribution($('overviewMaterialChart'), '素材分布', '现有素材库中的已登记素材。', [
    {label:'图片', color:'chart-green', value: complete ? counts.images : null},
    {label:'视频', color:'chart-blue', value: complete ? counts.videos : null},
    {label:'其他', color:'chart-slate', value: complete ? counts.total - counts.images - counts.videos : null},
  ]);
}

async function loadProjectMaterialCounts() {
  if (projectMaterialLoading) return;
  projectMaterialLoading = true;
  try {
    const counts = new Map(), seen = new Set();
    let expectedTotal = null, expectedPages = null;
    for (let page = 1; page <= 100; page++) {
      const data = await getJson(`/api/library?page=${page}&limit=100`, {timeoutMs: 15000});
      if (!Array.isArray(data.items) || !Number.isInteger(data.total) || data.total < 0 ||
          !Number.isInteger(data.pages) || data.pages < 1 || page > data.pages ||
          (expectedTotal !== null && (data.total !== expectedTotal || data.pages !== expectedPages))) throw new Error('incomplete');
      if (expectedTotal === null) { expectedTotal = data.total; expectedPages = data.pages; }
      for (const item of data.items) {
        if (!item || !/^[0-9a-f]{64}$/.test(item.id) || seen.has(item.id) || !Array.isArray(item.origins)) throw new Error('incomplete');
        seen.add(item.id);
        const projects = new Set(item.origins.map(o => o?.project_id).filter(id => typeof id === 'string' && id));
        for (const id of projects) counts.set(id, (counts.get(id) || 0) + 1);
      }
      if (page === expectedPages) {
        if (seen.size !== expectedTotal) throw new Error('incomplete');
        projectMaterialCounts = counts;
        break;
      }
    }
  } catch (_error) { projectMaterialCounts = null; }
  finally { projectMaterialLoading = false; if (state.view === 'projects') renderProjects(); }
}

async function loadProjectActions() {
  if (projectActionsTimer) clearTimeout(projectActionsTimer);
  projectActionsTimer = null;
  try {
    const data = await getJson('/api/project-actions', {timeoutMs:10000});
    if (!Array.isArray(data.items)) throw new Error('invalid');
    const next = new Map();
    for (const item of data.items) if (item?.project_key && !next.has(item.project_key)) next.set(item.project_key,item);
    const finished = [...next.entries()].some(([key,value]) => value.status !== 'running' && projectActions.get(key)?.id === value.id && projectActions.get(key)?.status === 'running');
    for (const [key,requestId] of projectActionsPending) if (next.get(key)?.id === requestId) projectActionsPending.delete(key);
    projectActions = next;
    if (state.view === 'projects') renderProjects();
    if (finished) { refresh({background:true}); loadProjectMaterialCounts(); }
    if (state.view === 'projects' && [...next.values()].some(x => x.status === 'running')) {
      projectActionsTimer = setTimeout(loadProjectActions,2000);
    }
  } catch (_error) {
    if (state.view === 'projects') projectActionsTimer = setTimeout(loadProjectActions,10000);
  }
}

async function startProjectAction(key,kind) {
  const project = (state.snapshot?.projects || []).find(p => p.key === key);
  if (!project || projectActionsPending.has(key) || projectActions.get(key)?.status === 'running') return;
  if (kind === 'delete' && !confirm(`确定删除云端项目“${project.title}”吗？项目内尚未保存的素材和编辑内容也会删除，素材库中已保存的文件会保留。如它是默认或备用项目，对应设置也会清除。`)) return;
  const requestId=crypto.randomUUID();
  projectActionsPending.set(key,requestId); renderProjects();
  try {
    await postJson('/api/project-actions',{project_key:key,kind},requestId);
    await loadProjectActions();
  } catch (error) {
    if (error.status) {
      projectActionsPending.delete(key);
      projectActions.set(key,{status:'failed',detail:{message:error.message}});
    } else {
      projectActions.set(key,{status:'unknown',detail:{message:'提交结果待核对，请勿重复操作。'}});
    }
    renderProjects();
  }
}

function renderProjects() {
  const snapshot = state.snapshot;
  if (!snapshot?.flow_state?.available) {
    $("projectListSummary").textContent = "暂时无法读取项目";
    $("projectTable").innerHTML = empty("项目暂不可读取", snapshot?.flow_state?.message || "请检查 Flow 后台连接。");
    return;
  }
  const query = $("projectSearch").value.trim().toLocaleLowerCase("zh-CN");
  const accounts = new Map((snapshot.real_accounts?.items || []).map((account) => [account.profile, account.email || account.label || ""]));
  const accountName = (item) => accounts.get(item.source_profile) || "账号名称未获取";
  const taskByProject = new Map();
  for (const task of snapshot.tasks || []) {
    if (!task.project_key) continue;
    const current = taskByProject.get(task.project_key) || { last: 0, active: false };
    const last = Math.max(Number(task.created) || 0, Number(task.finished) || 0);
    current.last = Math.max(current.last, last);
    current.active ||= ['queued', 'running'].includes(task.status);
    taskByProject.set(task.project_key, current);
  }
  const projects = snapshot.projects.filter((item) => [item.title, item.source_id, item.source_profile, accountName(item)].some((value) => value.toLocaleLowerCase("zh-CN").includes(query)));
  $("projectListSummary").textContent = (query ? `找到 ${projects.length} 项，共 ${snapshot.projects.length} 项` : `共 ${snapshot.projects.length} 项 · 保留原始编号与来源`) + (snapshot.flow_state.skipped_projects ? ` · ${snapshot.flow_state.skipped_projects} 项格式异常` : "");
  if (!projects.length) {
    $("projectTable").innerHTML = empty(query ? "没有找到符合条件的项目" : "还没有已登记项目", query ? "试试其他名称或编号。" : "请从原后台读取或登记项目。");
    return;
  }
  $("projectTable").innerHTML = `<table><thead><tr><th>项目名称</th><th>所属账号</th><th>最近工作台任务</th><th>素材入库记录</th><th>生命周期状态</th><th>操作</th></tr></thead><tbody>${projects.map((item) => {
    const task = taskByProject.get(item.key);
    const activity = task?.last ? dateTime(task.last) : '无工作台任务记录';
    const action = projectActions.get(item.key);
    const detail = action?.detail || {};
    const progress = action?.kind === 'transfer' && Number.isFinite(detail.total) ? `已识别 ${detail.total} · 新增 ${detail.added || 0} · 已有 ${detail.skipped || 0} · 失败 ${detail.failed || 0}${Number.isFinite(detail.tag_queued) ? ` · 标签排队 ${detail.tag_queued}` : ''}` : '';
    const busy = projectActionsPending.has(item.key) || action?.status === 'running' || (action?.kind === 'delete' && action?.status === 'unknown');
    const status = task?.active ? '任务进行中' : '待核对';
    const material = projectMaterialCounts === null ? (projectMaterialLoading ? '读取中' : '暂不可读取') : `已登记 ${projectMaterialCounts.get(item.source_id) || 0} 份`;
    return `<tr><td class="table-title"><strong>${esc(item.title)}</strong><br><span class="subtle" title="完整项目编号">${esc(item.source_id)}</span>${item.title_source === 'saved_flow_name' ? `<br><span class="subtle">${item.title_origin === 'browser_history' ? '浏览器历史名称 · 最后访问于' : 'Flow 名称 · 核对于'} ${esc(dateTime(item.title_observed))}</span>` : item.title === "未命名项目" ? '<br><span class="subtle">原后台未提供可确认的名称</span>' : ''}</td><td>${esc(accountName(item))}</td><td>${esc(activity)}<br><span class="subtle">不含网页内其他操作</span></td><td title="登记数量不代表云端素材已全部入库">${esc(material)}<br><span class="subtle">完整性待核对</span></td><td><span class="source-chip" title="需要核对项目时间和全部云端素材是否已保存">${status}</span>${detail.message ? `<br><span class="subtle">${esc(detail.message)}</span>` : ''}${progress ? `<br><span class="subtle">${esc(progress)}</span>` : ''}${detail.tag_message ? `<br><span class="subtle">${esc(detail.tag_message)}</span>` : ''}</td><td><button class="button button-light" type="button" data-project-action="transfer" data-project-key="${esc(item.key)}" title="只处理未入库的素材；新增素材将排队使用 Gemini 打标签" ${busy?'disabled':''}>转移资产到素材库</button> <button class="button button-light" type="button" data-project-action="delete" data-project-key="${esc(item.key)}" ${busy||task?.active?'disabled':''}>删除项目</button></td></tr>`;
  }).join("")}</tbody></table>`;
}

function taskFailureMarkup(item) {
  const labels = {task_window_binding_created:'建立账号、网页与桌面窗口的唯一绑定',task_window_binding_reused:'重新核对并复用固定窗口绑定',task_window_binding_repaired:'恢复变化后的窗口绑定',task_preparation_started:'开始准备',fixed_window_reused:'复用账号固定窗口',fixed_window_created:'建立账号固定窗口',
    preparation_recovery:'尝试恢复页面',preparation_recovered:'页面恢复成功',project_page_reload:'重新加载原项目页面',desktop_overview_dismissed:'收起桌面总览，恢复浏览器前台',
    input_preparation_recovery:'恢复窗口焦点与输入准备',paste_started:'开始单次粘贴',input_preparation_stopped:'输入准备停止，未重复粘贴',
    original_image_result_lookup:'查询原图片结果',original_video_address_lookup:'补取原视频地址',
    original_download_retry:'重新下载原文件',original_download_recovered:'原文件下载恢复成功',refresh_original_download_address:'刷新原作品下载地址',window_retained:'保留固定窗口'};
  labels.login_page_recheck='重新核对原账号登录页面';
  const events=(item.self_recovery || []).filter(e=>labels[e.action]);
  const failure=item.status==='failed' ? `<span class="task-failure-reason">失败原因：${esc(item.failure_reason || '后台未提供具体失败原因。')}</span>` : '';
  return failure + (events.length ? `<details class="task-recovery-record"><summary>调度与自救记录（${events.length}）</summary><ul>${events.map(e=>`<li>${esc(e.created ? dateTime(e.created) : '')} ${esc(labels[e.action])}</li>`).join('')}</ul></details>` : '');
}

function renderTaskWindows() {
  const table = $('taskWindowsTable');
  const summary = $('taskWindowsSummary');
  if (!table || !summary) return;
  const snapshot = state.snapshot;
  const coordination = snapshot?.flow_coordination;
  if (!snapshot?.flow_state?.available || !coordination || coordination.window_bindings_available !== true) {
    summary.textContent = '窗口绑定暂不可读取';
    table.innerHTML = empty('窗口状态待核对', '读取失败不会解除账号限制或重新发送任务。');
    return;
  }
  const bindings = new Map((coordination.window_bindings || []).map(b => [b.profile, b]));
  const windows = new Map((coordination.windows || []).map(w => [w.profile, w]));
  const lifecycle = new Map((coordination.window_lifecycle || []).map(w => [w.profile, w]));
  const accounts = snapshot.real_accounts?.items || [];
  const labels = {idle:'空闲',preparing:'准备中',submitting:'发送核对中',submitted:'等待结果',generated:'取回成果',result_unknown:'原任务待核对',blocked_draft:'保留草稿',needs_verification:'等待登录或验证'};
  const verified = accounts.filter(a => lifecycle.get(a.profile)?.unique_ok === 1).length;
  const actual = [...lifecycle.values()].reduce((sum,w) => sum + (w.flow_windows || 0),0);
  const inventoryText = coordination.window_lifecycle_available === true && lifecycle.size ? `上次盘点 ${actual} 扇 Flow 窗口 · ${verified} 个窗口唯一性达标` : '实际窗口尚未盘点或暂不可读取';
  summary.textContent = `${accounts.length} 个账号 · ${inventoryText} · 空闲30分钟保活`;
  const outcomes = {completed:'保活完成',cooling_read_only:'冷却中，仅检查状态',tasks_have_priority:'任务优先，已避让',account_busy:'账号正在使用',account_paused:'账号已暂停',window_not_idle:'原窗口状态待核对',window_not_unique:'窗口未唯一',manual_login_open:'人工登录中',foreground_busy:'前台正被使用',not_due:'尚未到期',maintenance:'维护期间暂缓',account_not_verified:'账号需核对',flow_window_protected:'页面有需保留内容',keepalive_safe_area_unconfirmed:'未确认安全交互区域',desktop_focus_unconfirmed:'前台焦点未确认'};
  table.innerHTML = accounts.length ? `<table><thead><tr><th>账号</th><th>实际窗口 / 页面</th><th>任务窗口与项目</th><th>任务状态</th><th>空闲保活</th></tr></thead><tbody>${accounts.map(a => {
    const binding = bindings.get(a.profile);
    const taskWindow = windows.get(a.profile);
    const life = lifecycle.get(a.profile);
    const project = binding && (snapshot.projects || []).find(p => p.source_profile === a.profile && binding.project_url === `https://flow.google.com/project/${p.source_id}`);
    const text = binding?.binding_state === 'verified' ? '上次绑定已核对' : binding ? '绑定需重新核对' : '尚未核对绑定';
    const count = life ? `${life.flow_windows} 扇 / ${life.flow_pages} 页 · ${life.unique_ok ? '唯一性达标' : '未达标'}` : '实际数量待盘点';
    const maintenance = life?.last_outcome ? (outcomes[life.last_outcome] || '保活未完成，已保留现场') : '等待首次空闲保活';
    return `<tr><td>${esc(a.email || a.label || a.profile)}</td><td>${esc(count)}${life?.protected ? '<br><span class="subtle">有内容需保护，未关闭</span>' : ''}${life?.observed ? `<br><span class="subtle">${esc(dateTime(life.observed))}</span>` : ''}</td><td>${esc(text)}${binding ? `<br><span class="subtle">固定窗口 ${esc(String(binding.window_id))}</span>` : ''}<br>${esc(project?.title || (binding ? '项目登记待核对' : '等待任务准备'))}</td><td>${esc(labels[taskWindow?.state] || '待准备')}${a.flow_cooling ? '<br><span class="subtle">账号冷却限制保留</span>' : ''}</td><td>${esc(maintenance)}${life?.last_keepalive ? `<br><span class="subtle">上次 ${esc(dateTime(life.last_keepalive))}</span>` : ''}${life?.next_due ? `<br><span class="subtle">下次不早于 ${esc(dateTime(life.next_due))}</span>` : ''}</td></tr>`;
  }).join('')}</tbody></table>` : empty('尚无账号窗口', '接入账号后，在发送前建立和核对窗口绑定。');
}

function renderTasks() {
  renderTaskWindows();
  const snapshot = state.snapshot;
  if (!snapshot?.flow_state?.available) {
    $("taskListSummary").textContent = "暂时无法读取任务";
    $("taskTable").innerHTML = empty("任务暂不可读取", snapshot?.flow_state?.message || "请检查 Flow 后台连接。");
    return;
  }
  const query = $("taskSearch").value.trim().toLocaleLowerCase("zh-CN");
  const status = $("taskStatusFilter").value;
  const kindFilter = $("taskKindFilter");
  const selectedKind = kindFilter.value;
  const kinds = [...new Set(snapshot.tasks.map((item) => String(item.kind || "")))];
  if (selectedKind && !kinds.includes(selectedKind.slice(5))) kinds.push(selectedKind.slice(5));
  kinds.sort((a, b) => kindLabel(a).localeCompare(kindLabel(b), "zh-CN"));
  kindFilter.replaceChildren(new Option("全部类型", ""), ...kinds.map((kind) => new Option(kindLabel(kind), `kind:${kind}`)));
  kindFilter.value = selectedKind;
  const tasks = snapshot.tasks.filter((item) => (!status || item.status === status) && (!selectedKind || `kind:${item.kind || ""}` === selectedKind) && [item.project_title, item.source_id, kindLabel(item.kind)].some((value) => value.toLocaleLowerCase("zh-CN").includes(query)));
  const filtered = query || status || selectedKind;
  $("taskListSummary").textContent = (filtered ? `找到 ${tasks.length} 条，共 ${snapshot.tasks.length} 条` : `共 ${snapshot.tasks.length} 条 · 原后台最近记录`) + (snapshot.flow_state.skipped_tasks ? ` · ${snapshot.flow_state.skipped_tasks} 条格式异常` : "");
  if(snapshot.flow_coordination?.window_policy==='one_per_account') {
    const held=(snapshot.flow_coordination.windows || []).filter(w=>['submitting','submitted','generated','result_unknown'].includes(w.state)).length;
    $('taskListSummary').textContent += ` · 每账号固定窗口、同账号排队 · 自动重新生成与换号关闭${held ? ` · ${held} 个窗口处理中或待核对` : ''}`;
  }
  if (!tasks.length) {
    $("taskTable").innerHTML = empty(filtered ? "没有找到符合条件的任务" : "暂时没有任务记录", filtered ? "可调整搜索内容、任务类型或状态。" : "新任务仍通过原后台创建。");
    return;
  }
  const byProfile = new Map((snapshot.real_accounts?.items || []).map(a => [a.profile, a.email || a.label || a.profile]));
  $("taskTable").innerHTML = `<table><thead><tr><th>任务类型</th><th>项目 / 账号</th><th>状态</th><th>提交时间</th><th>任务编号</th><th>结果</th><th>来源</th></tr></thead><tbody>${tasks.map((item) => `
    <tr class="task-kind-${["image", "video", "quote", "sync", "session_import", "check"].includes(item.kind) ? item.kind : "other"}"><td><strong>${esc(kindLabel(item.kind))}</strong>${item.retry ? `<br><span class="subtle">第 ${item.retry.ordinal + 1} 次 · ${esc(item.retry.reason === 'same_account' ? '原账号重试' : item.retry.reason === 'switch_account' ? '切号重试' : '原任务')}</span>` : ''}</td><td class="table-title">${esc(item.project_title)}<br><span class="subtle">${esc(byProfile.get(item.source_profile) || item.source_profile)}</span></td><td>${statusBadge(item.status)}${item.note ? `<br><span class="subtle">${esc(item.note)}</span>` : ""}</td><td>${esc(dateTime(item.created))}</td><td><code title="${esc(item.source_id)}">${esc(shortId(item.source_id))}</code>${item.retry && item.retry.ordinal ? `<br><span class="subtle" title="${esc(item.retry.root_id)}">原任务 ${esc(shortId(item.retry.root_id))}</span>` : ''}</td><td>${taskFailureMarkup(item)}${(item.downloads || []).map((file, index) => `<a class="text-link" href="http://127.0.0.1:18888${esc(file.path)}" target="_blank" rel="noopener noreferrer">下载${index + 1}</a>`).join(" ")}${item.recovered_path ? `<a class="text-link" href="${esc(item.recovered_path)}" target="_blank" rel="noopener noreferrer">打开已找回图片</a>` : ""}${item.recoverable ? `<button class="text-link" type="button" data-recover-flow="${esc(item.source_id)}">找回已生成图片</button>` : ""}${item.status !== "failed" && !(item.downloads || []).length && !item.recovered_path && !item.recoverable ? "—" : ""}</td><td><span class="source-chip">Flow</span></td></tr>`).join("")}</tbody></table>`;
}

$("taskTable").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-recover-flow]");
  if (!button) return;
  button.disabled = true;
  button.textContent = "正在找回…";
  try {
    const response = await fetch(`/api/real-pool/recoveries/${encodeURIComponent(button.dataset.recoverFlow)}`, {
      method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Workbench-Request": "1"}, body: "{}"
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "暂时无法找回。 ");
    await refresh();
  } catch (error) {
    button.textContent = "找回失败，可再尝试";
    button.disabled = false;
  }
});

function currentGeminiSummary(pool) {
  if (pool?.available !== true || !Array.isArray(pool.items) ||
      !pool.items.every(item => item && typeof item.gemini === 'string' && typeof item.gemini_auto_state === 'string')) return null;
  const counts = {automatic: 0, refresh: 0, limited: 0, unresolved: 0, busy: 0, other: 0};
  for (const item of pool.items) {
    if (geminiAccountAvailability(item).auto) {
      counts.automatic++;
      if (item.gemini_auto_state === 'refresh_needed') counts.refresh++;
    } else if (item.gemini_unresolved > 0) counts.unresolved++;
    else if (item.gemini_busy || item.gemini_auto_state === 'busy') counts.busy++;
    else if (item.gemini_check_reason === 'renewal_rate_limited' && item.gemini_auto_state === 'login_unconfirmed') counts.limited++;
    else counts.other++;
  }
  return counts;
}

function flowWindowFailureCount(snapshot) {
  if (snapshot?.flow_state?.available !== true || !Array.isArray(snapshot.tasks)) return null;
  const profiles = new Set((snapshot.real_accounts?.items || []).map(a => a.profile));
  const latest = new Map();
  for (const task of snapshot.tasks) {
    if (task.source !== 'Flow' || !profiles.has(task.source_profile) || !['image', 'video'].includes(task.kind)) continue;
    const previous = latest.get(task.source_profile);
    if (!previous || Number(task.created) > Number(previous.created)) latest.set(task.source_profile, task);
  }
  return [...latest.values()].filter(task => task.status === 'failed' && task.failure_reason === '无法确认账号对应的浏览器窗口，任务在发送前停止。').length;
}

function renderAccountInsights(snapshot, target) {
  const scope = $('operationsDashboard');
  const opened = !!scope.querySelector('[data-dashboard-running][open]');
  const expandedResults = [...scope.querySelectorAll('[data-activation-feedback] details[open]')].map(d=>d.closest('[data-activation-feedback]').dataset.activationFeedback);
  const focused = scope.contains(document.activeElement) ? document.activeElement : null;
  const focusSelector = focused?.hasAttribute('data-insight-activate') ? `[data-insight-activate="${focused.dataset.insightActivate}"]`
    : focused?.hasAttribute('data-insight-accounts') ? '[data-insight-accounts]'
    : focused?.hasAttribute('data-insight-conversations') ? '[data-insight-conversations]'
    : focused?.hasAttribute('data-dashboard-tasks') ? '[data-dashboard-tasks]'
    : focused?.matches('[data-dashboard-running] > summary') ? '[data-dashboard-running] > summary'
    : focused?.matches('summary') ? `[data-activation-feedback="${focused.closest('[data-activation-feedback]')?.dataset.activationFeedback}"] summary` : null;
  const flow = snapshot?.flow_insights;
  const amount = n=>typeof n==='number' && Number.isFinite(n) ? new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(n) : '—';
  const accounts = snapshot?.real_accounts?.items || [];
  const records = accounts.map(officialAccount);
  const officialKnown = state.channelStatus?.available && !channelReadFailed;
  const succeeded = records.filter(r=>r.outcome==='succeeded');
  const timing = succeeded.map(r=>r.call?.total_ms).filter(n=>typeof n==='number' && Number.isFinite(n));
  const latency = officialKnown && timing.length ? (timing.reduce((a,b)=>a+b,0)/timing.length/1000).toFixed(1) : '—';
  const health = flow?.total_accounts>0 && typeof flow?.healthy_accounts==='number' ? Math.min(100,flow.healthy_accounts/flow.total_accounts*100) : null;
  const successPercent = officialKnown && accounts.length ? succeeded.length/accounts.length*100 : null;
  const healthCounts = flow?.health_counts;
  const windowFailures = flowWindowFailureCount(snapshot);
  const sendStatus = windowFailures > 0
    ? `最近生成记录：${windowFailures} 个账号因无法确认对应浏览器窗口，在发送前停止。`
    : windowFailures === null ? '最近生成记录暂不可读取，发送状态未确认。' : '窗口发送能力以实际生成结果为准，账号检查通过不代表生图成功。';
  const flowCurrent = healthCounts && ['ready','cooling','attention','empty','paused'].every(k=>Number.isInteger(healthCounts[k]))
    ? `账号条件正常 ${healthCounts.ready} · 冷却 ${healthCounts.cooling} · 待核对 ${healthCounts.attention} · 积分不足 ${healthCounts.empty} · 暂停 ${healthCounts.paused}` : '账号状态暂未读取';
  const yesterday = flow?.yesterday;
  const recorded = yesterday?.recorded_usage == null ? '未记录' : amount(yesterday.recorded_usage)+((yesterday.unpriced || yesterday.unresolved) ? ' +' : '');
  const coverage = !yesterday || yesterday.coverage==='not_recorded' ? '昨日暂无覆盖记录'
    : `${yesterday.completed ?? '—'} 个完成 · ${yesterday.unpriced ?? '—'} 个未记价格${yesterday.unresolved ? ` · ${yesterday.unresolved} 个待确认` : ''}${yesterday.coverage==='partial' ? ' · 数据不完整' : ''}`;
  const dispatch = state.channelStatus?.dispatch;
  const eligible = officialKnown ? state.channelStatus.accounts.filter(a=>a.dispatch_eligible).length : null;
  const ring = (service,value,total,label,fill)=>`<div class="dashboard-ring-item"><span>${service}</span><button type="button" class="insight-ring ${fill==null ? 'is-unknown' : ''}" data-dashboard-ring="${service}" data-insight-activate="${service.toLowerCase()}" title="检查授权与额度" aria-label="检查 ${service} 授权与额度，当前 ${value} / ${total}"><div><strong>${value}<small> / ${total}</small></strong><span>${label}</span></div></button><small data-ring-action="${service.toLowerCase()}">检查授权与额度</small></div>`;
  target.innerHTML = `<div class="dashboard-metrics"><div class="dashboard-rings">${ring('FLOW',amount(flow?.healthy_accounts),amount(flow?.total_accounts),'账号条件正常',health)}${ring('GEMINI',officialKnown ? eligible : '—',accounts.length || '—','可派发账号',officialKnown && accounts.length ? eligible/accounts.length*100 : null)}</div><div class="dashboard-primary"><span>Flow 账号池总积分</span><strong>${amount(flow?.pool_balance)}</strong><small>${amount(flow?.balance_known_accounts)} / ${amount(flow?.total_accounts)} 个账号有余额 · 当前可用 ${amount(flow?.available_today)}</small></div><div class="dashboard-primary"><span>Gemini 最近成功平均耗时</span><strong>${latency}<em>${latency==='—' ? '' : ' 秒'}</em></strong><small>${officialKnown ? `派发可用 ${amount(eligible)} / ${accounts.length} · ` : ''}每账号仅取最近一次成功记录</small></div></div>
  <div class="dashboard-send-status ${windowFailures > 0 ? 'has-failure' : ''}" role="status"><strong>Flow 账号条件与发送状态分别核对</strong><p>圆环统计登录、积分与暂停等账号条件，不代表浏览器窗口已就绪。</p><p>${esc(sendStatus)}</p>${windowFailures > 0 ? '<button type="button" class="insight-account-link" data-dashboard-tasks>查看任务记录 →</button>' : ''}</div>
  <details class="dashboard-runtime-details" data-dashboard-running ${opened ? 'open' : ''}><summary>运行记录与统计说明<span>账号状态、用量和检查结果</span></summary><div class="dashboard-runtime-columns"><article><header><h3>Flow 运行记录</h3><div><button type="button" class="insight-account-link" data-runtime-activate="flow">检查授权与额度</button><button type="button" class="insight-account-link" data-insight-accounts>查看账号 →</button></div></header><p>${esc(flowCurrent)}</p><dl><div><dt>昨日记录用量</dt><dd>${esc(recorded)}</dd></div><div><dt>今日当前可用积分</dt><dd>${amount(flow?.available_today)}</dd></div></dl><p>${esc(coverage)}；按任务记录计算，非实际账单。</p><p>未提供积分总上限，不将余额换算为额度百分比。</p><p>${flow?.updated ? 'Flow 数据更新于 '+esc(dateTime(flow.updated)) : 'Flow 数据暂未读取'}</p><div class="insight-activation-feedback" data-activation-feedback="flow" hidden></div></article><article><header><h3>Gemini 运行记录</h3><button type="button" class="insight-account-link" data-insight-conversations>进入对话 →</button></header><p>${esc(officialSummary())}</p><p>官方账号池：${state.channelStatus?.production_send_available ? '已接入' : '暂不可用'}${dispatch ? ` · 排队 ${amount(dispatch.queued)} · 执行中 ${amount(dispatch.active)}` : ''}。</p><p>新对话轮询分配，多轮对话固定账号；按当前派发限制使用。</p><p>圆环显示当前可派发账号；点击检查官方授权与额度，不使用旧版网页保活状态。</p><p>${state.channelStatus?.read_at ? '实测记录读取于 '+esc(dateTime(state.channelStatus.read_at)) : '实测记录暂未读取'}。耗时不含旧通道；长期稳定性持续观察。</p><div class="insight-activation-feedback" data-activation-feedback="gemini" hidden></div></article></div></details>`;
  $('dashboardRuntime').replaceChildren(scope.querySelector('[data-dashboard-running]'));
  scope.querySelector('[data-dashboard-ring="FLOW"]').style.setProperty('--fill',`${health ?? 0}%`);
  scope.querySelector('[data-dashboard-ring="GEMINI"]').style.setProperty('--fill',`${officialKnown && accounts.length ? eligible/accounts.length*100 : 0}%`);
  scope.querySelector('[data-insight-accounts]').addEventListener('click',()=>setView('accounts'));
  scope.querySelector('[data-dashboard-tasks]')?.addEventListener('click',()=>setView('tasks'));
  scope.querySelector('[data-insight-conversations]').addEventListener('click',()=>setView('conversations'));
  scope.querySelectorAll('[data-insight-activate],[data-runtime-activate]').forEach(button=>button.addEventListener('click',()=>startServiceActivation(button.dataset.insightActivate || button.dataset.runtimeActivate)));
  renderServiceActivationFeedback(scope);
  for (const service of expandedResults) {
    const details=scope.querySelector(`[data-activation-feedback="${service}"] details`);
    if(details)details.open=true;
  }
  if(focusSelector)scope.querySelector(focusSelector)?.focus({preventScroll:true});
}

function renderServiceActivationFeedback(target = $('operationsDashboard')) {
  const records = state.serviceActivations || {};
  const running = Object.values(records).some(record => record.status === 'running');
  for (const service of ['flow', 'gemini']) {
    const button = target.querySelector(`[data-insight-activate="${service}"]`);
    const feedback = target.querySelector(`[data-activation-feedback="${service}"]`);
    if (!button || !feedback) continue;
    const record = records[service];
    button.disabled = insightActivationPosting || running || !state.snapshot?.real_accounts?.available;
    button.setAttribute('aria-busy', String(record?.status === 'running'));
    const action = target.querySelector(`[data-ring-action="${service}"]`);
    if (action) action.textContent = record?.status === 'running' ? '正在检查…' : '检查授权与额度';
    const runtimeButton = target.querySelector(`[data-runtime-activate="${service}"]`);
    if(runtimeButton) { runtimeButton.disabled=button.disabled; runtimeButton.textContent=record?.status === 'running' ? '正在检查…' : '检查授权与额度'; }
    feedback.hidden = !record || record.status === 'idle';
    if (feedback.hidden) continue;
    const items = Array.isArray(record.items) ? record.items : [];
    const done = items.filter(item => item.status === 'done').length;
    const pending = items.filter(item => ['queued', 'processing', 'waiting_flow', 'cooling'].includes(item.status)).length;
    const attention = items.length - done - pending;
    const accounts = new Map((state.snapshot?.real_accounts?.items || []).map(item => [item.key.slice('flow-account-'.length), item]));
    const started = typeof record.created === 'number' && Number.isFinite(record.created) && record.created > 0 ? dateTime(record.created) : '时间未记录';
    const label = record.status === 'running' ? `本轮检查（开始于 ${started}）` : `上次检查结果（开始于 ${started}）`;
    const message = record.error || (record.status === 'running' ? `正在逐项检查 · 本轮检查通过 ${done} · 处理中 ${pending} · 待处理 ${attention}` : `当时检查通过 ${done} · 当时待处理 ${attention}`);
    const expanded = feedback.querySelector('details')?.open;
    feedback.innerHTML = `<p role="status" aria-live="polite"><strong>${esc(label)}</strong><br>${esc(message)}</p>${items.length ? `<details ${expanded ? 'open' : ''}><summary>查看本轮账号结果</summary><ul>${items.map(item => `<li><strong>${esc(accounts.get(item.account_key)?.email || accounts.get(item.account_key)?.label || '账号')}</strong>：${esc(item.message)}</li>`).join('')}</ul></details>` : ''}`;
  }
}

async function refreshServiceActivations() {
  if (insightActivationPosting) return;
  const sequence = ++serviceActivationReadSequence;
  await Promise.all(['flow', 'gemini'].map(async service => {
    try {
      const result = await getJson('/api/account-activation?service=' + service);
      if (sequence !== serviceActivationReadSequence || insightActivationPosting || result.service !== service || !Array.isArray(result.items)) return;
      const previous = state.serviceActivations[service];
      state.serviceActivations[service] = result;
      if (previous?.status === 'running' && result.status === 'finished') refresh();
    } catch (_) { /* Keep last known progress without resubmitting an operation. */ }
  }));
  renderServiceActivationFeedback();
}

async function startServiceActivation(service) {
  if (insightActivationPosting || !['flow', 'gemini'].includes(service)) return;
  if (Object.values(state.serviceActivations || {}).some(r=>r.status==='running')) return;
  const runningPanel=$('dashboardRuntime').querySelector('[data-dashboard-running]');
  if(runningPanel) runningPanel.open=true;
  insightActivationPosting = true;
  ++serviceActivationReadSequence;
  state.serviceActivations[service] = {status:'running', items:[], error:'正在创建本轮账号检查…'};
  renderServiceActivationFeedback();
  try {
    const response = await fetch('/api/account-activation', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:JSON.stringify({service})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '创建未完成');
    if (result.service !== service || !Array.isArray(result.items)) throw new Error('检查结果格式未确认');
    state.serviceActivations[service] = result;
  } catch (error) {
    state.serviceActivations[service] = {status:'unknown', items:[], error:(error.message || '提交状态未确认') + '；请核对本轮结果，不会自动重试。'};
  } finally {
    insightActivationPosting = false;
    renderServiceActivationFeedback();
  }
}
setInterval(() => { if (state.view === 'overview' && !document.hidden && Object.values(state.serviceActivations).some(record => ['running','unknown'].includes(record.status))) refreshServiceActivations(); }, 5000);

function accountUsageBar(label, value, stale) {
  const known = typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
  const formatted = known ? `${new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 1}).format(value)}%` : "—";
  const meter = known
    ? `role="meter" aria-label="Gemini ${esc(label)}已用" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${value}"`
    : `role="img" aria-label="Gemini ${esc(label)}用量未知"`;
  return `<div class="account-usage ${stale ? 'is-stale' : ''}"><div class="account-usage-label"><span>${esc(label)}</span><strong>${esc(formatted)}</strong></div><div class="account-usage-track ${known ? '' : 'is-unknown'}" ${meter}>${known ? `<i data-usage-percent="${value}"></i>` : ''}</div></div>`;
}

function updateAccountUsageBars() {
  document.querySelectorAll('[data-usage-percent]').forEach((bar) => {
    const percent = Number(bar.dataset.usagePercent);
    if (Number.isFinite(percent) && percent >= 0 && percent <= 100) bar.style.width = `${percent}%`;
  });
}

function updateAccountCooldowns() {
  document.querySelectorAll('[data-cooldown-end]').forEach((clock) => {
    const deadline = Number(clock.dataset.cooldownEnd);
    if (!Number.isFinite(deadline)) return;
    const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    const time = clock.querySelector('.account-cooldown-time');
    const label = clock.querySelector('.account-cooldown-label');
    if (!time || !label) return;
    if (seconds === 0) {
      time.textContent = '待核对';
      label.textContent = '已到时间';
      clock.setAttribute('aria-label', '本地冷却倒计时结束，请刷新核对状态');
      const status = clock.closest('.account-service')?.querySelector('.account-health');
      if (status && clock.dataset.riskPaused !== 'true') { status.textContent = '待核对'; status.classList.add('is-unknown'); }
      return;
    }
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const remainder = String(seconds % 60).padStart(2, '0');
    time.textContent = hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
    label.textContent = '冷却中';
    clock.setAttribute('aria-label', `本地冷却剩余 ${hours ? hours + ' 小时 ' : ''}${minutes} 分 ${Number(remainder)} 秒`);
  });
}

setInterval(updateAccountCooldowns, 1000);

function accountOfficialQuotaMarkup(key) {
  const row = officialQuotaReadFailed ? null : officialQuotaData?.accounts?.[key];
  const now = Date.now()/1000;
  const age = row && typeof row.checked_at === 'number' ? now-row.checked_at : null;
  const fresh = row?.status === 'ok' && age !== null && Number.isFinite(age) && age >= 0 && age <= 600;
  const windowMarkup = (window, title) => {
    const raw = row?.[window]?.remaining_percent;
    const value = fresh && typeof raw === 'number' && Number.isFinite(raw) && raw >= 0 && raw <= 100 ? raw : null;
    const reset = row?.[window]?.reset_at;
    const text = value === null ? '—' : value.toFixed(2).replace(/\.00$/, '')+'%';
    const recovery = value === null ? '' : typeof reset === 'number' && Number.isFinite(reset) && reset > now ? '恢复 '+dateTime(reset) : value === 100 ? '额度充足' : '恢复时间待官方更新';
    return `<div class="account-quota-window"><div class="account-quota-label"><span>${title}</span><strong>${text}</strong></div><div class="account-quota-track${value === null ? ' is-unknown' : ''}" ${value === null ? `role="img" aria-label="Gemini ${title}尚未读取"` : `role="progressbar" aria-label="Gemini ${title}剩余" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${value}"`} >${value === null ? '' : `<i style="width:${value}%"></i>`}</div>${recovery ? `<span class="account-quota-reset">${esc(recovery)}</span>` : ''}</div>`;
  };
  const message = officialQuotaReadFailed ? '额度服务暂不可读取' : row?.status === 'ok' && !fresh ? '额度记录已过期，等待更新' : row?.status === 'verification_required' ? '额度暂未读到，不影响对话' : row?.status === 'network_error' ? '连接失败，额度暂未读到' : !fresh ? '额度暂未读到' : '';
  return `<div class="account-service-heading"><span>Gemini</span><small>剩余额度</small></div><div class="account-quota-windows">${windowMarkup('five_hour','5 小时额度')}${windowMarkup('weekly','每周额度')}</div>${message ? `<p class="account-quota-note">${esc(message)}</p>` : ''}`;
}

function renderAccountQuotas() {
  document.querySelectorAll('[data-account-quota]').forEach(panel=>{
    const markup = accountOfficialQuotaMarkup(panel.dataset.accountQuota);
    if (panel.innerHTML !== markup) panel.innerHTML = markup;
  });
}

function renderAccounts() {
  const opened = [...document.querySelectorAll('.account-details[open] summary')].map(e=>e.getAttribute('aria-controls'));
  const focusedDetail = document.activeElement?.closest('.account-details')?.querySelector('summary')?.getAttribute('aria-controls');
  const data = state.snapshot?.real_accounts;
  if (!data && !state.snapshotAttempted) {
    $('realAccountsSummary').textContent = '正在读取账号记录…';
    $('realAccountTable').innerHTML = empty('正在读取账号记录', '只读取现有状态，不会发起真实调用。');
    return;
  }
  if (!data?.available) {
    $("realAccountsSummary").textContent = "暂时无法读取现有账号";
    $("realAccountTable").innerHTML = empty("账号暂不可读取", data?.message || "请检查 Flow 后台连接。");
    $("saveDispatchPolicy").disabled = true;
    $('geminiSend').disabled = true; // Legacy Gemini sender removed.
    syncConversationControls(true);
    return;
  }
  const policy = data.dispatch_policy;
  if (policy && Number.isInteger(policy.five_hour_limit) && Number.isInteger(policy.weekly_limit)) {
    if (!dispatchPolicyDirty && !dispatchPolicySaving) {
      $("fiveHourLimit").value = policy.five_hour_limit;
      $("weeklyLimit").value = policy.weekly_limit;
      $("dispatchPolicyMessage").textContent = "当前规则已读取。";
    }
    $("saveDispatchPolicy").disabled = dispatchPolicySaving;
  } else {
    $("saveDispatchPolicy").disabled = true;
    $("dispatchPolicyMessage").textContent = "规则暂不可读取。";
  }
  const accounts = Array.isArray(data.items) ? data.items : [];
  const ready = accounts.filter((item) => item.flow_health === "ready").length;
  const verified = accounts.filter((item) => item.gemini === "verified" && item.gemini_enabled !== false);
  const selectable = accounts.filter((item) => geminiAccountAvailability(item).manual);
  const automatic = accounts.filter((item) => geminiAccountAvailability(item).auto);
  const paused = accounts.filter((item) => item.gemini_enabled === false).length;
  $("realAccountsSummary").textContent = `共 ${accounts.length} 个账号 · Flow 当前可用 ${ready} 个 · ${officialSummary()}`;
  for (const select of [$("geminiAccount"), $("chatAccount")]) {
    renderGeminiAccountOptions(select, accounts);
  }
  syncConversationControls(true);
  if (!state.geminiBusy) {
    syncQuickGeminiControls();
    if (!sessionStorage.getItem("geminiRequest")) $("geminiMessage").textContent = geminiSelectionReady($('geminiAccount').value) ? "请选择账号，输入内容后发送。" : `统一号池共 ${accounts.length} 个账号；不可用的账号已列出原因，请先在账号页核对。`;
  }
  if (!accounts.length) {
    $("realAccountTable").innerHTML = empty("没有已登记账号", "点击上方“添加本机账号”开始接入。");
    return;
  }
  const healthLabels = {ready:"可用", cooling:"冷却中", paused:"已暂停 · 待核查", attention:"需要检查", empty:"积分不足", unknown:"状态未知"};
  const autoLabels = {eligible:"可参与自动选号", refresh_needed:"自动选号前会核对用量", usage_unknown:"用量未知，不能自动选号", limit_reached:"上次用量已达到上限", disabled:"Gemini 已暂停", busy:"账号正在处理", unresolved:"有结果未确认的请求", login_unconfirmed:"Gemini 登录待核对", browser_unknown:"浏览器状态暂不可核对", browser_unavailable:"账号浏览器未确认运行"};
  const flowTasks = (state.snapshot?.tasks || []).filter(t => ['image','video'].includes(t.kind));
  const retryDrafts = new Map(Array.from($("realAccountTable").querySelectorAll?.('select[data-retry-project]') || []).map(s => [s.dataset.retryProject, s.value]));
  const issueCounts = new Map();
  for (const account of accounts) for (const issue of accountIssues(account)) {
    if (['historical_unconfirmed', 'quota_stale'].includes(issue.key)) continue;
    const previous = issueCounts.get(issue.key);
    issueCounts.set(issue.key, {title: issue.title, count: (previous?.count || 0) + 1});
  }
  const issueSummary = issueCounts.size ? `<section class="account-issue-summary" aria-label="当前账号状态"><strong>当前需关注</strong><div class="account-issue-summary-items">${Array.from(issueCounts.values()).map(issue => `<span>${esc(issue.title)} <b>${issue.count}</b></span>`).join('')}</div></section>` : '';
  $("realAccountTable").innerHTML = `${issueSummary}<div class="account-grid">${accounts.map((item) => {
    const identity = item.email || item.label || item.profile;
    const official = officialAccount(item);
    const issues = accountIssues(item).filter(issue => issue.key !== 'historical_unconfirmed');
    const issueBadges = issues.filter(issue => !['quota_stale', 'flow_paused', 'flow_cooling', 'flow_empty', 'flow_unknown', 'unresolved', 'gemini_disabled', 'keepalive_disabled', 'keepalive_paused'].includes(issue.key));
    const hasBalance = typeof item.flow_balance === "number" && Number.isFinite(item.flow_balance) && item.flow_balance >= 0;
    const balance = hasBalance ? item.flow_balance.toLocaleString("zh-CN") : "—";
    const freshness = !hasBalance ? "尚未读取" : item.flow_balance_fresh ? "最近读取" : "上次读取，可能已过期";
    const observed = typeof item.flow_balance_observed === "number" ? ` · ${dateTime(item.flow_balance_observed)}` : "";
    const login = item.flow_login === "verified" ? "Flow 已核对" : "Flow 未确认";
    const geminiUsage = item.gemini_usage?.current_5h?.usage_percentage;
    const weeklyUsage = item.gemini_usage?.weekly?.usage_percentage;
    const checkedAt = state.snapshot?.checked_at;
    const usageAge = typeof checkedAt === 'number' && typeof item.gemini_usage_checked === 'number' ? checkedAt - item.gemini_usage_checked : null;
    const usageStale = usageAge === null || !Number.isFinite(usageAge) || usageAge < 0 || usageAge > 900;
    const keepaliveText = item.keepalive_enabled === false ? '自动保活已手动暂停'
      : item.gemini_enabled === false ? '自动保活随 Gemini 使用暂停'
      : item.keepalive_state === 'blocked_result' ? '保活等待旧请求结果核对，不会自动执行'
      : item.keepalive_state === 'paused' ? '保活已暂停；处理原因后可单次恢复'
      : item.keepalive_state === 'dispatching' ? '正在检查或保活，请勿重复操作'
      : item.keepalive_next_due ? `下次保活不早于 ${dateTime(item.keepalive_next_due)}` : '保活时间待确认';
    const keepaliveBadge = item.keepalive_enabled === false || item.gemini_enabled === false || item.keepalive_state === 'paused' ? '保活已暂停'
      : item.keepalive_state === 'blocked_result' ? '保活待核对'
      : item.keepalive_state === 'dispatching' ? '保活处理中'
      : item.keepalive_next_due ? '保活已排程' : '保活待确认';
    const keepaliveWarn = item.keepalive_enabled === false || item.gemini_enabled === false || ['blocked_result', 'paused'].includes(item.keepalive_state);
    const cooling = item.flow_cooling === true || item.flow_health === 'cooling';
    const flowPaused = item.flow_risk_paused === true;
    // Derive each service's presentation from its own recorded state only.
    const flowIssues = issues.filter(issue => issue.key.startsWith('flow_'));
    const geminiIssues = issues.filter(issue => !issue.key.startsWith('flow_') && issue.key !== 'quota_stale');
    if (item.keepalive_state === 'blocked_result' && !geminiIssues.some(issue => issue.key === 'unresolved'))
      geminiIssues.push({key: 'keepalive_blocked', title: 'Gemini 保活待核对'});
    const flowNeedsAttention = flowIssues.some(issue => !['flow_cooling','flow_unknown'].includes(issue.key));
    const geminiNeedsAttention = official.attention;
    const needsAttention = flowNeedsAttention || geminiNeedsAttention;
    const geminiLastCheck = item.gemini_enabled === false ? '已暂停' : item.gemini === 'verified' ? '上次已核对' : '待核对';
    const geminiStatusText = official.label;
    const flowIssueTitle = flowIssues.map(issue => issue.title).join('；');
    const geminiIssueTitle = geminiIssues.map(issue => issue.title).join('；');
    const cardState = (flowPaused ? ' is-flow-paused' : cooling ? ' is-flow-cooling' : '') + (needsAttention ? ' is-account-issue' : '');
    const flowStatusText = flowPaused ? healthLabels.paused : cooling ? healthLabels.cooling : healthLabels[item.flow_health] || '状态未知';
    const cooldownSeconds = typeof item.flow_cooldown_seconds === 'number' && Number.isFinite(item.flow_cooldown_seconds) && item.flow_cooldown_seconds > 0 ? item.flow_cooldown_seconds : null;
    const cooldownEnd = cooldownSeconds === null ? null : (state.snapshotReceivedAt || Date.now()) + cooldownSeconds * 1000;
    const coolingInfo = cooling ? (cooldownSeconds === null ? '本地冷却中，剩余时间待核对。' : '圆圈内为上次读取后估算的本地冷却剩余时间；到时后请刷新核对，服务方是否恢复以实际状态为准。') : '';
    const accountKey = esc(item.key.slice("flow-account-".length));
    const accountProjects = (state.snapshot?.projects || []).filter(p => p.source_profile === item.profile);
    const selectedRetryProject = retryDrafts.has(accountKey) ? retryDrafts.get(accountKey) : state.snapshot?.retry_projects?.[item.profile] || '';
    const ownFlow = flowTasks.filter(t => t.source_profile === item.profile);
    const flowDone = ownFlow.filter(t => t.status === 'succeeded').length;
    const flowFailed = ownFlow.filter(t => t.status === 'failed').length;
    const ownGemini = state.snapshot?.gemini_stats?.accounts?.[item.key.slice('flow-account-'.length)];
    const flowPerformance = flowDone + flowFailed ? `任务 ${flowDone} 成功 / ${flowFailed} 失败 · 成功率 ${Math.round(flowDone * 100 / (flowDone + flowFailed))}%` : '暂无已结束生成任务';
    const gemPerformance = ownGemini ? `对话 ${ownGemini.completed} 完成 · 平均首段 ${typeof ownGemini.avg_first_ms === 'number' ? (ownGemini.avg_first_ms / 1000).toFixed(1) + ' 秒' : '暂无实测'}` : '暂无工作台对话记录';
    return `<article class="account-card${cardState}">
      <div class="account-card-heading"><div class="account-avatar" aria-hidden="true">${esc(String(identity).slice(0, 1).toUpperCase())}</div><div class="account-identity"><span class="account-alias">${esc(item.label || item.profile)}</span><h3 title="${esc(identity)}">${esc(identity)}</h3></div><span class="account-login ${item.flow_login === 'verified' ? 'is-ready' : ''}">${esc(login)}</span>
        <div class="account-detail-anchor"><details class="account-details"><summary aria-label="查看 ${esc(identity)} 的详细状态" aria-controls="account-detail-${accountKey}">详情</summary></details><div id="account-detail-${accountKey}" class="account-detail-popup"><strong>账号状态详情</strong><p>Flow：${esc(flowStatusText)} · ${esc(freshness + observed)}。${hasBalance ? '剩余 ' + esc(balance) + ' 积分。' : ''}未提供积分总额度。</p>${coolingInfo ? `<p>${esc(coolingInfo)}</p>` : ''}${officialDetails(official)}<p>Flow · ${esc(flowPerformance)}</p>${issues.length ? `<div class="account-detail-issues"><strong>状态说明</strong>${issues.map(issue => `<p><b>${esc(issue.title)}</b><br>${esc(issue.impact)} ${esc(issue.action)}</p>`).join('')}</div>` : ''}</div></div></div>
      <div class="account-services"><div class="account-service account-service-flow${flowNeedsAttention ? ' is-service-issue' : ''}" role="group" aria-label="${esc(flowIssueTitle || ('Flow：' + flowStatusText))}" title="${esc(flowIssueTitle || ('Flow：' + flowStatusText))}"><div class="account-service-heading"><span>Flow</span><strong class="account-health ${flowPaused ? 'is-paused' : cooling ? 'is-cooling' : item.flow_health !== 'ready' ? 'is-unknown' : ''}">${esc(flowStatusText)}</strong></div><div class="account-flow-value"><div><span>${hasBalance && !item.flow_balance_fresh ? '上次积分余额' : '积分余额'}</span><strong>${esc(balance)}</strong></div>${cooling ? `<div class="account-cooldown" data-risk-paused="${flowPaused}"${cooldownEnd !== null ? ` data-cooldown-end="${cooldownEnd}"` : ' aria-label="冷却中，剩余时间待核对"'}><span class="account-cooldown-time">—</span><span class="account-cooldown-label">冷却中</span></div>` : '<span class="account-credit-icon" aria-hidden="true">✦</span>'}</div></div>
      <div class="account-service account-service-gemini${geminiNeedsAttention ? ' is-service-issue' : ''}" role="group" aria-label="Gemini 账号额度" data-account-quota="${accountKey}">${accountOfficialQuotaMarkup(item.key.slice('flow-account-'.length))}</div></div>
      <div class="account-status-row"><span class="account-mini-status"><i aria-hidden="true"></i>${official.data?.dispatch_eligible ? '官方调用 · 可派发' : '官方调用 · 派发受限'}</span>${official.call?.verification === 'page_error_500' && official.outcome !== 'succeeded' ? '<span class="account-mini-status is-attention">核验页 500（该次记录）</span>' : ''}${official.paused ? '<span class="account-mini-status">已手动暂停</span>' : ''}</div>
      <details><summary>切号备用项目</summary><p class="subtle">切号重试将在此账号选定的项目生成，可能消耗积分。未指定时不会用此账号切号。</p><select data-retry-project="${accountKey}"><option value="">不指定</option>${accountProjects.map(p => `<option value="${esc(p.key)}" ${p.key === selectedRetryProject ? 'selected' : ''}>${esc(p.title)} · ${esc(shortId(p.source_id))}</option>`).join('')}</select><button class="text-link" type="button" data-save-retry-project="${accountKey}">保存备用项目</button></details>
      <div class="account-card-actions">${flowPaused ? `<button class="button button-light" type="button" data-resume-flow="${accountKey}">已核查，恢复 Flow</button>` : ''}</div></article>`;
  }).join("")}</div>`;
  updateAccountUsageBars();
  updateAccountCooldowns();
  for (const id of opened) { const summary = document.querySelector(`[aria-controls="${id}"]`); if (summary) summary.parentElement.open = true; }
  if (focusedDetail) document.querySelector(`[aria-controls="${focusedDetail}"]`)?.focus({preventScroll:true});
}

async function refreshActivation() {
  if (activationUpdating) return;
  activationUpdating = true;
  try {
    const response = await fetch('/api/account-activation', {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    const items = Array.isArray(data.items) ? data.items : [];
    const desktopIssues = Array.isArray(data.desktop_recovery) ? data.desktop_recovery : [];
    const counts = (kind) => items.filter(x => x.status === kind).length;
    $('activateAccounts').disabled = activationPosting || data.status === 'running' || !state.snapshot?.real_accounts?.available;
    const flowNote = {accepted:'Flow 积分刷新已受理，结果以账号卡片为准。', unknown:'Flow 积分刷新未确认，不会自动重复提交。', held:'上次 Flow 刷新未确认，本次跳过。', skipped_busy:'账号有任务，本次不刷新 Flow 积分。', skipped_fresh:'Flow 积分近期已更新，本次不重复刷新。', skipped_safety:'部分账号状态待核查，本次不批量刷新 Flow 积分。', skipped_poll:'轮询读取现有 Flow 状态，不重复刷新全部账号积分。'};
    $('activationProgress').textContent = data.status === 'idle' ? '点击后将逐个检查；不会重发消息、自动重试或换号。'
      : `本轮 ${items.length} 个：完成 ${counts('done')} · 等待冷却 ${counts('cooling')} · 待核查 ${counts('blocked') + counts('failed') + counts('interrupted')} · 处理中 ${counts('queued') + counts('processing') + counts('waiting_flow')}。${flowNote[data.flow_refresh] || (data.service === 'gemini' ? '本轮仅检查 Gemini，不操作 Flow。' : '正在准备 Flow 积分刷新。')}`;
    if (desktopIssues.length) $('activationProgress').textContent += ` · 有 ${desktopIssues.length} 条桌面恢复提示，查看下方详情。`;
    $('activationDetails').hidden = !items.length && !desktopIssues.length;
    const accountMap = new Map((state.snapshot?.real_accounts?.items || []).map(a => [a.key.slice('flow-account-'.length), a]));
    $('activationResults').innerHTML = items.map(i => {
      const account = accountMap.get(i.account_key);
      const classification = ['blocked', 'failed', 'interrupted'].includes(i.status) && account ? accountIssues(account).map(issue => issue.title).join('、') : '';
      return `<p>${esc(account?.email || account?.label || account?.profile || '账号')}${classification ? ` · ${esc(classification)}` : ''}：${esc(i.message)}${i.due && i.status === 'cooling' ? `（预计 ${esc(dateTime(i.due))} 后复查）` : ''}</p>`;
    }).join('') + desktopIssues.map(i => {
      const account = accountMap.get(i.account_key);
      return `<p>${esc(account?.email || account?.label || (i.account_key ? '账号' : '系统'))} · 桌面恢复：${esc(i.message)}</p>`;
    }).join('');
    const signature = JSON.stringify(items.map(i => [i.account_key, i.status]));
    if (activationSignature !== null && signature !== activationSignature) refresh();
    activationSignature = signature;
  } catch (_) {
    $('activateAccounts').disabled = true;
    $('activationProgress').textContent = '一键检查暂不可用，请检查工作台连接。';
  } finally { activationUpdating = false; }
}

$('activateAccounts').addEventListener('click', async () => {
  if (activationPosting) return;
  activationPosting = true;
  $('activateAccounts').disabled = true;
  $('activationProgress').textContent = '正在创建本轮检查…';
  try {
    const response = await fetch('/api/account-activation', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:'{}'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '创建失败');
  } catch (error) { $('accountActionMessage').textContent = error.message || '创建状态未确认，请刷新后查看；不会自动重复提交。'; }
  finally { activationPosting = false; await refreshActivation(); }
});
setInterval(() => { if (state.view === 'accounts') refreshActivation(); }, 5000);

let activationScheduleDirty = false;
let activationScheduleSaving = false;
let activationDailyEnabled = false;
let activationPollingEnabled = false;
function renderActivationToggle() {
  const active = [activationDailyEnabled ? '定时' : '', activationPollingEnabled ? '轮询' : ''].filter(Boolean);
  $('activationScheduleToggle').textContent = '通道与设置';
}
const activationSchedulePanel = $('activationSchedulePanel');
const activationScheduleForm = $('activationScheduleForm');
const scheduleDate = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) : '暂无';
async function refreshActivationSchedule(force = false) {
  try {
    const response = await fetch('/api/account-activation/schedule', {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!activationScheduleDirty || force) {
      $('activationScheduleEnabled').checked = data.enabled;
      $('activationScheduleTime').value = data.time;
      activationScheduleDirty = false;
    }
    activationDailyEnabled = data.enabled;
    renderActivationToggle();
    const warning = [data.held_accounts ? `${data.held_accounts} 个账号上次检查失败，需手动核对` : '', data.flow_hold ? 'Flow 积分刷新待人工核对' : ''].filter(Boolean).join('；');
    $('activationScheduleStatus').textContent = `${data.enabled ? '已开启' : '未开启'} · 下次：${scheduleDate(data.next_due)} · 上次：${scheduleDate(data.last_due)}${data.last_note ? '（' + data.last_note + '）' : ''}${warning ? ' · ' + warning : ''}`;
  } catch (_) { $('activationScheduleStatus').textContent = '定时状态暂不可读取，请检查工作台连接。'; }
}
$('activationScheduleToggle').addEventListener('click', () => {
  const opening = activationSchedulePanel.hidden;
  activationSchedulePanel.hidden = !opening;
  $('activationScheduleToggle').setAttribute('aria-expanded', String(opening));
  if (opening) { refreshActivationSchedule(); refreshActivationPoll(); }
});
activationScheduleForm.addEventListener('input', () => { activationScheduleDirty = true; });
activationScheduleForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (activationScheduleSaving) return;
  activationScheduleSaving = true;
  const button = activationScheduleForm.querySelector('button[type="submit"]');
  button.disabled = true;
  $('activationScheduleStatus').textContent = '正在保存…';
  try {
    const response = await fetch('/api/account-activation/schedule', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},
      body:JSON.stringify({enabled:$('activationScheduleEnabled').checked, time:$('activationScheduleTime').value})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    await refreshActivationSchedule(true);
  } catch (error) { $('activationScheduleStatus').textContent = `${error.message || '保存状态未确认'}，请刷新核对，不会自动重试。`; }
  finally { button.disabled = false; activationScheduleSaving = false; }
});
refreshActivationSchedule();
let activationPollDirty = false;
let activationPollSaving = false;
const activationPollForm = $('activationPollForm');
async function refreshActivationPoll(force = false) {
  try {
    const response = await fetch('/api/account-activation/poll', {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) throw new Error();
    const data = await response.json();
    activationPollingEnabled = data.enabled;
    renderActivationToggle();
    if (!activationPollDirty || force) {
      $('activationPollEnabled').checked = data.enabled;
      $('activationPollMinutes').value = data.interval_minutes;
      activationPollDirty = false;
    }
    const account = (state.snapshot?.real_accounts?.items || []).find(a => a.key === 'flow-account-' + data.last_account);
    const name = account ? (account.email || account.label || '账号') : data.last_account ? '账号已不在列表' : '';
    const hold = data.held_accounts ? ` · ${data.held_accounts} 个账号需通过一键检查手动核对` : '';
    $('activationPollStatus').textContent = `${data.enabled ? '轮询已开启' : '轮询未开启'} · 下次：${scheduleDate(data.next_due)} · 上次：${scheduleDate(data.last_due)}${name ? ' · ' + name : ''}${data.last_note ? '（' + data.last_note + '）' : ''}${hold}`;
  } catch (_) { $('activationPollStatus').textContent = '轮询状态暂不可读取，请检查工作台连接。'; }
}
activationPollForm.addEventListener('input', () => { activationPollDirty = true; });
activationPollForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (activationPollSaving) return;
  activationPollSaving = true;
  const button = activationPollForm.querySelector('button[type="submit"]');
  button.disabled = true;
  $('activationPollStatus').textContent = '正在保存轮询…';
  try {
    const response = await fetch('/api/account-activation/poll', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},
      body:JSON.stringify({enabled:$('activationPollEnabled').checked, interval_minutes:Number($('activationPollMinutes').value)})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    await refreshActivationPoll(true);
  } catch (error) { $('activationPollStatus').textContent = `${error.message || '保存状态未确认'}，请刷新核对，不会自动重试。`; }
  finally { button.disabled = false; activationPollSaving = false; }
});
refreshActivationPoll();
setInterval(() => { if (state.view === 'accounts' && !activationSchedulePanel.hidden) { if (!activationScheduleSaving) refreshActivationSchedule(); if (!activationPollSaving) refreshActivationPoll(); } }, 30000);

function renderFlowForm() {
  const snapshot = state.snapshot;
  const project = $("flowProject");
  const previousProject = project.value;
  const accounts = snapshot?.real_accounts?.items || [];
  const byProfile = new Map(accounts.filter((a) => a.flow_login === "verified").map((a) => [a.profile, a]));
  const projects = (snapshot?.projects || []).filter((p) => byProfile.has(p.source_profile));
  project.innerHTML = '<option value="auto">自动安排账号和项目</option>' + projects.map((p) => {
    const account = byProfile.get(p.source_profile);
    return `<option value="${esc(p.key)}">${esc(p.title)} · ${esc(account.email || account.label || account.profile)} · ${esc(shortId(p.source_id))}</option>`;
  }).join("");
  if (previousProject === "auto" || projects.some((p) => p.key === previousProject)) project.value = previousProject;
  renderFlowModels();
  $("flowSend").disabled = !!state.flowBusy || !$("flowModel").value;
}

function renderFlowModels() {
  const kind = $("flowKind").value;
  const model = $("flowModel");
  const oldModel = model.value;
  const models = state.snapshot?.flow_models?.[kind] || [];
  model.innerHTML = models.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
  if (models.some((m) => m.id === oldModel)) model.value = oldModel;
  const aspect = $("flowAspect");
  const oldAspect = aspect.value;
  const options = kind === "image" ? ["16:9", "4:3", "1:1", "3:4", "9:16"] : ["16:9", "9:16"];
  aspect.innerHTML = options.map((x) => `<option value="${x}">${x}</option>`).join("");
  if (options.includes(oldAspect)) aspect.value = oldAspect;
  $("flowVideoOptions").hidden = kind !== "video";
  $("flowSend").disabled = !!state.flowBusy || !model.value;
}

function renderAll() { renderConnections(); renderOverview(); renderProjects(); renderTasks(); renderAccounts(); renderFlowForm(); }

const workflowStatus = {waiting:"等待中",awaiting_authorization:"等待授权",awaiting_capability:"待接入",dispatching:"提交中",running:"进行中",completed:"已完成",failed:"失败",blocked:"已阻塞",cancelled:"已取消",unknown:"结果不明"};
const workflowKind = {generate:"Flow 生成",verify:"文件保存",analyse:"Gemini 分析衔接",conversation:"Gemini 对话"};
const demoLabels = {"demo-flow-shared":"模拟账号甲", "demo-gemini-shared":"模拟账号甲", "demo-flow-separate":"模拟账号乙", "demo-gemini-separate":"模拟账号乙"};
let demoAccountSequence = 0;

function demoServiceCell(item, account) {
  if (!item) return "<span class=\"subtle\">未关联</span>";
  const service = item.service === "flow" ? "Flow" : "Gemini";
  const login = {ready:"已登录", expired:"需登录", unknown:"未知"}[item.login_state] || "未知";
  const keep = item.keepalive_enabled ? (item.keepalive_result === "simulated_ok" ? "已模拟保活" : item.keepalive_result === "needs_login" ? "等待登录" : item.keepalive_result === "paused" ? "已暂停" : "等待模拟") : "未开启";
  const quota = item.quota_remaining === null ? "未知" : `${item.quota_remaining} 模拟点`;
  const canRun = account.enabled && item.enabled && item.login_state === "ready" && Number.isInteger(item.quota_remaining) && item.quota_remaining > 0;
  const pending = Array.isArray(item.pending_steps) ? item.pending_steps : [];
  return `<span class="source-chip">${service} · ${item.enabled ? "已启用" : "已暂停"}</span><br>
    <span class="subtle">登录：${login}（模拟资料） · 保活：${keep}<br>额度：${esc(quota)}${item.quota_scope === "shared" ? "（共用）" : "（独立）"}${pending.length ? ` · ${pending.length} 笔待核对` : ""}</span><br>
    <button class="text-link" type="button" data-demo-action="toggle" data-source="${esc(item.id)}" data-enabled="${item.enabled ? "false" : "true"}">${item.enabled ? "暂停模拟" : "启用模拟"}</button>&nbsp;&nbsp;
    <button class="text-link" type="button" data-demo-action="run" data-source="${esc(item.id)}" data-service="${esc(item.service)}" ${canRun ? "" : "disabled"}>模拟 ${service}</button><br>
    <button class="text-link" type="button" data-demo-action="login" data-source="${esc(item.id)}" data-state="${item.login_state === "ready" ? "expired" : "ready"}">${item.login_state === "ready" ? "模拟登录过期" : "模拟恢复登录"}</button>&nbsp;&nbsp;
    <button class="text-link" type="button" data-demo-action="keepalive" data-source="${esc(item.id)}" data-enabled="${item.keepalive_enabled ? "false" : "true"}">${item.keepalive_enabled ? "关闭模拟保活" : "开启模拟保活"}</button><br>
    ${item.quota_scope !== "shared" ? `<button class="text-link" type="button" data-demo-action="quota" data-source="${esc(item.id)}" data-value="0">模拟额度用尽</button>&nbsp;&nbsp;<button class="text-link" type="button" data-demo-action="quota" data-source="${esc(item.id)}" data-value="20">恢复模拟额度</button>&nbsp;&nbsp;<button class="text-link" type="button" data-demo-action="quota" data-source="${esc(item.id)}" data-value="unknown">设为未知</button>` : ""}
    ${pending.map((id) => `<br><span class="subtle">结果不明：先模拟核对任务已结束</span><br><button class="text-link" type="button" data-demo-action="reconcile" data-step="${esc(id)}" data-outcome="spent">已消耗</button>&nbsp;&nbsp;<button class="text-link" type="button" data-demo-action="reconcile" data-step="${esc(id)}" data-outcome="not_spent">未消耗</button>`).join("")}`;
}

function renderDemoAccounts(pool) {
  if (pool?.simulation !== true || pool?.real_accounts_connected !== false || !Array.isArray(pool.sources) || !Array.isArray(pool.links) || !Array.isArray(pool.accounts)) throw new Error("invalid simulation");
  const linked = new Set();
  for (const link of pool.links) {
    linked.add(link.flow_source); linked.add(link.gemini_source);
  }
  $("demoAccountTable").innerHTML = `<table><thead><tr><th>统一账号</th><th>Flow</th><th>Gemini</th><th>账号设置</th></tr></thead><tbody>${pool.accounts.map((account) => {
    const flow = account.services?.flow, gemini = account.services?.gemini;
    const link = pool.links.find((item) => item.flow_source === flow?.id && item.gemini_source === gemini?.id);
    return `<tr><td><strong>${esc(account.label)}</strong><br><span class="subtle">${account.enabled ? "账号已启用" : "账号已暂停"} · 仅供模拟</span><br><button class="text-link" type="button" data-demo-action="account-toggle" data-account="${esc(account.id)}" data-enabled="${account.enabled ? "false" : "true"}">${account.enabled ? "暂停整个模拟账号" : "启用整个模拟账号"}</button></td>
      <td>${demoServiceCell(flow, account)}</td><td>${demoServiceCell(gemini, account)}</td>
      <td><span class="subtle">${account.quota_scope === "shared" ? "共同模拟额度" : "各业务独立模拟额度"}</span><br>
      <button class="text-link" type="button" data-demo-action="quota-scope" data-account="${esc(account.id)}" data-scope="${account.quota_scope === "shared" ? "per_service" : "shared"}">${account.quota_scope === "shared" ? "切换为独立额度" : "模拟共同额度"}</button>
      ${account.quota_scope === "shared" ? `<br><button class="text-link" type="button" data-demo-action="quota-scope" data-account="${esc(account.id)}" data-scope="shared" data-value="0">共同额度用尽</button><br><button class="text-link" type="button" data-demo-action="quota-scope" data-account="${esc(account.id)}" data-scope="shared" data-value="20">恢复共同额度</button><br><button class="text-link" type="button" data-demo-action="quota-scope" data-account="${esc(account.id)}" data-scope="shared" data-value="unknown">设共同额度为未知</button>` : ""}
      ${flow && gemini ? `<br><button class="text-link" type="button" data-demo-action="browser-sharing" data-account="${esc(account.id)}" data-enabled="${account.shared_browser ? "false" : "true"}">${account.shared_browser ? "关闭模拟共用浏览器" : "模拟共用浏览器"}</button>` : ""}
      ${link ? `<br><button class="text-link" type="button" data-demo-action="unlink" data-link="${esc(link.id)}">解除模拟关联</button>` : "<br><span class=\"subtle\">待确认其他来源</span>"}</td></tr>`;
  }).join("")}</tbody></table>`;
  for (const service of ["flow", "gemini"]) {
    const select = $(service === "flow" ? "demoFlowSource" : "demoGeminiSource");
    const selected = select.value;
    const options = pool.sources.filter((item) => item.service === service && !linked.has(item.id));
    select.innerHTML = options.length ? options.map((item) => `<option value="${esc(item.id)}">${esc(item.label)} · ${service === "flow" ? "Flow" : "Gemini"}</option>`).join("") : '<option value="">暂无未关联来源</option>';
    if (options.some((item) => item.id === selected)) select.value = selected;
  }
  $("demoLink").disabled = !$('demoFlowSource').value || !$('demoGeminiSource').value;
}

async function refreshDemoAccounts() {
  const sequence = ++demoAccountSequence;
  try {
    const pool = await getJson("/api/scheduler/demo-accounts");
    if (sequence === demoAccountSequence) renderDemoAccounts(pool);
  } catch (_) {
    if (sequence === demoAccountSequence) $("demoAccountTable").innerHTML = empty("模拟账号暂不可读取", "原有两个后台不受影响。");
  }
}

async function postDemoAccount(action, data) {
  const response = await fetch("/api/scheduler/demo-accounts/" + action, {method:"POST", headers:{"Content-Type":"application/json", "X-Workbench-Request":"1"}, body:JSON.stringify(data)});
  if (!response.ok) { const detail = await response.json().catch(() => ({})); throw new Error(detail.error || "模拟操作未完成。"); }
  return response.json();
}

async function handleDemoAccount(event) {
  const button = event.target.closest("button[data-demo-action]");
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.demoAction === "run") {
      const service = button.dataset.service;
      await submitSimulation(service === "flow" ? "flow_only" : "gemini_only", {["mock_" + service + "_source"]: button.dataset.source});
      $("demoAccountMessage").textContent = `已创建 ${service === "flow" ? "Flow" : "Gemini"} 模拟任务，可在任务页查看。`;
    } else if (button.dataset.demoAction === "auto-run") {
      const service = button.dataset.service;
      await submitSimulation(service === "flow" ? "flow_only" : "gemini_only", {mock_pool_auto: true});
      $("demoAccountMessage").textContent = `已创建自动分配的 ${service === "flow" ? "Flow" : "Gemini"} 模拟任务，可在任务页查看。`;
    } else if (button.dataset.demoAction === "toggle") {
      await postDemoAccount("enabled", {source_id: button.dataset.source, enabled: button.dataset.enabled === "true"});
      $("demoAccountMessage").textContent = "模拟服务状态已更新，另一项服务不受影响。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "unlink") {
      await postDemoAccount("unlink", {link_id: button.dataset.link, confirm: true});
      $("demoAccountMessage").textContent = "模拟关联已解除，两个模拟来源仍可分别使用。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "account-toggle" || button.dataset.demoAction === "browser-sharing") {
      await postDemoAccount(button.dataset.demoAction === "account-toggle" ? "account-enabled" : "browser-sharing", {account_id: button.dataset.account, enabled: button.dataset.enabled === "true"});
      $("demoAccountMessage").textContent = "模拟账号设置已更新。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "login") {
      await postDemoAccount("login", {source_id: button.dataset.source, state: button.dataset.state});
      $("demoAccountMessage").textContent = "模拟登录状态已更新，没有连接真实账号。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "keepalive") {
      await postDemoAccount("keepalive", {source_id: button.dataset.source, enabled: button.dataset.enabled === "true"});
      $("demoAccountMessage").textContent = "模拟保活设置已更新，不会发送真实请求。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "quota") {
      await postDemoAccount("quota", {source_id: button.dataset.source, remaining: button.dataset.value === "unknown" ? null : Number(button.dataset.value)});
      $("demoAccountMessage").textContent = "模拟额度已更新。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "quota-scope") {
      await postDemoAccount("quota-scope", {account_id: button.dataset.account, scope: button.dataset.scope, remaining: button.dataset.value === "unknown" ? null : Number(button.dataset.value || 20)});
      $("demoAccountMessage").textContent = "模拟额度方式已更新，数值仅供演示。";
      await refreshDemoAccounts();
    } else if (button.dataset.demoAction === "reconcile") {
      await postDemoAccount("reconcile", {step_id: button.dataset.step, outcome: button.dataset.outcome});
      $("demoAccountMessage").textContent = "已模拟核对任务结束及额度，不会重新提交任务。";
      await refreshDemoAccounts();
    }
  } catch (error) { $("demoAccountMessage").textContent = error.message || "暂未确认结果，请先查看模拟账号和任务记录。"; }
  finally { button.disabled = false; }
}

async function linkDemoAccounts() {
  const button = $("demoLink"); button.disabled = true;
  try {
    await postDemoAccount("link", {flow_source: $("demoFlowSource").value, gemini_source: $("demoGeminiSource").value, confirm: true});
    $("demoAccountMessage").textContent = "模拟账号已关联，两个服务仍按各自方式运行。";
    await refreshDemoAccounts();
  } catch (_) { $("demoAccountMessage").textContent = "暂未确认关联结果，请先查看模拟账号列表。"; }
  finally { button.disabled = !$('demoFlowSource').value || !$('demoGeminiSource').value; }
}

let workflowSequence = 0;
async function refreshWorkflows() {
  const sequence = ++workflowSequence;
  try {
    const [health, result] = await Promise.all([getJson("/api/scheduler/health"), getJson("/api/scheduler/workflows")]);
    if (sequence !== workflowSequence) return;
    $("workflowHealth").textContent = health.worker_running && !health.worker_error ? "工作台流程运行正常" : "流程组件需要检查";
    $("workflowPolicy").textContent = health.real_execution === "locked" ? "工作台目前仅支持模拟流程，真实生成和素材分析尚未开启。" : "真实执行状态已变化，请先检查；素材分析尚未接入。";
    if (!Array.isArray(result.items)) throw new Error("invalid result");
    $("workflowRecords").innerHTML = result.items.length ? result.items.map((item) => `
      <article class="workflow-record"><div class="workflow-header"><strong>${esc(item.title)}</strong><span class="task-badge">${esc(workflowStatus[item.status] || "状态未知")}</span></div>
        <div class="workflow-meta">${item.mode === "simulation" ? "模拟流程" : "真实计划"} · ${esc(dateTime(item.created))}</div>
        <div class="workflow-steps">${(Array.isArray(item.steps) ? item.steps : []).map((step) => `<span class="workflow-step">${esc(workflowKind[step.kind] || "步骤")}：${esc(workflowStatus[step.status] || "状态未知")}${step.demo_source ? ` · ${esc(demoLabels[step.demo_source] || "模拟来源")}` : ""}${step.reason ? ` · ${esc(step.reason)}` : ""}${step.output?.simulation ? " · 模拟文本回复（未分析素材）" : ""}</span>`).join("")}</div>
      </article>`).join("") : empty("暂无工作台流程", "现在可使用模拟流程检查步骤衔接。");
  } catch (_) {
    if (sequence !== workflowSequence) return;
    $("workflowHealth").textContent = "流程组件暂不可用";
    $("workflowRecords").innerHTML = empty("流程记录暂不可读取", "工作台原后台任务列表仍可单独查看。");
  }
}

async function submitSimulation(scenario, options = {}) {
  const response = await fetch("/api/scheduler/workflows", {method:"POST", headers:{"Content-Type":"application/json", "X-Workbench-Request":"1", "Idempotency-Key":"demo-" + crypto.randomUUID()}, body:JSON.stringify({scenario, ...options})});
  if (!response.ok) throw new Error();
  return response.json();
}

async function simulateWorkflow() {
  const button = $("workflowSimulate");
  button.disabled = true;
  $("workflowMessage").textContent = "正在创建模拟流程…";
  try {
    await submitSimulation($("workflowScenario").value);
    $("workflowMessage").textContent = "模拟流程已创建；下方步骤状态会自动更新。";
    await refreshWorkflows();
  } catch (_) {
    $("workflowMessage").textContent = "暂未确认创建结果，请先查看流程记录，不要重复点击。";
  } finally { button.disabled = false; }
}

async function getJson(path, {timeoutMs = 0} = {}) {
  const controller = timeoutMs ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(path, {cache: "no-store", credentials: "same-origin", ...(controller ? {signal: controller.signal} : {})});
    if (!response.ok) throw new Error("工作台连接不可用");
    return await response.json();
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function refresh({background = false} = {}) {
  if (snapshotRefreshInFlight) return snapshotRefreshInFlight;
  if (overviewRefreshTimer) clearTimeout(overviewRefreshTimer);
  overviewRefreshTimer = null;
  snapshotRefreshInFlight = refreshSnapshot(background).finally(() => {
    snapshotRefreshInFlight = null;
    scheduleOverviewRefresh();
  });
  return snapshotRefreshInFlight;
}

async function refreshSnapshot(background) {
  const sequence = ++state.refreshSequence;
  const button = $("refresh");
  button.disabled = true;
  if (!background) $("lastUpdated").textContent = "正在刷新…";
  const [status, snapshot] = await Promise.allSettled([
    getJson("/api/status", {timeoutMs: 12000}), getJson("/api/snapshot", {timeoutMs: 12000})
  ]);
  if (sequence !== state.refreshSequence) return;
  state.snapshotAttempted = true;
  const snapshotReady = snapshot.status === 'fulfilled' && snapshot.value?.flow_state?.available === true;
  if (status.status === 'fulfilled') state.status = status.value;
  if (snapshotReady) {
    state.snapshot = snapshot.value;
    state.snapshotReceivedAt = Date.now();
  }
  const updated = state.snapshotReceivedAt ? new Date(state.snapshotReceivedAt).toLocaleTimeString('zh-CN',
    {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}) : '';
  const partial = status.status !== 'fulfilled' || (snapshotReady &&
    (snapshot.value.library?.available === false || snapshot.value.real_accounts?.available === false));
  $("lastUpdated").textContent = snapshotReady ? `${partial ? '部分更新于' : '更新于'} ${updated}`
    : updated ? `同步暂缓 · 保留 ${updated} 数据` : '数据暂不可读取';
  $("lastUpdated").title = snapshotReady
    ? '总览每 5 秒同步本地记录，回到窗口会立即同步。Google 积分和额度以上次核对时间为准。'
    : '本轮未取得完整快照，保留上次成功读取的数据；恢复连接后会自动同步，不会重新提交任务。';
  button.disabled = false;
  if (background) {
    if (state.view === 'overview') { renderConnections(); renderOverview(); }
  } else renderAll();
  if (!background && state.view === 'conversations') {
    renderConversationAccountFilter();
    renderRealTurnAccountLabels();
  }
  if (!background && state.view === "accounts") refreshGeminiActivity();
  scheduleFlowRefresh();
  refreshChannelStatus();
  refreshDispatchConcurrency();
  if (state.view === 'overview') refreshServiceActivations();
}

let geminiActivitySequence = 0;
async function refreshGeminiActivity() {
  const sequence = ++geminiActivitySequence;
  try {
    const data = await getJson('/api/real-pool/activity');
    if (sequence !== geminiActivitySequence) return;
    if (data.source !== 'workbench' || !Array.isArray(data.items)) throw new Error('invalid activity');
    const names = new Map((state.snapshot?.real_accounts?.items || []).map((item) => [item.key.slice('flow-account-'.length), item.email || item.label || item.profile]));
    const statuses = {dispatching:'处理中', completed:'已完成', failed:'失败', unknown:'结果未确认'};
    $('geminiActivity').innerHTML = data.items.length ? `<table><thead><tr><th>时间</th><th>账号</th><th>处理来源</th><th>状态</th><th>说明</th></tr></thead><tbody>${data.items.map((item) => `<tr><td>${esc(dateTime(item.created))}</td><td>${esc(names.get(item.account_key) || '账号已不在当前列表')}</td><td>${item.engine === 'official_antigravity_cli' ? '官方账号池' : /Antigravity CLI/.test(item.note || '') ? '官方 CLI · 独立实测' : item.engine === 'pooldesk' ? (item.engine_version === 'core-v2' ? 'PoolDesk 核心' : 'PoolDesk 适配版') : '原工作台'}</td><td>${esc(statuses[item.status] || '状态未知')}</td><td>${esc(safeRecordedNote(item.note) || (item.status === 'dispatching' ? '正在处理' : '—'))}</td></tr>`).join('')}</tbody></table>` : empty('暂无工作台 Gemini 请求记录');
  } catch (_) {
    if (sequence === geminiActivitySequence) $('geminiActivity').innerHTML = empty('请求记录暂不可读取');
  }
}

$('refreshGeminiActivity').addEventListener('click', refreshGeminiActivity);

for (const id of ['fiveHourLimit', 'weeklyLimit']) $(id).addEventListener('input', () => { dispatchPolicyDirty = true; });
$('dispatchPolicyForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (dispatchPolicySaving) return;
  const five = Number($('fiveHourLimit').value), weekly = Number($('weeklyLimit').value);
  if (!Number.isInteger(five) || !Number.isInteger(weekly) || five < 1 || five > 100 || weekly < 1 || weekly > 100) {
    $('dispatchPolicyMessage').textContent = '请输入 1 到 100 的整数。';
    return;
  }
  dispatchPolicySaving = true;
  $('saveDispatchPolicy').disabled = true;
  $('dispatchPolicyMessage').textContent = '正在保存…';
  try {
    await postJson('/api/real-pool/dispatch-policy', {five_hour_limit: five, weekly_limit: weekly});
    dispatchPolicyDirty = false;
    dispatchPolicySaving = false;
    await refresh();
    $('dispatchPolicyMessage').textContent = '规则已保存，仅影响新的自动选号。';
  } catch (_) {
    $('dispatchPolicyMessage').textContent = '保存结果未确认，请刷新页面核对后再操作。';
  } finally {
    dispatchPolicySaving = false;
    $('saveDispatchPolicy').disabled = !state.snapshot?.real_accounts?.available;
  }
});

function showGeminiRecord(record) {
  const status = record?.status;
  const account = state.snapshot?.real_accounts?.items?.find((item) => item.key === "flow-account-" + record?.account_key);
  const accountName = account ? account.email || account.label || account.profile : "";
  $("geminiReply").innerHTML = status === "completed" ? renderGeminiReply(record) : "";
  $("geminiNewRequest").hidden = status !== "unknown";
  state.geminiBusy = status === "dispatching" || status === "unknown";
  syncQuickGeminiControls();
  $("geminiFiles").disabled = state.geminiBusy;
  $("geminiMessage").textContent = status === "dispatching" ? "正在等待 Gemini 回复，请不要重复发送。" :
    status === "completed" ? `已收到 Gemini 的真实回复${accountName ? `（${accountName}）` : ""}。` :
    status === "failed" ? record.note || "本次没有提交，请检查账号后再操作。" :
    "结果尚未确认，不会自动重新发送；请先核对。";
}

async function observeGeminiRequest(id, attempts = 0) {
  if (sessionStorage.getItem("geminiRequest") !== id) return;
  try {
    const response = await fetch(`/api/real-pool/requests/${id}`, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("not available");
    const record = await response.json();
    showGeminiRecord(record);
    if (record.status === "dispatching" && attempts < 80) setTimeout(() => observeGeminiRequest(id, attempts + 1), 3000);
    else if (record.status === "dispatching") showGeminiRecord({status: "unknown"});
  } catch (_) {
    showGeminiRecord({status: "unknown"});
  }
}

$("realAccountTable").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-check-gemini], [data-toggle-gemini], [data-keepalive-gemini], [data-recover-gemini], [data-resume-flow], [data-save-retry-project]");
  if (!button) return;
  if (button.hasAttribute('data-save-retry-project')) {
    const select = button.closest('details').querySelector('select[data-retry-project]');
    const value = select.value || null;
    if (value && !window.confirm('保存后，等待切号的任务可能使用该项目自动生成并消耗积分。确认使用此项目？')) return;
    button.disabled = true;
    try {
      const response = await fetch('/api/real-pool/flow-retry-project', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:JSON.stringify({account:button.dataset.saveRetryProject, project:value})});
      const result = await response.json();
      $('accountActionMessage').textContent = response.ok && result.ok ? '备用项目已保存。' : result.error || '保存状态未确认。';
      await refresh();
    } catch (_) { $('accountActionMessage').textContent = '保存状态未确认，请刷新后核对。'; }
    finally { button.disabled = false; }
    return;
  }
  const isFlowResume = button.hasAttribute('data-resume-flow');
  if (isFlowResume && !window.confirm('确认已人工核查该账号的 Flow 状态？恢复后，等待中的任务可能在冷却结束后开始执行。')) return;
  button.disabled = true;
  const isToggle = button.hasAttribute('data-toggle-gemini');
  const isKeepalive = button.hasAttribute('data-keepalive-gemini');
  const isRecovery = button.hasAttribute('data-recover-gemini');
  $("accountActionMessage").textContent = isFlowResume ? '正在恢复 Flow…' : isToggle || isKeepalive ? "正在更新账号设置…" : isRecovery ? "正在单次检查并续期原账号；不会重发消息或换号…" : "正在核对该账号的 Gemini 登录与额度…";
  try {
    const response = await fetch(isFlowResume ? "/api/real-pool/flow-risk-resume" : isKeepalive ? "/api/real-pool/keepalive-enabled" : isToggle ? "/api/real-pool/enabled" : isRecovery ? "/api/real-pool/recover-account" : "/api/real-pool/check", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Workbench-Request": "1"},
      body: JSON.stringify(isFlowResume ? {account: button.dataset.resumeFlow} : isKeepalive ? {account: button.dataset.keepaliveGemini, enabled: button.dataset.enabled === 'true'} : isToggle ? {account: button.dataset.toggleGemini, enabled: button.dataset.enabled === 'true'} : {account: isRecovery ? button.dataset.recoverGemini : button.dataset.checkGemini})});
    const result = await response.json();
    $("accountActionMessage").textContent = result.message || result.error || "暂未确认，请稍后查看账号状态。";
    await refresh();
  } catch (_) {
    $("accountActionMessage").textContent = "结果暂未确认，请刷新账号列表后查看。";
  } finally { button.disabled = false; }
});

$('geminiAccount').addEventListener('change', syncQuickGeminiControls);

$("geminiSend").addEventListener("click", async () => {
  const account = $("geminiAccount").value;
  const prompt = $("geminiPrompt").value;
  if (!geminiSelectionReady(account)) { $('geminiMessage').textContent = '所选账号当前不能发送，请先到账号页核对状态。'; return; }
  if (!account || !prompt.trim()) { $("geminiMessage").textContent = "请选择账号并填写内容。"; return; }
  const files = Array.from($("geminiFiles").files || []);
  const extensions = files.map((file) => file.name.split(".").pop().toLowerCase());
  if (files.length > 4 || files.reduce((total, file) => total + file.size, 0) > 100 * 1024 * 1024 ||
      files.some((file, index) => !file.size || !["jpg", "jpeg", "png", "webp", "mp4", "mov", "webm", "pdf"].includes(extensions[index]))) {
    $("geminiMessage").textContent = "请检查附件格式，最多 4 个，合计不超过 100 MB。"; return;
  }
  const id = crypto.randomUUID();
  sessionStorage.setItem("geminiRequest", id);
  state.geminiBusy = true;
  $("geminiSend").disabled = true; // Legacy Gemini sender removed.
  $("geminiFiles").disabled = true;
  $("geminiReply").textContent = "";
  $("geminiMessage").textContent = "正在提交真实请求，请不要重复发送。";
  let chatStarted = false;
  try {
    const attachments = [];
    for (let index = 0; index < files.length; index++) {
      $("geminiMessage").textContent = `正在上传附件 ${index + 1}/${files.length}，尚未发送问题。`;
      const upload = await fetch(`/api/real-pool/uploads/${id}/${index}/${extensions[index]}`, {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/octet-stream", "X-Workbench-Request": "1"}, body: files[index]});
      const result = await upload.json();
      if (!upload.ok) throw new Error(result.error || "附件上传失败，本次没有发送问题。");
      attachments.push(result);
    }
    $("geminiMessage").textContent = "正在提交真实请求，请不要重复发送。";
    chatStarted = true;
    const response = await fetch("/api/real-pool/chat", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Workbench-Request": "1", "Idempotency-Key": id},
      body: JSON.stringify({account, prompt, attachments})});
    const record = await response.json();
    if (response.status === 400 || response.status === 403) {
      sessionStorage.removeItem("geminiRequest");
      state.geminiBusy = false;
      syncQuickGeminiControls();
      $("geminiFiles").disabled = false;
      $("geminiMessage").textContent = record.error || "请求没有提交，请检查账号和内容。";
      return;
    }
    if (!response.ok) throw new Error("outcome unknown");
    showGeminiRecord(record);
    if (record.status === "dispatching") observeGeminiRequest(id);
  } catch (error) {
    if (!chatStarted) {
      sessionStorage.removeItem("geminiRequest");
      state.geminiBusy = false;
      syncQuickGeminiControls();
      $("geminiFiles").disabled = false;
      $("geminiMessage").textContent = error.message || "附件上传失败，本次没有发送问题。";
    } else await observeGeminiRequest(id);
  }
});

$("geminiNewRequest").addEventListener("click", () => {
  sessionStorage.removeItem("geminiRequest");
  state.geminiBusy = false;
  $("geminiFiles").disabled = false;
  $("geminiNewRequest").hidden = true;
  syncQuickGeminiControls();
  $("geminiMessage").textContent = "已结束这条记录，可发起新的请求。";
});

function showFlowRecord(record) {
  const status = record?.status;
  state.flowBusy = status === "dispatching" || status === "unknown";
  $("flowAutoRetry").disabled = true;
  $("flowSend").disabled = state.flowBusy || !$("flowProject").value || !$("flowModel").value;
  $("flowNewRequest").hidden = status !== "unknown";
  $("flowMessage").textContent = status === "submitted" ? `已交给 Flow 排队（任务 ${record.task_id}），下方会更新执行状态。` :
    status === "dispatching" ? "正在提交，请不要重复操作。" : "结果尚未确认，请先到原后台核对；系统不会自动重复提交。";
}

let activeConversation = null;
let currentConversation = null;
let chatSending = false;
let showingHiddenConversations = false;
let simulatedConversationIds = new Set();
let conversationListRequest = 0;
let conversationDetailRequest = 0;
let conversationEntries = [];
let conversationMetadata = new Map();
let conversationMetadataRun = 0;
let conversationMetadataAbort = null;
let conversationMetadataState = {total: 0, done: 0, failed: 0, loading: false};
let realTurnAccountState = null;
let libraryPage = 1;
let libraryPages = 1;
let libraryRequest = 0;
let materialOpenRequest = 0;
let currentMaterial = null;
let currentMaterialExtra = null;
let materialDirty = false;
let materialSaving = false;
let autoMaterialState = null;
let backfillState = null;
let backfillRequest = null;
let backfillBusy = false;
const materialDimensions = {subject:"主体", scene:"场景", action:"动作", style:"风格", camera:"镜头", color:"色调", keywords:"关键词"};
const extraDimensions = {shot_size:"景别", viewpoint:"拍摄角度", composition:"构图", movement:"运镜"};

async function postJson(path, body, key) {
  const headers = {"Content-Type":"application/json", "X-Workbench-Request":"1"};
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(path, {method:"POST", credentials:"same-origin", headers, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.error || "操作暂未完成。"); error.status = response.status; throw error; }
  return data;
}

function conversationFilterMetadata(item) {
  const turns = Array.isArray(item.turns) ? item.turns : [];
  const imageIntent = /(?:生图|文生图|图生图|text[ -]to[ -]image|image[ -]to[ -]image)|(?:生成|绘制|画|制作|创作|设计|generate|create|draw|make|render)[^。！？\n]{0,32}(?:图片|图像|照片|插画|海报|一张图|一幅画|image|picture|photo|illustration)|(?:图片|图像|照片|插画|海报)[^。！？\n]{0,16}(?:生成|绘制|制作|创作)/i;
  const videoIntent = /(?:生视频|文生视频|图生视频|text[ -]to[ -]video|image[ -]to[ -]video)|(?:生成|制作|创建|做|创作|合成|generate|create|make|render)[^。！？\n]{0,32}(?:视频|短片|动画|影片|video|clip)|(?:视频|短片|动画|影片)[^。！？\n]{0,16}(?:生成|制作|创作|合成)/i;
  const imageResult = turns.some((turn) => Array.isArray(turn.images) && turn.images.length > 0);
  const image = imageResult || turns.some((turn) => (turn.image_status && Number(turn.image_status.expected) > 0)
    || /<Image\b[^>]*\bsrc=["']image_generation_content\//i.test(String(turn.response || ''))
    || imageIntent.test(String(turn.prompt || '')));
  const video = turns.some((turn) => videoIntent.test(String(turn.prompt || '')));
  const attachment = turns.some((turn) => Array.isArray(turn.attachments) && turn.attachments.length > 0);
  const last = turns[turns.length - 1];
  const status = (item.blocked && !officialConversation(item)) || turns.some((turn) => turn.status === 'unknown') ? 'unconfirmed'
    : turns.some((turn) => turn.status === 'dispatching') ? 'processing'
    : !last ? 'empty' : ['completed', 'failed'].includes(last.status) ? last.status : 'unknown';
  return {image, video, imageResult, attachment, status};
}

function renderConversationAccountFilter() {
  const select = $('chatFilterAccount');
  if (!select) return;
  const selected = select.value.startsWith('sim:') ? '' : select.value;
  const snapshotAccounts = state.snapshot?.real_accounts?.items;
  const accountNames = new Map((Array.isArray(snapshotAccounts) ? snapshotAccounts : [])
    .filter((account) => typeof account.key === 'string' && account.key.startsWith('flow-account-'))
    .map((account) => [account.key.slice('flow-account-'.length), account.email || account.label || account.profile || '账号']));
  const choices = new Map();
  let missing = 0;
  for (const item of conversationEntries) {
    if (item.simulated) continue;
    if (item.account_key) {
      const key = 'real:' + item.account_key;
      if (!choices.has(key)) choices.set(key, accountNames.get(item.account_key) || `不在当前号池的账号 ${++missing}`);
    } else choices.set('unbound', '未绑定账号');
  }
  const options = [...choices.entries()].sort((a,b) => a[1].localeCompare(b[1], 'zh-CN'));
  if (selected && !choices.has(selected)) options.push([selected, '当前账号（此列表中暂无对话）']);
  select.replaceChildren(new Option('全部账号', ''), ...options.map(([value, text]) => new Option(text, value)));
  select.value = selected;
}

function conversationFilterResults() {
  const search = ($('chatFilterSearch')?.value || '').trim().toLocaleLowerCase('zh-CN');
  const account = $('chatFilterAccount')?.value || '';
  const status = $('chatFilterStatus')?.value || '';
  const kind = $('chatFilterKind')?.value || '';
  const found = [];
  let pending = 0, unavailable = 0;
  for (const item of conversationEntries) {
    if (search && !String(item.title || '').toLocaleLowerCase('zh-CN').includes(search)) continue;
    if (account.startsWith('real:') && (item.simulated || 'real:' + item.account_key !== account)) continue;
    if (account === 'unbound' && (item.simulated || item.account_key)) continue;
    if (kind === 'simulated' && !item.simulated) continue;
    const meta = conversationMetadata.get(item.id);
    const needsMeta = !!status || (!!kind && kind !== 'simulated');
    if (needsMeta && !meta) { pending++; continue; }
    if (needsMeta && meta.error) { unavailable++; continue; }
    if (status && meta.status !== status) continue;
    if (kind === 'image' && !meta.image) continue;
    if (kind === 'video' && !meta.video) continue;
    if (kind === 'image_result' && !meta.imageResult) continue;
    if (kind === 'attachment' && !meta.attachment) continue;
    if (kind === 'other' && (meta.image || meta.video)) continue;
    found.push(item);
  }
  return {found, pending, unavailable, filtered: !!(search || account || status || kind)};
}

function renderConversationList() {
  const list = $('chatList');
  if (!list) return;
  const {found, pending, unavailable, filtered} = conversationFilterResults();
  const scrollTop = list.scrollTop;
  const focused = document.activeElement?.closest?.('#chatList [data-chat]')?.dataset.chat;
  const statusNames = {completed:'已完成', processing:'处理中', failed:'失败', unconfirmed:'待核对', empty:'未开始', unknown:'状态未知'};
  list.innerHTML = found.length ? found.map((item) => {
    const meta = conversationMetadata.get(item.id);
    const badges = [];
    if (officialConversation(item)) badges.push('<span class="chat-row-tag">官方调用</span>');
    if (meta && !meta.error) {
      const statusClass = meta.status === 'unconfirmed' ? ' is-attention' : meta.status === 'processing' ? ' is-processing' : meta.status === 'failed' ? ' is-failed' : '';
      badges.push(`<span class="chat-row-tag${statusClass}">${esc(statusNames[meta.status] || '状态未知')}</span>`);
      if (meta.image) badges.push('<span class="chat-row-tag">生图相关</span>');
      if (meta.video) badges.push('<span class="chat-row-tag is-video">生视频相关</span>');
      if (meta.imageResult) badges.push('<span class="chat-row-tag">已有图片</span>');
      if (meta.attachment) badges.push('<span class="chat-row-tag">含附件</span>');
    } else if (meta?.error) badges.push('<span class="chat-row-tag is-attention">筛选信息未读取</span>');
    return `<div class="compact-row"><button class="text-link${item.id === activeConversation ? ' chat-selected' : ''}" type="button" data-chat="${esc(item.id)}"${item.simulated ? ' data-simulated="1"' : ''}${item.id === activeConversation ? ' aria-current="true"' : ''}>${item.simulated ? '<span class="simulation-list-badge">模拟</span> ' : ''}${esc(item.title)}</button><span class="row-sub">${esc(dateTime(item.updated))}</span>${badges.length ? `<div class="chat-row-tags">${badges.join('')}</div>` : ''}</div>`;
  }).join('') : pending ? empty('正在读取筛选信息', '请稍候。') : filtered ? empty('没有符合筛选的对话', '可调整条件或清空筛选。') : empty(showingHiddenConversations ? '没有隐藏的对话' : '还没有对话', showingHiddenConversations ? '隐藏的对话可在这里恢复。' : '点击新建对话开始。');
  list.scrollTop = scrollTop;
  if (focused) [...list.querySelectorAll('[data-chat]')].find((button) => button.dataset.chat === focused)?.focus({preventScroll:true});
  const summary = $('chatFilterSummary');
  if (summary) {
    let text = `显示 ${found.length} / ${conversationEntries.length} 条`;
    if (conversationMetadataState.loading) text += ` · 正在读取 ${conversationMetadataState.done} / ${conversationMetadataState.total}`;
    if (conversationMetadataState.failed) text += ` · ${conversationMetadataState.failed} 条筛选信息未读取`;
    if (filtered && activeConversation && !found.some((item) => item.id === activeConversation)) text += ' · 当前对话保留在右侧';
    if (unavailable) text += ` · ${unavailable} 条未纳入筛选`;
    summary.textContent = text;
  }
}

async function hydrateConversationMetadata(entries, run, controller) {
  let next = 0;
  const workers = Array.from({length: Math.min(4, entries.length)}, async () => {
    while (next < entries.length && run === conversationMetadataRun) {
      const item = entries[next++];
      try {
        const url = (item.simulated ? '/api/simulated-conversations/' : '/api/conversations/') + encodeURIComponent(item.id);
        const response = await fetch(url, {cache:'no-store', credentials:'same-origin', signal:controller.signal});
        if (!response.ok) throw new Error('metadata unavailable');
        const detail = await response.json();
        if (run !== conversationMetadataRun) return;
        conversationMetadata.set(item.id, conversationFilterMetadata(detail));
      } catch (error) {
        if (run !== conversationMetadataRun || error.name === 'AbortError') return;
        conversationMetadata.set(item.id, {error:true});
        conversationMetadataState.failed++;
      }
      if (run !== conversationMetadataRun) return;
      conversationMetadataState.done++;
      if (conversationMetadataState.done % 10 === 0 && $('chatFilterSummary')) {
        $('chatFilterSummary').textContent = $('chatFilterSummary').textContent.replace(/正在读取 \d+ \/ \d+/, `正在读取 ${conversationMetadataState.done} / ${conversationMetadataState.total}`);
      }
    }
  });
  await Promise.all(workers);
  if (run !== conversationMetadataRun) return;
  conversationMetadataState.loading = false;
  renderConversationAccountFilter();
  renderConversationList();
}

for (const id of ['chatFilterSearch', 'chatFilterAccount', 'chatFilterStatus', 'chatFilterKind']) {
  $(id)?.addEventListener(id === 'chatFilterSearch' ? 'input' : 'change', renderConversationList);
}
$('chatFilterReset')?.addEventListener('click', () => {
  for (const id of ['chatFilterSearch', 'chatFilterAccount', 'chatFilterStatus', 'chatFilterKind']) $(id).value = '';
  renderConversationList();
});

async function loadConversations() {
  await refreshChannelStatus();
  const request = ++conversationListRequest;
  const hidden = showingHiddenConversations;
  if (conversationMetadataAbort) conversationMetadataAbort.abort();
  const run = ++conversationMetadataRun;
  try {
    const data = await getJson(hidden ? '/api/conversations/hidden' : '/api/conversations');
    let simulated = [];
    if (!hidden) {
      try { simulated = (await getJson('/api/simulated-conversations')).items || []; }
      catch (_) { $('chatCreateStatus').textContent = '模拟测试记录暂不可读取，真实对话不受影响。'; }
    }
    if (request !== conversationListRequest || hidden !== showingHiddenConversations) return false;
    const realIds = new Set(data.items.map((item) => item.id));
    simulated = simulated.filter((item) => item.simulated === true && !realIds.has(item.id));
    simulatedConversationIds = new Set(simulated.map((item) => item.id));
    const entries = [...data.items, ...simulated].sort((a,b) => (b.updated || 0) - (a.updated || 0));
    conversationEntries = entries;
    conversationMetadata = new Map();
    if (!activeConversation && entries.length) activeConversation = entries[0].id;
    conversationMetadataState = {total:entries.length, done:0, failed:0, loading:!!entries.length && !!$('chatFilterSearch')};
    renderConversationAccountFilter();
    renderConversationList();
    if ($('chatFilterSearch') && entries.length) {
      const controller = new AbortController();
      conversationMetadataAbort = controller;
      hydrateConversationMetadata(entries, run, controller);
    }
    if (activeConversation) await showConversation(activeConversation);
    return true;
  } catch (_) {
    if (request === conversationListRequest) $('chatList').innerHTML = empty('对话暂不可读取', '请点击页面右上角刷新后再试。');
    return false;
  }
}

function syncConversationControls(announce = false) {
  const item=currentConversation, ready=!channelReadFailed && state.channelStatus?.production_send_available === true;
  const select=$('chatAccount'), accounts=state.snapshot?.real_accounts?.items || [];
  $('chatNew').disabled=!ready;
  const prior=select.value;
  renderGeminiAccountOptions(select,accounts);
  if(item?.account_key) {
    if(![...select.options].some(o=>o.value===item.account_key)) select.add(new Option('绑定账号暂不可用',item.account_key));
    select.value=item.account_key;
  } else if([...select.options].some(o=>o.value===prior)) select.value=prior;
  const pending=!!item?.turns?.some(t=>['queued','dispatching','running'].includes(t.status));
  const archived=!!item && (item.simulated || !!item.blocked || (item.turns.length>0 && item.channel!=='official_antigravity_cli'));
  select.disabled=!item || !!item.account_key || archived || pending || chatSending || !!item.hidden || !ready;
  $('chatPrompt').disabled=!!item?.simulated;
  $('chatFiles').disabled=true;
  $('chatFiles').title='当前接入文字对话；已有附件和草稿保留。';
  $('chatModel').innerHTML='<option value="gemini-3.8-flash-medium">Gemini 3.8 Flash · 官方调用</option>';
  if(archived && item?.model) $('chatModel').innerHTML=`<option>${esc(item.model)}</option>`;
  $('chatModel').disabled=true;
  $('chatModelNote').textContent=archived ? '历史归档只读；原问答与草稿保留，请新建官方对话。' : '官方调用 · 同一对话固定账号与模型，不依赖网页常驻。';
  $('chatSend').disabled=!ready || !item || archived || pending || chatSending || !!item.hidden || !geminiSelectionReady(select.value);
  if(announce && !chatSending) $('chatMessage').textContent=!item ? '请选择或新建对话。' : archived ? '历史记录保留；请新建对话使用官方通道。' : pending ? '任务正在排队或处理，系统只查询结果，不重新发送。' : !ready ? '官方调用暂不可用，草稿保留。' : '新对话轮询分配；多轮固定账号。异常停止，不自动重试或换号。';
}

function renderRealTurnAccountLabels() {
  const current = realTurnAccountState;
  if (!current || current.conversation !== activeConversation) return;
  const items = state.snapshot?.real_accounts?.items;
  const accounts = Array.isArray(items) ? items : [];
  const first = current.results[0]?.account_key;
  for (const element of $('chatHistory').querySelectorAll('[data-real-turn-account]')) {
    const index = Number(element.dataset.realTurnAccount);
    const record = current.results[index];
    element.classList.remove('is-changed');
    if (record === undefined) { element.textContent = '本轮账号读取中…'; continue; }
    if (!record?.account_key) { element.textContent = '本轮账号未确认'; continue; }
    const account = accounts.find((candidate) => candidate.key === 'flow-account-' + record.account_key);
    const name = account?.email || account?.label || account?.profile || '账号信息未读取';
    const comparison = index === 0 ? '首轮账号' : !first ? '首轮账号未确认' : record.account_key === first ? '与首轮一致' : '与首轮不同';
    element.classList.toggle('is-changed', index > 0 && !!first && record.account_key !== first);
    element.textContent = `${record.status === 'completed' ? '本轮使用' : '本轮选择'}：${name} · ${comparison}`;
  }
}

function loadRealTurnAccountLabels(item, sequence) {
  const current = {conversation:item.id, results:new Array(item.turns.length)};
  realTurnAccountState = current;
  let next = 0;
  const workers = Array.from({length:Math.min(4, item.turns.length)}, async () => {
    while (next < item.turns.length && sequence === conversationDetailRequest && activeConversation === item.id) {
      const index = next++;
      const id = item.turns[index]?.request_id;
      try {
        if (!/^[0-9a-f-]{36}$/.test(id || '')) throw new Error('invalid request');
        const record = await getJson('/api/real-pool/requests/' + encodeURIComponent(id));
        current.results[index] = record?.request_id === id ? record : null;
      } catch (_) { current.results[index] = null; }
      if (sequence !== conversationDetailRequest || activeConversation !== item.id) return;
      renderRealTurnAccountLabels();
    }
  });
  Promise.all(workers).catch(() => {});
}

async function showConversation(id) {
  const request = ++conversationDetailRequest;
  const item = await getJson((simulatedConversationIds.has(id) ? '/api/simulated-conversations/' : '/api/conversations/') + encodeURIComponent(id));
  if (request !== conversationDetailRequest) return null;
  if (currentConversation?.id !== item.id) {
    $('chatModel').value = '';
    if (!item.account_key && [...$('chatAccount').options].some((option) => option.value === 'auto')) $('chatAccount').value = 'auto';
  }
  activeConversation = item.id;
  currentConversation = item;
  $('chatTitle').textContent = item.title;
  $('chatRename').disabled = !!item.simulated;
  $('chatHide').disabled = !!item.simulated;
  $('chatHide').textContent = item.hidden ? '恢复对话' : '隐藏';
  $('chatHide').dataset.hidden = item.hidden ? '1' : '0';
  syncConversationControls(true);
  if (item.simulated) {
    realTurnAccountState = null;
    const changed = item.turns.filter((turn) => turn.matches_first === false).length;
    const unknown = item.turns.filter((turn) => turn.matches_first === null).length;
    const result = changed ? `发现 ${changed} 轮账号与首轮不同` : unknown ? `有 ${unknown} 轮账号未确认` : '各轮使用同一模拟账号';
    $('chatHistory').innerHTML = `<div class="simulation-notice"><strong>模拟测试 · ${esc(result)}</strong><span>问题、回复和账号均由程序模拟，未调用真实 Gemini。</span></div>` + item.turns.map((turn) => {
      const account = turn.account_label || '未记录';
      const compare = turn.matches_first === false ? '发生切号' : turn.matches_first === true ? (turn.ordinal === 1 ? '首轮账号' : '与首轮一致') : '账号待确认';
      return `<div class="compact-row simulation-turn" data-simulated-turn="${turn.ordinal}"><div class="row-info"><div class="simulation-turn-heading"><strong>第 ${turn.ordinal} 轮</strong><span class="simulation-account${turn.matches_first === false ? ' is-changed' : ''}">本轮使用：${esc(account)} · ${esc(compare)}</span></div><strong>问题</strong><p>${esc(turn.prompt).replace(/\n/g, '<br>')}</p><strong>模拟回复</strong><p>${esc(turn.response).replace(/\n/g, '<br>')}</p><small class="response-timing">模拟耗时 ${(turn.elapsed_ms / 1000).toFixed(2)} 秒 · 提交 ${turn.attempts} 次${turn.status !== 'completed' ? ' · 未完成' : ''}</small></div></div>`;
    }).join('');
  } else {
    $('chatHistory').innerHTML = item.turns.length ? item.turns.map((turn,index) => `<div class="compact-row"><div class="row-info"><span class="chat-turn-account" data-real-turn-account="${index}">本轮账号读取中…</span><strong>你</strong><p>${esc(turn.prompt).replace(/\n/g, '<br>')}</p>${turn.attachments.length ? `<span class="subtle">附件：${esc(turn.attachments.join('、'))}</span>` : ''}<strong>Gemini</strong>${turn.selected_model ? `<small class="response-timing">所选模型：${esc(turn.selected_model)}</small>` : ''}${renderGeminiReply(turn)}${turn.status === 'completed' ? `<small class="response-timing">首段 ${typeof turn.first_text_ms === 'number' ? (turn.first_text_ms / 1000).toFixed(1) + ' 秒' : '未采集'} · 完成 ${typeof turn.total_ms === 'number' ? (turn.total_ms / 1000).toFixed(1) + ' 秒' : '未采集'} · 排队 ${typeof turn.queue_wait_ms === "number" ? (turn.queue_wait_ms / 1000).toFixed(1) + " 秒" : "未采集"}</small>` : ''}${turn.reconciliation_note ? `<p class="subtle">${esc(turn.reconciliation_note)}</p>` : ''}</div></div>`).join('') : empty('开始对话', '发送第一条消息后将保存记录。');
    loadRealTurnAccountLabels(item, request);
  }
  $('chatList').querySelectorAll('[data-chat]').forEach((button) => {
    const selected = button.dataset.chat === item.id;
    button.classList.toggle('chat-selected', selected);
    if (selected) button.setAttribute('aria-current','true'); else button.removeAttribute('aria-current');
  });
  return item;
}

$('chatList').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-chat]');
  if (button) { try { $('chatCreateStatus').textContent = ''; await showConversation(button.dataset.chat); } catch (_) { $('chatMessage').textContent = '对话暂不可读取。'; } }
});
$('chatNew').addEventListener('click', async () => {
  const button = $('chatNew');
  if (button.disabled) return;
  button.disabled = true; button.textContent = '创建中…';
  $('chatCreateStatus').textContent = '正在新建对话…';
  try {
    showingHiddenConversations = false; $('chatShowHidden').textContent = '查看已隐藏';
    const item = await postJson('/api/conversations', {title:'新对话'});
    ++conversationDetailRequest; currentConversation = null;
    activeConversation = item.id;
    if (!await loadConversations()) {
      $('chatCreateStatus').textContent = '对话已创建，列表暂时无法刷新，请点击右上角刷新。';
      return;
    }
    $('chatCreateStatus').textContent = '对话已创建，可以输入消息。';
    if (state.view === 'conversations') {
      $('chatPrompt').focus({preventScroll:true});
      $('chatPrompt').scrollIntoView({block:'center', behavior:'smooth'});
    }
  } catch (error) {
    $('chatCreateStatus').textContent = [400,403].includes(error.status) ? error.message : '创建结果暂未确认，请先刷新列表查看。';
    await loadConversations();
  } finally { button.disabled = false; button.textContent = '新建对话'; }
});
$('chatAccount').addEventListener('change', () => syncConversationControls(true));
$('chatShowHidden').addEventListener('click', async () => {
  showingHiddenConversations = !showingHiddenConversations;
  $('chatShowHidden').textContent = showingHiddenConversations ? '查看当前对话' : '查看已隐藏';
  activeConversation = null; currentConversation = null; ++conversationDetailRequest; $('chatCreateStatus').textContent = ''; $('chatTitle').textContent = '请选择或新建对话'; $('chatHistory').innerHTML = '';
  $('chatRename').disabled = true; $('chatHide').disabled = true; $('chatSend').disabled = true; // Legacy Gemini sender removed.
  syncConversationControls();
  await loadConversations();
});
$('chatHide').addEventListener('click', async () => {
  if (!activeConversation) return;
  try {
    await postJson('/api/conversations/' + activeConversation + '/visibility', {hidden:$('chatHide').dataset.hidden !== '1'});
    activeConversation = null; currentConversation = null; ++conversationDetailRequest; $('chatCreateStatus').textContent = ''; $('chatTitle').textContent = '请选择或新建对话'; $('chatHistory').innerHTML = '';
    $('chatRename').disabled = true; $('chatHide').disabled = true; $('chatSend').disabled = true; // Legacy Gemini sender removed.
    syncConversationControls();
    await loadConversations();
  } catch (error) { $('chatMessage').textContent = error.message; }
});
let renameConversationId = null;
$('chatRename').addEventListener('click', () => {
  if (!activeConversation) return;
  renameConversationId = activeConversation;
  $('chatRenameInput').value = $('chatTitle').textContent;
  $('chatRenameStatus').textContent = '';
  $('chatRenameDialog').showModal();
  $('chatRenameInput').focus(); $('chatRenameInput').select();
});
$('chatRenameCancel').addEventListener('click', () => $('chatRenameDialog').close());
$('chatRenameForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!renameConversationId || $('chatRenameSave').disabled) return;
  $('chatRenameSave').disabled = true;
  try {
    await postJson('/api/conversations/' + renameConversationId + '/rename', {title: $('chatRenameInput').value});
    $('chatRenameDialog').close(); await loadConversations();
  } catch (error) { $('chatRenameStatus').textContent = error.message; }
  finally { $('chatRenameSave').disabled = false; }
});
$('chatSend').addEventListener('click', async () => {
  if (!activeConversation || chatSending || currentConversation?.simulated) return;
  const account = $('chatAccount').value || 'auto';
  if (!geminiSelectionReady(account)) { $('chatMessage').textContent = '所选账号当前不能发送，请先到账号页核对状态。'; return; }
  const model = $('chatModel').value || null;
  const prompt = $('chatPrompt').value;
  if (!prompt.trim()) { $('chatMessage').textContent = '请输入消息。'; return; }
  const files = Array.from($('chatFiles').files || []);
  const extensions = files.map((file) => file.name.split('.').pop().toLowerCase());
  if (files.length > 4 || files.reduce((total,file) => total + file.size,0) > 100 * 1024 * 1024 || files.some((file,index) => !file.size || !['jpg','jpeg','png','webp','mp4','mov','webm','pdf'].includes(extensions[index]))) {
    $('chatMessage').textContent = '请检查附件格式，最多 4 个，合计不超过 100 MB。'; return;
  }
  const conversation = activeConversation, id = crypto.randomUUID();
  chatSending = true; $('chatSend').disabled = true; // Legacy Gemini sender removed. $('chatFiles').disabled = true;
  syncConversationControls();
  let submitted = false;
  try {
    const attachments = [];
    for (let index = 0; index < files.length; index++) {
      $('chatMessage').textContent = `正在上传附件 ${index + 1}/${files.length}，尚未发送消息。`;
      const response = await fetch(`/api/real-pool/uploads/${id}/${index}/${extensions[index]}`, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/octet-stream','X-Workbench-Request':'1'}, body:files[index]});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '附件上传失败。');
      attachments.push(result);
    }
    $('chatMessage').textContent = '正在等待回复，请勿重复发送。';
    submitted = true;
    sessionStorage.setItem('conversationRequest', JSON.stringify({id,conversation,prompt}));
    const record = await postJson('/api/real-pool/chat', {account,prompt,attachments,conversation_id:conversation,model}, id);
    if (['queued','dispatching','running'].includes(record.status)) {
      await loadConversations(); observeConversationRequest(id,conversation); return;
    }
    sessionStorage.removeItem('conversationRequest');
    if (record.status === 'completed') { $('chatPrompt').value = ''; $('chatFiles').value = ''; }
    chatSending = false; await loadConversations();
    if (record.status === 'failed') await refresh();
  } catch (error) {
    if (!submitted || [400,403].includes(error.status)) {
      sessionStorage.removeItem('conversationRequest'); chatSending = false;
      syncConversationControls();
      $('chatMessage').textContent = error.message;
    } else {
      $('chatMessage').textContent = '结果正在核对，系统不会自动重新发送。';
      observeConversationRequest(id, conversation);
    }
  }
});

async function observeConversationRequest(id, conversation, attempts=0) {
  try {
    const record = await getJson('/api/real-pool/requests/' + id);
    if (!['queued','dispatching','running'].includes(record.status)) {
      const saved=JSON.parse(sessionStorage.getItem('conversationRequest') || '{}');
      if(record.status==='completed' && activeConversation===conversation && saved.prompt===$('chatPrompt').value) $('chatPrompt').value='';
      sessionStorage.removeItem('conversationRequest'); chatSending = false;
      if (activeConversation === conversation) await loadConversations();
      if (record.status === 'failed') await refresh();
      return;
    }
  } catch (_) { /* A missing response is not evidence of non-submission. */ }
  if (attempts < 300) setTimeout(() => observeConversationRequest(id, conversation, attempts + 1), 3000);
  else $('chatMessage').textContent = '结果尚未确认，请稍后刷新核对；系统不会自动重复发送。';
}

function materialSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + ' GB';
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + ' MB';
  return Math.ceil(n / 1024) + ' KB';
}
function materialSource(origin) {
  if (origin.source === 'studiowright') return 'StudioWright';
  if (origin.source === 'flow2api-windows') return 'Flow2API · Windows';
  if (origin.source === 'flow2api-ubuntu') return 'Flow2API · Ubuntu';
  return origin.source || '原来源未记录';
}
function updateOptions(select, first, entries) {
  const previous = select.value;
  const options = [new Option(first, '')];
  for (const entry of entries || []) {
    const value = typeof entry === 'string' ? entry : entry?.value;
    if (typeof value !== 'string' || !value) continue;
    options.push(new Option(typeof entry === 'string' ? value : value + '（' + entry.count + '）', value));
  }
  select.replaceChildren(...options);
  if (previous && !options.some((option) => option.value === previous)) select.add(new Option(previous, previous));
  select.value = previous;
}
function materialValues(list) {
  return Array.isArray(list) ? [...new Set(list.filter((v) => typeof v === 'string').map((v) => v.trim()).filter(Boolean))] : [];
}
function materialTagGroups(item, compact = false) {
  const fields = {...item.structured_tags, ...item.shot_tags, custom: item.tags};
  const labels = {...materialDimensions, ...extraDimensions, custom: '自定义'};
  const order = ['subject', 'scene', 'viewpoint', 'composition', 'movement', 'shot_size', 'action', 'style', 'camera', 'color', 'keywords', 'custom'];
  const rows = order.flatMap((key) => {
    if (key === 'movement' && item.media_type !== 'video') return [];
    const values = materialValues(fields[key]);
    if (!values.length) return [];
    return ['<div class="material-tag-row"><dt>' + esc(labels[key]) + '：</dt><dd>'
      + esc(values.join('、')) + '</dd></div>'];
  });
  if (!rows.length) return '<p class="material-tag-empty">暂无标签</p>';
  const groups = (list, label) => '<dl class="material-tag-groups" aria-label="' + label + '">' + list.join('') + '</dl>';
  if (!compact) return groups(rows, '分类标签');
  return '<div class="material-tag-preview" tabindex="0">'
    + groups(rows.slice(0, 3), '标签摘要')
    + '<span class="material-tag-hint">全部标签</span>'
    + '<div class="material-tag-panel" tabindex="0">' + groups(rows, '全部分类标签') + '</div></div>';
}
function materialCard(item) {
  const id = encodeURIComponent(item.id);
  const origin = item.origins?.[0] || {};
  const title = origin.prompt || '原记录未保存提示词';
  const video = item.media_type === 'video';
  const url = '/api/library/' + id + '/file';
  const media = video
    ? '<video preload="metadata" muted playsinline src="' + url + '#t=0.1" aria-hidden="true"></video><span class="material-play" aria-hidden="true">▶</span>'
    : '<img loading="lazy" src="/api/library/' + id + '/thumbnail" alt="">';
  const source = materialSource(origin) + ((item.origins || []).length > 1 ? ' · ' + item.origins.length + ' 条来源' : '');
  return '<article class="material-card"><button type="button" class="material-cover" data-material="' + esc(item.id)
    + '" aria-label="查看' + (video ? '视频' : '图片') + '：' + esc(title.slice(0, 60)) + '">' + media
    + '<span class="material-kind">' + (video ? '视频' : '图片') + ' · ' + esc(materialSize(item.size))
    + '</span></button><div class="material-card-body"><h3 title="' + esc(title) + '">' + esc(title) + '</h3>'
    + '<p class="material-card-meta">' + esc(origin.account_email || origin.account_id || '原账号未记录') + '</p>'
    + '<p class="material-card-meta">' + esc(source) + '</p>' + materialTagGroups(item, true)
    + '<div class="material-card-actions"><button class="text-link" type="button" data-material="' + esc(item.id)
    + '">预览与管理 →</button><a href="' + url + '?download=1" download>下载</a></div></div></article>';
}
function updateLibraryFilters(data) {
  updateOptions($('libraryAccount'), '全部账号', data.accounts || []);
  for (const [key, label] of Object.entries(materialDimensions)) {
    updateOptions($('libraryFilter_' + key), '全部' + label, data.facets?.dimensions?.[key] || []);
  }
  if (data.stats) {
    $('libraryTotal').innerHTML = esc((data.stats.image || 0) + (data.stats.video || 0)) + '<small>份已保存素材</small>';
    $('libraryBreakdown').innerHTML = esc(data.stats.image || 0) + ' 张图片 · ' + esc(data.stats.video || 0)
      + ' 段视频<small>' + esc(materialSize(data.stats.bytes || 0)) + ' · Ubuntu 素材库</small>';
  }
}
$('libraryDimensionFilters').innerHTML = Object.entries(materialDimensions).map(([key, label]) =>
  '<label>' + esc(label) + '<select id="libraryFilter_' + esc(key) + '"><option value="">全部' + esc(label) + '</option></select></label>').join('');

async function loadLibrary() {
  loadAutoMaterialState();
  loadBackfillState();
  const sequence = ++libraryRequest;
  const isExtra = $('librarySearchScope').value === 'analysis';
  const query = new URLSearchParams({page: String(libraryPage), limit: '12'});
  const text = $('librarySearch').value.trim();
  if (text) query.set('q', text);
  if ($('libraryKind').value) query.set('kind', $('libraryKind').value);
  if ($('libraryAccount').value) query.set('account', $('libraryAccount').value);
  for (const key of Object.keys(materialDimensions)) {
    if ($('libraryFilter_' + key).value) query.set(key, $('libraryFilter_' + key).value);
  }
  $('librarySummary').textContent = '正在读取素材…';
  try {
    let data;
    if (isExtra) {
      const extraQuery = new URLSearchParams({page: String(libraryPage), q: text});
      data = await getJson('/api/material-extra-search?' + extraQuery.toString());
      const items = await Promise.allSettled(data.ids.map((id) => getJson('/api/library/' + encodeURIComponent(id))));
      data.items = items.filter((item) => item.status === 'fulfilled').map((item) => item.value);
      if (data.items.length !== data.ids.length) throw new Error('部分素材暂不可读取');
    } else {
      data = await getJson('/api/library?' + query.toString());
    }
    if (sequence !== libraryRequest) return;
    if (!isExtra) updateLibraryFilters(data);
    libraryPages = data.pages || 1;
    libraryPage = data.page || 1;
    $('librarySummary').textContent = '找到 ' + data.total + ' 份素材' + (isExtra ? ' · 按 Gemini 与补充标签搜索' : '');
    $('libraryPage').textContent = '第 ' + libraryPage + ' / ' + libraryPages + ' 页';
    $('libraryPrev').disabled = libraryPage <= 1;
    $('libraryNext').disabled = libraryPage >= libraryPages;
    $('libraryItems').innerHTML = data.items.length ? data.items.map(materialCard).join('')
      : empty('没有找到素材', '试试更换关键词或清空筛选条件。');
  } catch (_) {
    if (sequence !== libraryRequest) return;
    $('librarySummary').textContent = '素材库暂不可读取，请检查原素材库服务。';
    $('libraryItems').innerHTML = empty('暂时无法读取素材', '请稍后刷新重试。');
  }
}

async function loadAutoMaterialState() {
  const button = $('autoAnalysisToggle');
  try {
    const data = await getJson('/api/auto-material-analysis');
    autoMaterialState = data;
    const counts = data.counts || {};
    const summary = '等待 ' + (counts.waiting || 0) + ' · 已完成 ' + (counts.completed || 0)
      + ' · 需核对 ' + ((counts.unknown || 0) + (counts.failed || 0));
    $('autoAnalysisStatus').textContent = !data.enabled ? '已关闭' : data.paused_reason ? '已暂停：' + data.paused_reason : '已开启 · ' + summary;
    button.textContent = !data.enabled ? '开启自动分析' : data.paused_reason ? '继续自动分析' : '关闭自动分析';
    button.disabled = false;
  } catch (_) {
    autoMaterialState = null;
    $('autoAnalysisStatus').textContent = '设置暂不可读取。';
    button.textContent = '暂不可用'; button.disabled = true;
  }
}
$('autoAnalysisToggle').addEventListener('click', async () => {
  if (!autoMaterialState) return;
  const button = $('autoAnalysisToggle');
  button.disabled = true;
  try {
    const enabled = !autoMaterialState.enabled || !!autoMaterialState.paused_reason;
    await postJson('/api/auto-material-analysis', {enabled});
    await loadAutoMaterialState();
  } catch (error) {
    $('autoAnalysisStatus').textContent = error.message;
    button.disabled = false;
  }
});

function renderBackfill() {
  const run = backfillState?.run, counts = backfillState?.counts || {};
  const pending = (counts.waiting || 0) + (counts.running || 0);
  const paused = !!run?.paused_reason;
  const summary = '补打：已完成 ' + (counts.completed || 0) + ' · 等待 ' + (counts.waiting || 0)
    + ' · 处理中 ' + (counts.running || 0) + ' · 需核对 ' + ((counts.failed || 0) + (counts.unknown || 0));
  $('backfillStatus').textContent = run ? summary + (paused ? ' · 已暂停' : '') : '';
  $('backfillResume').hidden = !(paused && pending && ['untagged', 'unanalyzed'].includes(run.scope));
  $('backfillResume').disabled = backfillBusy;
  $('backfillOpen').disabled = backfillBusy || !backfillState || !!pending;
  $('backfillOpen').textContent = !backfillState ? '读取中…' : backfillBusy ? '正在处理…' : pending ? (paused ? '补打已暂停' : '补打进行中') : '补打标签';
  $('backfillIssues').innerHTML = (backfillState?.issues || []).map((issue) =>
    '<p class="storage-warning">' + esc(issue.note || '分析结果需要核对') + ' <button type="button" class="text-link" data-backfill-issue="' + esc(issue.asset_id) + '">查看素材</button></p>').join('');
  if (!backfillBusy && paused) $('backfillMessage').textContent = run.paused_reason;
  else if (!backfillBusy && pending) $('backfillMessage').textContent = '后台处理中；没有可用账号或额度时会等待。';
  else if (!backfillBusy && $('backfillMessage').textContent === '后台处理中；没有可用账号或额度时会等待。') $('backfillMessage').textContent = '本轮补打已结束。';
}

async function loadBackfillState() {
  try {
    const previous = backfillState, next = await getJson('/api/material-backfill');
    backfillState = next;
    if (backfillRequest && next.run?.id === backfillRequest) backfillRequest = null;
    renderBackfill();
    if (previous && next.run && previous.run?.id === next.run.id && (next.counts?.completed || 0) > (previous.counts?.completed || 0) && state.view === 'library') loadLibrary();
    return true;
  } catch (_) {
    backfillState = null; renderBackfill();
    $('backfillStatus').textContent = '补打进度暂不可读取。';
    return false;
  }
}

$('backfillOpen').addEventListener('click', async () => {
  if (backfillBusy || !backfillState || (backfillState.counts?.waiting || 0) + (backfillState.counts?.running || 0)) return;
  backfillBusy = true; renderBackfill();
  $('backfillMessage').textContent = '正在查找尚未进行 Gemini 分析的素材…';
  try {
    if (backfillRequest) {
      $('backfillMessage').textContent = '上次提交状态尚未确认，请查看补打进度，不会重复提交。';
      return;
    }
    const preview = await getJson('/api/material-backfill/preview');
    if (!Number.isInteger(preview.unanalyzed) || preview.unanalyzed < 0) throw new Error('素材状态暂不可读取。');
    if (!preview.unanalyzed) {
      $('backfillMessage').textContent = '当前没有可补充分析的素材。已有分析、失败或结果未确认的素材不会重复提交。';
      return;
    }
    $('backfillPreviewCount').textContent = String(preview.unanalyzed);
    $('backfillMessage').textContent = '请在弹窗中确认补打范围。';
    if (!$('backfillDialog').open) $('backfillDialog').showModal();
  } catch (error) {
    $('backfillMessage').textContent = error.message || '素材库暂不可读取，请稍后再试。';
  } finally {
    backfillBusy = false;
    renderBackfill();
  }
});
$('backfillCancel').addEventListener('click', () => {
  $('backfillDialog').close();
  $('backfillMessage').textContent = '已取消，本次没有提交分析。';
});
$('backfillDialog').addEventListener('cancel', (event) => {
  if (backfillBusy) { event.preventDefault(); return; }
  $('backfillMessage').textContent = '已取消，本次没有提交分析。';
});
$('backfillConfirm').addEventListener('click', async () => {
  if (backfillBusy || backfillRequest) return;
  backfillBusy = true;
  $('backfillConfirm').disabled = true;
  $('backfillCancel').disabled = true;
  renderBackfill();
  try {
    backfillRequest = crypto.randomUUID();
    backfillState = await postJson('/api/material-backfill', {scope:'unanalyzed'}, backfillRequest);
    backfillRequest = null;
    $('backfillDialog').close();
    $('backfillMessage').textContent = '已加入后台队列。';
  } catch (error) {
    const request = backfillRequest;
    const confirmed = await loadBackfillState();
    $('backfillDialog').close();
    if (request && backfillState?.run?.id === request) {
      backfillRequest = null;
      $('backfillMessage').textContent = '已加入后台队列。';
    } else {
      $('backfillMessage').textContent = error.status ? error.message : (confirmed && !request ? '素材库暂不可读取，请稍后再试。' : '提交状态暂未确认，请查看补打进度。');
      if (error.status) backfillRequest = null;
    }
  } finally {
    backfillBusy = false;
    $('backfillConfirm').disabled = false;
    $('backfillCancel').disabled = false;
    renderBackfill();
  }
});
$('backfillIssues').addEventListener('click', (event) => {
  const button = event.target.closest('[data-backfill-issue]');
  if (button) showMaterial(button.dataset.backfillIssue).catch(() => { $('backfillMessage').textContent = '素材详情暂不可读取。'; });
});
$('backfillResume').addEventListener('click', async () => {
  if (backfillBusy || !['untagged', 'unanalyzed'].includes(backfillState?.run?.scope)) return;
  backfillBusy = true; renderBackfill();
  try {
    backfillState = await postJson('/api/material-backfill/resume', {});
    $('backfillMessage').textContent = '已继续处理剩余素材，不会重复提交之前的素材。';
  } catch (error) { $('backfillMessage').textContent = error.message; }
  finally { backfillBusy = false; renderBackfill(); }
});
setInterval(() => { if (state.view === 'library') loadBackfillState(); }, 5000);

let materialActivityLoading = false, materialActivityPage = 1, materialActivityPages = 1;
function materialAnalysisAccount(key) {
  if (!key) return '未记录账号';
  const accounts = state.snapshot?.real_accounts?.items || [];
  const match = accounts.find((a) => a.key === key || a.key === 'flow-account-' + key);
  return match ? (match.email || match.label || match.profile || '账号名称未记录') : '账号信息暂不可用';
}
function materialAnalysisResult(record) {
  const labels = {...materialDimensions, ...extraDimensions};
  const values = Object.entries(record.result_tags || {}).filter(([, v]) => Array.isArray(v) && v.length)
    .map(([key, v]) => esc(labels[key] || key) + '：' + v.map(esc).join('、'));
  return values.length ? '<details><summary>查看本次返回的标签</summary>' + values.join('<br>') + '</details>' : '<small>本次没有可展示的标签结果</small>';
}
function materialAnalysisStatus(record) {
  return ({completed:'已完成', preparing:'处理中', saving:'保存中', failed:'失败', invalid:'回复格式待核对', unknown:'结果待核对'})[record.status] || '状态待核对';
}
async function loadMaterialActivity(page = materialActivityPage) {
  if (materialActivityLoading) return;
  materialActivityLoading = true;
  $('materialActivityRefresh').disabled = true;
  $('materialActivityStatus').textContent = '正在读取素材分析记录…';
  try {
    const data = await getJson('/api/material-analysis-history?page=' + page + '&limit=20');
    if (!Array.isArray(data.items) || !Number.isInteger(data.pages)) throw new Error('记录格式不正确');
    materialActivityPage = data.page; materialActivityPages = data.pages;
    const ids = [...new Set(data.items.map((row) => row.asset_id))];
    const results = await Promise.allSettled(ids.map((id) => getJson('/api/library/' + encodeURIComponent(id))));
    const assets = new Map(ids.map((id, i) => [id, results[i].status === 'fulfilled' ? results[i].value : null]));
    $('materialActivityStatus').textContent = '共 ' + data.total + ' 条分析记录。';
    $('materialActivityPage').textContent = '第 ' + materialActivityPage + ' / ' + materialActivityPages + ' 页';
    $('materialActivityPrev').disabled = materialActivityPage <= 1;
    $('materialActivityNext').disabled = materialActivityPage >= materialActivityPages;
    $('materialActivityList').innerHTML = data.items.length ? '<table><thead><tr><th>时间</th><th>素材</th><th>调用账号</th><th>状态与本次结果</th><th>查看素材</th></tr></thead><tbody>' + data.items.map((record) => {
      const item = assets.get(record.asset_id), title = (item?.origins?.[0]?.prompt || '素材信息暂不可读取').slice(0, 60);
      return '<tr><td>' + esc(dateTime(record.created)) + '</td><td>' + (item?.media_type === 'video' ? '视频 · ' : item ? '图片 · ' : '') + esc(title)
        + '</td><td>' + esc(materialAnalysisAccount(record.account_key)) + '</td><td>' + esc(materialAnalysisStatus(record)) + materialAnalysisResult(record)
        + '</td><td><button type="button" class="text-link" data-analysis-material="' + esc(record.asset_id) + '">查看素材和标签</button></td></tr>';
    }).join('') + '</tbody></table>' : empty('暂无素材分析记录');
  } catch (_) {
    $('materialActivityStatus').textContent = '素材分析记录暂不可读取，请稍后刷新。';
  } finally {
    materialActivityLoading = false;
    $('materialActivityRefresh').disabled = false;
  }
}
$('materialActivityPanel').addEventListener('toggle', () => { if ($('materialActivityPanel').open) loadMaterialActivity(); });
$('materialActivityRefresh').addEventListener('click', () => loadMaterialActivity());
$('materialActivityPrev').addEventListener('click', () => { if (materialActivityPage > 1) loadMaterialActivity(materialActivityPage-1); });
$('materialActivityNext').addEventListener('click', () => { if (materialActivityPage < materialActivityPages) loadMaterialActivity(materialActivityPage+1); });
$('materialActivityList').addEventListener('click', (event) => {
  const button = event.target.closest('[data-analysis-material]');
  if (button) showMaterial(button.dataset.analysisMaterial).catch(() => { $('materialActivityStatus').textContent = '素材详情暂不可读取。'; });
});

function materialTagSources(item, extra) {
  const groups = {'提示词标签': [], '视觉分析（与 Gemini 结果一致）': [], '视觉分析（来源待核实）': [], '手动补充': [], '其他或来源未记录': []};
  const labels = {...materialDimensions, ...extraDimensions};
  const known = new Set();
  for (const tag of item.tag_sources || []) {
    if (!tag || typeof tag.dimension !== 'string' || typeof tag.value !== 'string') continue;
    known.add(tag.dimension + '\u0000' + tag.value);
    const matches = extra.analysis?.status === 'completed' && Array.isArray(extra.suggested?.[tag.dimension]) && extra.suggested[tag.dimension].includes(tag.value);
    const group = tag.source === 'prompt_analysis' ? '提示词标签' : tag.source === 'visual_analysis' ? (matches ? '视觉分析（与 Gemini 结果一致）' : '视觉分析（来源待核实）') : '其他或来源未记录';
    groups[group].push((labels[tag.dimension] || tag.dimension) + '：' + tag.value);
  }
  for (const [key, values] of Object.entries(item.structured_tags || {})) {
    for (const value of Array.isArray(values) ? values : []) if (!known.has(key + '\u0000' + value)) groups['其他或来源未记录'].push((labels[key] || key) + '：' + value);
  }
  for (const value of item.tags || []) groups['其他或来源未记录'].push('自定义：' + value);
  for (const [key, values] of Object.entries(extra.manual || {})) for (const value of Array.isArray(values) ? values : []) groups['手动补充'].push((labels[key] || key) + '：' + value);
  return Object.entries(groups).filter(([, values]) => values.length).map(([name, values]) =>
    '<div class="material-suggestions"><strong>' + esc(name) + '</strong><br>' + values.map(esc).join('、') + '</div>').join('');
}

function materialShotNotes(item, extra) {
  const video = item.media_type === 'video';
  const complete = extra.analysis?.status === 'completed';
  const analyzed = complete ? extra.suggested : null;
  const fields = video ? ['movement', 'composition', 'viewpoint', 'shot_size'] : ['composition', 'viewpoint', 'shot_size'];
  const camera = materialValues(analyzed?.camera);
  const rows = fields.map((key) => {
    const manual = materialValues(extra.manual?.[key]);
    let result = materialValues(analyzed?.[key]);
    let source = manual.length ? '（手动）' : '';
    // Some analyses report a stationary camera only in the camera field.
    if (!manual.length && !result.length && key === 'movement' && camera.length && camera.every((v) => ['固定镜头', '固定机位', '静止镜头'].includes(v))) {
      result = ['固定镜头'];
      source = '（据镜头标签）';
    }
    const label = (manual.length ? manual : result).join('、');
    const explanation = extra.shot_notes?.[key];
    return '<div class="material-shot-row"><strong>' + esc(extraDimensions[key]) + '：</strong>'
      + esc(label || (explanation ? '' : '暂无记录')) + esc(source)
      + (key !== 'shot_size' && (label || explanation)
        ? '<p class="material-shot-explanation">' + esc(explanation || '暂无具体说明') + '</p>' : '')
      + '</div>';
  });
  const note = extra.shot_notes_source === 'gemini'
    ? (video ? '具体说明由 Gemini 根据按时间排序的视频抽帧生成。' : '具体说明由 Gemini 根据原图生成。')
    : complete && /抽帧/.test(extra.analysis?.note || '') && video
    ? 'Gemini 根据视频抽帧分析，细微运镜以原片为准。'
    : complete ? '说明来自已保存的 Gemini 分析和手动记录。' : '已有手动记录会显示在这里；未确认的分析不会当作结果。';
  return '<section class="material-suggestions" aria-label="镜头说明"><h3>镜头说明</h3>'
    + rows.join('') + '<small>' + esc(note) + '</small></section>';
}

async function loadMaterialHistory(id, page = 1) {
  const panel = $('materialHistory');
  if (!panel) return;
  panel.textContent = '正在读取此素材的分析历史…';
  try {
    const data = await getJson('/api/material-analysis-history?asset_id=' + encodeURIComponent(id) + '&page=' + page + '&limit=10');
    if (currentMaterial?.id !== id || $('materialHistory') !== panel) return;
    panel.innerHTML = data.items.length ? data.items.map((record) => '<div class="material-suggestions"><strong>' + esc(dateTime(record.created))
      + ' · ' + esc(materialAnalysisStatus(record)) + '</strong><br>调用账号：' + esc(materialAnalysisAccount(record.account_key))
      + '<br>' + materialAnalysisResult(record) + '</div>').join('')
      + '<div class="library-pagination"><button class="button button-light" type="button" data-material-history-page="' + (data.page-1) + '"' + (data.page <= 1 ? ' disabled' : '') + '>上一页</button><span>第 ' + data.page + ' / ' + data.pages + ' 页</span><button class="button button-light" type="button" data-material-history-page="' + (data.page+1) + '"' + (data.page >= data.pages ? ' disabled' : '') + '>下一页</button></div>'
      : '<p>暂无分析记录。</p>';
  } catch (_) { if ($('materialHistory') === panel) panel.textContent = '分析历史暂不可读取。'; }
}

async function showMaterial(id) {
  const sequence = ++materialOpenRequest;
  const [item, extra] = await Promise.all([getJson('/api/library/' + encodeURIComponent(id)), getJson('/api/material-extras/' + encodeURIComponent(id))]);
  if (sequence !== materialOpenRequest) return;
  currentMaterial = item;
  currentMaterialExtra = extra;
  materialDirty = false;
  const url = '/api/library/' + encodeURIComponent(id) + '/file';
  const technical = extra.technical || item;
  const resolution = technical.width && technical.height ? technical.width + ' × ' + technical.height : '未读取';
  const duration = item.media_type === 'image' ? '不适用' : (typeof technical.duration === 'number' ? technical.duration.toFixed(1) + ' 秒' : '未读取');
  const labels = {...materialDimensions, ...extraDimensions};
  const suggested = Object.entries(extra.suggested || {}).filter(([, values]) => values.length)
    .map(([key, values]) => (labels[key] || key) + '：' + values.join('、'));
  const analysisBusy = ['preparing', 'saving', 'unknown'].includes(extra.analysis?.status);
  const origin = item.origins?.[0] || {};
  const preview = item.media_type === 'image'
    ? '<img src="' + url + '" alt="素材原图">'
    : '<video src="' + url + '" controls playsinline preload="metadata"></video>';
  const editFields = Object.entries(materialDimensions).map(([key, label]) =>
    '<label>' + esc(label) + '<input id="dimension_' + esc(key) + '" value="' + esc((item.structured_tags?.[key] || []).join('，')) + '" maxlength="3000"></label>').join('')
    + Object.entries(extraDimensions).map(([key, label]) =>
      '<label>' + esc(label) + '<input id="dimension_' + esc(key) + '" value="' + esc((extra.manual?.[key] || []).join('，')) + '" maxlength="3000"></label>').join('');
  $('libraryDetail').innerHTML = '<div class="material-layout"><div><div class="material-preview">' + preview + '</div>'
    + '<p class="material-facts">分辨率：' + esc(resolution) + '　·　时长：' + esc(duration) + '　·　' + esc(materialSize(item.size)) + '</p>'
    + '<p class="material-facts">来源：' + esc(materialSource(origin)) + '　·　账号：'
    + esc(origin.account_email || origin.account_id || '未记录') + ((item.origins || []).length > 1 ? '　·　共 ' + item.origins.length + ' 条来源' : '') + '</p>'
    + '<div class="material-prompt">' + esc(origin.prompt || '原记录未保存提示词') + '</div></div><div class="material-panel"><h3>素材与标签</h3>'
    + '<a class="text-link material-download" href="' + url + '?download=1" download>下载原文件 ↓</a>'
    + '<div id="materialShotContent">' + materialShotNotes(item, extra) + '</div>'
    + (Object.keys(extra.shot_notes || {}).length || extra.shot_job ? '' : '<button id="materialShotAnalyze" class="button button-light" type="button">用 Gemini 补充镜头说明</button>')
    + '<p id="materialShotStatus" role="status">' + esc(extra.shot_job?.note || '') + '</p>'
    + materialTagGroups(item)
    + '<details class="material-edit" open><summary>标签来源</summary><p>提示词标签来自文字，不代表画面已核实；“与 Gemini 结果一致”表示内容匹配，不单独证明写入来源。</p>' + (materialTagSources(item, extra) || '<p>没有记录标签来源。</p>') + '</details>'
    + '<button id="materialAnalyze" class="button button-light" type="button"' + (analysisBusy ? ' disabled' : '') + '>用 Gemini 分析素材</button>'
    + '<p>会真实调用 Gemini，可能产生费用；不会覆盖已有标签。</p><p id="analysisMessage" role="status">' + esc(extra.analysis?.note || extra.automatic?.note || '') + '</p>'
    + (suggested.length ? '<div class="material-suggestions"><strong>Gemini 分析标签</strong><br>' + suggested.map(esc).join('<br>')
      + '</div><button id="materialApply" class="button button-light" type="button">将分析结果填入空白标签</button>' : '')
    + '<details class="material-edit" open><summary>此素材的 Gemini 分析历史</summary><div id="materialHistory" aria-live="polite">正在读取…</div></details>'
    + '<details class="material-edit" id="materialEdit"><summary>编辑标签与分析维度</summary><div class="material-fields">'
    + '<label class="wide">自定义标签<input id="materialTags" value="' + esc((item.tags || []).join('，')) + '" maxlength="2400"></label>' + editFields
    + '</div><button id="materialSave" class="button button-primary material-save" type="button">保存标签</button><p id="materialMessage" role="status"></p></details></div></div>';
  $('materialSave').addEventListener('click', saveMaterialTags);
  $('materialAnalyze').addEventListener('click', analyzeMaterial);
  $('materialShotAnalyze')?.addEventListener('click', async (event) => {
    const button = event.currentTarget, version = materialOpenRequest;
    button.disabled = true;
    $('materialShotStatus').textContent = '正在把素材交给 Gemini…';
    try {
      const result = await postJson('/api/material-shot-notes/' + id, {}, crypto.randomUUID());
      if (version === materialOpenRequest) {
        button.remove();
        updateMaterialShot(item, result, version);
      }
    } catch (error) {
      if (version === materialOpenRequest) $('materialShotStatus').textContent = '提交未确认，请重新打开详情核对。' + error.message;
    }
  });
  updateMaterialShot(item, extra, materialOpenRequest);
  $('materialHistory').addEventListener('click', (event) => {
    const button = event.target.closest('[data-material-history-page]');
    if (button && !button.disabled) loadMaterialHistory(id, Number(button.dataset.materialHistoryPage));
  });
  loadMaterialHistory(id);
  if ($('materialApply')) $('materialApply').addEventListener('click', () => {
    for (const [key, values] of Object.entries(currentMaterialExtra.suggested || {})) {
      const field = $('dimension_' + key);
      if (field && !field.value.trim()) field.value = values.join('，');
    }
    materialDirty = true;
    $('materialEdit').open = true;
    $('materialMessage').textContent = '分析结果已填入空白项，请核对后保存。';
  });
  if (!$('libraryDetailPanel').open) $('libraryDetailPanel').showModal();
}
function updateMaterialShot(item, extra, version, checks = 36) {
  if (version !== materialOpenRequest || !$('materialShotContent')) return;
  $('materialShotContent').innerHTML = materialShotNotes(item, extra);
  $('materialShotStatus').textContent = extra.shot_job?.note || '';
  if (checks > 0 && ['waiting', 'running'].includes(extra.shot_job?.status)) {
    setTimeout(async () => {
      if (version !== materialOpenRequest) return;
      try {
        const result = await getJson('/api/material-extras/' + encodeURIComponent(item.id));
        updateMaterialShot(item, result, version, checks - 1);
      } catch (_) {
        if (version === materialOpenRequest) $('materialShotStatus').textContent = '暂时无法读取进度，重新打开详情可核对。';
      }
    }, 5000);
  }
}
function splitTags(value) { return value.split(/[,，]/).map((part) => part.trim()).filter(Boolean); }
async function saveMaterialTags() {
  if (!currentMaterial || materialSaving) return;
  const data = {}, tags = splitTags($('materialTags').value), dimensions = {};
  if (JSON.stringify(tags) !== JSON.stringify(currentMaterial.tags || [])) data.tags = tags;
  for (const key of Object.keys(materialDimensions)) {
    const values = splitTags($('dimension_' + key).value);
    if (JSON.stringify(values) !== JSON.stringify(currentMaterial.structured_tags?.[key] || [])) dimensions[key] = values;
  }
  const extra = {};
  for (const key of Object.keys(extraDimensions)) extra[key] = splitTags($('dimension_' + key).value);
  const extraChanged = Object.keys(extraDimensions).some((key) => JSON.stringify(extra[key]) !== JSON.stringify(currentMaterialExtra.manual?.[key] || []));
  if (Object.keys(dimensions).length) data.structured_tags = dimensions;
  if (!Object.keys(data).length && !extraChanged) { materialDirty = false; $('materialMessage').textContent = '没有需要保存的更改。'; return; }
  let originalSaved = false, extraSaved = false, refreshing = false;
  const id = currentMaterial.id;
  materialSaving = true;
  $('materialSave').disabled = true;
  $('libraryDetail').querySelectorAll('input, #materialApply, #materialAnalyze').forEach((control) => { control.disabled = true; });
  $('materialMessage').textContent = '正在保存…';
  try {
    if (Object.keys(data).length) { await postJson('/api/library/' + id + '/tags', data); originalSaved = true; }
    if (extraChanged) { await postJson('/api/material-extras/' + id, extra); extraSaved = true; }
    materialDirty = false;
    refreshing = true;
    await showMaterial(id);
    $('materialEdit').open = true;
    $('materialMessage').textContent = '标签已保存。';
    loadLibrary();
  } catch (error) {
    $('materialMessage').textContent = refreshing
      ? '标签保存已提交，但详情刷新失败，请关闭后重新打开核对。'
      : ((originalSaved && !extraSaved ? '原素材库标签已保存；其他维度未保存：' : '') + error.message);
  } finally {
    materialSaving = false;
    if ($('materialSave')) $('materialSave').disabled = false;
    $('libraryDetail').querySelectorAll('input, #materialApply').forEach((control) => { control.disabled = false; });
    if ($('materialAnalyze') && !['preparing', 'saving', 'unknown'].includes(currentMaterialExtra?.analysis?.status)) $('materialAnalyze').disabled = false;
  }
}
async function analyzeMaterial() {
  if (!currentMaterial) return;
  if (materialDirty) { $('analysisMessage').textContent = '请先保存或放弃未保存的标签，再分析素材。'; return; }
  const id = currentMaterial.id, request = crypto.randomUUID();
  $('materialAnalyze').disabled = true;
  $('libraryDetail').querySelectorAll('input, #materialSave, #materialApply').forEach((control) => { control.disabled = true; });
  $('analysisMessage').textContent = '正在分析，请勿重复提交。';
  try {
    await postJson('/api/material-analysis/' + id, {}, request);
    if (currentMaterial?.id === id) await showMaterial(id);
  } catch (_) {
    if (currentMaterial?.id === id) {
      $('libraryDetail').querySelectorAll('input, #materialSave, #materialApply').forEach((control) => { control.disabled = false; });
      $('analysisMessage').textContent = '结果暂未确认，请刷新核对；系统不会自动重复提交。';
    }
  }
}
$('libraryItems').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-material]');
  if (button) { try { await showMaterial(button.dataset.material); } catch (_) { $('librarySummary').textContent = '素材详情暂不可读取。'; } }
});
$('libraryDetail').addEventListener('input', (event) => { if (event.target.matches('input')) materialDirty = true; });
$('libraryDetailPanel').addEventListener('cancel', (event) => {
  if (materialSaving || (materialDirty && !confirm('标签尚未保存，确定关闭并放弃修改吗？'))) event.preventDefault();
});
$('libraryDetailPanel').addEventListener('close', () => { materialOpenRequest++; currentMaterial = null; currentMaterialExtra = null; materialDirty = false; });
$('libraryClose').addEventListener('click', () => {
  if (materialSaving || (materialDirty && !confirm('标签尚未保存，确定关闭并放弃修改吗？'))) return;
  $('libraryDetailPanel').close();
});
$('libraryFind').addEventListener('click', () => { libraryPage = 1; loadLibrary(); });
$('librarySearch').addEventListener('input', () => { libraryRequest++; });
$('librarySearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); libraryPage = 1; loadLibrary(); } });
$('libraryKind').addEventListener('change', () => { libraryPage = 1; loadLibrary(); });
$('libraryAccount').addEventListener('change', () => { libraryPage = 1; loadLibrary(); });
$('libraryDimensionFilters').addEventListener('change', () => { libraryPage = 1; loadLibrary(); });
$('librarySearchScope').addEventListener('change', () => {
  const extra = $('librarySearchScope').value === 'analysis';
  $('libraryKind').disabled = extra;
  $('libraryAccount').disabled = extra;
  $('libraryDimensionFilters').querySelectorAll('select').forEach((control) => { control.disabled = extra; });
  libraryPage = 1; loadLibrary();
});
$('libraryReset').addEventListener('click', () => {
  $('librarySearch').value = '';
  $('libraryKind').value = '';
  $('libraryAccount').value = '';
  $('librarySearchScope').value = 'original';
  $('libraryKind').disabled = false;
  $('libraryAccount').disabled = false;
  $('libraryDimensionFilters').querySelectorAll('select').forEach((control) => { control.value = ''; control.disabled = false; });
  libraryPage = 1; loadLibrary();
});
$('libraryPrev').addEventListener('click', () => { if (libraryPage > 1) { libraryPage--; loadLibrary(); } });
$('libraryNext').addEventListener('click', () => { if (libraryPage < libraryPages) { libraryPage++; loadLibrary(); } });

async function showLibraryStorage() {
  $('libraryStorageMessage').textContent = '';
  $('libraryStoragePath').textContent = '正在读取…';
  $('libraryStorageSpace').textContent = '';
  $('libraryStoragePending').textContent = '';
  if (!$('libraryStorageDialog').open) $('libraryStorageDialog').showModal();
  try {
    const data = await getJson('/api/storage/library');
    $('libraryStoragePath').textContent = data.current;
    $('libraryStorageSpace').textContent = '所在磁盘可用 ' + materialSize(data.available_bytes);
    if (data.plan) {
      $('libraryStorageTarget').value = data.plan.target;
      $('libraryStoragePending').textContent = '已保存计划位置（尚未生效）：' + data.plan.target
        + (data.plan.current !== data.current ? '。当前库位置已变化，请重新核对。' : '');
    }
  } catch (_) { $('libraryStoragePath').textContent = '暂时无法读取，请检查原素材库服务。'; }
}
$('libraryStorageOpen').addEventListener('click', showLibraryStorage);
$('libraryStorageClose').addEventListener('click', () => $('libraryStorageDialog').close());
$('libraryStoragePlan').addEventListener('click', async () => {
  const button = $('libraryStoragePlan');
  button.disabled = true;
  $('libraryStorageMessage').textContent = '正在检查目录…';
  try {
    const result = await postJson('/api/storage/library/plan', {target: $('libraryStorageTarget').value.trim()});
    $('libraryStorageTarget').value = result.target;
    $('libraryStoragePending').textContent = '已保存计划位置（尚未生效）：' + result.target;
    $('libraryStorageMessage').textContent = '目录可写、可用空间 ' + materialSize(result.available_bytes) + '。当前素材仍保存在原位置，切换前还需停机迁移并复核。';
  } catch (error) { $('libraryStorageMessage').textContent = error.message; }
  finally { button.disabled = false; }
});

async function observeFlowRequest(id, attempts = 0) {
  if (sessionStorage.getItem("flowRequest") !== id) return;
  try {
    const response = await fetch(`/api/real-pool/flow-requests/${id}`, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("not available");
    const record = await response.json();
    showFlowRecord(record);
    if (record.status === "dispatching" && attempts < 10) setTimeout(() => observeFlowRequest(id, attempts + 1), 2000);
    else if (record.status === "dispatching") showFlowRecord({status: "unknown"});
    else if (record.status === "submitted") refresh();
  } catch (_) { showFlowRecord({status: "unknown"}); }
}

$("flowKind").addEventListener("change", renderFlowModels);
$("flowProject").addEventListener("change", renderFlowModels);
$("flowSend").addEventListener("click", async () => {
  const project_key = $("flowProject").value;
  const kind = $("flowKind").value;
  const params = {prompt: $("flowPrompt").value, model: $("flowModel").value, aspect: $("flowAspect").value,
    count: Number($("flowCount").value), resolution: $("flowResolution").value,
    duration: Number($("flowDuration").value), max_credits: Number($("flowBudget").value)};
  if (!params.prompt.trim() || !$("flowConfirm").checked) {
    $("flowMessage").textContent = "请填写提示词并确认可能的积分消耗。"; return;
  }
  const id = crypto.randomUUID();
  const autoRetry = false;
  const payload = {kind, params, confirm: true, auto_retry: autoRetry};
  if (project_key && project_key !== "auto") payload.project_key = project_key;
  $("flowAutoRetry").checked = false;
  $("flowAutoRetry").disabled = true;
  sessionStorage.setItem("flowRequest", id);
  state.flowBusy = true;
  $("flowSend").disabled = true;
  $("flowMessage").textContent = "正在提交真实任务，请不要重复操作。";
  try {
    const response = await fetch("/api/real-pool/flow", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Workbench-Request": "1", "Idempotency-Key": id},
      body: JSON.stringify(payload)});
    const record = await response.json();
    if (response.status === 400 || response.status === 403) {
      sessionStorage.removeItem("flowRequest"); state.flowBusy = false;
      $("flowAutoRetry").disabled = true;
      $("flowMessage").textContent = record.error || "任务未提交，请检查填写内容。";
      renderFlowForm(); return;
    }
    if (!response.ok) throw new Error("outcome unknown");
    showFlowRecord(record);
    if (record.status === "dispatching") observeFlowRequest(id);
    else if (record.status === "submitted") { $("flowConfirm").checked = false; $("flowAutoRetry").checked = false; await refresh(); }
  } catch (_) { await observeFlowRequest(id); }
});

$("flowNewRequest").addEventListener("click", () => {
  sessionStorage.removeItem("flowRequest"); state.flowBusy = false;
  $("flowAutoRetry").disabled = true;
  $("flowNewRequest").hidden = true;
  $("flowMessage").textContent = "已结束这条记录，可创建新的任务。";
  renderFlowForm();
});

async function loadApiKeys() {
  const status = $('apiKeyStatus');
  try {
    const response = await fetch('/api/access-keys', {cache:'no-store', credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok || !Array.isArray(data.items)) throw new Error('unavailable');
    const list = $('apiKeyList'); list.replaceChildren();
    for (const key of data.items) {
      if (!/^[0-9a-f]{32}$/.test(key.id)) continue;
      const row = document.createElement('div'); row.className = 'api-key-row';
      const detail = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = key.name;
      const meta = document.createElement('small');
      meta.textContent = '•••• ' + key.suffix + ' · 使用中 · 创建于 ' + new Date(key.created * 1000).toLocaleString('zh-CN');
      detail.append(title, meta); row.appendChild(detail);
      const actions = document.createElement('div'); actions.className = 'api-key-actions';
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'api-key-copy';
      copy.textContent = '复制'; copy.dataset.keyId = key.id; copy.dataset.keyAction = 'copy';
      copy.setAttribute('aria-label', '复制密钥 ' + key.name);
      if (!key.copyable) { copy.disabled = true; copy.title = '旧密钥未保存原文，无法复制，请新建密钥。'; }
      const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'api-key-revoke';
      revoke.textContent = '撤销'; revoke.dataset.keyId = key.id; revoke.dataset.keyName = key.name; revoke.dataset.keyAction = 'revoke';
      revoke.setAttribute('aria-label', '撤销密钥 ' + key.name);
      actions.append(copy, revoke); row.appendChild(actions);
      list.appendChild(row);
    }
    status.textContent = data.items.length ? '新建密钥可重复复制；撤销后将删除，已提交的请求可能继续执行。' : '还没有密钥，创建后即可接入。';
  } catch (_) {
    $('apiKeyList').replaceChildren();
    status.textContent = '密钥管理暂不可用，请确认工作台已连接。';
  }
}

$('apiKeyForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('apiKeyCreate'); if (button.disabled) return;
  button.disabled = true; $('apiKeyStatus').textContent = '正在创建密钥…';
  try {
    const response = await fetch('/api/access-keys', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:JSON.stringify({name:$('apiKeyName').value.trim()})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '创建失败。');
    if (!/^swu_[A-Za-z0-9_-]{43}$/.test(data.key)) throw new Error('密钥返回格式不正确。');
    $('apiKeyName').value = ''; $('apiKeySecret').value = data.key; $('apiKeyCopyStatus').textContent = '';
    $('apiKeyDialogHeading').textContent = '密钥创建成功'; $('apiKeyDialogHelp').textContent = '密钥已加密保存，以后可在列表中再次复制。';
    $('apiKeyDialog').showModal(); $('apiKeyCopy').focus();
    loadApiKeys();
  } catch (error) {
    $('apiKeyStatus').textContent = '创建未确认，请先刷新页面核对密钥列表。' + (error.message || '');
  } finally { button.disabled = false; }
});
$('apiKeyList').addEventListener('click', async event => {
  const button = event.target.closest('button[data-key-id][data-key-action]'); if (!button || button.disabled) return;
  const action = button.dataset.keyAction;
  if (action === 'revoke' && !window.confirm('撤销并删除后，“' + button.dataset.keyName + '”的新请求会被拒绝，已提交的请求可能继续执行。确定撤销？')) return;
  if (!['copy', 'revoke'].includes(action)) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/access-keys/' + button.dataset.keyId + '/' + action, {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:'{}'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作未完成。');
    if (action === 'copy') {
      if (!/^swu_[A-Za-z0-9_-]{43}$/.test(data.key)) throw new Error('密钥不可复制。');
      try {
        if (!navigator.clipboard?.writeText) throw new Error('unavailable');
        await navigator.clipboard.writeText(data.key);
        $('apiKeyStatus').textContent = '密钥已复制，请妥善保管。';
      } catch (_) {
        $('apiKeyDialogHeading').textContent = '复制密钥'; $('apiKeyDialogHelp').textContent = '自动复制不可用，密钥已选中，请按 Ctrl+C。';
        $('apiKeySecret').value = data.key; $('apiKeyCopyStatus').textContent = '';
        $('apiKeyDialog').showModal(); $('apiKeySecret').focus(); $('apiKeySecret').select();
      }
      button.disabled = false;
      return;
    }
    if (!data.revoked) throw new Error('撤销结果未确认，请刷新页面核对。');
    await loadApiKeys();
  } catch (error) { $('apiKeyStatus').textContent = error.message || '操作未完成，请刷新页面核对。'; button.disabled = false; }
});
$('apiKeyCopy').addEventListener('click', async () => {
  const input = $('apiKeySecret');
  try {
    if (!navigator.clipboard?.writeText) throw new Error('unavailable');
    await navigator.clipboard.writeText(input.value);
    $('apiKeyCopyStatus').textContent = '密钥已复制，请妥善保存。';
  } catch (_) {
    input.focus(); input.select(); $('apiKeyCopyStatus').textContent = '密钥已选中，请按 Ctrl+C 复制。';
  }
});
$('apiKeyDone').addEventListener('click', () => $('apiKeyDialog').close());
$('apiKeyDialog').addEventListener('close', () => { $('apiKeySecret').value = ''; $('apiKeyCreate').focus(); });

function completeApiDocument() {
  const root = $('apiView');
  const clean = (node) => (node?.textContent || '').replace(/\s+/g, ' ').trim();
  const fence = String.fromCharCode(96).repeat(3);
  const out = ['# 创作工作台 API 调用说明', clean(root.querySelector('.api-intro p')),
    '工作台地址：' + clean(root.querySelector('.api-base code'))];
  for (const panel of root.querySelectorAll('.api-main > section.api-panel')) {
    for (const child of panel.children) {
      if (child.classList.contains('api-heading')) {
        out.push('## ' + clean(child.querySelector('h2')));
        const note = clean(child.querySelector('p')); if (note) out.push(note);
      } else if (child.classList.contains('api-callout')) {
        out.push('**' + clean(child.querySelector('strong')) + '**');
        child.querySelectorAll('p').forEach(p => out.push(clean(p)));
      } else if (child.classList.contains('api-endpoint')) {
        out.push('**' + clean(child.querySelector('b')) + ' ' + clean(child.querySelector('code')) + '** · ' + clean(child.querySelector('span')));
      } else if (child.tagName === 'H3') {
        out.push('### ' + clean(child));
      } else if (child.tagName === 'P') {
        out.push(clean(child));
      } else if (child.classList.contains('api-code-head')) {
        out.push('**' + clean(child.querySelector('span')) + '**');
      } else if (child.tagName === 'PRE') {
        out.push(fence + 'python\n' + child.querySelector('code').textContent.trim() + '\n' + fence);
      } else if (child.classList.contains('api-params')) {
        out.push([...child.children].map(row => '- **' + clean(row.querySelector('strong')) + '**：' + clean(row.querySelector('span'))).join('\n'));
      } else if (child.classList.contains('api-table-wrap')) {
        const rows = [...child.querySelectorAll('tr')].map(row => [...row.querySelectorAll('th,td')].map(cell => clean(cell).replace(/\|/g, '\\|')));
        if (rows.length) out.push(['| ' + rows[0].join(' | ') + ' |', '| ' + rows[0].map(() => '---').join(' | ') + ' |',
          ...rows.slice(1).map(row => '| ' + row.join(' | ') + ' |')].join('\n'));
      } else if (child.classList.contains('api-links')) {
        out.push([...child.querySelectorAll('a')].map(a => '- [' + clean(a.querySelector('strong')) + '](' + a.getAttribute('href') + ')：' + clean(a.querySelector('small'))).join('\n'));
      } else if (child.classList.contains('api-legacy')) {
        const link = child.querySelector('a');
        out.push('### ' + clean(child.querySelector('summary')), clean(child.querySelector('p')),
          '- [' + clean(link) + '](' + link.getAttribute('href') + ')');
      }
    }
  }
  return out.filter(Boolean).join('\n\n') + '\n';
}

let apiCopyInvoker = $('apiCopyAll');
document.body.appendChild($('apiCopyDialog'));
async function copyCompleteApiDocument(status = $('apiCopyAllStatus'), invoker = $('apiCopyAll')) {
  const content = completeApiDocument();
  apiCopyInvoker = invoker;
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(content); copied = true; }
  } catch (_) { /* Try the local fallback below. */ }
  if (!copied) {
    const scratch = document.createElement('textarea');
    scratch.className = 'api-copy-scratch'; scratch.value = content;
    document.body.appendChild(scratch); scratch.focus(); scratch.select();
    try { copied = document.execCommand('copy'); } catch (_) { copied = false; }
    scratch.remove();
  }
  if (copied) {
    status.textContent = '完整文档已复制，可以直接粘贴。';
    invoker.focus({preventScroll: true});
  } else {
    const dialog = $('apiCopyDialog'); const fallback = $('apiCopyFallback');
    fallback.value = content; dialog.showModal(); fallback.focus(); fallback.select();
    status.textContent = '请在弹窗中复制完整文档。';
  }
}
$('apiCopyAll').addEventListener('click', () => copyCompleteApiDocument());
$('overviewApiCopy').addEventListener('click', () => copyCompleteApiDocument($('overviewCopyStatus'), $('overviewApiCopy')));
let dispatchConcurrencySaving = false;
let dispatchConcurrencySequence = 0;
let dispatchConcurrencyMaximum = null;
function dispatchStateText(paused) { return state.channelStatus?.production_send_available ? '官方调用已接入 · 新对话轮询，多轮固定账号，异常不重试或换号。' : '官方调用暂不可用；消息不会自动改走旧通道。'; }
function updateDispatchState(paused) {
  state.dispatchPaused = typeof paused === 'boolean' ? paused : null;
  const detail = document.querySelector('[data-gemini-dispatch-state]');
  if (detail) detail.textContent = dispatchStateText(state.dispatchPaused);
}
function showDispatchConcurrency(data) {
  if (!Number.isInteger(data.maximum) || data.maximum < 1 || data.maximum > 5 || !Number.isInteger(data.active)) throw new Error('并发设置暂不可读取。');
  updateDispatchState(data.available === true ? data.paused : null);
  dispatchConcurrencyMaximum = data.maximum;
  $('dispatchConcurrency').value = String(data.maximum);
  $('dispatchConcurrency').disabled = false;
  $('dispatchConcurrencyStatus').textContent = data.paused ? '维护中' : `执行中 ${data.active} / ${data.maximum}`;
  $('dispatchConcurrencyStatus').title = data.active > data.maximum ? '已有任务继续完成，新任务按新上限派发。' : '共用并发上限，超过时等待；设置自动保存，立即影响新任务。';
}
async function refreshDispatchConcurrency() {
  if (dispatchConcurrencySaving) return;
  const sequence = ++dispatchConcurrencySequence;
  try {
    const data = await getJson('/api/dispatch/concurrency');
    if (sequence !== dispatchConcurrencySequence || dispatchConcurrencySaving) return;
    showDispatchConcurrency(data);
  } catch (error) {
    if (sequence !== dispatchConcurrencySequence || dispatchConcurrencySaving) return;
    updateDispatchState(null);
    $('dispatchConcurrency').disabled = true;
    $('dispatchConcurrencyStatus').textContent = '并发设置暂不可读取';
  }
}
$('dispatchConcurrency').addEventListener('change', async () => {
  if (dispatchConcurrencySaving) return;
  dispatchConcurrencySaving = true;
  ++dispatchConcurrencySequence;
  const control = $('dispatchConcurrency');
  control.disabled = true;
  $('dispatchConcurrencyStatus').textContent = '正在保存…';
  try {
    showDispatchConcurrency(await postJson('/api/dispatch/concurrency', {maximum:Number(control.value)}));
    $('dispatchConcurrencyStatus').textContent += ' · 已保存';
  } catch (error) {
    if (dispatchConcurrencyMaximum !== null) control.value = String(dispatchConcurrencyMaximum);
    control.disabled = false;
    $('dispatchConcurrencyStatus').textContent = error.message || '保存状态未确认，请刷新核对。';
  } finally {
    dispatchConcurrencySaving = false;
  }
});
setInterval(() => { if (state.view === 'overview' && !document.hidden) refreshDispatchConcurrency(); }, 12000);
$('overviewSchedule').addEventListener('click', () => {
  setView('accounts');
  if (activationSchedulePanel.hidden) $('activationScheduleToggle').click();
  $('activationScheduleToggle').focus({preventScroll:true});
  $('activationSchedulePanel').scrollIntoView({block:'nearest'});
});
$('apiCopyClose').addEventListener('click', () => $('apiCopyDialog').close());
$('apiCopyDialog').addEventListener('close', () => apiCopyInvoker.focus({preventScroll: true}));

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
document.querySelectorAll('[data-api-target]').forEach((button) => button.addEventListener('click', () => {
  const target = $(button.dataset.apiTarget);
  if (!target) return;
  target.scrollIntoView({block: 'start'});
  target.focus({preventScroll: true});
}));
document.querySelectorAll('[data-copy-code]').forEach((button) => button.addEventListener('click', async () => {
  const source = $(button.dataset.copyCode);
  if (!source) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText(source.textContent);
    button.textContent = '已复制';
    $('apiCopyStatus').textContent = '示例已复制，不会自动提交请求。';
  } catch (_) {
    button.textContent = '请手动选择复制';
    $('apiCopyStatus').textContent = '自动复制不可用，请选择下方示例后复制。';
  }
  setTimeout(() => { button.textContent = '复制'; }, 3500);
}));
$("refresh").addEventListener("click", () => { refresh(); if (state.view === 'conversations') loadConversations(); });
$("projectSearch").addEventListener("input", renderProjects);
$('projectTable').addEventListener('click', event => {
  const button = event.target.closest('button[data-project-action]');
  if (button && !button.disabled) startProjectAction(button.dataset.projectKey,button.dataset.projectAction);
});
$("taskSearch").addEventListener("input", renderTasks);
$("taskStatusFilter").addEventListener("change", renderTasks);
$("taskKindFilter").addEventListener("change", renderTasks);
setView(location.hash.slice(1) || "overview");
refresh();
const previousGeminiRequest = sessionStorage.getItem("geminiRequest");
if (previousGeminiRequest && /^[0-9a-f-]{36}$/.test(previousGeminiRequest)) {
  state.geminiBusy = true;
  $("geminiSend").disabled = true; // Legacy Gemini sender removed.
  observeGeminiRequest(previousGeminiRequest);
}
const previousFlowRequest = sessionStorage.getItem("flowRequest");
if (previousFlowRequest && /^[0-9a-f-]{36}$/.test(previousFlowRequest)) {
  state.flowBusy = true;
  $("flowAutoRetry").disabled = true;
  $("flowSend").disabled = true;
  observeFlowRequest(previousFlowRequest);
}
try {
  const previous = JSON.parse(sessionStorage.getItem('conversationRequest') || 'null');
  if (previous && /^[0-9a-f-]{36}$/.test(previous.id) && /^[0-9a-f-]{36}$/.test(previous.conversation)) {
    activeConversation = previous.conversation; chatSending = true;
    observeConversationRequest(previous.id, previous.conversation);
  }
} catch (_) { /* Ignore malformed local state. */ }



// Contextual explanations stay separate from changing status and action warnings.

(() => {

  let serial = 0;

  const tips = new Set();

  function positionTip(tip) {

    const button = tip.querySelector('.ui-help-button');

    const popup = tip.querySelector('.ui-help-popup');

    if (!button || !popup || !button.isConnected) return;

    const anchor = button.getBoundingClientRect();

    const box = popup.getBoundingClientRect();

    const left = Math.max(8, Math.min(anchor.left, window.innerWidth - box.width - 8));

    const below = anchor.bottom;

    const top = below + box.height <= window.innerHeight - 8 ? below : Math.max(8, anchor.top - box.height);

    popup.style.left = `${left}px`;

    popup.style.top = `${top}px`;

  }

  function closeTip(tip) {

    tip.dataset.open = 'false';

    const button = tip.querySelector('.ui-help-button');

    if (button) button.setAttribute('aria-expanded', 'false');

  }

  function attach(anchor, sources, label) {

    tips.forEach(tip => { if (!tip.isConnected) tips.delete(tip); });

    const eligible = sources.filter(source => source && !source.classList.contains('ui-help-source-hidden') && source.textContent.trim());

    if (!anchor || !eligible.length) return;

    const tip = document.createElement('span');

    tip.className = 'ui-help-tip';

    const button = document.createElement('button');

    const popup = document.createElement('span');

    const id = 'ui-help-' + (++serial);

    button.type = 'button';

    button.className = 'ui-help-button';

    button.setAttribute('aria-label', `查看${label}说明`);

    button.setAttribute('aria-expanded', 'false');

    button.setAttribute('aria-describedby', id);

    button.textContent = 'i';

    popup.id = id;

    popup.className = 'ui-help-popup';

    popup.setAttribute('role', 'tooltip');

    popup.textContent = eligible.map(source => {

      const content = source.classList.contains('notice') ? source.querySelector('span:last-child') : null;

      return (content || source).textContent.trim();

    }).join('\n');

    eligible.forEach(source => source.classList.add('ui-help-source-hidden'));

    tip.append(button, popup);

    anchor.append(tip);

    tips.add(tip);

    tip.addEventListener('mouseenter', () => positionTip(tip));

    button.addEventListener('focus', () => positionTip(tip));

    button.addEventListener('click', () => {

      const shouldOpen = tip.dataset.open !== 'true';

      tips.forEach(other => { if (other !== tip) closeTip(other); });

      tip.dataset.open = String(shouldOpen);

      button.setAttribute('aria-expanded', String(shouldOpen));

      positionTip(tip);

    });

  }

  const one = selector => document.querySelector(selector);

  function page(id, extra) {

    const heading = one('#' + id);

    const header = heading?.closest('.page-heading');

    const intro = header?.querySelector(':scope > div > p:not([id])');

    attach(heading, [intro, extra ? one(extra) : null], heading?.textContent.trim() || '页面');

  }

  page('overviewHeading');

  page('accountsHeading', '#accountsView > .notice');

  page('projectsHeading', '#projectsView > .notice');

  page('tasksHeading', '#tasksView > .notice');

  page('conversationsHeading');

  page('libraryHeading');

  page('apiHeading');

  attach(one('#statusHeading'), [one('#overviewView .status-section .section-title > .subtle')], '服务连接');

  document.querySelectorAll('#overviewView .summary-card').forEach(card => {

    const caption = card.querySelector(':scope > p:not([id])');

    const label = card.querySelector('.metric-label');

    attach(label, [caption], label?.textContent.trim() || '统计');

  });

  attach(one('#realAccountsHeading'), [one('#accountsView section[aria-labelledby="realAccountsHeading"] > p.panel-caption:not([id])')], '通道状态');

  attach(one('#activationScheduleForm .activation-schedule-fields'), [one('#activationScheduleForm p.panel-caption:not([id])')], '定时检查');

  attach(one('#activationPollForm .activation-schedule-fields'), [one('#activationPollForm p.panel-caption:not([id])')], '轮询检查');

  attach(one('#dispatchPolicyHeading'), [one('#dispatchPolicyHeading + p.panel-caption')], '自动选号规则');

  attach(one('#geminiActivityHeading'), [one('#geminiActivityHeading + p.panel-caption')], '请求记录');

  attach(one('#conversationsView .conversation-filter-footer'), [one('#chatFilterHelp')], '对话类型筛选');

  attach(one('#libraryView .library-intro h2'), [one('#libraryView .library-intro p')], '素材库');

  document.addEventListener('pointerdown', event => {

    tips.forEach(tip => { if (!tip.contains(event.target)) closeTip(tip); });

  });

  document.addEventListener('keydown', event => {

    if (event.key === 'Escape' && document.activeElement?.classList.contains('ui-help-button')) {

      const button = document.activeElement;

      closeTip(button.parentElement);

      button.blur();

    }

  });

  window.addEventListener('resize', () => tips.forEach(tip => { if (tip.isConnected) positionTip(tip); }));

  window.addEventListener('scroll', () => tips.forEach(tip => { if (tip.isConnected && (tip.dataset.open === 'true' || tip.contains(document.activeElement))) positionTip(tip); }), true);

})();


// Keep the account summary and completed activation details in the existing help tip.
(() => {
  const summary = document.querySelector('#realAccountsSummary');
  const progress = document.querySelector('#activationProgress');
  const popup = document.querySelector('#realAccountsHeading .ui-help-popup');
  if (!summary || !progress || !popup) return;
  const explanation = popup.textContent.trim();
  function sync() {
    const current = progress.textContent.trim();
    summary.classList.toggle('ui-account-summary-hidden', !/无法读取|不可用|失败|异常/.test(summary.textContent));
    // Keep an active operation or an issue that may need attention directly visible.
    const needsAttention = /不可用|未确认|失败|待人工|正在创建|处理中\s*[1-9]/.test(current);
    progress.classList.toggle('ui-account-summary-hidden', !needsAttention);
    popup.textContent = [explanation, summary.textContent.trim(), current].filter(Boolean).join('\n');
  }
  const observer = new MutationObserver(sync);
  for (const node of [summary, progress]) observer.observe(node, {childList:true, characterData:true, subtree:true});
  sync();
})();

// Collect automatic-analysis explanations and routine counts in one help tip.
(() => {
  const section = document.querySelector('#libraryView .auto-analysis');
  const heading = section?.querySelector('strong');
  const explanation = section?.querySelector('small');
  const status = document.querySelector('#autoAnalysisStatus');
  const backfill = document.querySelector('#backfillStatus');
  const actions = section?.querySelector('.auto-analysis-actions');
  if (!heading || !explanation || !status || !backfill || !actions || section.querySelector('.library-analysis-help')) return;
  const tip = document.createElement('span');
  tip.className = 'ui-help-tip library-analysis-help';
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'ui-help-button'; button.textContent = 'i';
  button.setAttribute('aria-label', '查看素材自动分析说明');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-describedby', 'library-analysis-help');
  const popup = document.createElement('span');
  popup.id = 'library-analysis-help'; popup.className = 'ui-help-popup'; popup.setAttribute('role', 'tooltip');
  tip.append(button, popup); heading.append(tip);
  const cost = document.createElement('span');
  cost.className = 'library-analysis-cost'; cost.textContent = '可能产生费用';
  actions.prepend(cost);
  explanation.classList.add('ui-account-summary-hidden');
  function position() {
    const anchor = button.getBoundingClientRect(), box = popup.getBoundingClientRect();
    popup.style.left = Math.max(8, Math.min(anchor.left, innerWidth - box.width - 8)) + 'px';
    popup.style.top = (anchor.bottom + box.height <= innerHeight - 8 ? anchor.bottom : Math.max(8, anchor.top - box.height)) + 'px';
  }
  function sync() {
    const auto = status.textContent.trim(), fill = backfill.textContent.trim();
    const autoWarning = !/^(已开启|已关闭|正在读取设置)/.test(auto) || /需核对\s*[1-9]/.test(auto);
    const fillWarning = !!fill && (!/^补打：/.test(fill) || /已暂停|需核对\s*[1-9]/.test(fill));
    status.classList.toggle('ui-account-summary-hidden', !autoWarning);
    backfill.classList.toggle('ui-account-summary-hidden', !fillWarning);
    popup.textContent = [explanation.textContent.trim(), auto, fill].filter(Boolean).join('\n');
    if (tip.dataset.open === 'true' || tip.contains(document.activeElement)) position();
  }
  new MutationObserver(sync).observe(status, {childList:true, characterData:true, subtree:true});
  new MutationObserver(sync).observe(backfill, {childList:true, characterData:true, subtree:true});
  tip.addEventListener('mouseenter', position);
  button.addEventListener('focus', position);
  button.addEventListener('click', () => {
    const open = tip.dataset.open !== 'true'; tip.dataset.open = String(open);
    button.setAttribute('aria-expanded', String(open)); position();
  });
  document.addEventListener('pointerdown', event => {
    if (!tip.contains(event.target)) { tip.dataset.open = 'false'; button.setAttribute('aria-expanded', 'false'); }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.activeElement === button) {
      tip.dataset.open = 'false'; button.setAttribute('aria-expanded', 'false'); button.blur();
    }
  });
  window.addEventListener('resize', position);
  window.addEventListener('scroll', () => { if (tip.dataset.open === 'true' || tip.contains(document.activeElement)) position(); }, true);
  sync();
})();

// Keep unsupported legacy quick-send and attachment paths disabled.
for (const id of ['geminiSend','chatFiles','geminiFiles']) {
  const button=document.getElementById(id);
  if(button){button.disabled=true;button.title='请在对话管理使用官方文字调用；附件暂未启用';}
}

setInterval(() => { if (!document.hidden && ['accounts','conversations'].includes(state.view)) refreshChannelStatus(); }, 10000);
async function readRetainedSettings() {
  const target = $('retainedSettings');
  try {
    const [daily, poll] = await Promise.all([getJson('/api/account-activation/schedule'),getJson('/api/account-activation/poll')]);
    target.textContent = `旧设置已保留：每天定时 ${daily.enabled ? '开启（'+daily.time+'）' : '关闭'} · 轮询 ${poll.enabled ? '开启（'+poll.interval_minutes+' 分钟）' : '关闭'}。这些设置不驱动官方 CLI 调用。`;
  } catch (_) { target.textContent = '旧定时与轮询设置暂不可读取；官方通道未接入这些设置。'; }
}
readRetainedSettings();


// Official quota is a separate read-only source; request counts are never quotas.
let officialQuotaData = null;
let officialQuotaBusy = false;
let officialQuotaReadFailed = false;
function renderOfficialQuota() {
  const target = $('geminiQuotaOverview');
  if (!target) return;
  const opened = !!target.querySelector('details[open]');
  const focused = target.contains(document.activeElement) ? document.activeElement?.dataset.quotaAction : null;
  const data = officialQuotaReadFailed ? null : officialQuotaData;
  const known = data?.known_accounts || 0;
  const total = data?.total_accounts || state.snapshot?.real_accounts?.items?.length || 0;
  const refreshing = data?.refreshing === true;
  const percent = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
  const number = value => value == null ? '—' : value.toFixed(2).replace(/\.00$/, '') + '%';
  const resetText = seconds => typeof seconds === 'number' ? `最近恢复 ${dateTime(seconds)}` : '恢复时间以官方状态为准';
  const windowMarkup = (key, title) => {
    const window = data?.windows?.[key];
    const value = percent(window?.average_remaining_percent);
    return `<div class="quota-window"><div class="quota-window-label"><span>${title}</span><strong>${number(value)}<small> 平均剩余</small></strong></div><div class="quota-track ${value == null ? 'is-unknown' : ''}" ${value == null ? 'role="img" aria-label="额度尚未读取"' : `role="progressbar" aria-label="${title}账号平均剩余" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${value}"`}><i data-quota-fill="${value ?? 0}"></i></div><span class="quota-reset">${value == null ? '等待官方额度数据' : esc(resetText(window?.next_reset_at))}</span></div>`;
  };
  const taskKnown = state.snapshot?.flow_state?.available === true && Array.isArray(state.snapshot?.tasks);
  const tasks = taskKnown ? state.snapshot.tasks.filter(t=>['image','video'].includes(t.kind)) : [];
  const completed = tasks.filter(t=>t.status==='succeeded').length;
  const failed = tasks.filter(t=>t.status==='failed').length;
  const success = completed+failed ? Math.round(completed/(completed+failed)*100) : null;
  const flowMarkup = `<div class="quota-window quota-window-flow"><div class="quota-window-label"><span>FLOW · 图片 / 视频成功率</span><strong>${success == null ? '—' : success+'%'}<small> 任务成功率</small></strong></div><div class="quota-track ${success == null ? 'is-unknown' : ''}" ${success == null ? 'role="img" aria-label="Flow 任务成功率未知"' : `role="progressbar" aria-label="Flow 图片与视频任务成功率" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${success}"`}><i data-quota-fill="${success ?? 0}"></i></div><span class="quota-reset">${taskKnown ? `${completed} 成功 · ${failed} 失败 · 含任务下载阶段` : '任务记录暂未读取'}</span></div>`;
  const accounts = state.snapshot?.real_accounts?.items || [];
  const names = new Map(accounts.map(a=>[a.key.replace(/^flow-account-/,''),a.email || a.label || a.profile]));
  const rows = Object.entries(data?.accounts || {});
  const rowMarkup = rows.map(([key,row],index)=>{
    const fresh = row.status === 'ok' && Date.now()/1000-row.checked_at <= 600;
    const values = ['five_hour','weekly'].map(w=>fresh ? percent(row[w]?.remaining_percent) : null);
    const resets = ['five_hour','weekly'].map(w=>row[w]?.reset_at).filter(t=>fresh && typeof t==='number' && t>Date.now()/1000);
    const status = fresh ? (resets.length ? resetText(Math.min(...resets)) : '额度可用') : row.status==='verification_required' ? '额度查询提示核验（不限制对话）' : row.status==='eligibility_failed' ? '官方资格检查未通过（未确认需核验）' : row.status==='network_error' ? '连接官方服务失败' : row.status==='authorization_required' ? '官方要求重新授权' : row.status==='authorization_unavailable' ? '本地授权资料未就绪' : row.status==='not_read' ? (refreshing ? '等待本轮读取' : '尚未读取') : row.status==='ok' ? '记录已过期' : '本轮未读到额度';
    return `<tr><th scope="row">${esc(names.get(key) || `账号 ${index+1}`)}</th>${values.map((v,i)=>`<td><div class="quota-account-value">${number(v)}</div><div class="quota-account-track" role="img" aria-label="${i ? '每周' : '5小时'}剩余 ${v == null ? '未知' : v+'%'}"><i data-quota-fill="${v ?? 0}"></i></div></td>`).join('')}<td>${esc(status)}</td></tr>`;
  }).join('');
  const checked = rows.map(([,r])=>r.checked_at).filter(t=>typeof t==='number');
  const coverage = refreshing ? `正在读取 ${known} / ${total} 个账号` : known ? `已读取 ${known} / ${total} 个账号${known<total ? ' · 汇总仅含已读账号' : ''}` : officialQuotaReadFailed ? '额度服务暂不可读取' : '尚未读到官方额度';
  target.setAttribute('aria-busy',String(refreshing));
  target.innerHTML = `<div class="quota-main">${flowMarkup}${windowMarkup('five_hour','GEMINI · 5 小时额度')}${windowMarkup('weekly','GEMINI · 每周额度')}</div><div class="quota-footer"><div class="quota-source"><span>Gemini：${esc(coverage)}${checked.length ? ` · 更新于 ${esc(dateTime(Math.max(...checked)))}` : ''}</span><span>Flow 进行中和未确认任务不计入；Gemini 为账号平均剩余，不同账号分别恢复。</span></div><button type="button" class="quota-refresh" data-quota-action="refresh" ${refreshing || officialQuotaBusy ? 'disabled' : ''}>${refreshing ? '额度读取中…' : '刷新 Gemini 额度'}</button></div><details class="quota-details" ${opened ? 'open' : ''}><summary data-quota-action="details">Gemini 各账号额度<span>查看剩余与恢复时间</span></summary><div class="quota-table-wrap"><table><thead><tr><th>账号</th><th>5 小时剩余</th><th>每周剩余</th><th>状态 / 最近恢复</th></tr></thead><tbody>${rowMarkup || '<tr><td colspan="4">正在读取账号列表…</td></tr>'}</tbody></table></div></details>`;
  target.querySelectorAll('[data-quota-fill]').forEach(meter=>{meter.style.width=Number(meter.dataset.quotaFill)+'%';});
  target.querySelector('[data-quota-action="refresh"]').addEventListener('click',()=>refreshOfficialQuota(true));
  if (focused) target.querySelector(`[data-quota-action="${focused}"]`)?.focus({preventScroll:true});
}
async function refreshOfficialQuota(force=false) {
  if (officialQuotaBusy) return;
  officialQuotaBusy = true;
  try {
    let data;
    if (force) {
      const response = await fetch('/api/official-quota/refresh',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},body:'{}',signal:AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('unavailable');
      data = await response.json();
    } else data = await getJson('/api/official-quota',{timeoutMs:8000});
    if (data.schema_version !== 1 || data.source !== 'official_cli_usage' || !data.accounts || !data.windows) throw new Error('unavailable');
    officialQuotaData = data; officialQuotaReadFailed = false;
  } catch (_) { officialQuotaReadFailed = true; }
  finally { officialQuotaBusy = false; renderOfficialQuota(); renderAccountQuotas(); }
}
refreshOfficialQuota();
setInterval(()=>{if(!document.hidden && ['overview','accounts'].includes(state.view)) refreshOfficialQuota();},4000);
