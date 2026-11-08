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
      item: {profile:'test', gemini:'verified'}, state:{snapshot:{}},
      flowRow:{status,issues:[]}, geminiRow:{status}, issues:[],
      flowWindowIssue: () => null, readinessBadge: row => row.status,
    });
    const result = vm.runInContext(presentation + '\n({flowFallback,geminiFallback,flowStatusText});', context);
    assert.equal(result.flowStatusText, status);
    assert.equal(Boolean(result.flowFallback), status !== 'ready');
    assert.equal(Boolean(result.geminiFallback), status !== 'ready');
  }
});
