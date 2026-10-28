"""本地压测（针对 demo server，不直接打上游）。

用法：
    python bench.py                  # 默认 http://127.0.0.1:8931
    python bench.py --base http://127.0.0.1:8931 --concurrency 8 --total 32
    python bench.py --health-only

约束：real 模式压测会真实消耗账号额度，脚本默认只跑 mock 建议的画像，
并用 --allow-real 显式二次确认。
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import requests

B64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f0300050001a5f645400000000049454e44ae426082"
    )
).decode()


def one(base: str, model: str) -> tuple[bool, float, str]:
    t0 = time.time()
    try:
        r = requests.post(
            f"{base}/v1/images/generations",
            json={"model": model, "prompt": "bench portrait", "size": "512x512", "response_format": "b64_json"},
            timeout=300,
        )
        dt = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            ok = bool(data.get("data")) and bool(data["data"][0].get("b64_json") or data["data"][0].get("url"))
            return ok, dt, "" if ok else "empty data"
        return False, dt, f"HTTP {r.status_code} {r.text[:120]}"
    except Exception as exc:
        return False, (time.time() - t0) * 1000, f"{type(exc).__name__}: {exc}"


def health_rps(base: str, seconds: float = 3.0) -> float:
    n = 0
    t_end = time.time() + seconds
    with ThreadPoolExecutor(max_workers=10) as pool:
        while time.time() < t_end:
            list(pool.map(lambda _: requests.get(f"{base}/health", timeout=5), range(20)))
            n += 20
    return n / seconds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8931")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--total", type=int, default=32)
    ap.add_argument("--model", default="photify/starter")
    ap.add_argument("--health-only", action="store_true")
    ap.add_argument("--allow-real", action="store_true")
    args = ap.parse_args()

    h = requests.get(f"{args.base}/health", timeout=5).json()
    print(f"target {args.base} mode={h['mode']} accounts={h['accounts']}")
    if h["mode"] == "real" and not args.allow_real:
        print("refuse: real 模式会消耗账号额度，需要 --allow-real 显式确认")
        return

    print(f"health throughput: {health_rps(args.base):.0f} req/s")

    if args.health_only:
        return

    lat, ok, errs = [], 0, {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for good, dt, err in pool.map(lambda _: one(args.base, args.model), range(args.total)):
            lat.append(dt)
            if good:
                ok += 1
            else:
                errs[err] = errs.get(err, 0) + 1
    wall = time.time() - t0
    lat.sort()

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    print(
        f"gen: total={args.total} concurrency={args.concurrency} ok={ok} "
        f"fail={args.total - ok} wall={wall:.1f}s rps={args.total / wall:.2f} "
        f"mean={statistics.mean(lat):.0f}ms p50={pct(0.5):.0f}ms p95={pct(0.95):.0f}ms max={lat[-1]:.0f}ms"
    )
    if errs:
        for k, v in errs.items():
            print(f"  err x{v}: {k}")

    print("error-path checks:")
    for payload, expect in [
        ({"model": "photify/supreme-ultra"}, 400),
        ({"model": "nope", "prompt": "x"}, 400),
        ({"model": "photify/supreme-ultra", "prompt": "x", "image": "!!!notbase64"}, 400),
    ]:
        r = requests.post(f"{args.base}/v1/images/generations", json=payload, timeout=30)
        flag = "OK" if r.status_code == expect else "MISMATCH"
        print(f"  {flag} expect {expect} got {r.status_code}: {json.dumps(payload)[:60]}")


if __name__ == "__main__":
    main()
