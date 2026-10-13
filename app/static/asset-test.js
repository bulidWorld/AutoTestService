'use strict';
const $ = (id) => document.getElementById(id);
let selected = null;
let rows = [];
let revision = 0;
let running = false;
const sensitiveName = /password|passwd|secret|token|authorization|api[_-]?key|cookie/i;
function notice(text, error=false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); }
async function api(path, options={}) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const details = (result.issues || []).map((issue) => `${issue.location}: ${issue.reason}`).join('\n');
    throw new Error(`${result.detail || '操作失败'}${details ? '\n' + details : ''}`);
  }
  return result;
}
async function all(path) {
  const items = [];
  let page=1;
  while (true) {
    const result = await api(`${path}${path.includes('?') ? '&' : '?'}page=${page}&page_size=100`);
    items.push(...result.items);
    if (items.length >= result.total || result.items.length === 0) return items;
    page++;
  }
}
function choices(id, items, label, preferred) {
  const node = $(id); node.replaceChildren();
  for (const item of items) { const option = document.createElement('option'); option.value = String(item.id ?? item.version); option.textContent = label(item); node.append(option); }
  node.disabled = items.length === 0;
  if (preferred && [...node.options].some((option) => option.value === String(preferred))) node.value = String(preferred);
}
function projectPath() { return `/api/v1/projects/${$('project').value}`; }
function filePath() { return `${projectPath()}/yaml-files/${$('file').value}`; }
function clearFrom(ids) { for (const id of ids) { $(id).replaceChildren(); $(id).disabled=true; } selected=null; $('configuration').hidden=true; $('result-panel').hidden=true; }
async function projects(preferred) {
  const stamp=++revision; clearFrom(['project','file','version','interface']); $('import-yaml').disabled=true;
  const items=await all('/api/v1/projects'); if(stamp!==revision) return;
  choices('project',items,(p)=>p.name,preferred);
  if(!items.length) { notice('暂无项目，可先新建项目并导入 YAML。'); return; }
  await files();
}
async function files(preferred) {
  const stamp=++revision; clearFrom(['file','version','interface']); $('import-yaml').disabled=!$('project').value;
  if(!$('project').value) return;
  const items=await all(`${projectPath()}/yaml-files`); if(stamp!==revision) return;
  choices('file',items,(f)=>f.name,preferred);
  if(!items.length) { notice('此项目暂无接口文件，请先导入 YAML。'); return; }
  await versions();
}
async function versions() {
  const stamp=++revision; clearFrom(['version','interface']);
  const items=await all(`${filePath()}/versions`); if(stamp!==revision) return;
  choices('version',items,(v)=>`v${v.version}${v.is_current?' · 当前版本':''}`);
  if(items.length) await interfaces();
}
async function interfaces() {
  const stamp=++revision; clearFrom(['interface']);
  const items=await all(`${filePath()}/interfaces?version=${$('version').value}`); if(stamp!==revision) return;
  choices('interface',items,(i)=>`${i.method} ${i.path}${i.summary?' · '+i.summary:''}`);
  if(!items.length) { notice('此版本暂无接口。'); return; }
  await detail();
}
function initial(schema, depth=0) {
  if ('default' in schema) return schema.default;
  if (depth>8) return undefined;
  if (schema.type==='object') {
    const value={}; for(const [key,child] of Object.entries(schema.properties||{})) { const item=initial(child,depth+1); if(item!==undefined) value[key]=item; } return value;
  }
  return undefined;
}
function parameterRow(p) {
  const element=document.createElement('div'); element.className='parameter';
  const label=document.createElement('label'); const enabled=document.createElement('input'); enabled.type='checkbox'; enabled.checked=p.required||('default' in p.schema); enabled.disabled=!!p.required;
  const title=document.createElement('span'); title.textContent=p.name+(p.required?' *':''); label.append(enabled,title);
  const metadata=document.createElement('div'); metadata.className='meta'; metadata.textContent=`${p.location} · ${p.schema.type||'JSON'}${p.description?' · '+p.description:''}`; label.append(metadata);
  const input=document.createElement('input'); input.autocomplete='off'; input.setAttribute('aria-label',`${p.location} ${p.name}`);
  if('default' in p.schema) input.value=p.schema.type==='string'?p.schema.default:JSON.stringify(p.schema.default);
  else if ('example' in p.schema) input.placeholder=p.schema.type==='string'?String(p.schema.example):JSON.stringify(p.schema.example);
  else input.placeholder=p.schema.type==='string'?'输入字符串':'输入 JSON 值';
  input.required=!!p.required;
  const secretLabel=document.createElement('label'); secretLabel.className='inline'; const secret=document.createElement('input'); secret.type='checkbox'; secret.checked=sensitiveName.test(p.name); secretLabel.append(secret,document.createTextNode('敏感'));
  const sync=()=>{input.disabled=!enabled.checked;input.type=secret.checked?'password':'text';}; enabled.addEventListener('change',sync);secret.addEventListener('change',sync);sync();
  element.append(label,input,secretLabel); rows.push({parameter:p,enabled,input,secret});return element;
}
async function detail() {
  const stamp=++revision; selected=null; $('configuration').hidden=true; $('result-panel').hidden=true;
  const data=await api(`${filePath()}/interfaces/${$('interface').value}`);if(stamp!==revision) return;
  selected=data;rows=[];$('parameters').replaceChildren(...data.parameters.map(parameterRow));$('extra-headers').replaceChildren();
  const badge=document.createElement('span');badge.className='method';badge.textContent=data.method;
  $('endpoint').replaceChildren(badge,document.createTextNode(`${data.path} · YAML v${data.version}${data.summary?' · '+data.summary:''}`));
  const body=data.request_body;const json=body&&body.content_type==='application/json';
  $('body-panel').hidden=!json;$('unsupported').hidden=!body||json;$('run-test').disabled=!!body&&!json;
  $('send-body').checked=!!body?.required;$('send-body').disabled=!!body?.required;$('body-sensitive').checked=false;
  $('request-body').value=json?JSON.stringify(initial(body.schema)??(body.schema.type==='array'?[]:{}),null,2):'';
  $('body-schema').textContent=json?JSON.stringify(body.schema,null,2):'';
  $('request-body').disabled=!$('send-body').checked;
  $('configuration').hidden=false;notice('填写参数后点击“单次测试”。可选参数取消勾选后不发送。');
}
function addHeader() {
  const row=document.createElement('div');row.className='extra';
  const name=document.createElement('input');name.placeholder='Header 名称';name.setAttribute('aria-label','额外请求头名称');
  const value=document.createElement('input');value.placeholder='Header 值';value.setAttribute('aria-label','额外请求头值');value.autocomplete='off';
  const secret=document.createElement('input');secret.type='checkbox';const label=document.createElement('label');label.className='inline';label.append(secret,document.createTextNode('敏感'));
  const remove=document.createElement('button');remove.type='button';remove.className='secondary small';remove.textContent='移除';remove.onclick=()=>row.remove();
  name.addEventListener('input',()=>{secret.checked=sensitiveName.test(name.value);value.type=secret.checked?'password':'text';});secret.addEventListener('change',()=>{value.type=secret.checked?'password':'text';});
  row.append(name,value,label,remove);row.fields={name,value,secret};$('extra-headers').append(row);
}
function parseParameter(row) {
  const {parameter:p,input}=row;
  if(p.schema.type==='string') return input.value;
  let value;try{value=JSON.parse(input.value);}catch{throw new Error(`${p.name} 必须输入有效 JSON 值。`);}
  if(p.schema.type==='integer'&&!Number.isSafeInteger(value)) throw new Error(`${p.name} 必须为安全范围内的整数。`);
  return value;
}
function renderResult(data) {
  const stats=$('stats');stats.replaceChildren();
  for(const text of [data.status==='succeeded'?'测试成功':'测试失败',`HTTP ${data.response_status??'无响应'}`,`${data.duration_ms} ms`,`YAML v${data.version}`]) {const node=document.createElement('span');node.textContent=text;stats.append(node);}
  stats.firstChild.className=data.status==='succeeded'?'success':'failed';
  $('result-error').hidden=!data.error_code;$('result-error').textContent=data.error_code?`${data.error_code} · ${data.error_message}`:'';
  $('redacted-note').hidden=!data.redacted;
  $('request-result').textContent=`${data.method} ${data.actual_url}\n\n${(data.request_headers||[]).map((h)=>`${h.name}: ${h.value}`).join('\n')}\n\n${data.request_body??'（无请求体）'}`;
  let body=data.response_body??'（无响应体）';if(data.response_body_encoding==='utf-8'){try{body=JSON.stringify(JSON.parse(body),null,2);}catch{ /* Plain response text is supported. */ }}
  $('response-result').textContent=`${(data.response_headers||[]).map((h)=>`${h.name}: ${h.value}`).join('\n')}\n\n${body}${data.response_body_encoding==='base64'?'\n（响应体使用 base64 编码）':''}`;
  $('result-panel').hidden=false;
}
async function submitTest(event) {
  event.preventDefault();if(!selected||running) return;
  try {
    const parameters=rows.filter((row)=>row.enabled.checked).map((row)=>({location:row.parameter.location,name:row.parameter.name,value:parseParameter(row),sensitive:row.secret.checked}));
    for(const row of $('extra-headers').children){const {name,value,secret}=row.fields;if(!name.value.trim())throw new Error('请填写额外请求头名称。');parameters.push({location:'header',name:name.value.trim(),value:value.value,sensitive:secret.checked});}
    const payload={base_url:$('base-url').value.trim(),timeout_seconds:Number($('timeout').value),parameters,body_sensitive:$('body-sensitive').checked};
    if(!$('body-panel').hidden&&$('send-body').checked){try{payload.request_body=JSON.parse($('request-body').value);}catch{throw new Error('请求体必须为有效 JSON。');}}
    const path=`${filePath()}/interfaces/${selected.id}/test`;const stamp=revision;running=true;$('run-test').disabled=true;$('run-test').textContent='测试中…';$('result-panel').hidden=true;notice('正在发送单次请求…');
    const data=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(stamp===revision){renderResult(data);notice('单次测试已完成。');}
  }catch(error){notice(error.message,true);}finally{running=false;$('run-test').disabled=!!selected?.request_body&&selected.request_body.content_type!=='application/json';$('run-test').textContent='单次测试';}
}
function bindSelect(id,action){$(id).addEventListener('change',()=>action().catch((error)=>notice(error.message,true)));}
bindSelect('project',files);bindSelect('file',versions);bindSelect('version',interfaces);bindSelect('interface',detail);
$('reload').onclick=()=>projects($('project').value).catch((error)=>notice(error.message,true));$('add-header').onclick=addHeader;
$('test-form').addEventListener('submit',submitTest);$('send-body').onchange=()=>{$('request-body').disabled=!$('send-body').checked;};
$('create-project').onclick=()=>$('project-dialog').showModal();$('import-yaml').onclick=()=>$('import-dialog').showModal();
for(const button of document.querySelectorAll('[data-close]'))button.onclick=()=>$(button.dataset.close).close();
$('project-form').onsubmit=async(event)=>{event.preventDefault();try{const p=await api('/api/v1/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('project-name').value,description:$('project-description').value})});$('project-dialog').close();$('project-form').reset();await projects(p.id);}catch(error){notice(error.message,true);}};
$('import-form').onsubmit=async(event)=>{event.preventDefault();try{const payload=new FormData();payload.append('name',$('yaml-name').value);payload.append('file',$('yaml-upload').files[0]);const data=await api(`${projectPath()}/yaml-files`,{method:'POST',body:payload});$('import-dialog').close();$('import-form').reset();await files(data.file.id);}catch(error){notice(error.message,true);}};
projects().catch((error)=>notice(error.message,true));
