#!/usr/bin/env python3
"""容错阶梯末级：headless 渲染读取可见文本（处理 JS 渲染页）。

仅在 WebFetch + requests 静态抓取都失败时由主 agent 调用，受 headless_budget 约束。
复用系统已装的 Chromium 系浏览器（不下载 playwright 自带 chromium）。

护栏：仅渲染读文本、不登录、不绕验证码、超时 15s、尊重站点 ToS。

用法:  python fetch_rendered.py <url>
输出:  {"ok": true, "text": "...", "browser_used": "chrome"}
       {"ok": false, "error": "..."}   (playwright 未装 / 无可用浏览器 → 上层应跳过)
"""
from __future__ import annotations

import json
import sys

_TEXT_LIMIT = 8000
_TIMEOUT_MS = 15000


def read_default_progid() -> str:
    """读 Windows 默认浏览器 ProgId（仅 Windows）。"""
    try:
        import winreg
        path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, "ProgId")[0]
    except Exception:
        return ""


def browser_candidates() -> list[dict]:
    """按"系统默认优先 → 回退枚举"返回 playwright launch 参数候选。

    只覆盖 Chromium 系（chromium.launch 限制）；默认是 Firefox/Safari 等非
    Chromium 时，回退尝试系统可能也装着的 Edge/Chrome。
    """
    progid = read_default_progid()
    pref = {
        "ChromeHTML": {"channel": "chrome"},
        "MSEdgeHTML": {"channel": "msedge"},
        "MSEdgeDHTML": {"channel": "msedge"},
        "BraveHTML": {"channel": "chrome"},  # Brave 多与 chrome channel 兼容，失败再回退
    }.get(progid)

    cands: list[dict] = []
    if pref:
        cands.append(pref)
    for c in ({"channel": "msedge"}, {"channel": "chrome"}):  # Win 上 Edge 基本必装
        if c not in cands:
            cands.append(c)
    return cands


def fetch(url: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright 未安装（pip install playwright），跳过 headless 兜底"}

    last_err = "无可用 Chromium 系浏览器"
    with sync_playwright() as p:
        for opt in browser_candidates():
            browser = None
            try:
                browser = p.chromium.launch(headless=True, **opt)
                page = browser.new_page(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"))
                page.goto(url, timeout=_TIMEOUT_MS, wait_until="networkidle")
                text = page.inner_text("body")
                return {"ok": True, "text": (text or "")[:_TEXT_LIMIT],
                        "browser_used": opt.get("channel", "chromium")}
            except Exception as e:
                last_err = f"{opt.get('channel','?')}: {type(e).__name__}: {e}"
                continue
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    return {"ok": False, "error": last_err}


def main() -> None:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "用法: python fetch_rendered.py <url>"}))
        sys.exit(1)
    # 纯 ASCII 输出，跨 console 编码安全（与其它脚本一致）
    print(json.dumps(fetch(sys.argv[1])))


if __name__ == "__main__":
    main()
