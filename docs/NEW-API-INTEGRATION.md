# photify-2api × new-api 对接指南

> 依据：`server.py` 实际实现 + 本轮真实出图/压测结果（2026-10-29）
> 目标渠道类型：**OpenAI（type = 1）**

---

## 0. 先明确一件事：Photify 是「图生图」

Photify 的生成永远需要一张**身份图**（人像）作为输入，服务端会先做人脸解析（gender / hair_color /
skin_color），再用 prompt 生成写真。它不是纯文生图。

因此本网关有两种用法：

| 用法 | 走哪个接口 | 人脸来源 | 适用 |
| --- | --- | --- | --- |
| A. 每用户自带人像 | `POST /v1/images/edits`（multipart） | 请求里的 `image` 文件 | 多用户、真实产品 |
| B. 固定人像池 | `POST /v1/images/generations`（JSON） | 账号缓存的 `identity_image_id` | 单机演示、批量同一人设 |

**new-api 的 OpenAI 图片转发默认只走 `POST /v1/images/generations`（JSON）**，属于用法 B。
如果你的用户要上传自己的人像，需要客户端/渠道透传 multipart（见 §4 方案 A′）。

---

## 1. 部署网关

```bash
cd photify-2api
pip install fastapi uvicorn pillow requests

python -c "from photify_client import PhotifyClient as C; c=C.create_anonymous(); print(c.account.refresh_token, c.account.firebase_uid)"
# 把这两个值填进 accounts.json

copy accounts.json.example accounts.json

set PHOTIFY_MODE=real
set PHOTIFY_IDENTITY_IMAGE=assets/default_identity.jpg
set PHOTIFY_MAX_CONCURRENCY=2
python server.py        # 默认 0.0.0.0:8931
```

自检：

```bash
curl http://127.0.0.1:8931/health
curl http://127.0.0.1:8931/admin/accounts     # 看三个 token 余额
curl -X POST http://127.0.0.1:8931/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"photify/starter","prompt":"editorial portrait, soft light","response_format":"url"}'
```

---

## 2. new-api 渠道配置

| 字段 | 值 |
| --- | --- |
| 类型 | OpenAI（`1`） |
| 名称 | `photify-2api` |
| Base URL | `http://<网关内网地址>:8931`（**不带 `/v1`**，new-api 会自己拼 `/v1/images/generations`） |
| 密钥 | 任意非空字符串（网关不校验，占位如 `sk-photify-local`） |
| 模型 | `photify/starter`、`photify/supreme`、`photify/supreme-ultra`、`photify/nano-banana` … |
| 模型重定向 | 按需把 `gpt-image-1` → `photify/starter` 之类的别名做映射 |

> ⚠️ new-api 的「测试」按钮打的是 `POST /v1/chat/completions`。网关已实现该端点（把最后一条
> user 消息当 prompt，返回 markdown 图片），所以**测试按钮应返回 200**；若返回 4xx，看网关日志里的
> 上游文案（额度/模型名）。

---

## 3. 模型与计费（重要）

| 模型 | 上游实际成本 | 建议 new-api 定价 |
| --- | --- | --- |
| `photify/starter` | 本轮实测**不扣 token**（连续 4 次成功，`photoTokens` 恒为 0） | 按次固定价（如 1500 quota/次 ≈ ¥0.003），或 `ModelPrice` 极小值 |
| `photify/supreme` / `supreme-ultra` | 需 `premiumTokens`，实测 400 额度不足 | 未购入额度前**不要对外暴露**（会 400） |
| `photify/nano-banana` / `-pro` | 5 / 10 premiumTokens | 同上 |

**必须手工配价**：new-api 对未登记的模型名会走默认 `ModelRatio`，图片类请求会按 token 估算导致
「暴扣」或「扣 0」。在 `设置 → 运营设置 → 模型固定价格 / ModelRatio` 里显式给这几个模型定价。

---

## 4. 两种接法

### 方案 A′：让用户上传自己人像（推荐）

new-api 侧需要能透传 multipart 到 `/v1/images/edits`（部分 fork / 自建客户端支持）。
网关该端点的语义与 OpenAI 一致：

```bash
curl -X POST http://127.0.0.1:8931/v1/images/edits \
  -F "image=@selfie.jpg" \
  -F "prompt=cinematic portrait, neon street at night" \
  -F "model=photify/starter" \
  -F "response_format=url"
# → {"created":…, "data":[{"url":"/files/xxxx.jpg"}]}
```

`n` 只支持 1（上游一次一张），传 `n>1` 直接 400。

若不支持 multipart 透传：让前端先直连网关的 `/v1/images/edits`，再把结果 URL 回传到业务侧。

### 方案 B：固定人像池（只改渠道配置即可）

1. 每个账号先在网关里绑一张身份图：`accounts.json` 的 `identity_image_id`，或提供
   `PHOTIFY_IDENTITY_IMAGE` 让网关首次自动上传；
2. 用户请求只给 `prompt`，new-api 走 `/v1/images/generations`：

```json
{"model":"photify/starter","prompt":"…","response_format":"url","size":"1024x1024"}
```

1 个账号 = 1 张固定人脸。**要把「N 个不同人设」卖给用户，就配 N 个账号**，网关按轮询分摊
（同账号内部已加锁串行，避免身份图互相覆盖）。

---

## 5. 容量与超时

| 项 | 实测值 | 建议 |
| --- | --- | --- |
| 单次出图延迟 | 常态 5–13s，长尾出现过 144s | 渠道超时 ≥ 180s，客户端超时 ≥ 200s |
| 单账号并发 | 并发 3 时上游出现 `Connection reset` | `PHOTIFY_MAX_CONCURRENCY=1~2`／账号 |
| 扩容方式 | 加账号，而不是加并发 | 每账号一张身份图，轮询分摊 |
| 图片体积 | ≈ 60–85 KB / 768×1152 JPEG | 网关按魔数落盘，无需转码 |

---

## 6. 错误映射

| 上游 | 网关返回 | 含义 |
| --- | --- | --- |
| `400 Not enough premium tokens` | 400 `upstream_error` | 该模型要 premium 额度，换 `photify/starter` 或充值 |
| `400 Image not found` | 400 | 身份图 id 失效/未就绪（见逆向报告 §11） |
| `400 Image processing error` | 400 | 上传的不是可用人像（服务端人脸校验） |
| 5xx / 连接重置 | 网关重试 2 次后 502 | 上游不稳，降并发 |
| 401 | 网关自动重新登录一次 | accessToken 过期（24h） |

---

## 7. 上线前检查清单

- [ ] `accounts.json` 已填 refreshToken，且**不在版本库里**（`.gitignore` 已排除）
- [ ] `/admin/accounts` 三个余额符合预期
- [ ] `PHOTIFY_MAX_CONCURRENCY` 与账号数匹配（≤ 账号数 × 2）
- [ ] new-api 里手工配了模型价格，未走默认 ratio
- [ ] 只把 `photify/starter` 暴露给用户（其余需 premium 额度）
- [ ] 网关放在内网 / 加 IP 白名单（本身无鉴权）
