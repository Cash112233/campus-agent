const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
function setup(failSave = false) {
  const fields = {
    '#cfg-key': {value: 'unit-test-key'},
    '#cfg-base': {value: 'https://api.deepseek.com'},
    '#cfg-model': {value: 'deepseek-v4-pro'},
    '#cfg-temp': {value: '0.4'}, '#cfg-history': {value: '12'},
    '#cfg-auto': {checked: false}, '#cfg-auto-th': {value: '4'},
    '#cfg-status': {className: '', textContent: ''},
  };
  const requests = [];
  const context = vm.createContext({
    $: id => fields[id], toast: () => {}, loadConfigPage: () => {}, refreshKeyDot: () => {},
    api: async (url, options) => {
      requests.push({url, body: options.body && JSON.parse(options.body)});
      if (url === '/api/config' && failSave) throw new Error('保存失败');
      return {ok: true, reply: '正常', latency_ms: 1, model: 'deepseek-v4-pro'};
    },
  });
  for (const name of ['saveConfig', 'testConfig']) {
    const match = html.match(new RegExp('async function ' + name + '\\(\\) \\{[\\s\\S]*?\\n\\}'));
    assert.ok(match, name);
    vm.runInContext(match[0], context);
  }
  return {context, requests, fields};
}

test('测试连接使用页面当前选择的模型、地址和 Key', async () => {
  const {context, requests, fields} = setup();
  await context.testConfig();
  assert.equal(requests[0].url, '/api/config');
  assert.equal(requests[0].body.model, 'deepseek-v4-pro');
  assert.equal(requests[0].body.base_url, 'https://api.deepseek.com');
  assert.equal(requests[0].body.api_key, 'unit-test-key');
  assert.equal(requests[1].url, '/api/config/test');
  assert.match(fields['#cfg-status'].textContent, /连接成功/);
});

test('配置保存失败时显示错误并停止连接测试', async () => {
  const {context, requests, fields} = setup(true);
  await context.testConfig();
  assert.equal(requests.length, 1);
  assert.match(fields['#cfg-status'].textContent, /保存失败/);
});
