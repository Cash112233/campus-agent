const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
function loadFunctions() {
  const ctx = vm.createContext({ URL });
  for (const name of ['esc', 'safeSourceUrl', 'sourceRefsHtml']) {
    const re = new RegExp('function ' + name + '\\([^]*?(?=\\n(?:async )?function |\\n/\\*|$)');
    const found = source.match(re);
    assert.ok(found, name + ' must exist');
    vm.runInContext(found[0], ctx);
  }
  return ctx;
}
test('来源链接只允许网页地址且拒绝脚本和带凭据的 URL', () => {
  const ctx = loadFunctions();
  assert.equal(ctx.safeSourceUrl('javascript:alert(1)'), '');
  assert.equal(ctx.safeSourceUrl('https://user:secret@example.com/'), '');
  assert.equal(ctx.safeSourceUrl('https://www.byau.edu.cn/919/list.htm'), 'https://www.byau.edu.cn/919/list.htm');
});
test('来源标题与文档编号不能注入页面代码', () => {
  const ctx = loadFunctions();
  const html = ctx.sourceRefsHtml([{doc_id: '1);alert(1)', title: '<img src=x onerror=alert(1)>', source_url: 'javascript:alert(1)', indexed_at: '2026-10-26'}]);
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('onclick='));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('&lt;img'));
});
