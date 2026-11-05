# 创作工作台 API

统一入口：`http://127.0.0.1:18922`。在工作台「API 文档 → 密钥管理」创建或复制密钥，调用时发送 `Authorization: Bearer <密钥>`。程序可从 `STUDIOWRIGHT_API_KEY` 环境变量读取密钥。密钥管理在工作台完成；撤销会拒绝后续调用，已经提交的任务可能继续执行。

接口仅监听本机。跨机器时使用已有的 SSH 转发，统一 API 不接受浏览器跨域调用。工作台页面提供可一键复制的 Python 示例。

| 接口 | 用途 |
| --- | --- |
| GET /health | 检查统一 API 服务，无需密钥。 |
| GET /api/snapshot | 读取账号、项目、可用模型及最近任务。 |
| GET /api/conversations | 列出对话。 |
| POST /api/conversations | 创建对话，传 `title`。 |
| GET /api/conversations/{id} | 查看对话及消息记录。 |
| POST /api/real-pool/chat | 发送官方文字对话，传 `conversation_id`、`prompt` 与 `account: "auto"`，并提供请求编号。后续轮次继续使用原对话和绑定账号。 |
| GET /api/real-pool/requests/{请求编号} | 查询 Gemini 处理进度和回复。 |
| POST /api/real-pool/flow | 提交文生图或文生视频，并提供请求编号。 |
| GET /api/real-pool/flow-requests/{请求编号} | 查询 Flow 提交记录及原任务编号。 |
| GET /api/tasks/{任务编号}/files/{序号} | 下载已完成任务的文件；以快照的 `downloads[].path` 为准。 |
| GET /api/library?page=1&limit=24 | 查询已入库素材。 |
| GET /api/library/{素材ID} | 读取素材详情与标签。 |
| GET /api/library/{素材ID}/file | 下载素材原文件。 |

Flow 请求传 `kind`（`image` 或 `video`）、`confirm: true` 和 `params`，参数包括 `prompt`、`model`、`aspect`、`count`、`resolution`、`duration`、`max_credits`。模型 ID 从快照读取；视频按允许的积分上限提交。可省略 `project_key` 由工作台安排同账号项目，或传完整项目 key。当前 `auto_retry` 只能省略或设为 `false`。

发送对话和生成任务时，先保存 `Idempotency-Key` 请求头中的 UUID。Flow 的 `submitted` 表示已交给后台排队。遇到超时、断线或 `unknown`，先用原 UUID 查询；不要换新编号重复发送。

素材的基础分类在 `structured_tags`，可用的补充镜头标签在 `shot_tags`。Gemini 对运镜、构图和拍摄角度的文字说明在工作台素材详情查看或补充，统一 API 不开放补分析。
