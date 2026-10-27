const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8');
function extract(name,async=false){
  const match=html.match(new RegExp((async?'async ':'')+'function '+name+'\\([^]*?\\n\\}'));
  assert.ok(match,'missing function '+name);return match[0];
}
test('地图结果来源、评分缺失和不提供评论均明确，且不能注入脚本',()=>{
  const ctx=vm.createContext({URL,Number});
  for(const name of ['esc','safeAmapUrl','placeResultsHtml'])vm.runInContext(extract(name),ctx);
  const rendered=ctx.placeResultsHtml({available:true,provider:'高德地图',notice:'接口不提供用户评论正文',fetched_at:'2026-10-27',results:[
    {id:'BTEST',name:'<img src=x onerror=alert(1)>',rating:null,cost:null,source_url:'javascript:alert(1)',map_url:'https://evil.test',phone:'',opening_hours:''},
    {id:'B2',name:'正常地点',rating:'4.6',cost:'30',source_url:'https://uri.amap.com/poidetail?poiid=B2'}
  ]});
  assert.ok(rendered.includes('&lt;img'));assert.ok(!rendered.includes('<img'));
  assert.ok(!rendered.includes('href="javascript:'));assert.ok(!rendered.includes('https://evil.test'));
  assert.match(rendered,/评分未提供/);assert.match(rendered,/人均未提供/);
  assert.match(rendered,/不提供用户评论正文/);assert.match(rendered,/4.6/);assert.match(rendered,/高德地图/);
});
function settings(fail=false){
  const fields={
    '#map-key':{value:'isolated-map-test-key'},'#map-city':{value:'大庆市'},
    '#map-school':{value:'黑龙江八一农垦大学'},'#map-location':{value:''},
    '#map-radius':{value:'3000'},'#map-status':{textContent:'',className:''},
  },requests=[];
  const context=vm.createContext({$:s=>fields[s],Number,loadMapsConfig:async()=>{},toast:()=>{},
    api:async(url,opts)=>{requests.push({url,body:opts.body && JSON.parse(opts.body)});if(fail)throw Error('保存失败');return {ok:true,message:'连接成功'};}});
  for(const name of ['saveMapsConfig','testMapsConfig'])vm.runInContext(extract(name,true),context);
  return {context,requests,fields};
}
test('高德保存与测试只提交地图配置，空白密钥保留原值',async()=>{
  const {context,requests,fields}=settings();
  await context.testMapsConfig();
  assert.deepEqual(requests.map(r=>r.url),['/api/maps/config','/api/maps/test']);
  assert.equal(requests[0].body.api_key,'isolated-map-test-key');
  assert.equal(fields['#map-key'].value,'');
  await context.saveMapsConfig();
  assert.equal(requests[2].body.api_key,undefined);
});
test('高德配置保存失败时保留提示且不继续测试',async()=>{
  const {context,requests,fields}=settings(true);
  await context.testMapsConfig();assert.equal(requests.length,1);
  assert.match(fields['#map-status'].textContent,/保存失败/);
});
