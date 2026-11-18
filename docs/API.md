# 创作工作台 API

工作台只按请求类型分流：对话、推理、识图走 Gemini；生图、生视频走 Flow。不会根据正文改派，也不会自动串联两条路线。两条路线使用同一把工作台密钥，无需 Google API 密钥。

## 请求准备

调用地址是 `http://127.0.0.1:18922`。远程连接时需保持工作台的端口转发。请求带 `Authorization: Bearer <工作台密钥>`；JSON 用 `application/json`，文件原始字节用 `application/octet-stream`。

提交对话、识图或生成前，用 `Idempotency-Key` 传入并保存一个 UUID。超时或返回 `unknown` 时用原编号查询。

**`GET /api/snapshot`** · 用 flow_models 取模型 ID，用 tasks 查任务状态和下载路径。

**Python 公共准备 · 先设置 STUDIOWRIGHT_API_KEY**

```python
import json, os
from pathlib import Path
from uuid import uuid4
from urllib.request import Request, build_opener, ProxyHandler

BASE = "http://127.0.0.1:18922"
AUTH = {"Authorization": "Bearer " + os.environ["STUDIOWRIGHT_API_KEY"]}
HTTP = build_opener(ProxyHandler({}))

def call(path, payload=None, request_id=None, *, file=None):
    headers = dict(AUTH)
    body = None
    if request_id:
        headers["Idempotency-Key"] = request_id
    if file is not None:
        headers["Content-Type"] = "application/octet-stream"
        body = Path(file).read_bytes()
    elif payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    with HTTP.open(Request(BASE + path, data=body, headers=headers), timeout=180) as res:
        return json.load(res)
```

## Gemini · 对话与推理

可选参数 `effort` 适用于文字对话和识图（含只校验接口）。当前模型 Gemini 3.8 Flash 经线上 CLI 列表核实支持 `low`（低）、`medium`（中）、`high`（高），分别使用对应后缀的模型 ID。不传、null、空字符串或任何不支持的值/类型都回退 `medium`，不会因等级值报错。每轮请求单独选择；本轮未传仍使用中等，不继承上一轮的高等级。自动重试沿用原请求等级，不换等级。请求详情 `model` 回显实际选择；同一请求编号变更有效等级会产生参数冲突，应为新一轮使用新编号。

文字示例：
```python
chat = call("/api/conversations", {"title": "高思考等级示例"})
print(call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto",
    "prompt": "请分析这个方案的优缺点。", "effort": "high"
}, str(uuid4())))
```
识图时在原请求体加入 `"effort": "low"` 或 `"effort": "high"` 即可；上传方式保持不变。省略该字段使用中等。只校验接口也接受该字段，并按同样规则回退。


容量不足的受控重试：文字和识图业务仅在收到明确的容量不足 503 结束回执、且没有任何回复时，使用原账号和原请求编号，先等待 10 秒、再等待 30 秒，最多新增两次尝试。部分回复、结果未知、普通网络错误不触发此策略；`retry_policy: never` 禁止后台重试。每次重试前复查账号暂停、维护和请求状态；不换账号，不重放历史任务。验活和自动素材分析沿用原有规则。

若 CLI 曾报告容量不足 503，但后续在同一原生对话中给出明确的完整回复结束标记，且流式文字与最终全文逐字一致，后台确认 `completed`，并在说明及原始调用回执保留“早期容量503已恢复”的记录。仅有文字、没有完整结束证据，或出现后续错误、对话不符、进程中止时，不据此确认成功。历史记录的核对恢复不会创建新请求或增加尝试次数。

Gemini CLI 账号池默认总并发 20，与 Flow 的 1～5 并发设置独立；超出的请求排队。同一账号可执行多个独立对话，同一对话仍逐轮执行。20 是本系统调度上限，不代表供应商承诺的稳定并发。

文字和识图的单次模型等待默认最多 600 秒；连续 240 秒没有收到文字进展时停止等待，保留已收到的部分回复和未确认状态。排队时间另计；提交接口仍返回请求编号供轮询，不应将 HTTP 查询期限当成生成期限。

管理员可在工作台状态目录的 `gemini-execution.json` 配置 `text_total_seconds`、`image_total_seconds`、`idle_seconds`、`probe_total_seconds`、`probe_idle_seconds`，数值须为 1～3600 秒。业务默认 600/600/240 秒；验活默认总计 180 秒、无进展 120 秒。配置仅对下一次调用生效。

需要关闭某条 Gemini 文字或识图请求的自动重试时，可在请求体传 `"retry_policy": "never"`。两条通道均不再预留或发起额外尝试；该字段省略时沿用原有策略。同一请求编号重查时保持参数一致。

**`POST /api/conversations`** · 传 title，返回对话 id。

**`POST /api/real-pool/chat`** · 传 conversation_id、account: "auto"、prompt，返回 request_id 和 status。

接着问时沿用 `conversation_id`，为新消息生成新的请求编号。查回复用下方「结果与下载」中的 Gemini 查询接口。

仅在确认消息未发出、且遇到临时启动故障时，工作台才用原账号自动重试一次。已收到部分回复或结果不明时不会重发。用原请求编号查询，文字对话的 `attempts` 记录每次尝试的状态、原因和耗时。

**复制文字请求 · 实际发送**

```python
chat = call("/api/conversations", {"title": "我的对话"})
rid = str(uuid4())
print("请求编号:", rid, flush=True)
print(call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto", "prompt": "请用一句话介绍你自己。"
}, rid))
```

## Gemini · 上传图片并识别

**`POST /api/real-pool/uploads/{请求编号}/{序号}/{扩展名}`** · 传图片原始字节。序号从 0 开始；返回 index、extension、sha256。

**`POST /api/real-pool/image-analysis`** · 传 account: "auto"、prompt、attachments（上传回执数组），请求头沿用上传路径中的编号。返回 request_id、status 和 conversation_id。

可传 1～4 张 JPG、PNG、WebP，单张最多 8 MiB、合计 16 MiB、单张不超过 2400 万像素。只校验时用 `POST /api/real-pool/image-analysis-validate`，参数相同，返回 `valid`、`submitted: false` 和 `image_count`。

**复制识图请求 · 默认只校验**

```python
files = [Path("产品.png"), Path("场景.jpg")]
rid = str(uuid4())
print("请求编号:", rid, flush=True)
attachments = [
    call(f"/api/real-pool/uploads/{rid}/{i}/{f.suffix.lower().lstrip('.')}", file=f)
    for i, f in enumerate(files)
]
payload = {"account": "auto", "prompt": "请按顺序分别描述图片。", "attachments": attachments}
print(call("/api/real-pool/image-analysis-validate", payload, rid))
# 正式识图：print(call("/api/real-pool/image-analysis", payload, rid))
```

## Flow · 生成图片或视频

**源码更新（尚未部署到 Ubuntu）：** 启用前台输入的 Flow 会先输入最多 3 个字符，再一次粘贴剩余提示词。输入框聚焦后，输入和全文核对须在 3 秒内完成；超时不提交生成。这个时限不包含排队、页面准备或生成，也不是 API 请求超时。请求字段和调用方式保持不变。

**`POST /api/real-pool/flow`** · 传 kind、confirm: true 和 params。返回 request_id、status、task_id；submitted 表示已排队。

单图或单视频（`count: 1`，含参考素材）由平台统一管理失败后的重试，最多增加 3 次尝试。仅在后台确认符合重试条件时继续，结果不明时暂停核对。查询和下载始终使用原 `task_id`；每次尝试的账号、状态和失败原因在 `retry.attempts` 中记录。调用方不要传 `auto_retry`；当前实现忽略该字段，不能通过它关闭重试。

在 `params` 里填写 `prompt`、`model`、`aspect`、`count`、`resolution` 和 `duration`。模型 ID 从快照取；账号和项目由工作台安排。积分按 Flow 实际报价计费，平台不设单笔积分上限。`max_credits` 可省略；旧调用携带该字段仍可兼容，但不会形成有效上限。

**复制纯文字生成请求 · 实际提交**

```python
rid = str(uuid4())
print("请求编号:", rid, flush=True)
payload = {
    "kind": "image",  # 生视频时改为 video，并填写对应模型和时长
    "confirm": True,
    "params": {"prompt": "一张清晨海边的照片", "model": "填写快照中的模型 ID",
               "aspect": "16:9", "count": 1, "resolution": "720p",
               "duration": 8}
}
print(call("/api/real-pool/flow", payload, rid))
```

### 画风 ID（可选）

上游节点在 Flow 生图或生视频请求中传入可选的 1～256 字符字符串 `params.style_id`，例如 `"style_id": "ink-wash-v1"`。画风 ID 是素材管理字段；用于生成的画风描述仍放在 `prompt`。

- 有值时，Flow 按原字符串记录，保留大小写和符号，不从提示词猜测，不改写、不替换。字段已提供但类型错误或值为空时明确报错。**完全省略 `params.style_id` 时，照常接收任务、生成并自动入库**；任务和素材来源不补填画风 ID。已有节点无需修改请求。
- 任务详情通过 `GET /api/tasks/{任务编号}` 的 `style_id` 回显原值；查工作台请求记录时位于 `task.style_id`。自动重试、换账号和多份产物沿用同一值，每个产物入库时写入对应来源的 `origins[].style_id`。
- 同一媒体文件去重时，保留各来源独立的画风 ID。自动分析标签与画风 ID 分开存储，任何分析结果不覆盖上游传值。
- 验收标准：从同一次提交分别读取任务详情及素材来源，两处均与上游传值逐字相同；未传时不补填，去重及重试不丢值。

若连接的是旧版后台，请省略此字段。

### Omni 视频模型

| 界面名称 | API 的 `params.model` | 支持的 `params.duration`（秒） |
| --- | --- | --- |
| Omni 1.1 Flash | `omni-flash-1.1` | 4、6、8、10 |
| Omni 1.2 Flash | `omni-flash-1.2` | 3～15 的任意整数 |

新调用推荐使用带版本号的 ID。旧 ID `omni-flash` 和 `omni-flash-2` 仍可调用，模型快照也可能沿用旧 ID。

**Omni 1.2 Flash · 15 秒纯文字生成（实际提交，可能消耗积分）**

```python
rid = str(uuid4())
print("请求编号:", rid, flush=True)
payload = {
    "kind": "video",
    "confirm": True,
    "params": {"prompt": "清晨海边，镜头沿海岸线平稳前推",
               "model": "omni-flash-1.2", "aspect": "16:9",
               "count": 1, "resolution": "720p", "duration": 15}
}
print(call("/api/real-pool/flow", payload, rid))
```

### 带参考素材

**`POST /api/real-pool/flow-references/auto/{素材 UUID}/{扩展名}`** · 逐个上传文件原始字节，返回 reference_id。上传、校验和提交使用同一个 Idempotency-Key，不填账号或项目。

| 模式 | 在 params 中添加 |
| --- | --- |
| 图生图 | `mode: "image-to-image"` 和 `reference_ids`（图片编号数组） |
| Veo / Omni 1.1、1.2 首帧或首尾帧 | `mode: "frames"`、`start_reference_id`，可选 `end_reference_id`（图片编号） |
| Omni 1.1 Flash 混合参考 | `model: "omni-flash-1.1"`、`mode: "ingredients"` 和 `reference_ids`（图片、MP4 编号数组） |

参考文件支持 JPG、PNG、WebP 和上述模式允许的 MP4，不接收独立音频。单文件最多 8 MiB、合计 64 MiB，编号 24 小时有效。只校验时发送 `POST /api/real-pool/flow-validate` 并设 `confirm: false`，返回 `valid`、`submitted: false` 和 `reference_count`。

**复制参考素材请求 · 默认只校验**

```python
rid = str(uuid4())
print("请求编号:", rid, flush=True)
file = Path("首帧.png")
ext = file.suffix.lower().lstrip(".")
ref = call(f"/api/real-pool/flow-references/auto/{uuid4()}/{ext}",
           request_id=rid, file=file)
payload = {
    "kind": "video", "confirm": False,
    "params": {"prompt": "镜头平稳前推", "model": "veo-fast", "aspect": "16:9",
               "count": 1, "resolution": "720p", "duration": 8,
               "mode": "frames", "start_reference_id": ref["reference_id"]}
}
print(call("/api/real-pool/flow-validate", payload, rid))
# 正式提交：将 confirm 改成 True，再调用 /api/real-pool/flow，可能消耗生成积分。
```

## 结果与下载

**`GET /api/real-pool/requests/{请求编号}`** · 查询文字或识图。status 为 completed 时，response 是回复；处理中继续查，失败或未知时读 note。

**`GET /api/real-pool/flow-requests/{请求编号}`** · 查询提交记录。task_id 始终是原任务号，task 是最新执行结果。

**`GET /api/tasks/{原任务编号}`** · 直接查询最新状态和下载路径，换号重试后仍用原任务号。

**`GET /api/tasks/{任务编号}/files/{序号}`** · 下载生成文件。使用任务的 downloads[].path，带同一密钥，返回文件字节。

**复制查询与下载**

```python
# rid 为发送时保存的编号。查文字或识图：
# print(call("/api/real-pool/requests/" + rid))
record = call("/api/real-pool/flow-requests/" + rid)
print(record)
if record.get("task_id"):
    task = call("/api/tasks/" + record["task_id"])
    if task:
        print(task["status"])
        for item in task["downloads"]:
            with HTTP.open(Request(BASE + item["path"], headers=AUTH), timeout=180) as res:
                with open(Path(item["name"]).name, "xb") as output:
                    while chunk := res.read(65536):
                        output.write(chunk)
```

已入库素材：`GET /api/library?page=1&limit=24` 查列表，`GET /api/library/{素材ID}` 查详情，`GET /api/library/{素材ID}/file` 下载原文件。

错误通常返回 `{"error":"原因"}`。`401` 检查密钥，`400` 检查请求参数，`404` 核对路径或编号。`GET /health` 检查 API 是否可访问，无需密钥。

### 工作台音乐任务恢复

以下是工作台同源接口（18920 端口），沿用工作台的 Origin 和 `X-Workbench-Request: 1` 校验，不是统一 API 密钥端口的接口。

- `POST /api/music/jobs`：传入 `request_id`（标准 UUID）、`prompt`、`model`，返回 `202` 和 `job`。相同编号、相同内容只创建一次；同编号不同内容拒绝。
- `GET /api/music/jobs/{request_id}`：只读取原任务，不发起生成。
- `GET /api/music/jobs/latest`：读取最近任务，供页面首次恢复使用。

状态依次为 `generating`（生成中）、`ingesting`（结果已收到、正在入库）、`completed`。完成后的 `result` 保存音频地址和入库结果。`unknown` 表示结果待核对，不会自动重发。页面刷新、网络恢复仅查询原编号；后台服务重启时未完成任务保留为待核对记录，不会自动重复生成。旧同步接口 `/api/music/generate` 保留兼容，新页面使用持久化任务接口。
