import os
import sys
import time
from playwright.sync_api import sync_playwright
from loguru import logger
from grabber import CourseGrabber

# 配置日志
logger.remove()
logger.add(sys.stdout, level="DEBUG", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

INJECT_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inject.js")
TARGET_DOMAIN = "https://zdbk.zju.edu.cn"


class CourseHunter:
    """
    主控流程：
    1. 启动浏览器 → 用户手动登录
    2. 检测选课页面 → 注入 inject.js
    3. 用户点击"抢课"按钮 → 回调 Python
    4. 提取 Cookie → 启动 Grabber 循环
    """

    def __init__(self):
        self.selected_course = None

    # ── 生命周期 ──

    def run(self):
        with open(INJECT_JS_PATH, "r", encoding="utf-8") as f:
            inject_js = f.read()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(no_viewport=True)

            # 每个新页面都暴露 Python 回调
            context.on("page", self._setup_page)

            page = context.new_page()
            self._setup_page(page)

            logger.info("正在打开教务系统，请手动登录...")
            try:
                page.goto(f"{TARGET_DOMAIN}/jwglxt/xtgl/index_initMenu.html")
            except Exception:
                pass

            logger.info("登录后请点击进入【自主选课】，脚本会自动识别新窗口。")

            # ── 阶段 1: 等待用户选课 ──
            try:
                self._wait_and_inject(context, inject_js)
            except KeyboardInterrupt:
                logger.info("用户取消，正在退出...")
                browser.close()
                return

            if not self.selected_course:
                browser.close()
                return

            # ── 阶段 2: 提取 Cookie 并启动抢课 ──
            course = self.selected_course
            logger.info(f"🎯 目标锁定: {course.get('course_name', '未知课程')}")
            logger.info("正在提取浏览器 Cookie...")

            # 获取所有 Cookie（不过滤域名，与 v1.py 手动复制整个 Cookie 的行为一致）
            all_cookies = context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)
            logger.debug(f"提取到 {len(all_cookies)} 个 Cookie, 总长度 {len(cookie_str)} 字符")
            logger.debug(f"Cookie 名称: {[c['name'] for c in all_cookies]}")

            # 提取浏览器 User-Agent
            try:
                user_agent = context.pages[0].evaluate("navigator.userAgent")
            except Exception:
                user_agent = "Mozilla/5.0"

            # 浏览器任务完成，关闭释放资源
            browser.close()
            logger.info("浏览器已关闭，进入纯请求模式。\n")

            su = course.get("su", "")
            if not su:
                logger.error("无法获取学号(su)，抢课无法进行。")
                return

            grabber = CourseGrabber(cookie_str, user_agent, su)
            try:
                success = grabber.grab(course)
            except KeyboardInterrupt:
                grabber.stop()
                logger.info("\n用户手动停止。")
                success = False

            if success:
                logger.success("🎉 恭喜，抢课成功！")
            else:
                logger.error("抢课结束（未成功）。如需重试请重新运行脚本。")

    # ── 内部方法 ──

    def _setup_page(self, page):
        """为新页面绑定 Python 回调函数"""
        try:
            page.expose_binding("py_grab_func", self._on_grab_request)
        except Exception:
            pass  # 同一 context 下 expose_binding 只需成功一次

    def _wait_and_inject(self, context, inject_js):
        """轮询所有页面，检测选课页面并注入脚本"""
        while not self.selected_course:
            if not context.pages:
                logger.error("所有页面已关闭，退出。")
                return

            for page in list(context.pages):
                try:
                    if page.is_closed():
                        continue
                    for frame in page.frames:
                        self._try_inject_frame(frame, inject_js)
                except Exception:
                    pass

            time.sleep(1.5)

    def _try_inject_frame(self, frame, inject_js):
        """对单个 Frame 尝试注入"""
        try:
            already = frame.evaluate(
                "() => document.documentElement.getAttribute('data-zju-injected') === 'true'"
            )
            if already:
                return

            has_content = frame.evaluate(
                "() => !!document.querySelector('.xuanke') || !!document.getElementById('sessionUserKey')"
            )
            if not has_content:
                return

            frame.evaluate(inject_js)

            ok = frame.evaluate(
                "() => document.documentElement.getAttribute('data-zju-injected') === 'true'"
            )
            if ok:
                logger.success("✅ 抢课助手已注入！请在页面中点击目标课程的【抢课】按钮。")
        except Exception:
            pass

    def _on_grab_request(self, source, data):
        """JS 端点击抢课按钮后的回调"""
        name = data.get("course_name", "未知课程")
        xkkh = data.get("xkkh", "")
        logger.info(f"收到选课请求: {name} ({xkkh})")
        self.selected_course = data


def main():
    print(
        "\n"
        "  ╔══════════════════════════════════════════╗\n"
        "  ║     ZJU Course Hunter  抢课助手 v3.0     ║\n"
        "  ╠══════════════════════════════════════════╣\n"
        "  ║  1. 在弹出的浏览器中手动登录教务系统     ║\n"
        "  ║  2. 点击进入【自主选课】                 ║\n"
        "  ║  3. 展开课程，点击红色的【抢课】按钮     ║\n"
        "  ║  4. 按 Ctrl+C 可随时停止                 ║\n"
        "  ╚══════════════════════════════════════════╝\n"
    )
    CourseHunter().run()


if __name__ == "__main__":
    main()
