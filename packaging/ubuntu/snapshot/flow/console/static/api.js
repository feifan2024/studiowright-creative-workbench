'use strict';
const el=id=>document.getElementById(id);
let state=null;
const base=location.origin;
el('baseUrl').textContent=base;
el('connectCode').textContent=`$base = '${base}'
$config = Join-Path $env:USERPROFILE '.ssh\\flow2api_ubuntu.conf'
$remote = '/home/xiao/.local/share/studiowright-evaluation'
$ticket = (& ssh.exe -F $config -o BatchMode=yes flow-ubuntu "$remote/venv/bin/python $remote/console/server.py ticket" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $ticket -notmatch '^[A-Za-z0-9_-]{40,60}$') { throw '取得连接码失败，请检查 SSH 连接' }
$headers = @{ 'X-Console-Request' = '1' }
Invoke-RestMethod -Uri "$base/api/auth/connect" -Method Post -ContentType 'application/json' -Headers $headers -Body (@{ ticket = $ticket } | ConvertTo-Json) -SessionVariable ApiSession
$state = Invoke-RestMethod -Uri "$base/api/state" -WebSession $ApiSession
$state.accounts | Select-Object profile,email,state
$state.projects | Select-Object profile,id,title
$state.models`;
el('pythonCode').textContent=`import http.cookiejar, json, os, subprocess, urllib.request
base = os.environ.get('SW_BASE', '${base}')
remote = '/home/xiao/.local/share/studiowright-evaluation'
ticket = subprocess.check_output([
    'ssh', '-F', os.path.expanduser('~/.ssh/flow2api_ubuntu.conf'),
    '-o', 'BatchMode=yes', 'flow-ubuntu',
    f'{remote}/venv/bin/python {remote}/console/server.py ticket'], text=True).strip()
jar = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def api(path, body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(base + path, data=data, headers={
        'Content-Type': 'application/json', 'X-Console-Request': '1'})
    with client.open(req, timeout=30) as response:
        return json.load(response)
api('/api/auth/connect', {'ticket': ticket})
state = api('/api/state')
print([(a['profile'], a['email'], a['state']) for a in state['accounts']])
# 同一 client 保留会话，后续可调用 api('/api/tasks', 任务参数)。`;
el('pollCode').textContent=`# $submitted 来自前面的提交响应；同一个 PowerShell 会话保留 ApiSession。
$taskId = $submitted.id
if (-not $taskId) { throw '缺少任务编号，不能轮询' }
$deadline = (Get-Date).AddMinutes(30)
do {
  Start-Sleep -Seconds 3
  $s = Invoke-RestMethod -Uri "$base/api/state" -WebSession $ApiSession
  $task = $s.tasks | Where-Object { $_.id -eq $taskId } | Select-Object -First 1
  if (-not $task) { throw '当前列表未找到原任务，请查看后台记录；不要重新提交生成' }
  $task | Select-Object id,status,error
  if ((Get-Date) -gt $deadline) { throw '等待超时，原任务可能仍在执行；保留 taskId 继续查询，不重新提交' }
} while ($task.status -in @('queued','running'))
if ($task.status -ne 'succeeded') { throw ("原任务未成功：" + $task.error) }
$file = $task.downloads | Select-Object -First 1
if ($file) {
  $target = Join-Path (Get-Location) $file.name
  if (Test-Path -LiteralPath $target) { throw '同名文件已存在，请改下载目录后再下载' }
  Invoke-WebRequest -Uri "$base$($file.url)" -WebSession $ApiSession -OutFile $target
} else { '检查或报价任务通常没有下载文件；请读取 task.result' }`;
function fill(id,items,label,value,preferred){
  const select=el(id);select.replaceChildren();
  for(const item of items){const option=document.createElement('option');option.textContent=label(item);option.value=value(item);select.append(option);}
  if(!items.length){const option=document.createElement('option');option.textContent='当前没有可选项';option.value='';select.append(option);}
  if(preferred&&[...select.options].some(o=>o.value===preferred))select.value=preferred;
}
function projects(){
  const profile=el('apiAccount').value;
  fill('apiProject',(state?.projects||[]).filter(p=>p.profile===profile),p=>`${p.title} · ${p.id}`,p=>p.id,el('apiProject').value);
  render();
}
function postExample(body){return `$body = @'\n${JSON.stringify(body,null,2)}\n'@\n$submitted = Invoke-RestMethod -Uri "$base/api/tasks" -Method Post -ContentType 'application/json; charset=utf-8' -Headers $headers -WebSession $ApiSession -Body ([Text.Encoding]::UTF8.GetBytes($body))\n$submitted | Select-Object id,status,kind\n# 继续执行第 6 节的轮询示例。`;}
function render(){
  const profile=el('apiAccount').value||'请从账号列表选择 profile';
  const project=el('apiProject').value||'请从此账号的已有项目选择完整 ID';
  el('selectionInfo').textContent=`当前 profile：${profile}；project：${project}。仅生成示例文本，未提交任务。`;
  el('syncCode').textContent=postExample({profile,kind:'sync',request_key:'check-'+crypto.randomUUID()});
  for(const kind of ['image','video']){
    const params={prompt:kind==='image'?'阳光下的一只橘猫，写实摄影':'阳光下的一只橘猫缓缓转头，镜头稳定',model:el(kind==='image'?'apiImageModel':'apiVideoModel').value||'请从后台模型列表选择 ID',aspect:'16:9',count:1};
    if(kind==='video')Object.assign(params,{duration:8,resolution:'720p',max_credits:20});
    el(kind+'Code').textContent=postExample({profile,project,kind,request_key:kind+'-'+crypto.randomUUID(),confirm_generation:true,params});
  }
}
async function load(){
  const button=el('checkConnection');button.disabled=true;
  try{
    const health=await fetch('/health',{credentials:'same-origin'});if(!health.ok)throw new Error('健康检查未通过');
    const response=await fetch('/api/state',{credentials:'same-origin'});
    if(response.status===401)throw new Error('请先从本机入口连接后台，再返回本页读取实际账号和模型。静态说明仍可阅读。');
    if(!response.ok)throw new Error('读取 API 列表失败：HTTP '+response.status);
    state=await response.json();
    const old=el('apiAccount').value;
    fill('apiAccount',state.accounts,a=>`${a.email||a.label} · ${a.profile}`,a=>a.profile,old);
    for(const kind of ['image','video'])fill(kind==='image'?'apiImageModel':'apiVideoModel',state.models[kind]||[],m=>`${m.label} · ${m.id}`,m=>m.id);
    projects();
    el('apiStatus').textContent=`${state.test?'模拟环境':'正式后台'} API 已连接（源版本 ${state.source_version} / ${state.source_commit.slice(0,8)}）：${state.accounts.length} 个账号，${state.projects.length} 条已登记项目，图片模型 ${state.models.image.length} 个、视频模型 ${state.models.video.length} 个。仅查询本地列表，未启动 Google 检查或生成。`;
  }catch(error){el('apiStatus').textContent=error.message;render();}finally{button.disabled=false;}
}
el('apiAccount').addEventListener('change',projects);
for(const id of ['apiProject','apiImageModel','apiVideoModel'])el(id).addEventListener('change',render);
el('checkConnection').addEventListener('click',load);
let copyTimer;
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-copy]');if(!button)return;
  try{await navigator.clipboard.writeText(el(button.dataset.copy).textContent);el('copyStatus').textContent='已复制示例，未执行。';}catch{el('copyStatus').textContent='未能自动复制，请选中代码框内容后手动复制。';}
  el('copyStatus').className='api-copy-status';clearTimeout(copyTimer);copyTimer=setTimeout(()=>{el('copyStatus').className='hint';},4000);
});
load();
