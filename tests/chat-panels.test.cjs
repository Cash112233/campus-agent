const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8');
test('feature navigation and standalone feature pages are removed',()=>{
  assert.doesNotMatch(html,/<nav[\s>]/);
  assert.doesNotMatch(html,/<section[^>]+id="page-(services|knowledge|memory|learning|settings)"/);
  for(const panel of ['services','knowledge','memory','learning','settings'])
    assert.match(html,new RegExp('<template id="chat-panel-'+panel+'"'));
});
test('设置卡片只保留学校模板，其他服务配置不出现在界面',()=>{
  assert.match(html,/function mountChatPanels\(/);
  assert.match(html,/mountChatPanels\(div, toolEvents/);
  assert.match(html,/mountChatPanels\(shell, ev.data.tool_events/);
  const settingsTemplate = html.match(/<template id="chat-panel-settings">[\s\S]*?<\/template>/)?.[0] || '';
  assert.match(settingsTemplate,/id="school-picker"/);
  assert.match(settingsTemplate,/id="school-crawl"/);
  assert.doesNotMatch(settingsTemplate,/id="cfg-key"/);
  assert.doesNotMatch(settingsTemplate,/id="map-key"/);
  assert.doesNotMatch(settingsTemplate,/大模型配置（DeepSeek）/);
  assert.doesNotMatch(settingsTemplate,/高德地图 · 地点与评分/);
  assert.doesNotMatch(html,/querySelector\('nav button/);
});
