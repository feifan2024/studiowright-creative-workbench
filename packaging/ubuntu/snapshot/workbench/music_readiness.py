"""Repair only active, expired music sessions; never enables accounts or generates."""
from datetime import datetime, timezone
import threading


def ready(row):
    if not row.get('is_active'):
        return False
    try:
        expiry = datetime.fromisoformat(str(row.get('at_expires', '')).replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            return False
        return expiry > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


class MusicReadiness:
    def __init__(self, call, token, busy):
        self.call, self.token, self.busy = call, token, busy
        self.lock = threading.Lock()
        self.result = {'state': 'idle', 'items': [], 'message': ''}

    def status(self):
        with self.lock:
            return {**self.result, 'items': [dict(item) for item in self.result['items']]}

    def start(self):
        with self.lock:
            if self.result['state'] == 'running':
                return {**self.result, 'items': list(self.result['items'])}
            self.result = {'state': 'running', 'items': [], 'message': '正在检查未就绪音乐账号…'}
        threading.Thread(target=self.run, daemon=True, name='music-readiness').start()
        return self.status()

    def run(self):
        items = []
        try:
            token = self.token()
            rows = self.call('GET', '/api/tokens', token=token)
            for original in rows:
                ident = original.get('id')
                if type(ident) is not int or ident <= 0:
                    continue
                item = {'id': ident, 'email': original.get('email', ''), 'result': ''}
                if not original.get('is_active'):
                    item['result'] = '已停用，保持原状'
                elif ready(original):
                    item['result'] = '已就绪，已跳过'
                elif self.busy():
                    item['result'] = '有任务运行，已跳过'
                else:
                    # Re-read immediately before mutation: another refresh may have finished.
                    latest = next((row for row in self.call('GET', '/api/tokens', token=token)
                                   if row.get('id') == ident), None)
                    if latest is None or not latest.get('is_active'):
                        item['result'] = '账号已停用或移除，已跳过'
                    elif ready(latest):
                        item['result'] = '已就绪，已跳过'
                    elif self.busy():
                        item['result'] = '有任务运行，已跳过'
                    else:
                        try:
                            self.call('POST', f'/api/tokens/{ident}/refresh-at', body={}, token=token)
                            self.call('POST', f'/api/tokens/{ident}/test', body={}, token=token)
                            checked = next((row for row in self.call('GET', '/api/tokens', token=token)
                                            if row.get('id') == ident), {})
                            item['result'] = '续期与认证成功' if ready(checked) else '仍未就绪，需要核对授权'
                        except (OSError, ValueError, TimeoutError):
                            item['result'] = '自动修复未完成，需要重新授权或人工核对'
                items.append(item)
                with self.lock:
                    self.result['items'] = list(items)
            result = {'state': 'completed', 'items': items,
                      'message': '检查完成。已就绪和已停用账号保持原状；未创建生成任务。'}
        except Exception:
            result = {'state': 'failed', 'items': items, 'message': '音乐服务或任务状态暂不可读取，已停止修复，请稍后重试。'}
        with self.lock:
            self.result = result
