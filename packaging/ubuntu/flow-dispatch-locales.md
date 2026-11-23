# Flow 账号派发自检

在 Flow 安装目录的 `console-data/dispatch-locales.json` 中按账号的本地浏览器配置名称设置语言和时区。默认安装目录为 `~/.local/share/studiowright-evaluation`。以下为示例，请使用账号实际批准的设置。

```json
{
  "account-example": {
    "languages": ["zh-CN", "zh"],
    "timezone": "Asia/Shanghai"
  }
}
```

语言按 Chrome 设置中的顺序核对，时区使用 Ubuntu 当前配置的 IANA 时区名称。缺少配置、浏览器语言或系统时区不匹配，以及扩展或代理自检失败时，账号的图片和视频任务会留在队列。

后台在核对登录或检查已连接的 Flow 页面时遇到异常活动提示，会先尝试保存截图，再停派该账号。在工作台账号卡片上核查后可手动解除这条停派记录。其他派发限制仍会逐项核对。

## 可选在线自检

`preflight.dispatch_check(root, profile, online=True)` 先通过离线检查，再读取已运行的本机 Chrome。在线检查从已有 Flow 标签页或空白标签页读取 `navigator.webdriver`、UA、屏幕参数、Canvas 和 WebGL。`navigator.webdriver` 必须为 `false`，四项信号的组合摘要必须与基线一致。运行中的浏览器不可访问、信号不全或基线缺失时，在线自检失败。现有派发入口使用默认离线模式。

基线保存为 `console-data/fingerprint-baseline.json`，键为本地浏览器配置名称，值为 64 位小写 SHA-256 摘要。核查可信会话后，可在 Ubuntu 的 Flow Python 环境中调用 `preflight.collect_browser_fingerprint(root, profile)`。返回值为 `(摘要, 错误代码)`，只有错误代码为空时才可人工登记摘要。检查过程不会自动登记或更新基线。异步调用方可通过 `asyncio.to_thread` 调用这些同步函数。
