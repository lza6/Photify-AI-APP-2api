# Photify AI 3.0.8 逆向报告（第一轮）

> 分析对象：`Photify_AI_3.0.8_ProUltra_Mod.apk`（74.6 MB）
> 分析目录：`D:/apk-analysis/photify/`
> 时间：2026-10-29
> 结论级别标注：**已验证**（实际请求观察）/ **静态确认**（读 smali 确认）/ **合理推断** / **待验证**

---

## 1. 结论摘要

Photify 是一个「一张人像 → AI 写真」的商业化 App（官网 `photify.ai`，运营方 Ex-Human Inc.），
有自建后端 `https://api.photify.ai/photify/v1/`，**协议明文、无请求签名、无证书固定**，
鉴权只有一条 `Authorization: Bearer <accessToken>`。

三个关键发现：

1. **鉴权可直接绕过社交登录**：App 用 Firebase 认证后把 `{userId, token}` 发给 `auth/login` 换自有
   accessToken。Firebase 项目 `photify-ai` 的 Web API Key 硬编码在 `res/values/strings.xml`，
   且**匿名登录（accounts:signUp）是开启的** → 不需要 Google/Apple 账号即可拿到合法 accessToken，
   且 `refreshToken` 可长期复用（等价于一个可持久化的免费账号）。**已验证**
2. **`images/standard` 是同步流式出图**：`@Streaming` 直接返回图片二进制（实测 768×1152 JPEG），
   无任务轮询、无回调，链路上比同类 App 简单得多。**已验证**
3. **`Starter` 模型当前不扣 token**：账号 `photoTokens=0` 仍连续出图成功 4 次。**已验证**，
   但上游是否有每日/总量上限**待验证**（未做高强度试探）。

---

## 2. 产物信息

| 项 | 值 |
| --- | --- |
| 包名 | `ai.photify.app` |
| versionName / Code | `3.0.8` |
| compileSdk / minSdk | 36 / 26（Manifest 静态确认） |
| dex | 5 个（classes…classes5），46689 类 |
| native | `libprotect.so` / `libstub.so` / `libsigner.so` / ffmpeg / sentry / applovin-crash |
| 加固 | 未见整体壳；`libprotect`/`libsigner` 仅为广告 SDK 的完整性/签名辅助（静态确认） |
| 网络库 | OkHttp 5 + Retrofit + kotlinx.serialization（手动组装，R8 混淆后为 `n60`/`p60`/`e50`） |
| 广告/归因栈 | AppLovin MAX、IronSource/LevelPlay、Meta Audience、Chartboost、Mintegral、InMobi、Adjust、Adapty、Amplitude、Sentry、OneSignal |
| 内购 | Google Play Billing + Adapty（服务端回执上报 `users/adapty-info`） |
| 后端 | FastAPI（错误体是 pydantic v2 默认格式，`errors.pydantic.dev/2.7`） |
| 对象存储 | Cloudflare R2 公开桶 `pub-98d071d737284f25a6285c558582c6f1.r2.dev` |

App 内功能面（从 `ai.photify.*Route` 字符串静态确认）：`generation`（写真生成）、`video`（文生视频/
参考图生视频/延长）、`explore`/`discover`/`publish`（社区）、`paywall`、`referral`、`identity`、`dev`。

---

## 3. 静态分析路径与踩坑

| 步骤 | 结果 |
| --- | --- |
| `apktool.jar`（/d/tools） | 损坏，改用 `/d/apk-analysis/apktool.jar`（2.11.1） |
| `apktool d --no-src`（资源/Manifest） | 成功 |
| `apktool d --no-res`（smali） | 成功，最终全部结论基于 smali（jadx 在此机不可用） |
| `jadx 1.5.6`（GUI jar CLI） | **卡死在 `Resetting disk code cache`**，换 JADX_CACHE_DIR、`-j 8`、删缓存都无效；
换 `--no-debug-info`/`--no-res` 亦无效 → 放弃 jadx，改读 smali |
| 全库 `grep -rn` on smali | 6 万文件量级，Windows 上极慢；改用内置 ripgrep（快 10×+） |

**R8 混淆映射（本次逆向的钥匙）**：

| 混淆名 | 真实含义 | 判定依据 |
| --- | --- | --- |
| `n60/*` | Retrofit 本体 | `n60/v0` 内含 “API declarations must be interfaces” |
| `p60/a` | `@Body` | 出现在 `images/standard` 的实体参数上 |
| `p60/b` | `@DELETE` | 用于 `videos/{id}/published`、`bookmarks/{type}/{id}` 等删除语义 |
| `p60/f` | `@GET` | 用于全部查询端点 |
| `p60/l` | `@Multipart` | 与 `p60/o` 同现于上传端点 |
| `p60/o` | `@POST` | `auth/login`、`images/standard`、`nano-banana/generate` |
| `p60/p` | `@PUT`（推断） | `images/{id}`、`users/me` 带实体更新 |
| `p60/n` | `@PATCH`/`@POST`（待验证） | 仅用于 `videos/{id}/hide` |
| `p60/q` | `@Part` | 有 `value` + `encoding` 字段 |
| `p60/s` | `@Path` | 有 `value` + `encoded` |
| `p60/t` | `@Query` | 同上，用于 `page`/`size` 等 |
| `p60/w` | `@Streaming` | 与 `images/standard`、`images/pair` 同现，返回 `ResponseBody` |
| `e50/d0` / `e50/e1` | `KSerializer` / `PluginGeneratedSerialDescriptor` | castArray 里的 descriptor 字符串即 JSON key |
| `d1/a` | 主 API 接口（74 个方法，方法名混淆为 A/B/C…） | `Retrofit.create` 的唯一接口 |
| `d1/b2` | 主 Repository | 实现 `d1/e`，持有 `d1/a` + token 存储 |
| `h1/{a,b,c,d,e}` | OkHttp 拦截器（见 §4） | 逐个读 `intercept` |

---

## 4. 传输层（静态确认）

`ae/e0.smali` 中组装 Retrofit：

```
baseUrl: https://api.photify.ai/photify/v1/        (dev: https://api.dev.photify.ai/photify/v1/)
OkHttpClient: connect/read/write 60s，HttpLoggingInterceptor(BODY)
5 个 Interceptor（按添加顺序）：
  h1/a → Accept-Language: <Locale.toLanguageTag()>
  h1/e → x-device: android
  h1/d → Cohort: <Sentry cohort id>
  h1/b → x-app-version: 3.0.8
  h1/c → Authorization: Bearer <accessToken>（token 为空则跳过）
```

**没有 HMAC/时间戳签名、没有设备指纹校验、没有证书固定。** 这意味着抓包/重放/脚本化毫无阻力。

---

## 5. 鉴权链路（已验证）

硬编码 Firebase 配置（`res/values/strings.xml`）：

```
project_id        = photify-ai
google_api_key    = AIzaSyCcLY4t55haLF1ijFBf8jms_oB9loUaxlg
google_app_id     = 1:382959099961:android:a2086a8ba49ea83cf3ede8
facebook_app_id   = 1822410924879938
```

链路（curl / requests 全部实测 200）：

```
# 1) 匿名注册（等价于 App 首次冷启动）
POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<google_api_key>
     {"returnSecureToken": true}
  → {idToken, refreshToken, localId}

# 2) 用 Firebase 身份换 Photify token
POST https://api.photify.ai/photify/v1/auth/login
     headers: x-device: android, x-app-version: 3.0.8
     {"userId": <localId>, "token": <idToken>,
      "adaptyProfileId": null, "adaptyEmail": null, "enableRecommendationPack": false}
  → {"accessToken": "eyJhbGci…", "tokenType": "bearer"}     # HS256 JWT，exp 24h

# 3) 后续所有请求
Authorization: Bearer <accessToken>
```

`refreshToken` 可用于 `https://securetoken.googleapis.com/v1/token?key=<api_key>`
无限续期（实测 200），**所以一个 refreshToken 就是一个可长期复用的账号**，
不需要任何社交账号、手机号或验证码。

`LoginRequest` DTO（静态确认，来自 `e50/e1` descriptor）：
`userId, token, adaptyProfileId, adaptyEmail, enableRecommendationPack`。

---

## 6. 图片链路（已验证）

### 6.1 普通上传（不做人脸解析）

```
POST images/uploaded            multipart: image=<file>  (part 名固定 "image")
  → {"imageId":"c46aaef5-…","imageUrl":"https://pub-…r2.dev/uploaded_images/…jpg"}
GET  images/uploaded/{id}       → {"id":…,"image":…}
```

### 6.2 身份图上传（人脸 traits 提取，生成的实际输入）

```
POST upload_identity_image      multipart: identity_image=<file>, form: user_id=<uid>
  → {"identity_image_id":"d46fb52b-…",
     "identity_image_url":"https://pub-…r2.dev/identity_images/<uid>/…jpg",
     "identity_traits":{"eye_color":null,"gender":"man","hair_color":"blonde","skin_color":"white"}}
```

服务端会拒绝非人像：纯色块测试图返回 `400 Image processing error`。**已验证**

### 6.3 生成（同步流式）

```
POST images/standard
     {"identityImageId":"<identity_image_id>",
      "gender":"woman|man|nonbinary",
      "skinColor":"pale|white|tanned|black",
      "hairColor":"black|brown|blonde|pink|blue|red|violet|green|silver|bald",
      "bodyType":"skinny|lean|muscular|curvy|heavyset",
      "promptId": null | "<uuid>",
      "prompt": "<风格描述>",
      "category": null | "<pack 名>",
      "cohort": null,
      "generationModelId": "<uuid>",
      "modelType": "standard|lora"}
  → 200, content-type: image/jpeg, body = 图片二进制（768×1152）  ← @Streaming ResponseBody
  → 400 {"icon_key":"unknown","title":"Not enough premium tokens","message":"…"}
```

**枚举值全部来自服务端 pydantic 校验回显**（故意传非法值拿 422），不是猜测。**已验证**

`GET images/models?include_premium_models=true` 返回模型表（含 id / 价格 / 访问级别）。
`GET users/energy` 返回 `{photoTokens, videoTokens, premiumTokens}`。

### 6.4 身份图语义（A/B 对照实验，已验证）

同一账号、同一模型、同一 prompt（`plain studio portrait, neutral gray background, front facing`），
只换 `identityImageId`：

| 用例 | 身份图 | 请求 gender | 输出 |
| --- | --- | --- | --- |
| A | 女性写真（768×1152） | `woman` | 与 A 图同一张脸，挽发、正面 |
| B | 蓝皮肤男性赛博朋克肖像 | `woman` | **女性**，长棕发，但脸型/发型与 A 不同 |

结论：

1. `identityImageId` **确实参与生成**（A 与 B 输出不同）；
2. `gender / skinColor / hairColor / bodyType` 是**显式覆盖参数**，优先于身份图本身的 traits
   —— 传 `gender=woman` 时即便身份图是男性，也会产出女性；
3. 人脸不是 1:1 复刻，而是「人设参考 + 参数化重构」。

> 工程含义：调用时必须把 `upload_identity_image` 返回的 `identity_traits` 回填到生成参数，
> 否则会被默认值「换人」。本网关已按此实现（`server.py` 中 `auto = client.account.identity_traits`）。

### 6.5 其它生成能力（协议已还原，未逐个跑通）

| 端点 | 用途 | 请求 DTO |
| --- | --- | --- |
| `POST nano-banana/generate` | Nano Banana / Pro | `{generation_model_id, uploaded_image_ids[], generated_image_ids[], prompt, prompt_id}` → `{id,url,…}` |
| `GET nano-banana/{image_id}` | 取 Nano Banana 结果 | — |
| `POST images/pair` | 双人合影 | 双份 `firstPerson*/secondPerson*` + prompt |
| `POST images/{image_id}/animate` | 静态图动画化 | `LivePhotoGenerateRequest{modelId,…}` |
| `POST images/edit` / `images/edit/{image_id}` | 图编辑 | multipart / `ModifyImageDTO{prompt}` |
| `PUT images/{image_id}` | 编辑已生成图 | `{prompt}` |
| `POST prompts/enhance` | prompt 扩写 | `{prompt, gender}` → `{prompt}` |
| `POST videos/text-to-video` | 文生视频 | `TextToVideoRequest{modelId,…}` |
| `POST videos/reference-to-video` | 参考图生视频 | `ReferenceToVideoRequest{prompt,…}` |
| `POST videos/{video_id}/prolong` | 视频延长 | `{length, prompt}` |

---

## 7. 端点全表（74 个，静态确认）

`*` = 方法动词由语义推断（`p60/p` / `p60/n` 无法从注解类空签名区分），其余为确定性映射。

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| GET | `users/following` | p1:page=… p2:size=… |
| GET | `videos/user/{user_id}/published` | p1:{user_id} p2:video-type=… p3:page=… p4:size=… |
| multipart POST | `upload_identity_image` | p2:part:user_id |
| POST | `categories/{category_id}/move` | p1:{category_id} |
| GET | `users/top` | p1:following_only=… p2:page=… p3:size=… |
| multipart POST | `images/uploaded` |  |
| DELETE | `videos/{video_id}/published` | p1:{video_id} |
| DELETE | `categories/{category_id}/user` | p1:{category_id} |
| POST | `users/adapty-info` |  |
| GET | `images/user` | p1:reaction=… p2:page=… p3:size=… |
| DELETE | `users/{user_id}/unfollow` | p1:{user_id} |
| POST stream | `images/standard` |  |
| GET | `users/{user_id}` | p1:{user_id} |
| GET | `bookmarks?type=preview_image` | p1:page=… p2:size=… |
| POST | `categories/{category_id}/prompts` | p1:{category_id} |
| POST | `reports` |  |
| PUT* | `images/edit/{image_id}` | p1:{image_id} |
| POST | `images/{image_id}/published` | p1:{image_id} |
| POST | `videos/{video_id}/prolong` | p1:{video_id} |
| GET | `images/{image_id}` | p1:{image_id} |
| POST | `videos/{video_id}/published` | p1:{video_id} |
| GET | `videos` | p1:status=… p2:page=… p3:size=… p4:video-type=… |
| GET | `videos/{video_id}` | p1:{video_id} |
| GET | `bookmarks?type=image` | p1:page=… p2:size=… |
| GET | `categories/user/{user_id}` | p1:{user_id} p2:page=… p3:size=… |
| GET | `images/edit` | p1:page=… p2:size=… |
| PUT* stream | `images/{image_id}` | p1:{image_id} |
| POST | `users/{user_id}/follow` | p1:{user_id} |
| GET | `users/me` |  |
| GET | `nano-banana/{image_id}` | p1:{image_id} |
| POST | `categories/{category_id}/user` | p1:{category_id} |
| POST | `image_reaction` |  |
| GET | `bookmarks?type=video` | p1:page=… p2:size=… |
| DELETE | `categories/{category_id}` | p1:{category_id} |
| POST | `categories/{category_id}/reject` | p1:{category_id} |
| POST | `auth/login` |  |
| DELETE | `images/{image_id}/published` | p1:{image_id} |
| GET | `videos/models` | p1:model-types=… p2:include_models_with_audio=… |
| DELETE | `categories/{category_id}/prompts/{prompt_id}` | p1:{category_id} p2:{prompt_id} |
| POST | `videos/reference-to-video` |  |
| DELETE | `bookmarks/{item_type}/{content_id}` | p1:{item_type} p2:{content_id} |
| DELETE | `images/user` | p1:id=… |
| GET | `search` | p1:q=… p2:field=… p3:page=… p4:size=… |
| DELETE | `images/edit/{image_id}` | p1:{image_id} |
| POST stream | `images/pair` |  |
| POST | `categories/{category_id}/approve` | p1:{category_id} |
| PUT* | `users/me` |  |
| POST | `categories/create-custom-pack` |  |
| PUT* | `categories/{category_id}` | p1:{category_id} |
| GET | `users/followers` | p1:page=… p2:size=… |
| GET | `previewimages/{preview_image_id}` | p1:{preview_image_id} |
| POST | `bookmarks` |  |
| GET | `tags` | p1:type=… p2:exclude-type=… p3:page=… p4:size=… |
| GET | `categories` | p1:tag-id=… p2:sort=… p3:page=… p4:size=… |
| POST | `images/{image_id}/animate` | p1:{image_id} |
| PATCH* | `videos/{video_id}/hide` | p1:{video_id} |
| GET | `categories/{category_id}` | p1:{category_id} p2:with-prompts=… p3:with-preview-images=… |
| POST | `prompts/enhance` |  |
| DELETE | `videos` | p1:id=… |
| POST stream | `categories/prompts/preview` |  |
| multipart POST | `images/edit` |  |
| POST | `nano-banana/generate` |  |
| GET | `images/models` | p1:include_premium_models=… p2:model_type=… |
| GET | `images/uploaded/{image_id}` | p1:{image_id} |
| GET | `categories/user` | p1:page=… p2:size=… p3:enableRecommendationPack=… |
| GET | `bookmarks?type=category` | p1:page=… p2:size=… |
| GET | `feed/mixed` | p1:following_only=… p2:cursor=… p3:page_size=… p4:gender=… p5:filter_only_images=… p6:filter_only_videos=… p7:filter_only_packs=… |
| PUT* | `prompts/{prompt_id}` | p1:{prompt_id} |
| GET | `videos` | p1:id=… p2:video-type=… |
| GET | `users/energy` |  |
| POST | `categories/autocreate` |  |
| POST | `users/referral/{referred_user_id}` | p1:{referred_user_id} |
| POST | `videos/text-to-video` |  |
| GET | `images/user/{user_id}/published` | p1:{user_id} p2:page=… p3:size=… |

---

## 8. 关键 DTO（来自 kotlinx descriptor，字段名即 JSON key）

```
GenerateImageEntity      identityImageId, gender, skinColor, hairColor, bodyType,
                         promptId, prompt, category, cohort, generationModelId, modelType
GeneratePairImageEntity  firstPerson{IdentityImageId,Gender,SkinColor,HairColor,BodyType},
                         secondPerson{…}, promptId, prompt, category, cohort, generationModelId
GenerateNanoBananaRequest generation_model_id, uploaded_image_ids[], generated_image_ids[], prompt, prompt_id
NanoBananaResponse       id, url, generated_image_ids[], uploaded_image_ids[], prompt, prompt_id, owner_id
IdentityImageEntity      identity_image_id, identity_image_url, identity_traits
ImageTraitsEntity        gender, hair_color, skin_color, (eye_color)
LoginRequest             userId, token, adaptyProfileId, adaptyEmail, enableRecommendationPack
LoginResponse            accessToken
MeDTO                    id, name, role, followersCount, followingCount
BalanceDTO               videoTokens, premiumTokens
CategoryEntity           id, categoryName, isPublic, isOwner, isAdded, isPremium, ownerId,
                         ownerName, tags, generatedImagesCount, slug, promptsCount,
                         previewImageId, previewImageUrl, isBookmarked
PromptEntity             id, prompt, previewImageId, previewImageUrl
EnhancePromptRequest     prompt, gender
TextToVideoRequest       modelId, …            ProlongVideoRequest  length, prompt
```

---

## 9. 模型与计费（已验证：`GET images/models`）

| 内部名 | 上游名 | 访问级别 | 价格(tokens) | premiumTokens | 实测 |
| --- | --- | --- | --- | --- | --- |
| `a379eda7-…` | Starter | free | 1 | 否 | **0 余额也出图成功** |
| `6946a2cc-…` | Supreme | free | 1 | 是 | 400 额度不足 |
| `1a56f85c-…` | Supreme Flux | free | 1 | 是 | 未单独验证 |
| `da36ae44-…` | Supreme Ultra | free | 2 | 是 | 400 额度不足 |
| `a26a77c9-…` | Nano Banana | free | 5 | 是 | 400 额度不足（10.9s 后返回） |
| `5cd7960c-…` | Nano Banana Pro | free | 10 | 是 | 未单独验证 |
| `e1656e92-…` | Elite | premium | 1 | 否 | 同上（模型 id 见接口） |
| `3fc0bbbc-…` | Elite Pro | ultra | 1 | 否 | — |
| `3986d9bb-…` | Polaroid | ultra | 1 | 否 | — |

额度来源（静态确认）：内购经 Adapty → `POST users/adapty-info` 上报；另有 `freeEliteGenerationCount`、
`referral`（`POST users/referral/{id}`）等字段。**没有发现「纯免费额度」的公开接口**；
`Starter` 不扣费看起来是上游当前的计费策略（bug 或免费档），不是我们绕过校验。

---

## 10. 实测证据时间线

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 05:47 | Firebase 匿名 `accounts:signUp` | 200，拿到 refreshToken/localId |
| 05:47 | `auth/login` | 200，`accessToken` (HS256, exp 24h) |
| 05:47 | `GET users/me` | 200，`role:user`，账号创建于 2026-10-28T21:46:51Z |
| 05:48 | `GET users/energy` | 200，`{photoTokens:0, videoTokens:0, premiumTokens:0}` |
| 05:48 | `GET images/models?include_premium_models=true` | 200，9 个模型 |
| 05:49 | `GET categories?page=1&size=3` | 200，风格包列表（含预览图 CDN URL） |
| 05:51 | `POST images/uploaded`（纯色测试图） | 200 |
| 05:52 | `POST upload_identity_image`（纯色图） | **400 Image processing error**（服务端人脸校验） |
| 05:53 | `POST upload_identity_image`（真人预览图） | 200，traits `{gender:man, hair_color:blonde, skin_color:white}` |
| 05:54 | `images/standard` + Starter + standard | 404 `Image not found`（**异常，见 §11**） |
| 05:55 | `images/standard` + `modelType:lora` | 500 `Internal server error` |
| 05:55 | `nano-banana/generate` | 400 `Not enough premium tokens`（10.9s） |
| 06:02 | 网关 `/v1/images/edits`（supreme-ultra, standard） | 400 `Not enough premium tokens`（4.5s）→ 路径通，仅缺额度 |
| 06:03 | 网关 `/v1/images/generations`（**starter**, standard） | **200，JPEG 768×1152，4.99s** |
| 06:04 | 连续 3 次 starter | 200 / 200 / 200，6.2s / 8.6s / 13.1s，`photoTokens` 始终 0 |
| 06:17 | 重启后 starter（缓存身份图） | 200，**144s**（长尾，见 §12） |
| 06:20 | 真实并发 3 × supreme-ultra | 0/3 成功：2×额度不足 + 1×上游 `Connection reset`（99s） |
| 06:26 | 浏览器 e2e（playwright，real 模式 demo 页） | PASS，11.0s 出图 |
| 06:31 | `images/standard` 直连，身份图=女性写真 / gender=woman | 200，6.8s，出图与身份图同一张脸 |
| 06:32 | 同 prompt，身份图换成男性蓝皮肤肖像 / gender=woman | 200，6.2s，**仍出女性** → traits 参数覆盖身份图（§6.4） |
| 06:39 | 网关修正为「traits 自动回填」后，上传男性身份图 | 200，29.8s，输出**金发男性** → 链路正确 |
| 06:07 | mock 压测 4 并发 ×8 | 8/8 成功，p50 1286ms，health 780 rps |
| 06:08 | mock 压测 8 并发 ×24（闸门 4） | 24/24 成功，p50 2441ms，p95 2446ms |
| 06:08 | 错误路径（缺 prompt / 未知模型 / 非法 base64） | 3/3 返回 400 |

---

## 11. 未解释现象：初次 `images/standard` 404 → 后续成功

同一账号、同一身份图 id，05:54 用 directly-curl 调用 Starter 返回 `404 Image not found`，
06:03 经网关（重新上传身份图）同参数返回 200。已排除的因素：`modelType`、`promptId`、`category`、
`generationModelId`、请求头、token 有效性（当时 `users/me` 正常）。

最可能解释（**待验证**）：身份的可用性依赖「上传 → 服务端建档」的即时状态，directly-curl 那次拿到的是
刚上传未完全就绪的 id；网关路径重新上传后稳定可用。也可能是上游那段时间的实例不一致。
这一条不影响当前可用性（网关路径稳定出图），但排障时值得先怀疑身份图。

## 12. 上游行为特征（影响网关设计）

- **同步出图**：一次请求拿到图片，无轮询，省掉任务状态机。
- **延迟方差大**：常态 5–13s，长尾出现 144s（同样参数、同样账号）。网关超时默认 180s。
- **并发不友好**：3 并发时出现过上游兜底 `Connection reset`；建议单账号串行、多账号并行。
- **身份图是唯一「上下文」**：`identityImageId` 影响出图，但显式 `gender/skinColor/hairColor/bodyType`
  会覆盖身份图自身的 traits（详见 §6.4）。同一身份图 + 同一 traits + 不同 prompt → 人脸基本一致（实测 6 张）。
- **延迟不随模型价格分层**：`Starter` 也有 5–13s 常态与 144s 长尾。

---

## 13. 未验证 / 风险

| 项 | 状态 |
| --- | --- |
| `Starter` 免费额度是否有每日/总量上限 | **待验证**（未做高频试探，避免滥用） |
| `Elite` / `Polaroid` / `Supreme Flux` / `Nano Banana Pro` 实际效果 | 待验证（需付费额度） |
| 视频链路（text-to-video / prolong） | 协议已还原，未实跑（需 videoTokens） |
| `p60/p`、`p60/n` 的确切动词 | 待验证（PUT/PATCH 推断） |
| 国内网络直连 `api.photify.ai` | 本机出现过 SSL EOF 抖动，脚本已带重试 |

## 14. 合规边界（本项目执行标准）

- 不伪造广告回调（rewarded ad）、不伪造内购回执（Adapty / Play Billing）来凭空造额度；
  额度只来自运行者自己的账号。
- 不对生产上游做高强度压测（本轮真实生成合计 ≈ 9 次，压测全部在本地 mock 模式完成）。
- 生成的图片与身份图属于运行者自己上传的数据，仓库不提交任何凭证与产物（见 `.gitignore`）。
