// 地图响应样本只留在浏览器内；真实配置写入和连接测试全部拦截。
const {test}=require('node:test'),assert=require('node:assert/strict');
const {chromium}=require(process.env.CAMPUS_PLAYWRIGHT || 'playwright');
test('高德地点结果卡片、无密钥提示、详情上下文与手机布局',async()=>{
  const browser=await chromium.launch({headless:true,args:['--disable-gpu']});
  const page=await browser.newPage({viewport:{width:1366,height:900}});
  if(process.env.CAMPUS_ACCESS_TOKEN) await page.addInitScript(token=>localStorage.setItem('campus_token',token),process.env.CAMPUS_ACCESS_TOKEN);
  const base=process.env.CAMPUS_TEST_URL || 'http://127.0.0.1:8300';
  const errors=[],mapWrites=[],chats=[],modelWrites=[];
  const authHeaders=()=>process.env.CAMPUS_ACCESS_TOKEN?{'X-Access-Token':process.env.CAMPUS_ACCESS_TOKEN}:{};
  let cid,connectionOK=false;
  let mapConfig={has_key:false,masked_key:'',city:'大庆市',campus_name:'黑龙江八一农垦大学',campus_location:'',radius:3000};
  page.on('pageerror',e=>errors.push(e.message));
  const sample={
    available:true,provider:'高德地图',fetched_at:'2026-10-27T01:00:00+00:00',
    notice:'评分来自高德；接口不提供用户评论正文。学校周边不等于校外，校园边界尚未核定。',
    results:[
      {id:'BTEST001',name:'浏览器测试咖啡店',address:'测试地址（仅用于验收）',city:'大庆市',rating:'4.6',cost:'30',distance_m:'850',
        source_url:'https://uri.amap.com/poidetail?poiid=BTEST001',map_url:'https://uri.amap.com/marker?position=125,46',
        navigation_url:'https://uri.amap.com/navigation?from=125,46&to=125.1,46.1&mode=walk'},
      {id:'BTEST002',name:'未提供评分的测试地点',address:'测试地址',rating:null,cost:null}
    ]
  };
  try{
    await page.route('**/api/config',async route=>{
      if(route.request().method()==='POST'){modelWrites.push(1);await route.fulfill({json:{ok:true}});return;}
      const response=await route.fetch();await route.fulfill({json:{...await response.json(),auto_learn:false}});
    });
    await page.route('**/api/maps/config',async route=>{
      if(route.request().method()==='POST'){
        const body=route.request().postDataJSON();mapWrites.push(body);
        mapConfig={...mapConfig,city:body.city,campus_name:body.campus_name,campus_location:body.campus_location,radius:body.radius,has_key:!!body.api_key || mapConfig.has_key,masked_key:'isol****key'};
        await route.fulfill({json:{ok:true}});return;
      }
      await route.fulfill({json:mapConfig});
    });
    await page.route('**/api/maps/test',route=>route.fulfill({status:connectionOK?200:400,json:connectionOK?{ok:true,message:'连接验证成功（浏览器受控响应）'}:{ok:false,error:'高德接口错误（10009）：请使用 Web 服务类型的 Key。'}}));
    await page.route('**/api/chat',async route=>{
      const body=route.request().postDataJSON();chats.push(body);
      if(body.message==='学校附近评分高的餐馆'){
        const noKey={available:false,config_needed:true,results:[],message:'请先配置高德 Web 服务 Key。'};
        const event={name:'search_campus_places',args:{query:'餐馆',scope:'nearby'},result:noKey,ok:true};
        const events=[
          {type:'tool_call',data:{name:event.name,args:event.args}},
          {type:'tool_result',data:{name:event.name,result:noKey,ok:true}},
          {type:'answer',data:{message_id:0,content:'请先配置高德',tool_events:[event]}},{type:'done'}
        ];
        await route.fulfill({contentType:'text/event-stream',body:events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join('')});
        return;
      }
      if(body.message!=='学校附近的测试咖啡店'){await route.continue();return;}
      const event={name:'search_campus_places',args:{query:'测试咖啡店'},result:sample,ok:true};
      const events=[
        {type:'tool_call',data:{name:event.name,args:event.args}},
        {type:'tool_result',data:{name:event.name,result:sample,ok:true}},
        {type:'answer',data:{message_id:0,content:'地点展示验收',tool_events:[event]}},{type:'done'}
      ];
      await route.fulfill({contentType:'text/event-stream',body:events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join('')});
    });
    await page.goto(base);
    cid=(await (await page.request.post(base+'/api/conversations',{headers:authHeaders()})).json()).id;
    await page.evaluate(id=>openConversation(id),cid);
    async function send(message){
      await page.locator('#msg-input').fill(message);await page.locator('#send-btn').click();
      await page.waitForFunction(()=>document.querySelector('#send-btn').textContent==='发送');
    }
    await send('学校附近评分高的餐馆');
    assert.equal(await page.locator('[data-map-config]').count(),0);
    assert.equal(await page.locator('#map-key').count(),0);
    assert.equal(modelWrites.length,0);
    assert.equal(mapWrites.length,0);
    await send('学校附近的测试咖啡店');
    await page.getByRole('heading',{name:'浏览器测试咖啡店',exact:true}).waitFor();
    const card=page.locator('.place-card').filter({has:page.getByRole('heading',{name:'浏览器测试咖啡店',exact:true})});
    assert.match(await card.innerText(),/高德评分 4.6/);
    assert.match(await page.locator('.place-results').last().innerText(),/评分未提供/);
    assert.equal(await card.getByRole('link',{name:'从学校导航'}).getAttribute('href'),'https://uri.amap.com/navigation?from=125,46&to=125.1,46.1&mode=walk');
    await page.setViewportSize({width:390,height:844});
    await card.scrollIntoViewIfNeeded();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.ok(await page.evaluate(()=>document.querySelector('.chat-input').getBoundingClientRect().bottom<=innerHeight));
    if(process.env.CAMPUS_MAP_SCREENSHOT)await page.screenshot({path:process.env.CAMPUS_MAP_SCREENSHOT,fullPage:true});
    await card.getByRole('button',{name:'更新地点详情'}).click();
    await page.waitForFunction(()=>document.querySelector('#send-btn').textContent==='发送');
    assert.equal(chats.at(-1).place_id,'BTEST001');
    assert.ok(!chats.at(-1).message.includes('BTEST001'));
    assert.ok(!JSON.stringify(chats).includes('isolated-browser-map-key'));
    await page.reload();
    await page.locator('.place-results').last().waitFor();
    assert.equal(await page.locator('nav').count(),0);
    assert.deepEqual(errors,[]);
  }finally{
    if(cid)await page.request.delete(base+'/api/conversations/'+cid,{headers:authHeaders()});
    await browser.close();
  }
});
