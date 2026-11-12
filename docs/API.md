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

需要关闭某条 Gemini 文字请求的自动重试时，可在请求体传 `"retry_policy": "never"`。同一请求编号重查时保持参数一致。

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

### 带参考素材

**`POST /api/real-pool/flow-references/auto/{素材 UUID}/{扩展名}`** · 逐个上传文件原始字节，返回 reference_id。上传、校验和提交使用同一个 Idempotency-Key，不填账号或项目。

| 模式 | 在 params 中添加 |
| --- | --- |
| 图生图 | `mode: "image-to-image"` 和 `reference_ids`（图片编号数组） |
| Veo / Omni 1.1、1.2 首帧或首尾帧 | `mode: "frames"`、`start_reference_id`，可选 `end_reference_id`（图片编号） |
| Omni 混合参考 | `model: "omni-flash"`、`mode: "ingredients"` 和 `reference_ids`（图片、MP4 编号数组） |

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
