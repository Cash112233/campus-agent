// 运行真实本地服务；仅创建并清理本测试对话。配置保存被拦截，不改真实密钥。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.CAMPUS_PLAYWRIGHT || 'playwright');
test('单一对话界面：五类卡片、表单、历史恢复及手机布局',async()=>{
  const browser=await chromium.launch({headless:true,args:['--disable-gpu']});
  const page=await browser.newPage({viewport:{width:1366,height:900}});
  if(process.env.CAMPUS_ACCESS_TOKEN) await page.addInitScript(token=>localStorage.setItem('campus_token',token),process.env.CAMPUS_ACCESS_TOKEN);
  const errors=[],sent=[],saved=[],learnRequests=[];
  const authHeaders=()=>process.env.CAMPUS_ACCESS_TOKEN?{'X-Access-Token':process.env.CAMPUS_ACCESS_TOKEN}:{};
  page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.CAMPUS_TEST_URL || 'http://127.0.0.1:8300';
  let conversationId;
  try{
    await page.route('**/api/config',async route=>{
      if(route.request().method()==='POST'){
        saved.push(route.request().postDataJSON());
        await route.fulfill({json:{ok:true}});return;
      }
      const response=await route.fetch();
      await route.fulfill({json:{...await response.json(),auto_learn:true,auto_learn_threshold:2}});
    });
    await page.route('**/api/conversations/*/learn',async route=>{
      learnRequests.push(route.request().url());
      await route.fulfill({json:{memories_added:[],knowledge_pending:[]}});
    });
    page.on('request',r=>{if(r.url().endsWith('/api/chat'))sent.push(r.postDataJSON().message)});
    await page.goto(base);
    await page.waitForFunction(()=>typeof openConversation==='function');
    const created=await page.request.post(base+'/api/conversations',{headers:authHeaders()});
    conversationId=(await created.json()).id;
    await page.evaluate(id=>openConversation(id),conversationId);
    assert.equal(await page.locator('nav').count(),0);
    assert.equal(await page.locator('main > section').count(),1);
    async function send(text,panel){
      await page.locator('#msg-input').fill(text);
      await page.locator('#send-btn').click();
      const card=page.locator('.chat-panel[data-panel="'+panel+'"]').last();
      await card.waitFor();
      await page.waitForFunction(p=>{
        const cards=document.querySelectorAll('.chat-panel[data-panel="'+p+'"]');
        return cards.length && cards[cards.length-1].getAttribute('aria-busy')==='false';
      },panel);
      await page.waitForFunction(()=>document.querySelector('#send-btn').textContent==='发送');
      return card;
    }
    await send('办事导航','services');
    assert.ok(await page.locator('.service-card').count()>0);
    await page.locator('.service-card').first().click();
    await page.locator('#service-ask').waitFor();
    await send('查看知识库','knowledge');
    assert.ok(Number(await page.locator('#kn-count').textContent())>0);
    assert.ok(await page.locator('#kn-tbody tr').count()<=30);
    await page.locator('#kn-filter').fill('食堂');
    assert.ok(await page.locator('#kn-tbody tr').count()>0);
    await page.locator('#kn-tbody button').first().click();
    await page.locator('.inline-document pre').waitFor();
    await send('查看记忆','memory');
    await page.locator('#mem-count').waitFor();
    await send('审核学习内容','learning');
    assert.equal(await page.locator('#learn-stats .stat').count(),8);
    await send('修改设置','settings');
    await page.locator('#school-picker').waitFor();
    assert.ok(await page.locator('#school-picker option').count() >= 1);
    assert.equal(await page.locator('#cfg-key').count(),0);
    assert.equal(await page.locator('#map-key').count(),0);
    await send('修改设置','settings');
    assert.equal(await page.locator('#cfg-key').count(),0);
    assert.equal(await page.locator('#map-key').count(),0);
    assert.equal(saved.length,0);
    const response=await page.request.get(base+'/api/conversations/'+conversationId+'/messages',{headers:authHeaders()});
    const history=await response.json();
    assert.equal(history.filter(m=>m.role==='assistant' && m.tool_events.some(e=>e.name==='open_chat_panel')).length,6);
    await page.reload();
    await page.locator('.chat-panel').last().waitFor();
    assert.equal(await page.locator('.chat-panel-body:visible').count(),0);
    const settings=page.locator('.chat-panel[data-panel="settings"]');
    await settings.last().getByRole('button',{name:'打开操作卡片'}).click();
    await page.locator('#school-picker').waitFor();
    await settings.first().getByRole('button',{name:'打开操作卡片'}).click();
    assert.equal(await page.locator('#cfg-key').count(),0);
    assert.equal(await page.locator('#map-key').count(),0);
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    assert.ok(await page.evaluate(()=>{
      const input=document.querySelector('#school-picker');
      return input.closest('.card').getBoundingClientRect().width > input.closest('.chat-panel-body').getBoundingClientRect().width * .75;
    }),'手机上的学校模板卡片应保持单列显示');
    assert.ok(await page.evaluate(()=>document.querySelector('.chat-input').getBoundingClientRect().bottom<=window.innerHeight),'手机输入框应保持在视口内');
    if(process.env.CAMPUS_SCREENSHOT)await page.screenshot({path:process.env.CAMPUS_SCREENSHOT,fullPage:true});
    assert.deepEqual(learnRequests,[],'打开操作卡片不应触发自动知识提炼');
    await settings.first().getByRole('button',{name:'收起',exact:true}).click();
    const learned=page.waitForResponse(r=>r.url().endsWith('/learn'),{timeout:5000});
    await page.locator('#msg-input').fill('中午吃什么');
    await page.locator('#send-btn').click();
    await learned;
    assert.equal(learnRequests.length,1,'普通问答仍按配置触发自动学习');
    const diningHistory=await (await page.request.get(base+'/api/conversations/'+conversationId+'/messages',{headers:authHeaders()})).json();
    const food=diningHistory.at(-1).tool_events.find(e=>e.name==='recommend_campus_food').result.results;
    assert.ok(food.length>=3 && food.length<=5);
    assert.deepEqual(errors,[]);
  }finally{
    if(conversationId)await page.request.delete(base+'/api/conversations/'+conversationId,{headers:authHeaders()});
    await browser.close();
  }
});
