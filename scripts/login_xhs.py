"""小红书交互式登录脚本（用系统 Edge 浏览器）。

用法：
  d:\\agentplatform\\.venv\\Scripts\\python.exe d:\\agentplatform\\scripts\\login_xhs.py

流程：
  1. 弹出 Edge 浏览器，打开小红书登录页
  2. 你手动扫码或账号密码登录
  3. 登录成功后回到此终端按回车，脚本保存 Cookie
  4. 之后 Agent 调用 xhs__reload_cookies 即可直接使用

可选环境变量：
  XHS_PROXY=http://user:pass@host:port  配置代理
"""
import json
import os
import sys
import time
from pathlib import Path

COOKIE_DIR = Path.home() / ".xhs-mcp"
COOKIE_FILE = COOKIE_DIR / "cookies.json"


def main():
    from playwright.sync_api import sync_playwright

    COOKIE_DIR.mkdir(parents=True, exist_ok=True)

    proxy_url = os.environ.get("XHS_PROXY", "").strip()
    launch_kwargs = {
        "headless": False,  # 有头模式
        "slow_mo": 200,     # 每个操作慢一点
    }
    # 用系统已安装的 Edge
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if os.path.exists(edge_path):
        launch_kwargs["executable_path"] = edge_path
        print(f"使用系统 Edge: {edge_path}")
    else:
        print("未找到系统 Edge，用 Playwright 自带 Chromium")

    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}
        print(f"使用代理: {proxy_url}")

    print("\n[1/4] 正在启动浏览器...")
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(**launch_kwargs)
        print("[2/4] 浏览器已启动")
    except Exception as e:
        print(f"浏览器启动失败: {e}")
        print("\n请手动检查：")
        print("  1. 关闭所有 Edge 窗口（脚本需要独占控制）")
        print("  2. 网络是否正常")
        input("按回车退出...")
        sys.exit(1)

    try:
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        print("[3/4] 打开小红书...")
        page.goto("https://www.xiaohongshu.com", timeout=120000, wait_until="domcontentloaded")
        print("  页面已打开，URL:", page.url)
        time.sleep(1)

        print()
        print("=" * 60)
        print("  浏览器已打开小红书")
        print()
        print("  请在浏览器中手动登录：")
        print("    - 点击右上角「登录」")
        print("    - 扫码或账号密码登录")
        print("    - 确认登录成功（能看到头像/用户名）")
        print()
        print("  登录成功后，回到此终端按回车保存 Cookie")
        print("=" * 60)

        # 用 input() 阻塞，等用户手动确认登录完成
        # 这是唯一可靠的方式：小红书匿名会话也有 web_session cookie
        # 不能用 cookie 自动检测
        input("\n登录完成后按回车继续...")

        # 保存 cookie
        cookies = ctx.cookies()
        if not cookies:
            print("警告：未获取到 cookie，可能登录失败")
            input("按回车退出...")
            browser.close()
            pw.stop()
            sys.exit(1)

        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        print(f"\n[4/4] Cookie 已保存到: {COOKIE_FILE}")
        print(f"共保存 {len(cookies)} 个 cookie")
        print("\n现在可以关闭浏览器了。")
        print("Agent 下次调用 xhs__reload_cookies 会自动加载这些 Cookie。")

        time.sleep(2)
        browser.close()
        pw.stop()

        print("\n完成！按回车退出。")
        input()

    except Exception as e:
        print(f"\n运行异常: {e}")
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        input("\n按回车退出...")
        sys.exit(1)


if __name__ == "__main__":
    main()
