'use strict';
const creditNumber=n=>typeof n==='number'&&Number.isFinite(n)?new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(n):'—';
function renderAccountInsights(s){
 if(!s){for(const id of ['healthTotal','poolCredits','yesterdayCredits','availableCredits'])$(id).textContent='—';$('insightStatus').textContent='统计正在读取，尚未取得的数据不会显示为 0。';return;}
 const c=s.health_counts;
 $('healthTotal').innerHTML=`${s.healthy_accounts} <em>/ ${s.total_accounts}</em>`;
 $('healthDetail').textContent=`${c.ready} 个可用 · ${c.cooling} 个冷却 · ${c.attention} 个待检查 · ${c.empty} 个余额不足`;
 $('poolCredits').textContent=creditNumber(s.pool_balance);
 $('poolCreditDetail').textContent=`${s.balance_known_accounts}/${s.total_accounts} 个账号已读余额 · ${s.balance_fresh_accounts} 个近期更新`;
 const y=s.yesterday;
 $('yesterdayCredits').textContent=y.recorded_usage===null?'未记录':creditNumber(y.recorded_usage)+(y.unpriced||y.unresolved?' +':'');
 $('yesterdayDetail').textContent=y.coverage==='not_recorded'?'昨日没有覆盖记录，无法补算实际消耗':`${y.completed} 个完成任务 · ${y.unpriced} 个未记价格${y.unresolved?' · '+y.unresolved+' 个结果待确认':''}${y.coverage==='partial'?' · 数据不完整':''}`;
 $('availableCredits').textContent=creditNumber(s.available_today);
 $('availableDetail').textContent=`当前 ${s.healthy_accounts} 个可用账号的余额；冷却和待检查账号未计入`;
 $('insightStatus').textContent=`${s.refreshing?'正在刷新现有账号窗口的余额…':'每 5 分钟只读更新余额。'} 北京时间 ${s.today_date}；今日已记录用量 ${creditNumber(s.today.recorded_usage)}${s.today.unpriced||s.today.unresolved?'（存在未记价格或待确认任务）':''}。${s.balance_oldest_observed?'余额最早读取于 '+date(s.balance_oldest_observed)+'。':''}`;
 $('insightDefinition').textContent='健康度按线上身份、最近登录记录、冷却及余额判断，不保证每次生成成功。总积分为已读账号的剩余余额合计；当前可用排除冷却、登录待检查及余额过期的账号。用量按完成任务记录价格统计，不是 Google 实扣账单；不把报价、失败任务当作已消费。';
 $('refreshCredits').disabled=!!s.refreshing;$('refreshCredits').textContent=s.refreshing?'余额更新中…':'刷新账号余额';
 $('insightRows').innerHTML=s.accounts.map(a=>`<tr><td>${esc(a.email||a.profile)}</td><td><span class="badge ${a.health==='ready'?'verified':a.health==='cooling'?'cooling_down':'attention'}">${({ready:'可用',cooling:'冷却中',attention:'待检查',empty:'积分不足'})[a.health]}</span><small>${esc(a.reason)}</small></td><td>${creditNumber(a.balance)}${a.balance!==null&&!a.balance_fresh?' <small>上次余额</small>':''}</td><td>${esc(date(a.balance_observed))}</td></tr>`).join('')||'<tr><td colspan="4">尚未登记账号</td></tr>';
}
function setupAccountInsights(){
 $('refreshCredits').onclick=()=>withButton($('refreshCredits'),async()=>{try{const result=await api('/api/accounts/credits/refresh',{});$('insightStatus').textContent=result.message;await refresh(true);}catch(e){$('insightStatus').textContent=e.message;}});
}
