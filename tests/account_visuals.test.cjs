const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'packaging', 'ubuntu', 'snapshot', 'workbench', 'app.js'), 'utf8');
function load(name, context) {
  const match = source.match(new RegExp('^(?:async )?function ' + name + '\\([^]*?^}', 'm'));
  assert.ok(match, name + ' should exist');
  vm.runInContext(match[0], context);
}

test('overview renders one shared quota card with both time windows', () => {
  const target = {
    querySelector: selector => selector === 'details[open]' ? null : {addEventListener() {}},
    querySelectorAll: () => [],
    contains: () => false,
    setAttribute() {}
  };
  const context = vm.createContext({
    $: () => target,
    document: {},
    state: {},
    officialQuotaReadFailed: false,
    officialQuotaRequest: null,
    officialQuotaData: {
      advanced_pool: {
        known_accounts: 2,
        total_accounts: 3,
        windows: {
          five_hour: {total_remaining_percent: 175, average_remaining_percent: 87.5},
          weekly: {total_remaining_percent: 120, average_remaining_percent: 60}
        }
      }
    }
  });
  for (const name of ['esc', 'savedQuotaSummary', 'renderOfficialQuota']) load(name, context);
  vm.runInContext('renderOfficialQuota()', context);
  assert.equal((target.innerHTML.match(/class="quota-shared-card"/g) || []).length, 1);
  assert.match(target.innerHTML, /Hydrogen \/ Neon 共用 · 2 \/ 3 个账号已读/);
  assert.match(target.innerHTML, /5 小时总账池额度<\/span><strong>175%/);
  assert.match(target.innerHTML, /每周总账池额度<\/span><strong>120%/);
});

test('account avatars mark only enabled advanced Gemini model choices', () => {
  const context = vm.createContext({});
  load('esc', context);
  load('accountAvatarMarkup', context);
  const render = families => vm.runInContext(`accountAvatarMarkup('account', ${JSON.stringify(families.map(family => ({family})))})`, context);
  for (const family of ['gemini-4-neon', 'gemini-4-hydrogen']) {
    const markup = render(['gemini-3.8-flash', family]);
    assert.match(markup, /account-avatar is-gemini-advanced/);
    assert.match(markup, /aria-label="支持 Gemini 高级模型"/);
  }
  for (const families of [[], ['gemini-3.8-flash']]) {
    assert.match(render(families), /class="account-avatar" aria-hidden="true"/);
  }
});

test('usage bars preserve zero, exact usage, and unknown values', () => {
  const context = vm.createContext({Intl});
  load('esc', context);
  load('accountUsageBar', context);
  const render = (value) => vm.runInContext(`accountUsageBar('5 小时', ${value}, false)`, context);
  assert.match(render('0'), /aria-valuenow="0"/);
  assert.match(render('47.5'), /data-usage-percent="47.5"/);
  assert.match(render('100'), /aria-valuenow="100"/);
  for (const bad of ['null', 'undefined', '-1', '101', 'NaN']) {
    const result = render(bad);
    assert.match(result, /用量未知/);
    assert.doesNotMatch(result, /data-usage-percent|aria-valuenow/);
  }
});

test('Flow refresh cooldown belongs to the account that was already rechecked', async () => {
  const first = {profile:'first', flow_login:'verified'};
  const second = {profile:'second', flow_login:'verified'};
  const context = vm.createContext({
    state:{snapshotFresh:true,snapshot:{real_accounts:{items:[first,second]}}},
    flowReadinessObservation:{}, rememberFlowReadiness:()=>{},
    autoFlowRecovery:null, autoFlowRecoveryProfiles:new Set(),
    autoFlowRecoveryAfter:new Map([['first',Date.now()+300000]]),
    postJson:async () => ({accounts:{first:'overlay',second:'ok'}}),
  });
  load('autoRecoverFlowPages',context);
  vm.runInContext("autoRecoverFlowPages({accounts:{first:'overlay',second:'overlay'}})",context);
  assert.deepEqual(Array.from(context.autoFlowRecoveryProfiles), ['second']);
  await context.autoFlowRecovery;
});

test('compact Gemini quota keeps recovery dates in hover details', () => {
  const now = 1800000000;
  const context = vm.createContext({
    Date: {now: () => now * 1000}, dateTime: value => '日期' + value,
    officialQuotaReadFailed: false, governanceAudits: {},
    officialQuotaData: {accounts: {sample: {status: 'ok', checked_at: now - 60,
      five_hour: {remaining_percent: 72, reset_at: now + 300},
      weekly: {remaining_percent: 60, reset_at: now - 60}}}},
  });
  for (const name of ['esc', 'savedQuotaValue', 'accountOfficialQuotaMarkup']) load(name, context);
  const markup = () => vm.runInContext("accountOfficialQuotaMarkup('sample').html", context);
  assert.match(markup(), /title="5 小时额度 · 恢复 日期1800000300"/);
  assert.match(markup(), /title="每周额度 · 恢复时间待官方更新"/);
  assert.doesNotMatch(markup(), /account-quota-reset/);
  assert.doesNotMatch(markup(), /日期1799999940/);
  context.officialQuotaData.accounts.sample.checked_at = now - 601;
  assert.match(markup(), /title="5 小时额度 · 恢复 日期1800000300（上次读取）"/);
});

test('a missing Gemini quota is neutral, and a confirmed zero stays visible', () => {
  const now = 1800000000;
  const context = vm.createContext({
    Date: {now: () => now * 1000}, officialQuotaReadFailed: false,
    officialQuotaData: {accounts: {}},
  });
  for (const name of ['esc', 'savedQuotaValue', 'accountOfficialQuotaMarkup']) load(name, context);
  const show = row => {
    context.officialQuotaData.accounts.account = row;
    return vm.runInContext("accountOfficialQuotaMarkup('account')", context);
  };
  for (const status of ['not_read', 'unavailable', 'network_error', 'eligibility_failed']) {
    const {message, html} = show({status});
    assert.equal(message, '额度暂未获取');
    assert.doesNotMatch(html, /aria-valuenow=/);
  }
  const partial = show({status: 'ok', checked_at: now, five_hour: {remaining_percent: 0}});
  assert.equal(partial.message, '额度暂未获取');
  assert.match(partial.html, /aria-valuenow="0"/);
  assert.match(show({status: 'authorization_required'}).message, /重新授权/);
});

test('an open account page re-reads saved Gemini quota after its initial load', async () => {
  let now = 1800000000000;
  let quotaReads = 0;
  const noop = () => {};
  const context = vm.createContext({
    Date: {now: () => now}, viewReads: new Map(),
    officialQuotaData: {refreshing: false}, officialQuotaReadAt: now, OFFICIAL_QUOTA_SYNC_MS: 15000,
    refreshOfficialQuota: () => { quotaReads++; },
    refreshChannelStatus: noop, refreshFlowReadinessStatus: noop, refreshGovernanceCases: noop,
    refreshGeminiActivity: noop, refreshActivation: noop,
    activationScheduleSaving: true, activationPollSaving: true,
  });
  load('syncVisiblePage', context);
  const sync = async () => {
    vm.runInContext("syncVisiblePage('accounts', true)", context);
    await Promise.all(context.viewReads.values());
  };
  await sync();
  assert.equal(quotaReads, 0);
  now += context.OFFICIAL_QUOTA_SYNC_MS;
  await sync();
  assert.equal(quotaReads, 1);
});

test('Gemini readiness requests quota recovery and still finishes when quota is unavailable', async () => {
  const snapshot = {real_accounts: {available: true, items: []}};
  const channel = {available: true};
  const requests = [];
  const quotaReads = [];
  const context = vm.createContext({
    governanceAudits: {}, state: {view: 'accounts'}, officialQuotaData: null, channelReadFailed: false,
    async getJson(path) { return path === '/api/snapshot' ? snapshot : channel; },
    async postJson(path) {
      requests.push(path);
      return path === '/api/gemini-readiness' ? {readiness_probes: []} : {state: 'completed', completed: 0};
    },
    async refreshOfficialQuota(force) { quotaReads.push(force); return null; },
    auditGeminiAccounts: () => [{status: 'ready'}],
    renderGovernance() {}, renderAccountQuotas() {}, refreshGovernanceCases: async () => {},
  });
  load('startGovernanceAudit', context);
  await vm.runInContext("startGovernanceAudit('gemini')", context);
  assert.deepEqual(quotaReads, [true]);
  assert.deepEqual(requests, ['/api/gemini-readiness', '/api/official-reauth/activate']);
  const audit = context.governanceAudits.gemini;
  assert.equal(audit.rows[0].status, 'ready');
  assert.equal(audit.error, '');
  assert.equal(audit.running, false);
  assert.ok(audit.checkedAt);
});

test('cooldown counts down and becomes awaiting confirmation without changing eligibility', () => {
  const time = {textContent: ''};
  const label = {textContent: ''};
  const status = {textContent: '冷却中', classList: {add(value) { this.added = value; }}};
  const clock = {
    dataset: {cooldownEnd: '20000'},
    querySelector(selector) { return selector === '.account-cooldown-time' ? time : label; },
    closest() { return {querySelector() { return status; }}; },
    setAttribute(_name, value) { this.accessibleLabel = value; },
  };
  let now = 14000;
  const context = vm.createContext({document: {querySelectorAll: () => [clock]}, Date: {now: () => now}});
  load('updateAccountCooldowns', context);
  vm.runInContext('updateAccountCooldowns()', context);
  assert.equal(time.textContent, '0:06');
  assert.equal(status.textContent, '冷却中');
  now = 20000;
  vm.runInContext('updateAccountCooldowns()', context);
  assert.equal(time.textContent, '待核对');
  assert.equal(label.textContent, '已到时间');
  assert.equal(status.textContent, '待核对');
  assert.match(clock.accessibleLabel, /刷新核对/);
});

test('account cards render fallback states without aborting the account list', () => {
  const render = source.slice(source.indexOf('function renderAccounts()'));
  const presentation = render.slice(render.indexOf('    const cooling ='), render.indexOf('    const cooldownSeconds ='));
  for (const status of ['ready', 'blocked', 'unknown', 'checking']) {
    const context = vm.createContext({
      item: {profile:'test', key:'flow-account-test', gemini:'verified'}, state:{snapshot:{}},
      channelReadFailed:false, officialQuotaData:null, officialQuotaReadFailed:false,
      flowRow:{status,issues:[]}, geminiRow:{status}, issues:[],
      flowWindowIssue: () => null, readinessBadge: row => row.status,
      officialAccount: () => ({paused:false, data:null}),
      geminiAccountAvailability: () => ({manual: status === 'ready', auto: status === 'ready'}),
      geminiCardStatus: () => ({label: status === 'ready' ? '可调用' : '待确认', attention: status !== 'ready'}),
      Date,
    });
    const result = vm.runInContext(presentation + '\n({flowFallback,geminiFallback,flowStatusText,geminiStatusText});', context);
    assert.equal(result.flowStatusText, status);
    assert.equal(Boolean(result.flowFallback), status !== 'ready');
    assert.equal(Boolean(result.geminiFallback), status !== 'ready');
    assert.equal(result.geminiStatusText, status === 'ready' ? '可调用' : '待确认');
  }
});

test('account reason identifies running work without inventing a failure', () => {
  const context = vm.createContext({});
  load('flowAccountReason', context);
  context.account = {profile:'a'};
  context.snapshot = {flow_coordination:{windows:[{profile:'a',task_id:'current',stage:'preparing'}]},
    tasks:[{source_id:'current',source_profile:'a',kind:'image',status:'running',retry:{state:'active',attempts:[]}}]};
  const reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'busy')", context);
  assert.equal(reason.label, '当前占用原因');
  assert.match(reason.text, /current.*准备输入/);
  assert.match(reason.text, /尚未收到失败回执/);
});

test('the account card shows only the matching failure screenshot', () => {
  const context = vm.createContext({});
  load('flowAccountReason', context);
  load('flowAccountNoticeMarkup', context);
  context.account = {profile:'a'};
  context.snapshot = {flow_coordination:{windows:[{profile:'a',task_id:'stopped',stage:'submitting'}]},
    tasks:[{source_id:'stopped',source_profile:'a',kind:'image',status:'interrupted',
      retry:{state:'needs_review',attempts:[{profile:'a',status:'failed',failure_reason:'等待图片回执超时，结果未确认'}]}}]};
  context.reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'busy')", context);
  assert.equal(context.reason.taskId, 'stopped');
  const markup = vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'stopped'})", context);
  assert.match(markup, /href="\/api\/flow-warning-screenshots\/123456789abc\.png"/);
  assert.match(markup, /<img .*src="\/api\/flow-warning-screenshots\/123456789abc\.png"/);
  assert.doesNotMatch(markup, /失败原因|等待图片回执超时/);
  assert.equal(vm.runInContext("flowAccountNoticeMarkup(reason,null)", context), '');
  assert.equal(vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'other'})", context), '');
});

test('Flow cooldown shows the failed attempt even after the task succeeds on another account', () => {
  const context = vm.createContext({});
  load('esc', context);
  load('flowAccountReason', context);
  load('flowAccountNoticeMarkup', context);
  context.account = {profile:'a', flow_cooling:true};
  context.snapshot = {
    flow_coordination:{windows:[{profile:'a', state:'idle', task_id:'original'}]},
    tasks:[{source_id:'original', source_profile:'b', kind:'video', status:'succeeded', retry:{
      state:'succeeded', attempts:[
        {id:'original', profile:'a', status:'failed', failure_reason:'PUBLIC_ERROR_UNUSUAL_ACTIVITY'},
        {id:'original', profile:'b', status:'succeeded'}
      ]
    }}]
  };
  context.reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'ok')", context);
  const missing = vm.runInContext('flowAccountNoticeMarkup(reason,null)', context);
  assert.match(missing, /任务 original.*异常活动.*本次未保存截图/);
  const captured = vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'original'})", context);
  assert.match(captured, /123456789abc\.png/);
  assert.match(captured, /任务 original.*异常活动/);
  assert.doesNotMatch(captured, /本次未保存截图/);
  const unrelated = vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'other'})", context);
  assert.doesNotMatch(unrelated, /123456789abc\.png/);
});

test('unlinked busy state cannot be described as a confirmed failure', () => {
  const context = vm.createContext({});
  load('flowAccountReason', context);
  const reason = vm.runInContext("flowAccountReason({profile:'a'},{tasks:[]},{status:'blocked'},'busy')", context);
  assert.equal(reason.label, '暂不可用原因');
  assert.match(reason.text, /未查到关联/);
});

test('a running account is queued rather than marked failed, while real blockers remain', () => {
  const account = {profile:'a'};
  const context = vm.createContext({
    flowReadinessObservation:{pageChecks:{accounts:{a:'busy'}}},
    flowWindowIssue:()=>null, flowRecoveryLabels:{},
    flowGovernance:()=>({shared:[],rows:[{account,issues:[]}]}),
  });
  load('accountIssues', context);
  load('auditFlowAccounts', context);
  context.account = account;
  context.snapshot = {real_accounts:{items:[account]},tasks:[{kind:'image',source_profile:'a',status:'running'}],
    flow_coordination:{windows:[{profile:'a',state:'submitted'}]}};
  assert.equal(vm.runInContext('accountIssues(account,snapshot).length',context),0);
  const rows=vm.runInContext("auditFlowAccounts(snapshot,{available:true,active:1,maximum:3},{accounts:{a:'busy'}},0)",context);
  assert.equal(rows[0].status,'queue');
  context.flowGovernance=()=>({shared:[],rows:[{account,issues:[{title:'Google 要求验证'}]}]});
  assert.equal(vm.runInContext("auditFlowAccounts(snapshot,{available:true},{accounts:{a:'busy'}},0)[0].status",context),'blocked');
});

test('a finished no-image lookup is failed while an active attempt remains active', () => {
  const context=vm.createContext({statusBadge:value=>value});
  load('flowTaskStatusBadge',context);
  context.item={kind:'image',status:'interrupted',retry:{state:'needs_review',attempts:[{
    failure_reason:'no image reply within 240 s, and no new image is in the project'}]}};
  assert.equal(vm.runInContext('flowTaskStatusBadge(item)',context),'failed');
  context.item.status='running';context.item.retry.state='active';
  assert.equal(vm.runInContext('flowTaskStatusBadge(item)',context),'running');
});


test('ready account clears historical failure but preserves task history and current blockers', () => {
  const context=vm.createContext({});
  load('flowAccountReason',context);
  context.account={profile:'a'};
  context.snapshot={flow_coordination:{windows:[{profile:'a',task_id:'old'}]},tasks:[{
    kind:'image',source_profile:'a',source_id:'old',status:'failed',failure_reason:'Google 提示异常活动'}]};
  const original=JSON.stringify(context.snapshot);
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{status:'ready',issues:[]},'ready')",context),null);
  assert.equal(JSON.stringify(context.snapshot),original);
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked',issues:[{title:'风控暂停'}]},'ready').taskId",context),'old');
});
