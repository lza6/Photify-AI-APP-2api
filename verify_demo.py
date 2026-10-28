"""demo 页真实浏览器验证：打开页面 → 上传人像 → 点生成 → 断言出现结果图。

用法：
    python verify_demo.py                 # 默认 http://127.0.0.1:8981/demo
    python verify_demo.py --url ... --out shot.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8981/demo")
    ap.add_argument("--image", default="assets/default_identity.jpg")
    ap.add_argument("--out", default="output/demo_shot.png")
    args = ap.parse_args()

    image = Path(args.image)
    if not image.exists():
        raise SystemExit(f"缺少测试人像 {image}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(args.url, wait_until="networkidle")
        assert "Photify" in page.title(), page.title()

        mode = page.inner_text("#mode")
        page.set_input_files("#file", str(image))
        page.fill("#prompt", "browser e2e check, studio portrait")
        page.click("#go")

        page.wait_for_function("document.querySelectorAll('#out img').length > 0", timeout=120_000)
        src = page.get_attribute("#out img", "src")
        assert src and "/files/" in src, src

        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=args.out)
        print(f"PASS demo e2e | {mode} | 结果图 {src} | 截图 {args.out}")
        browser.close()


if __name__ == "__main__":
    main()
