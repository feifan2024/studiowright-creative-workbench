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
