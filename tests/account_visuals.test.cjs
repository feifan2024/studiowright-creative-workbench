const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'packaging', 'ubuntu', 'snapshot', 'workbench', 'app.js'), 'utf8');
function load(name, context) {
  const match = source.match(new RegExp('^function ' + name + '\\([^]*?^}', 'm'));
  assert.ok(match, name + ' should exist');
  vm.runInContext(match[0], context);
}

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

test('account reason exposes the original task failure and handles missing detail honestly', () => {
  const context = vm.createContext({});
  load('flowAccountReason', context);
  context.account = {profile:'a'};
  context.snapshot = {flow_coordination:{windows:[{profile:'a',task_id:'stopped',stage:'submitting'}]},
    tasks:[{source_id:'stopped',source_profile:'a',kind:'image',status:'interrupted',
      retry:{state:'needs_review',attempts:[{profile:'a',status:'failed',failure_reason:'等待图片回执超时，结果未确认'}]}}]};
  const reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'busy')", context);
  assert.equal(reason.label, '失败原因');
  assert.match(reason.text, /等待图片回执超时.*stopped/);
  context.snapshot.tasks[0].retry.attempts[0].failure_reason = '';
  const unknown = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'busy')", context);
  assert.match(unknown.text, /未返回具体错误/);
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
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked',issues:[{title:'风控暂停'}]},'ready').label",context),'失败原因');
});
