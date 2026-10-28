"""Photify AI 3.0.8 协议客户端。

协议来源：ai.photify.app v3.0.8 静态还原（Retrofit 接口 = d1/a，注解 = p60/*）+ 实际接口验证。

鉴权链路（已验证）：
    1. Firebase 匿名注册（Web API Key）→ refreshToken / idToken / localId
    2. POST auth/login {userId=localId, token=idToken} → accessToken（JWT，24h）
    3. 之后所有请求带 Authorization: Bearer <accessToken>
    4. refreshToken 可长期复用（securetoken.googleapis.com），等价于一个可持久化的匿名账号

固定请求头：x-device: android、x-app-version: 3.0.8
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

BASE_URL = "https://api.photify.ai/photify/v1/"
# 来自 res/values/strings.xml: google_api_key（Firebase 项目 photify-ai）
FIREBASE_WEB_API_KEY = "AIzaSyCcLY4t55haLF1ijFBf8jms_oB9loUaxlg"
APP_VERSION = "3.0.8"

# 服务端 pydantic 校验泄露出来的枚举（直接取自 422 detail）
GENDERS = ("man", "woman", "nonbinary")
SKIN_COLORS = ("pale", "white", "tanned", "black")
HAIR_COLORS = ("black", "brown", "blonde", "pink", "blue", "red", "violet", "green", "silver", "bald")
BODY_TYPES = ("skinny", "lean", "muscular", "curvy", "heavyset")
MODEL_TYPES = ("lora", "standard")


class PhotifyError(RuntimeError):
    """上游返回的业务错误。code 为 HTTP 状态码，title/message 为服务端文案。"""

    def __init__(self, status: int, title: str, message: str, payload: Any = None):
        super().__init__(f"{status} {title}: {message}")
        self.status = status
        self.title = title
        self.message = message
        self.payload = payload


@dataclass
class AnonymousAccount:
    """一个可长期复用的匿名账号（Firebase refreshToken 即凭证）。"""

    refresh_token: str
    firebase_uid: str = ""
    access_token: str = ""
    access_exp: float = 0.0
    id_token: str = ""
    id_exp: float = 0.0
    # 生成用的身份图缓存
    identity_image_id: str = ""
    identity_traits: dict[str, Any] = field(default_factory=dict)


class PhotifyClient:
    def __init__(self, account: AnonymousAccount, timeout: float = 180.0, retries: int = 2):
        self.account = account
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()
        self.session = requests.Session()

    # ---------------------------------------------------------------- 账号

    @classmethod
    def create_anonymous(cls, timeout: float = 30.0) -> "PhotifyClient":
        """注册一个全新匿名账号（等价于 app 首次冷启动拿到的账号）。"""
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_WEB_API_KEY}",
            json={"returnSecureToken": True},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        acc = AnonymousAccount(refresh_token=data["refreshToken"], firebase_uid=data["localId"])
        return cls(acc)

    @classmethod
    def from_refresh_token(cls, refresh_token: str, firebase_uid: str = "") -> "PhotifyClient":
        return cls(AnonymousAccount(refresh_token=refresh_token, firebase_uid=firebase_uid))

    def _refresh_firebase(self) -> str:
        resp = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_WEB_API_KEY}",
            data={"grant_type": "refresh_token", "refresh_token": self.account.refresh_token},
            timeout=30,
        )
        if resp.status_code != 200:
            raise PhotifyError(resp.status_code, "Firebase refresh failed", resp.text[:300], resp.text)
        data = resp.json()
        self.account.refresh_token = data.get("refresh_token", self.account.refresh_token)
        self.account.firebase_uid = data.get("user_id", self.account.firebase_uid)
        self.account.id_token = data["id_token"]
        self.account.id_exp = time.time() + int(data.get("expires_in", 3600)) - 120
        return self.account.id_token

    def _login(self) -> str:
        if not self.account.id_token or time.time() >= self.account.id_exp:
            self._refresh_firebase()
        body = {
            "userId": self.account.firebase_uid,
            "token": self.account.id_token,
            "adaptyProfileId": None,
            "adaptyEmail": None,
            "enableRecommendationPack": False,
        }
        resp = self._raw_request("POST", "auth/login", json=body, with_auth=False)
        data = resp.json()
        self.account.access_token = data["accessToken"]
        # 服务端 JWT 里 exp 为 24h，本地按 23h 记
        self.account.access_exp = time.time() + 23 * 3600
        return self.account.access_token

    def _access_token(self) -> str:
        with self._lock:
            if not self.account.access_token or time.time() >= self.account.access_exp:
                self._login()
            return self.account.access_token

    # ---------------------------------------------------------------- HTTP

    def _headers(self, with_auth: bool = True, content_type: str | None = None) -> dict[str, str]:
        h = {
            "x-device": "android",
            "x-app-version": APP_VERSION,
            "Accept-Language": "en-US",
            "User-Agent": "okhttp/5.1.0",
        }
        if content_type:
            h["Content-Type"] = content_type
        if with_auth:
            h["Authorization"] = "Bearer " + self._access_token()
        return h

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        with_auth: bool = True,
        timeout: float | None = None,
    ) -> requests.Response:
        """带重试的底层请求；4xx 不重试（除 401 重新登录一次）。"""
        url = BASE_URL + path
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=self._headers(with_auth, "application/json" if json is not None else None),
                    json=json,
                    data=data,
                    files=files,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:  # SSL 抖动 / 连接被重置
                last_exc = exc
                time.sleep(1.0 + attempt)
                continue
            if resp.status_code == 401 and with_auth and attempt == 0:
                with self._lock:
                    self.account.access_token = ""
                continue
            if resp.status_code >= 500 and attempt < self.retries:
                time.sleep(1.0 + attempt)
                continue
            return resp
        raise PhotifyError(0, "network", str(last_exc))

    @staticmethod
    def _raise_for_error(resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        title = message = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                title = payload.get("title") or ""
                message = payload.get("message") or ""
                if not message and payload.get("detail"):
                    message = json.dumps(payload["detail"], ensure_ascii=False)
            else:
                message = str(payload)[:300]
        except ValueError:
            message = resp.text[:300]
        raise PhotifyError(resp.status_code, title or "error", message, resp.text[:1000])

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._raw_request(method, path, **kwargs)
        self._raise_for_error(resp)
        return resp.json() if resp.content else {}

    # ---------------------------------------------------------------- 只读接口

    def me(self) -> dict[str, Any]:
        return self._json("GET", "users/me")

    def energy(self) -> dict[str, Any]:
        """返回 photoTokens / videoTokens / premiumTokens。"""
        return self._json("GET", "users/energy")

    def models(self, include_premium: bool = True) -> list[dict[str, Any]]:
        data = self._json("GET", f"images/models?include_premium_models={str(include_premium).lower()}")
        return data.get("items", [])

    def packs(self, page: int = 1, size: int = 20) -> list[dict[str, Any]]:
        data = self._json("GET", f"categories?page={page}&size={size}")
        return data.get("items", [])

    def pack_prompts(self, category_id: str) -> list[dict[str, Any]]:
        data = self._json("GET", f"categories/{category_id}?with-prompts=true&with-preview-images=false")
        return data.get("prompts") or (data.get("category") or {}).get("prompts") or []

    # ---------------------------------------------------------------- 上传

    def upload_image(self, content: bytes, filename: str = "image.jpg", mime: str = "image/jpeg") -> dict[str, Any]:
        """POST images/uploaded，part 名固定为 image。返回 {imageId, imageUrl}。"""
        return self._json(
            "POST", "images/uploaded", files={"image": (filename, content, mime)}
        )

    def upload_identity(self, content: bytes, filename: str = "selfie.jpg", mime: str = "image/jpeg") -> dict[str, Any]:
        """POST upload_identity_image，part 名 identity_image + 表单 user_id。

        返回 {identity_image_id, identity_image_url, identity_traits}；
        服务端内置人脸解析（gender / hair_color / skin_color）。
        """
        uid = self.account.firebase_uid or self.me()["id"]
        data = self._json(
            "POST",
            "upload_identity_image",
            files={"identity_image": (filename, content, mime)},
            data={"user_id": uid},
            timeout=120,
        )
        self.account.identity_image_id = data["identity_image_id"]
        self.account.identity_traits = data.get("identity_traits") or {}
        return data

    def ensure_identity(self, content: bytes, filename: str = "selfie.jpg", mime: str = "image/jpeg") -> dict[str, Any]:
        if self.account.identity_image_id:
            return {
                "identity_image_id": self.account.identity_image_id,
                "identity_traits": self.account.identity_traits,
            }
        return self.upload_identity(content, filename, mime)

    # ---------------------------------------------------------------- 生成

    def generate(
        self,
        identity_image_id: str,
        prompt: str,
        model_id: str,
        *,
        model_type: str = "standard",
        gender: str = "woman",
        skin_color: str = "white",
        hair_color: str = "brown",
        body_type: str = "lean",
        prompt_id: str | None = None,
        category: str | None = None,
        cohort: str | None = None,
    ) -> tuple[bytes, str]:
        """POST images/standard（@Streaming）：成功时响应体就是图片二进制。

        返回 (content, content_type)。
        """
        if model_type not in MODEL_TYPES:
            raise ValueError(f"model_type 必须是 {MODEL_TYPES}")
        if gender not in GENDERS:
            raise ValueError(f"gender 必须是 {GENDERS}")
        if skin_color not in SKIN_COLORS:
            raise ValueError(f"skin_color 必须是 {SKIN_COLORS}")
        if hair_color not in HAIR_COLORS:
            raise ValueError(f"hair_color 必须是 {HAIR_COLORS}")
        if body_type not in BODY_TYPES:
            raise ValueError(f"body_type 必须是 {BODY_TYPES}")
        body = {
            "identityImageId": identity_image_id,
            "gender": gender,
            "skinColor": skin_color,
            "hairColor": hair_color,
            "bodyType": body_type,
            "promptId": prompt_id,
            "prompt": prompt,
            "category": category,
            "cohort": cohort,
            "generationModelId": model_id,
            "modelType": model_type,
        }
        resp = self._raw_request("POST", "images/standard", json=body)
        self._raise_for_error(resp)
        ctype = resp.headers.get("content-type", "image/png")
        return resp.content, ctype

    def nano_banana(
        self,
        prompt: str,
        model_id: str,
        *,
        uploaded_image_ids: list[str] | None = None,
        generated_image_ids: list[str] | None = None,
        prompt_id: str | None = None,
    ) -> dict[str, Any]:
        """POST nano-banana/generate → {id, url, generated_image_ids, ...}。消耗 premiumTokens。"""
        body = {
            "generation_model_id": model_id,
            "uploaded_image_ids": uploaded_image_ids or [],
            "generated_image_ids": generated_image_ids or [],
            "prompt": prompt,
            "prompt_id": prompt_id,
        }
        return self._json("POST", "nano-banana/generate", json=body)

    def nano_banana_result(self, image_id: str) -> dict[str, Any]:
        return self._json("GET", f"nano-banana/{image_id}")

    def enhance_prompt(self, prompt: str, gender: str = "woman") -> str:
        data = self._json("POST", "prompts/enhance", json={"prompt": prompt, "gender": gender})
        return data.get("prompt", prompt)
