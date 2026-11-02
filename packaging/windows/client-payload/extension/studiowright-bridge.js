/* StudioWright manual bridge; independent of the legacy Flow2API endpoint. */
let swSyncRunning = false;

async function swNative(action, config) {
    const result = await chrome.runtime.sendNativeMessage('com.studiowright.bridge', {action, config});
    if (!result?.ok) throw new Error(result?.error || '本机共享组件不可用。');
    return result.config;
}

async function swRequest(config, path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
        const response = await fetch(config.apiUrl + path, {
            method: body === undefined ? 'GET' : 'POST',
            headers: {'Authorization': 'Bearer ' + config.connectionToken,
                ...(body === undefined ? {} : {'Content-Type': 'application/json', 'X-Console-Request': '1'})},
            body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal
        });
        const result = await response.json();
        if (!response.ok) {
            const error = new Error(result.error || '后台未接收登录信息。');
            error.confirmedRejected = true;
            throw error;
        }
        return result;
    } catch (error) {
        if (error.name === 'AbortError') throw new Error('后台连接超时，请确认 StudioWright 后台已打开。');
        throw error;
    } finally {
        clearTimeout(timeout);
    }
}

function swNormalizeEmail(value) {
    const email = String(value || '').trim().toLowerCase();
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? email : '';
}

// Read the identity of the Chrome profile explicitly selected for this job.
// The page already has read-only access to its own local profile manager.
async function swExpectedFromJob(sender) {
    if (!Number.isInteger(sender?.tab?.id)) throw new Error('无法确认所选 Chrome 用户，已停止采集。');
    let results;
    try {
        results = await chrome.scripting.executeScript({
        target: {tabId: sender.tab.id}, world: 'MAIN',
        func: async () => {
            if (!['http://127.0.0.1:18888', 'http://localhost:18888'].includes(location.origin)) return null;
            const match = /^#chrome-job=([A-Za-z0-9_-]{40,60})$/.exec(location.hash);
            if (!match) return null;
            const base = 'http://127.0.0.1:18890';
            const jobResponse = await fetch(base + '/api/job/' + match[1], {cache:'no-store'});
            if (!jobResponse.ok) return null;
            const job = await jobResponse.json();
            if (job.status !== 'running' || !/^(Default|Profile \d+)$/.test(job.profile || '')) return null;
            const listResponse = await fetch(base + '/api/profiles', {cache:'no-store'});
            if (!listResponse.ok) return null;
            const list = await listResponse.json();
            const selected = (Array.isArray(list.profiles) ? list.profiles : []).filter(p => p.profile === job.profile);
            return selected.length === 1 ? selected[0].email : null;
        }
        });
    } catch (error) {
        throw new Error('无法核实当前采集任务和所选 Chrome 用户，已停止采集。');
    }
    const expected = swNormalizeEmail(results[0]?.result);
    if (!expected) throw new Error('无法核实所选 Chrome 用户的邮箱，已停止采集。');
    return expected;
}

async function swFindFlowAccount(expected) {
    const active = await chrome.tabs.query({active:true, lastFocusedWindow:true, url:'https://flow.google.com/*'});
    if (active.length === 1) {
        let email;
        try {
            email = swNormalizeEmail(await readFlowAccount(active[0].id));
        } catch (error) {
            if (/Cannot access contents of the page|showing error page/i.test(String(error.message || error))) {
                throw new Error('当前 Flow 页面未正常加载，请打开该账号的 Flow 页面后再手动采集。');
            }
            throw error;
        }
        if (expected && email !== expected) throw new Error('当前 Flow 邮箱与所选 Chrome 用户不一致，已停止采集。');
        if (!email) throw new Error('当前 Flow 页面的邮箱无法确认，已停止采集。');
        return email;
    }
    if (!expected) throw new Error('请先切到要同步账号的 Flow 页面，再使用插件同步。');
    const tabs = await chrome.tabs.query({url:'https://flow.google.com/*'});
    const found = new Set();
    for (const tab of tabs) {
        try {
            const email = swNormalizeEmail(await readFlowAccount(tab.id));
            if (email) found.add(email);
        } catch (error) {
            // A browser error page cannot contain a usable Flow identity.
            if (/Cannot access contents of the page|showing error page/i.test(String(error.message || error))) continue;
            throw error;
        }
    }
    if (found.has(expected)) return expected;
    if (found.size) throw new Error('Flow 页面邮箱与所选 Chrome 用户不一致，已停止采集。');
    throw new Error('所选 Chrome 用户的 Flow 页面未正常加载或未登录，已停止采集。');
}

async function swSync(expectedEmail = '') {
    if (swSyncRunning) throw new Error('此窗口正在同步，请等待结果。');
    swSyncRunning = true;
    try {
        const config = await swNative('sw_get');
        if (!config) throw new Error('请先打开 StudioWright 后台，在「账号与项目」点击「连接本机插件」。');
        if (config.apiUrl !== 'http://127.0.0.1:18888/api/plugin/session') throw new Error('连接地址不正确，请重新连接本机插件。');
        let email = '';
        const pages = await chrome.tabs.query({url:'https://flow.google.com/*'});
        if (!pages.length && expectedEmail) {
            await chrome.tabs.create({url:'https://flow.google.com/', active:false});
            for (let i = 0; i < 20; i++) {
                await new Promise(resolve => setTimeout(resolve, 1500));
                try { email = await swFindFlowAccount(expectedEmail); break; }
                catch (error) { if (/不一致/.test(String(error.message))) throw error; }
            }
            if (!email) throw new Error('Flow 页面已打开，但尚未确认所选账号登录；请手动检查页面。');
        } else {
            email = await swFindFlowAccount(expectedEmail);
        }
        const allowed = new Set(['google.com', 'flow.google.com', 'accounts.google.com', 'www.google.com']);
        const all = await chrome.cookies.getAll({domain:'google.com'});
        const cookies = all.filter(c => allowed.has(c.domain.replace(/^\./,'')) && (!c.expirationDate || c.expirationDate > Date.now()/1000))
            .map(c => ({name:c.name,value:c.value,domain:c.domain,path:c.path,secure:c.secure,httpOnly:c.httpOnly,
                hostOnly:c.hostOnly,sameSite:c.sameSite,expirationDate:c.expirationDate,partitionKey:c.partitionKey}));
        if (!cookies.length) throw new Error('当前 Chrome 用户未找到 Flow 登录 Cookie。');
        let result;
        try {
            result = await swRequest(config, '', {email, cookies});
        } catch (error) {
            if (error.confirmedRejected) throw error;
            throw new Error('提交后连接中断，结果未确认；请先查看 StudioWright 任务队列，不要重复采集。');
        }
        if (typeof result?.id !== 'string' || !result.id) {
            throw new Error('请求结果尚未确认，请先查看 StudioWright 任务队列，不要重复采集。');
        }
        await chrome.storage.local.set({swLast:{status:'pending',email,id:result.id,time:Date.now(),message:'已接收，正在 Ubuntu 核对实际邮箱。'}});
        for (let i = 0; i < 70; i++) {
            await new Promise(resolve => setTimeout(resolve, 1500));
            let state;
            try {
                state = await swRequest(config, '/' + result.id);
            } catch (error) {
                throw new Error('请求已提交但核对中断，结果未确认；请先查看 StudioWright 任务队列，不要重复采集。');
            }
            if (state.status === 'succeeded') {
                if (swNormalizeEmail(state.email) !== email) {
                    throw new Error('Ubuntu 核对的邮箱与所选账号不一致，不能报告同步成功，请检查任务队列。');
                }
                const message = 'Ubuntu 登录核对成功：' + state.email;
                await chrome.storage.local.set({swLast:{status:'success',email:state.email,time:Date.now(),message}});
                return {ok:true, message};
            }
            if (['failed','interrupted','cancelled'].includes(state.status)) throw new Error(state.error || 'Ubuntu 未能确认登录，请使用后台的远程登录。');
        }
        return {ok:true, message:'后台仍在检查，结果请在 StudioWright 任务队列查看。'};
    } catch (error) {
        await chrome.storage.local.set({swLast:{status:'error',time:Date.now(),message:error.message}});
        throw error;
    } finally {
        swSyncRunning = false;
    }
}

chrome.runtime.onMessage.addListener((request, sender, respond) => {
    if (request?.action !== 'sw_sync' && request?.action !== 'sw_status') return false;
    const promise = request.action === 'sw_sync' ? swSync() :
        Promise.all([swNative('sw_get'), chrome.storage.local.get('swLast')]).then(([config,data]) => ({ok:true,connected:!!config,last:data.swLast}));
    promise.then(respond).catch(error => respond({ok:false,error:error.message}));
    return true;
});

chrome.runtime.onMessageExternal.addListener((request, sender, respond) => {
    if (!['sw_pair','sw_probe','sw_sync'].includes(request?.action)) return false;
    if (!/^http:\/\/(127\.0\.0\.1|localhost):18888\/(?:[?#].*)?$/.test(sender.url || '')) {
        respond({ok:false,error:'仅允许本机 StudioWright 后台连接。'});
        return false;
    }
    if (request.action === 'sw_probe') { respond({ok:true,version:'1.0.0'}); return false; }
    const promise = request.action === 'sw_sync' ? swExpectedFromJob(sender).then(expected => swSync(expected)) :
        swNative('sw_set',request.config).then(() => ({ok:true}));
    promise.then(respond).catch(error => respond({ok:false,error:error.message}));
    return true;
});
