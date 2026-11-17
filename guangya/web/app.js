'use strict';
const $ = id => document.getElementById(id);
const labels = {queued:'已排队',inspecting:'读取分享',saving:'转存中',confirming:'核对转存',saved:'已转存',planning:'识别规划中',executing:'整理中',uploading:'上传中',preview_ready:'等待确认',completed:'已完成',partial:'部分完成',needs_review:'待核对',failed:'失败'};
const kindNames = {share:'分享转存',preview:'整理预览',approved:'媒体整理',organize:'媒体整理',upload:'上传',legacy_share:'迁移任务'};
let config = {}, jobs = [], currentJob = null, toastTimer, picker = {};
function node(tag, text, className) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if(className) element.className = className; return element; }
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,6500); }
async function api(path, data) {
  const response = await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
  const result = await response.json();
  if(response.status===401) { $('application').hidden=true; $('login').hidden=false; }
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'请求失败，请检查输入或任务记录');
  return result;
}
async function action(button, fn) { if(button.disabled) return; button.disabled=true; try {await fn();} catch(error) {toast(error.message);} finally {button.disabled=false;} }
function showPage(page) {
  document.querySelectorAll('.page').forEach(element=>element.hidden=element.id!=='page-'+page);
  document.querySelectorAll('nav button').forEach(button=>button.classList.toggle('active',button.dataset.page===page));
  $('page-title').textContent={tasks:'任务中心',organize:'媒体整理',upload:'上传到光鸭',settings:'设置'}[page];
}
function setDefaults() {
  const local=$('org-storage').value==='local';
  $('org-source').value=local?config.local_source:config.cloud_source;
  $('org-target').value=local?config.local_target:config.cloud_target;
  $('org-mode').value=local?config.mode:(config.mode==='link'?'move':config.mode);
  $('org-scrape').checked=config.scrape;
  $('org-upload').checked=local&&config.upload_after_local;
  $('org-upload').disabled=!local;
}
function renderConfig() {
  const form=$('settings-form');
  for(const [key,value] of Object.entries(config)) {
    const field=form.elements.namedItem(key); if(!field) continue;
    if(field.type==='checkbox') field.checked=Boolean(value);
    else field.value=Array.isArray(value)?value.join('\n'):value??'';
  }
  $('secret-status').textContent=`TMDB ${config.tmdb_api_key_set?'已设置':'未设置'} · Bot ${config.telegram_token_set?'已设置':'未设置'} · TG ${config.telegram_enabled?'已启用':'未启用'}`;
  $('cloud-state').textContent=config.access_token_set?'光鸭已登录':'光鸭未登录';
  $('cloud-state').className='pill'+(config.access_token_set?'':' warn');
  $('upload-target').value=config.cloud_upload;
  $('share-dirs').textContent=`${config.cloud_source} → ${config.cloud_target}`;
  setDefaults();
}
async function loadJobs() {
  const result=await api('jobs'); jobs=result.items; renderJobs();
  if(currentJob && $('detail-dialog').open) await showJob(currentJob,false);
}
function renderJobs() {
  const active=new Set(['queued','inspecting','saving','confirming','saved','planning','executing','uploading']);
  $('count-active').textContent=jobs.filter(job=>active.has(job.state)).length;
  $('count-done').textContent=jobs.filter(job=>job.state==='completed').length;
  $('count-review').textContent=jobs.filter(job=>['needs_review','failed','partial','preview_ready'].includes(job.state)).length;
  $('job-list').replaceChildren();
  if(!jobs.length) { $('job-list').append(node('div','还没有任务。粘贴分享链接，或先预览一个媒体目录。','empty')); return; }
  for(const job of jobs) {
    const card=node('div',undefined,'job'); card.tabIndex=0; card.setAttribute('role','button');
    card.append(node('div',job.kind==='share'?'↗':job.kind==='upload'?'↑':'▤','job-icon'));
    const main=node('div',undefined,'job-main');main.append(node('strong',job.message||kindNames[job.kind]||job.kind),node('small',`${kindNames[job.kind]||job.kind} · ${job.id} ${job.source?'· '+job.source:''}`));card.append(main);
    const cls=['needs_review','partial','preview_ready'].includes(job.state)?' warn':job.state==='failed'?' bad':job.state==='completed'?'':' muted';
    card.append(node('span',labels[job.state]||job.state,'pill'+cls),node('span',new Date(job.created*1000).toLocaleString(),'job-time'));
    card.onclick=()=>showJob(job.id).catch(error=>toast(error.message));card.onkeydown=event=>{if(event.key==='Enter')card.click();};$('job-list').append(card);
  }
}
async function showJob(id,open=true) {
  const job=await api('jobs/'+encodeURIComponent(id));currentJob=id;
  const content=$('detail-content');content.replaceChildren(node('h3',`${job.id} · ${labels[job.state]||job.state}`));
  const info=node('div',undefined,'details-meta');info.append(node('div',job.message),node('div','源：'+(job.source||'—')),node('div','目标：'+(job.target||'—')));content.append(info);
  const result=job.result||{};
  for(const error of result.errors||[]) content.append(node('div',`${error.source||''} ${error.message}`,'notice'));
  const items=result.plans||result.items||[];
  if(items.length) {
    const table=node('table'), head=node('tr'); for(const title of ['源文件','目标文件','媒体 / 结果'])head.append(node('th',title));table.append(head);
    for(const item of items) {const row=node('tr'); row.append(node('td',item.source),node('td',item.target),node('td',`${item.title||''} ${item.tmdb_id?'TMDB '+item.tmdb_id:''} ${labels[item.state]||item.state||''}`));table.append(row);}
    const wrap=node('div',undefined,'table-scroll');wrap.append(table);content.append(wrap);
  }
  if(job.operations?.length) {
    content.append(node('h3','文件操作记录'));
    for(const op of job.operations) content.append(node('div',`${op.state} · ${op.source} → ${op.target}`,'hint'));
  }
  const actions=$('detail-actions');actions.replaceChildren();
  if(job.state==='preview_ready') {const button=node('button','确认执行','primary');button.onclick=()=>action(button,async()=>{await api('jobs/'+id+'/execute',{});toast('已加入执行队列');await loadJobs();});actions.append(button);}
  if(['needs_review','failed','partial'].includes(job.state) && job.kind!=='upload') {
    const select=node('select');select.append(new Option('电影','movie'),new Option('电视剧','tv'));
    const input=node('input');input.type='number';input.placeholder='指定 TMDB ID';input.min='1';
    const button=node('button','重新生成预览');button.onclick=()=>action(button,async()=>{if(!input.value)throw new Error('请填写 TMDB ID');const result=await api('jobs/'+id+'/retry',{tmdb_id:Number(input.value),media_type:select.value});toast('已创建预览 '+result.id);$('detail-dialog').close();await loadJobs();});
    actions.append(select,input,button);
  }
  if(open&&!$('detail-dialog').open) $('detail-dialog').showModal();
}
$('login-form').onsubmit=async event=>{event.preventDefault();await action(event.submitter,async()=>{try {await api('login',{password:$('password').value});$('password').value='';await boot();}catch(error){$('login-error').textContent=error.message;}});};
$('logout').onclick=async()=>{await api('logout',{});location.reload();};
document.querySelectorAll('nav button').forEach(button=>button.onclick=()=>showPage(button.dataset.page));
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
$('org-storage').onchange=setDefaults;
$('refresh').onclick=()=>action($('refresh'),loadJobs);
$('share-form').onsubmit=async event=>{event.preventDefault();await action(event.submitter,async()=>{const result=await api('share',{text:$('share-text').value});$('share-text').value='';toast((result.duplicate?'已有任务：':'已加入队列：')+result.id);await loadJobs();});};
$('organize-form').onsubmit=async event=>{event.preventDefault();await action(event.submitter,async()=>{const payload={storage:$('org-storage').value,source:$('org-source').value,target:$('org-target').value,mode:$('org-mode').value,media_type:$('org-media-type').value,tmdb_id:$('org-tmdb').value?Number($('org-tmdb').value):null,scrape:$('org-scrape').checked,upload_after:$('org-upload').checked};const result=await api('organize/preview',payload);toast('正在识别，任务 '+result.id);showPage('tasks');await loadJobs();});};
$('upload-form').onsubmit=async event=>{event.preventDefault();await action(event.submitter,async()=>{const paths=$('upload-paths').value.split('\n').map(v=>v.trim()).filter(Boolean);const result=await api('upload',{paths,target:$('upload-target').value});toast('已加入上传队列：'+result.id);showPage('tasks');await loadJobs();});};
$('settings-form').onsubmit=async event=>{event.preventDefault();await action(event.submitter,async()=>{const values={};for(const field of event.target.elements){if(!field.name)continue;values[field.name]=field.type==='checkbox'?field.checked:field.name==='local_roots'?field.value.split('\n').map(v=>v.trim()).filter(Boolean):field.type==='number'?Number(field.value):field.value;}config=await api('config',values);event.target.elements.tmdb_api_key.value='';event.target.elements.telegram_token.value='';renderConfig();toast('设置已保存');});};
$('tmdb-test').onclick=()=>action($('tmdb-test'),async()=>{const result=await api('tmdb/test',{});toast('TMDB 连接成功：'+result.title);});
async function account(){const data=await api('cloud/account');$('account-status').textContent=`光鸭已登录 ${data.name||''} · 可用 ${(data.available/1024**4).toFixed(2)} TB`;}
$('cloud-refresh').onclick=()=>action($('cloud-refresh'),account);
$('cloud-login').onclick=()=>action($('cloud-login'),async()=>{const data=await api('cloud/login/start',{});const container=$('cloud-login-info');container.replaceChildren(node('p','登录码：'+data.user_code));if(data.qr_image){const image=node('img');image.src=data.qr_image;image.alt='光鸭登录二维码';image.width=200;image.height=200;container.append(image);}const url=data.verification_uri_complete||data.verification_uri;if(url){const parsed=new URL(url);if(parsed.protocol==='https:'&&(parsed.hostname==='guangyapan.com'||parsed.hostname.endsWith('.guangyapan.com'))){const link=node('a','打开光鸭登录页面');link.href=url;link.target='_blank';link.rel='noopener noreferrer';container.append(link);}}$('cloud-poll').hidden=false;});
$('cloud-poll').onclick=()=>action($('cloud-poll'),async()=>{const result=await api('cloud/login/poll',{});if(result.logged_in){config=await api('config');renderConfig();await account();$('cloud-poll').hidden=true;toast('光鸭登录成功');}else toast('尚未完成登录，请在光鸭页面确认');});
async function browse(storage,path,onSelect,allowFiles=false) {picker={storage,path,onSelect,allowFiles};$('picker-dialog').showModal();await browsePath(path);}
async function browsePath(path){const data=await api(`files?storage=${picker.storage}&path=${encodeURIComponent(path)}`);picker.path=data.path;$('picker-path').value=data.path;$('picker-list').replaceChildren();for(const item of data.items){if(item.type!=='dir'&&!picker.allowFiles)continue;const entry=node('div',undefined,'picker-entry');entry.append(node('span',(item.type==='dir'?'▸ ':'· ')+item.name));if(item.type==='dir')entry.onclick=()=>browsePath(item.path).catch(error=>toast(error.message));else entry.onclick=()=>{picker.onSelect(item.path);$('picker-dialog').close();};$('picker-list').append(entry);}}
document.querySelectorAll('[data-browse]').forEach(button=>button.onclick=()=>{const id=button.dataset.browse;const storage=id==='upload-target'?'guangya':$('org-storage').value;const value=$(id).value||'/';browse(storage,value,path=>$(id).value=path).catch(error=>toast(error.message));});
$('upload-browse').onclick=()=>browse('local',config.local_source,path=>{$('upload-paths').value+=($('upload-paths').value?'\n':'')+path;},true).catch(error=>toast(error.message));
$('picker-up').onclick=()=>{const path=picker.path.replace(/\/$/,'');const parent=path.slice(0,path.lastIndexOf('/'))||'/';browsePath(parent).catch(error=>toast(error.message));};
$('picker-go').onclick=()=>browsePath($('picker-path').value).catch(error=>toast(error.message));
$('picker-choose').onclick=()=>{picker.onSelect(picker.path);$('picker-dialog').close();};
async function boot(){config=await api('config');$('login').hidden=true;$('application').hidden=false;renderConfig();await loadJobs();}
boot().catch(()=>{});setInterval(()=>{if(!document.hidden&&!$('application').hidden)loadJobs().catch(()=>{});},4000);
