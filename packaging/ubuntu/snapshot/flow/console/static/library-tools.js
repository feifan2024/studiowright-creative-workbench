'use strict';
const materialDimensions={subject:'主体',scene:'场景',action:'动作',style:'风格',camera:'镜头',color:'色调',keywords:'关键词'};
let materialOpenRequest=0,materialDirty=false,materialSaving=false;
const splitMaterialTags=value=>[...new Set(value.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean))];
function libraryFilters(){
 const params=new URLSearchParams({q:$('libraryQuery').value.trim(),kind:$('libraryKind').value,account:$('libraryAccount').value,tag:$('libraryTag').value});
 for(const d of Object.keys(materialDimensions))if($('filter-'+d).value)params.set(d,$('filter-'+d).value);
 for(const d of ['model','project_id','created_from','created_to','index_status'])if($('filter-'+d).value)params.set(d,$('filter-'+d).value);
 return params;
}
function updateDimensionFilters(lib){
 for(const [d,label] of Object.entries(materialDimensions)){
  const select=$('filter-'+d),value=select.value;
  select.replaceChildren(new Option('全部'+label,''),...(lib.facets?.dimensions[d]||[]).map(t=>new Option(`${t.value}（${t.count}）`,t.value)));
  if(value&&![...select.options].some(o=>o.value===value))select.add(new Option(value,value));select.value=value;
 }
 const counts=lib.facets?.counts.index_status||{};
 $('indexSummary').textContent=`标签索引：已完成 ${counts.ready||0} · 待分析 ${counts.pending||0} · 分析中 ${counts.processing||0} · 失败 ${counts.failed||0}。标签来源以素材详情为准：提示词整理与实际画面识别分别记录，人工标签优先。`;
}
function materialTagBadges(item){
 const tags=Object.entries(item.structured_tags||{}).flatMap(([d,values])=>values.map(v=>`${materialDimensions[d]}：${v}`));
 return [...tags,...item.tags].slice(0,7).map(t=>`<span>${esc(t)}</span>`).join('')||'<small>尚无标签，可手动补充或配置自动分析</small>';
}
function fillMaterialDimensions(item){
 for(const d of Object.keys(materialDimensions))$('edit-'+d).value=(item.structured_tags?.[d]||[]).join('，');
 const evidence=item.index_provenance?.media_evidence;
 const source=item.index_provenance?.tag_source==='visual_analysis'?'实际画面识别'+(evidence?.media_type==='video'?` · 抽样 ${evidence.sample_count} 帧，不含音频`:' · 图片看图'):'提示词整理／人工编辑，未确认实际画面';
 $('materialAnalysisStatus').textContent=`标签分析：${({pending:'待分析',processing:'分析中',ready:'已完成',failed:'失败'})[item.index_status]||'待分析'}。${source}。人工编辑优先。${item.index_error||''}`;
 materialDirty=false;
}
async function copyTextSafely(text){
 try{await navigator.clipboard.writeText(text);return true;}catch{
  const box=document.createElement('textarea');box.value=text;box.setAttribute('aria-label','待复制内容');box.style.position='fixed';box.style.opacity='0';document.body.append(box);box.select();let ok=false;try{ok=document.execCommand('copy');}finally{box.remove();}if(ok)return true;
  const fallbackBox=document.getElementById('copyFallbackText');fallbackBox.value=text;document.getElementById('copyFallback').showModal();fallbackBox.select();return false;
 }
}
async function copyLibrarySearch(){
 const p=libraryFilters();p.set('limit','30');p.set('offset','0');
 const doc={schema_version:1,base_url:location.origin,method:'GET',path:'/api/library',query:Object.fromEntries(p),url:location.origin+'/api/library?'+p,headers:{Authorization:'Bearer <STUDIOWRIGHT_API_KEY>'},pagination:{limit:30,offset:0},combination:'不同维度和来源筛选按 AND 组合；q 匹配提示词或标签；未填写的维度不限制'};
 if(await copyTextSafely(JSON.stringify(doc,null,2)))toast('已复制完整检索条件，其他项目可直接按此调用。');
}
async function copyMaterialReuse(){
 if(!selectedMaterial)return;
 if(materialDirty){toast('请先保存标签，复制内容才会与素材库一致。',true);return;}
 const i=selectedMaterial;
 const doc={schema_version:1,asset_id:i.id,sha256:i.sha256,media_type:i.media_type,mime:i.mime,size:i.size,width:i.width,height:i.height,duration:i.duration,tags:i.structured_tags,structured_tags:i.structured_tags,custom_tags:i.tags,tag_schema:i.tag_schema,tag_sources:i.tag_sources,observed_tags:i.observed_tags,index_status:i.index_status,index_provenance:i.index_provenance,origins:i.origins,detail_url:location.origin+'/api/library/'+i.id,file_url:location.origin+i.url,headers:{Authorization:'Bearer <STUDIOWRIGHT_API_KEY>'},note:'文件和详情均需鉴权；素材 ID 是新库内容哈希，原库编号保留在 origins.source_id。标签来源见 tag_sources；observed_tags 为实际画面识别结果，视频仅覆盖抽样帧，不含音频。'};
 if(await copyTextSafely(JSON.stringify(doc,null,2)))toast('已复制素材调用信息、七类标签及全部原始来源。');
}
function setupLibraryTools(){
 $('dimensionFilters').innerHTML=Object.entries(materialDimensions).map(([d,l])=>`<label>${l}<select id="filter-${d}"><option value="">全部${l}</option></select></label>`).join('')+`<label>生成模型<input id="filter-model" maxlength="200" placeholder="完整模型 ID"></label><label>原项目<input id="filter-project_id" maxlength="200" placeholder="完整项目 ID"></label><label>从哪天<input id="filter-created_from" type="date"></label><label>到哪天<input id="filter-created_to" type="date"></label><label>标签分析状态<select id="filter-index_status"><option value="">全部状态</option><option value="pending">待分析</option><option value="processing">分析中</option><option value="ready">已完成</option><option value="failed">失败</option></select></label>`;
 $('dimensionEditors').innerHTML=Object.entries(materialDimensions).map(([d,l])=>`<label>${l}<input id="edit-${d}" maxlength="3030" placeholder="多个标签用逗号分隔"></label>`).join('');
 $('dimensionFilters').addEventListener('change',()=>loadLibrary(1));
 // Invalidate a pending response immediately, even before the user submits new text.
 $('librarySearch').addEventListener('input',()=>{libraryRequest++;});
 $('dimensionFilters').addEventListener('input',()=>{libraryRequest++;});
 $('materialTagForm').addEventListener('input',()=>{materialDirty=true;});
 $('copySearch').onclick=copyLibrarySearch;$('copyMaterial').onclick=copyMaterialReuse;
 $('resetLibraryFilters').onclick=()=>{$('librarySearch').reset();for(const e of $('dimensionFilters').querySelectorAll('input,select'))e.value='';loadLibrary(1);};
 $('materialDialog').addEventListener('cancel',e=>{if(materialSaving||materialDirty&&!confirm('标签尚未保存，确定关闭并放弃修改吗？'))e.preventDefault();});
 $('retryIndex').onclick=()=>withButton($('retryIndex'),async()=>{if(!selectedMaterial)return;try{await api('/api/library/'+selectedMaterial.id+'/retry-index',{});$('materialAnalysisStatus').textContent='已排队；只有启用分析服务才会提取标签，不会生成媒体。';}catch(e){toast(e.message,true);}});
 $('openIndexSettings').onclick=async()=>{try{const cfg=await api('/api/library/config');$('analysisEnabled').checked=cfg.analysis_enabled;$('analysisMode').value=cfg.analysis_mode||'vision';$('videoFrames').value=cfg.video_frames||4;$('imageMaxSide').value=cfg.image_max_side||1024;for(const n of ['primary','fallback']){const p=cfg[n];for(const k of ['enabled','bypass_proxy'])$(n+'-'+k).checked=p[k];for(const k of ['base_url','model','timeout_seconds'])$(n+'-'+k).value=p[k];$(n+'-api_key').value='';$(n+'-api_key').placeholder=p.key_configured?'已保存；留空保留原密钥':'输入分析服务密钥';}$('indexSettingsStatus').textContent='保存配置不等于分析成功。启用后会调用所配置模型，可能产生费用。';$('indexSettings').showModal();}catch(e){toast(e.message,true);}};
 $('indexSettingsForm').onsubmit=async e=>{e.preventDefault();await withButton(e.submitter,async()=>{const cfg={analysis_enabled:$('analysisEnabled').checked,analysis_mode:$('analysisMode').value,video_frames:Number($('videoFrames').value),image_max_side:Number($('imageMaxSide').value)};for(const n of ['primary','fallback']){cfg[n]={};for(const k of ['enabled','bypass_proxy'])cfg[n][k]=$(n+'-'+k).checked;for(const k of ['base_url','model','api_key'])cfg[n][k]=$(n+'-'+k).value.trim();cfg[n].timeout_seconds=Number($(n+'-timeout_seconds').value);}try{await api('/api/library/config',cfg);for(const n of ['primary','fallback'])$(n+'-api_key').value='';$('indexSettingsStatus').textContent='配置已保存。'+(cfg.analysis_enabled?'后台将按所选方式分析排队素材；不会生成媒体。':'自动分析关闭，手动标签与检索仍可使用。');}catch(e){$('indexSettingsStatus').textContent=e.message;}});};
}
