"""Photify AI → OpenAI 兼容网关（第一轮）。

设计定位：Photify 的生成能力绑定「账号 + 身份图 + 额度」，没有匿名白嫖通道，
所以网关是「自带额度的账号池转发器」：
    - 你提供 photify 账号（匿名 Firebase refreshToken 或已有账号）与一张身份图
    - 网关把它们包装成 OpenAI 风格的 /v1/images/generations、/v1/images/edits

两种运行模式：
    PHOTIFY_MODE=mock  ：不碰上游，本地合成图片，用于 demo 与压测（默认）
    PHOTIFY_MODE=real  ：真实调用 api.photify.ai（消耗账号额度）

启动：
    pip install fastapi uvicorn pillow requests
    set PHOTIFY_MODE=mock && python server.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from photify_client import AnonymousAccount, PhotifyClient, PhotifyError

MODE = os.environ.get("PHOTIFY_MODE", "mock").lower()
ACCOUNTS_FILE = Path(os.environ.get("PHOTIFY_ACCOUNTS", "accounts.json"))
OUTPUT_DIR = Path(os.environ.get("PHOTIFY_OUTPUT", "output"))
MAX_CONCURRENCY = int(os.environ.get("PHOTIFY_MAX_CONCURRENCY", "4"))
UPSTREAM_TIMEOUT = float(os.environ.get("PHOTIFY_TIMEOUT", "180"))
DEFAULT_IDENTITY = os.environ.get("PHOTIFY_IDENTITY_IMAGE", "")  # 本地默认身份图路径

OUTPUT_DIR.mkdir(exist_ok=True)

# 上游模型 → 对外 slug。价格为 photoTokens（Starter=1 … Nano Banana Pro=10）
MODELS: dict[str, dict[str, Any]] = {
    "photify/starter": {"id": "a379eda7-8e6e-40f8-8653-e4794f0b2845", "price": 1},
    "photify/supreme": {"id": "6946a2cc-cabf-4333-8fa1-36cc7cef2d95", "price": 1},
    "photify/supreme-flux": {"id": "1a56f85c-b556-443a-9953-87af9a0fce88", "price": 1},
    "photify/supreme-ultra": {"id": "da36ae44-15cd-47fa-a48e-6ba6c7c442e7", "price": 2},
    "photify/nano-banana": {"id": "a26a77c9-7ade-478e-b0fb-9ac9bdb225f1", "price": 5, "premium": True},
    "photify/nano-banana-pro": {"id": "5cd7960c-3cfb-4ddb-b81c-0a6ddad9bbef", "price": 10, "premium": True},
}
DEFAULT_MODEL = "photify/starter"

app = FastAPI(title="photify-2api", version="0.1.0")
_sem = asyncio.Semaphore(MAX_CONCURRENCY)
_stats = {"requests": 0, "ok": 0, "failed": 0, "inflight": 0, "latency_ms": []}
_accounts: list[PhotifyClient] = []
_account_locks: list[threading.Lock] = []
_personas: dict[str, int] = {}  # persona 名 → 账号下标（一个账号 = 一张固定人脸）
_rr = 0


# --------------------------------------------------------------------- 账号池


def load_accounts() -> None:
    """从 accounts.json 载入账号池。

    文件格式：
        {"accounts": [{"refresh_token": "...", "firebase_uid": "...",
                       "identity_image_id": "可选，复用已上传的身份图"}]}
    """
    global _accounts, _account_locks, _personas
    _accounts = []
    _account_locks = []
    _personas = {}
    if not ACCOUNTS_FILE.exists():
        return
    data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    for item in data.get("accounts", []):
        rt = (item.get("refresh_token") or "").strip()
        if not rt:
            continue
        acc = AnonymousAccount(refresh_token=rt, firebase_uid=item.get("firebase_uid", ""))
        acc.identity_image_id = item.get("identity_image_id", "")
        _accounts.append(PhotifyClient(acc, timeout=UPSTREAM_TIMEOUT))
        _account_locks.append(threading.Lock())
        persona = (item.get("persona") or "").strip().lower()
        if persona:
            _personas[persona] = len(_accounts) - 1
    print(f"[accounts] loaded {len(_accounts)}  personas={sorted(_personas)}")


def pick_client(persona: str | None = None) -> tuple[PhotifyClient, threading.Lock]:
    """persona 指定时锁定到对应账号；否则轮询。
    同一账号的「上传身份图 + 生成」必须串行，否则身份图会互相覆盖。"""
    global _rr
    if not _accounts:
        raise HTTPException(503, "no photify account configured (see accounts.json.example)")
    if persona:
        idx = _personas.get(persona)
        if idx is None:
            raise HTTPException(404, f"unknown persona '{persona}'")
        return _accounts[idx], _account_locks[idx]
    idx = _rr % len(_accounts)
    _rr += 1
    return _accounts[idx], _account_locks[idx]


def resolve_model(model: str) -> tuple[str, str | None]:
    """把对外模型名解析成 (上游模型 slug, persona)。

    `photify/<persona>` 是「按账号固定人设」的写法：同一张身份图 + 指定账号，
    供 new-api 里一个渠道对应一个人设。
    """
    if model in MODELS:
        return model, None
    if model.startswith("photify/"):
        persona = model.split("/", 1)[1].strip().lower()
        if persona in _personas:
            return DEFAULT_MODEL, persona
    raise HTTPException(400, f"unknown model '{model}', available: {sorted(MODELS) + [f'photify/{p}' for p in sorted(_personas)]}")


@app.on_event("startup")
def _startup() -> None:
    load_accounts()
    print(f"[mode] {MODE}  concurrency={MAX_CONCURRENCY}")


def record(latency_ms: float, ok: bool) -> None:
    _stats["requests"] += 1
    _stats["ok" if ok else "failed"] += 1
    _stats["latency_ms"].append(latency_ms)
    if len(_stats["latency_ms"]) > 2000:
        _stats["latency_ms"] = _stats["latency_ms"][-2000:]


def openai_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "upstream_error", "code": code}},
    )


# --------------------------------------------------------------------- 图片落盘


def store_image(content: bytes, ext: str = "") -> str:
    if not ext:
        if content[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif content[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        else:
            ext = "bin"
    name = f"{uuid.uuid4().hex}.{ext}"
    (OUTPUT_DIR / name).write_bytes(content)
    return name


def mock_image(prompt: str, size: str) -> bytes:
    """mock 模式：合成一张带 prompt 文本的渐变图，用于打通链路与压测。"""
    from PIL import Image, ImageDraw

    w, h = (1024, 1024)
    if size and "x" in size:
        try:
            w, h = (int(x) for x in size.lower().split("x")[:2])
        except ValueError:
            pass
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    bands = 48
    for i in range(bands):
        y0 = int(h * i / bands)
        y1 = int(h * (i + 1) / bands)
        draw.rectangle(
            [0, y0, w, y1],
            fill=(int(120 + 100 * i / bands), int(60 + 120 * i / bands), int(200 - 80 * i / bands)),
        )
    draw.text((32, 32), "photify mock", fill=(255, 255, 255))
    draw.text((32, 64), prompt[:180], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------- 核心生成


async def run_generation(
    *,
    prompt: str,
    model: str,
    size: str,
    image_bytes: bytes | None,
    identity_hint: dict[str, Any] | None = None,
) -> bytes:
    """统一入口：mock 直接合成，real 走 Photify 真实链路。"""
    if not prompt.strip():
        raise HTTPException(400, "prompt is required")

    spec_key, persona = resolve_model(model)

    if MODE != "real":
        await asyncio.sleep(1.2)  # 模拟上游同步生成耗时（实测 2s 量级）
        return mock_image(prompt, size)

    client, lock = pick_client(persona)
    spec = MODELS[spec_key]
    traits = identity_hint or {}

    def _call() -> bytes:
        with lock:
            if image_bytes:
                # 显式传图时每次上传新身份图，允许不同人像串用同一账号
                client.upload_identity(image_bytes)
            elif not client.account.identity_image_id and DEFAULT_IDENTITY and Path(DEFAULT_IDENTITY).exists():
                # 账号没有缓存身份图时，用本地默认人像兜底（只上传一次）
                client.upload_identity(Path(DEFAULT_IDENTITY).read_bytes())
            if not client.account.identity_image_id:
                raise PhotifyError(400, "identity_required", "real 模式需要上传一张身份图（image 字段）")
            # 外观参数优先跟随身份图 traits，否则传默认值会把「本人」换成别人
            auto = client.account.identity_traits or {}
            gender = traits.get("gender") or auto.get("gender") or "woman"
            skin = traits.get("skin_color") or auto.get("skin_color") or "white"
            hair = traits.get("hair_color") or auto.get("hair_color") or "brown"
            body = traits.get("body_type") or "lean"
            if spec.get("premium"):
                data = client.nano_banana(prompt, spec["id"], uploaded_image_ids=[])
                if data.get("url"):
                    import requests as _rq

                    return _rq.get(data["url"], timeout=UPSTREAM_TIMEOUT).content
                raise PhotifyError(502, "nano_no_url", json.dumps(data)[:300])
            content, _ctype = client.generate(
                client.account.identity_image_id,
                prompt,
                spec["id"],
                gender=gender,
                skin_color=skin,
                hair_color=hair,
                body_type=body,
            )
            return content

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call)


# --------------------------------------------------------------------- 路由


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "mode": MODE, "accounts": len(_accounts)}


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    lat = sorted(_stats["latency_ms"])
    def pct(p: float) -> float:
        if not lat:
            return 0.0
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1)
    return {
        **_stats,
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": lat[-1] if lat else 0},
    }


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    data = [
        {
            "id": name,
            "object": "model",
            "owned_by": "photify",
            "photify_tokens": spec["price"],
        }
        for name, spec in MODELS.items()
    ]
    # 每个命名 persona（= 一个账号 + 一张固定身份图）单独暴露一个模型名
    for persona in sorted(_personas):
        data.append(
            {
                "id": f"photify/{persona}",
                "object": "model",
                "owned_by": "photify",
                "photify_tokens": 1,
                "persona": True,
            }
        )
    return {"object": "list", "data": data}


@app.post("/v1/images/generations")
async def images_generations(payload: dict[str, Any]) -> Response:
    """OpenAI 风格。Photify 本质是「身份图 → 风格化写真」，
    因此建议走 /v1/images/edits；这里保留 generations 以兼容只会调它的客户端。"""
    started = time.time()
    model = payload.get("model") or DEFAULT_MODEL
    prompt = payload.get("prompt") or ""
    size = payload.get("size") or "1024x1024"
    response_format = payload.get("response_format", "b64_json")
    identity_b64 = payload.get("image") or payload.get("identity_image")

    image_bytes = None
    if identity_b64:
        try:
            image_bytes = base64.b64decode(identity_b64.split(",", 1)[-1])
        except Exception:
            return openai_error(400, "bad_image", "image 字段不是合法 base64")

    _stats["inflight"] += 1
    try:
        async with _sem:
            content = await run_generation(
                prompt=prompt, model=model, size=size, image_bytes=image_bytes
            )
    except PhotifyError as exc:
        record((time.time() - started) * 1000, False)
        return openai_error(exc.status or 502, exc.title, exc.message)
    except HTTPException as exc:
        record((time.time() - started) * 1000, False)
        return openai_error(exc.status_code, "bad_request", str(exc.detail))
    finally:
        _stats["inflight"] -= 1

    record((time.time() - started) * 1000, True)
    b64 = base64.b64encode(content).decode()
    item: dict[str, Any] = {"revised_prompt": prompt}
    if response_format == "b64_json":
        item["b64_json"] = b64
    else:
        name = store_image(content)
        item["url"] = f"/files/{name}"
    return JSONResponse({"created": int(time.time()), "data": [item]})


@app.post("/v1/images/edits")
async def images_edits(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    model: str = Form(DEFAULT_MODEL),
    size: str = Form("1024x1024"),
    response_format: str = Form("b64_json"),
    n: int = Form(1),
) -> Response:
    """OpenAI /v1/images/edits 语义：上传一张人像 + 风格描述 → 输出写真。"""
    if n > 1:
        return openai_error(400, "n_not_supported", "Photify 单请求只出 1 张，请循环调用")
    content_in = await image.read()
    if not content_in:
        return openai_error(400, "empty_image", "上传文件为空")

    started = time.time()
    _stats["inflight"] += 1
    try:
        async with _sem:
            content = await run_generation(
                prompt=prompt, model=model, size=size, image_bytes=content_in
            )
    except PhotifyError as exc:
        record((time.time() - started) * 1000, False)
        return openai_error(exc.status or 502, exc.title, exc.message)
    except HTTPException as exc:
        record((time.time() - started) * 1000, False)
        return openai_error(exc.status_code, "bad_request", str(exc.detail))
    finally:
        _stats["inflight"] -= 1

    record((time.time() - started) * 1000, True)
    item: dict[str, Any] = {"revised_prompt": prompt}
    if response_format == "b64_json":
        item["b64_json"] = base64.b64encode(content).decode()
    else:
        item["url"] = f"/files/{store_image(content)}"
    return JSONResponse({"created": int(time.time()), "data": [item]})


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]) -> Response:
    """最小可用：把最后一条 user 消息当 prompt，返回 markdown 图片。
    仅为了让只会调 chat 的客户端也能用。"""
    messages = payload.get("messages") or []
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            prompt = msg.get("content") or ""
            break
    model = payload.get("model") or DEFAULT_MODEL
    async with _sem:
        content = await run_generation(prompt=prompt, model=model, size="1024x1024", image_bytes=None)
    url = f"/files/{store_image(content)}"
    text = f"![generated]({url})"
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
        }
    )


@app.get("/admin/accounts")
def admin_accounts() -> JSONResponse:
    out = []
    for i, c in enumerate(_accounts):
        row = {
            "index": i,
            "firebase_uid": c.account.firebase_uid,
            "identity_image_id": c.account.identity_image_id,
        }
        if MODE == "real":
            try:
                row["energy"] = c.energy()
            except Exception as exc:  # 账号失效时不阻塞整表
                row["energy_error"] = str(exc)[:200]
        out.append(row)
    return JSONResponse({"mode": MODE, "accounts": out})


@app.get("/files/{name}")
def files(name: str) -> FileResponse:
    path = OUTPUT_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path)


@app.get("/demo", response_class=HTMLResponse)
def demo() -> str:
    return (Path(__file__).parent / "demo.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8931")))
