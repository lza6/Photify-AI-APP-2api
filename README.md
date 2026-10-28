# photify-2api

Photify AI（`ai.photify.app` 3.0.8）逆向出的 OpenAI 兼容网关。

Photify 的能力本质是「一张人像 + 风格 prompt → AI 写真」，所以它更像
OpenAI 的 `/v1/images/edits`（图生图）而不是纯文生图。网关两种接口都提供。

## 现状（第一轮，2026-10-29）

| 项目 | 状态 |
| --- | --- |
| 鉴权链（匿名 Firebase → accessToken） | 已验证，可无限新建账号并长期复用 |
| 图片上传 / 身份图上传（人脸 traits 提取） | 已验证 |
| `images/standard` 真实出图（`photify/starter`） | **已验证：200 + 768×1152 JPEG** |
| `nano-banana/*` 等付费模型 | 已验证：无 premiumTokens 时上游明确 400 |
| mock 模式（本地合成图，用于压测/demo） | 已验证：24/24 成功，p50 2.4s@并发 8 |
| 上游并发行为 | 已发现问题：并发 3 时出现连接重置 / 144s 长尾，需串行或低并发 |

详细协议与证据见 `docs/REVERSE-REPORT.md`，接入 new-api 见 `docs/NEW-API-INTEGRATION.md`。

## 快速开始

```bash
pip install fastapi uvicorn pillow requests

# 1) 造一个匿名账号（term 里拿 refreshToken，写进 accounts.json）
python -c "from photify_client import PhotifyClient as C; c=C.create_anonymous(); print(c.account.refresh_token, c.account.firebase_uid)"

cp accounts.json.example accounts.json     # 把上面两个值填进去
set PHOTIFY_MODE=real
set PHOTIFY_IDENTITY_IMAGE=assets/default_identity.jpg   # 可选：默认人像
python server.py                            # 默认 0.0.0.0:8931
```

打开 `http://127.0.0.1:8931/demo` 就是 demo 页。

mock 模式（不碰上游，压测/演示用）：

```bash
set PHOTIFY_MODE=mock
python server.py
python bench.py --base http://127.0.0.1:8931 --concurrency 8 --total 24
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 模式与账号数 |
| GET | `/metrics` | 请求数 / 失败数 / p50·p95 延迟 |
| GET | `/v1/models` | 6 个对外模型名 + 上游 token 价 |
| POST | `/v1/images/generations` | OpenAI 风格；`image` 字段（base64）可选，缺省用账号缓存身份图 |
| POST | `/v1/images/edits` | 标准 multipart：`image` + `prompt` + `model` |
| POST | `/v1/chat/completions` | 最小兼容：user 消息当 prompt，返回 markdown 图片 |
| GET | `/admin/accounts` | 账号额度（photoTokens/videoTokens/premiumTokens） |
| GET | `/files/{name}` | 生成的图片 |
| GET | `/demo` | demo 页 |

## 模型

| 对外名 | 上游模型 | 上游价（tokens） | 实测 |
| --- | --- | --- | --- |
| `photify/starter` | Starter | 1 | **免费出图成功（账号 0 token）** |
| `photify/supreme` | Supreme | 1 | 需 premiumTokens |
| `photify/supreme-flux` | Supreme Flux | 1 | 未单独验证 |
| `photify/supreme-ultra` | Supreme Ultra | 2 | 需 premiumTokens |
| `photify/nano-banana` | Nano Banana | 5 | 需 premiumTokens |
| `photify/nano-banana-pro` | Nano Banana Pro | 10 | 未单独验证 |

## 已实现的工程约束

- 账号池轮询；**同一账号内「上传身份图 + 生成」串行加锁**（否则身份图会被并发覆盖）
- 上游 4xx 不重试、5xx 与网络抖动重试 2 次、401 自动重新登录一次
- OpenAI 错误体映射（`{"error":{message,type,code}}`）
- `asyncio.Semaphore` 并发闸门（`PHOTIFY_MAX_CONCURRENCY`，默认 4）
- 上游返回流式图片（`@Streaming ResponseBody`）直接落盘，按魔数判定 jpg/png

### 多人设：`photify/<persona>`

Photify 是「一张身份图 = 一张脸」，一个人设就得一个账号。在 `accounts.json` 里给账号命名：

```json
{"accounts": [
  {"refresh_token": "...", "firebase_uid": "...", "persona": "alice"},
  {"refresh_token": "...", "firebase_uid": "...", "persona": "bob"}
]}
```

网关会自动多暴露两个模型名 `photify/alice` / `photify/bob`，分别固定路由到对应账号
（未命名的账号继续参与轮询）。这样在 new-api 里可以「一个渠道一个人设」，用户在模型列表里
直接选人设，而不会每张图都是不同的人。

实测：`GET /v1/models` 返回 6 个内置模型 + 全部 persona；`photify/alice` 200，
未知 persona 400（错误信息里会列出可用项）。

## 身份图语义（实测，别踩的坑）

- `identityImageId` 参与生成，但 `gender / skinColor / hairColor / bodyType` 是**显式覆盖参数**：
  传错就会「换人」（实测：上传男性蓝皮肤身份图 + `gender=woman` → 出女性写真）。
- 所以网关在上传后自动采用 `upload_identity_image` 返回的 `identity_traits` 回填生成参数。
  客户端想强制风格可以显式传 traits 覆盖。

## 边界

- 不伪造广告回调、不伪造内购凭证：账号额度来自用户自己的账号
- `photify/starter` 当前实测不扣 token，但**上游是否设每日上限未验证**，不做高强度压测
- 上游为生产服务，默认低并发；压测请在 mock 模式下做

## 仓库内容与脱敏

仓库内**不含**任何账号凭证、生成产物与人像素材：

| 排除项 | 原因 |
| --- | --- |
| `accounts.json` | 含真实 refreshToken（`.gitignore` 已排除，仅保留 `.example` 模板） |
| `output/`、`assets/*.jpg` | 生成的写真与测试人像属于运行者私有数据 |
| `*.log`、`*.pid` | 运行期日志 |

`photify_client.py` 中的 `FIREBASE_WEB_API_KEY` 是 APK `res/values/strings.xml` 里公开的客户端常量
（与 Web 端 Firebase API Key 同性质，不是服务端密钥），保留它才能复现协议。

## 免责声明

本项目为 Android 客户端协议互操作性研究，用于自有账号在自有设备/自建网关上的自动化调用。
使用者需自行确保对目标服务的使用符合其服务条款与当地法律；作者不提供任何账号额度、
不绕过付费与广告校验，也不对第三方服务的中断或条款变更负责。
