import os
import sys
import json
import time
from typing import Optional
from bootstrap_env import ensure_runtime_ready

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ensure_runtime_ready(BASE_DIR)

from playwright.sync_api import sync_playwright
from loguru import logger
from auth import cas_login
from grabber import CourseGrabber

# ── 配置 ──
logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

INJECT_JS_PATH = os.path.join(BASE_DIR, "inject.js")
CREDENTIALS_PATH = os.path.join(BASE_DIR, "data/credentials.json")

COURSE_SELECT_URL = (
    "https://zdbk.zju.edu.cn/jwglxt/xsxk/"
    "zzxkghb_cxZzxkGhbIndex.html?gnmkdm=N253530&layout=default&su={su}"
)
SSO_BOOTSTRAP_URL = (
    "https://zjuam.zju.edu.cn/cas/login?"
    "service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html"
)
INDEX_URL = "https://zdbk.zju.edu.cn/jwglxt/xtgl/index_initMenu.html?jsdm=06"


# ── 凭证管理 ──

def load_credentials() -> Optional[dict]:
    """从本地文件读取保存的账号密码，不存在则返回 None"""
    if not os.path.exists(CREDENTIALS_PATH):
        return None
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            creds = json.load(f)
        if creds.get("username") and creds.get("password"):
            return creds
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_credentials(username: str, password: str):
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump({"username": username, "password": password}, f, ensure_ascii=False)
    logger.info(f"凭证已保存到 {CREDENTIALS_PATH}")


def prompt_credentials() -> tuple[str, str]:
    """在命令行中要求用户输入账号密码"""
    print()
    username = input("  请输入学号: ").strip()
    password = input("  请输入密码: ").strip()
    return username, password


# ── 主流程 ──

class CourseHunter:
    """
    完整流程:
    1. 读取/输入凭证 → requests 自动登录 CAS
    2. 将 Session Cookie 注入 Playwright 浏览器 → 直接打开选课页
    3. 用户在页面中点击"抢课"按钮 → 回调 Python
    4. 复用同一 Session 启动 Grabber 循环
    """

    def __init__(self):
        self.selected_course = None
        self.session = None  # requests.Session (登录后)
        self.su = ""         # 学号

    def run(self):
        # ── 阶段 0: 获取凭证并登录 ──
        creds = load_credentials()
        if creds:
            logger.info(f"检测到已保存的凭证 (用户: {creds['username']})")
            self.su = creds["username"]
            try:
                self.session = cas_login(creds["username"], creds["password"])
            except RuntimeError as e:
                logger.error(f"自动登录失败，请重新输入账号密码")
                creds = None

        if not creds:
            username, password = prompt_credentials()
            self.su = username
            try:
                self.session = cas_login(username, password)
            except RuntimeError as e:
                logger.error(f"登录失败: {e}")
                return
            save_credentials(username, password)

        # ── 阶段 1: 打开 Playwright 浏览器，注入 Cookie，直接进选课页 ──
        with open(INJECT_JS_PATH, "r", encoding="utf-8") as f:
            inject_js = f.read()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(no_viewport=True)

            # 将 requests.Session 中的 Cookie 注入到 Playwright context
            self._inject_cookies(context)

            context.on("page", self._setup_page)
            page = context.new_page()
            self._setup_page(page)

            # 在后台走一遍 CAS -> jwglxt 的标准跳转链路，避免页面闪现登录界面
            logger.info("正在初始化浏览器登录态...")
            self._bootstrap_browser_session(context)

            # 再打开选课页面
            target_url = COURSE_SELECT_URL.format(su=self.su)
            logger.info("正在打开选课页面...")
            try:
                page.goto(target_url, wait_until="domcontentloaded")
            except Exception:
                pass

            logger.info("请在页面中展开课程，点击红色的【抢课】按钮。")

            # ── 阶段 2: 等待用户选课 ──
            try:
                self._wait_and_inject(context, inject_js)
            except KeyboardInterrupt:
                logger.info("用户取消，正在退出...")
                browser.close()
                return

            if not self.selected_course:
                browser.close()
                return

            course = self.selected_course
            logger.info(f"目标锁定: {course.get('course_name', '未知课程')}")

            browser.close()
            logger.info("浏览器已关闭，进入纯请求模式。\n")

            # ── 阶段 3: 抢课 ──
            # 直接复用登录时的 Session，Cookie 完美一致
            grabber = CourseGrabber(self.session, self.su)
            try:
                success = grabber.grab(course)
            except KeyboardInterrupt:
                grabber.stop()
                logger.info("\n用户手动停止。")
                success = False

            if success:
                logger.success("抢课成功！")
            else:
                logger.error("抢课结束（未成功）。如需重试请重新运行脚本。")

    # ── 内部方法 ──

    def _inject_cookies(self, context):
        """将 requests.Session 的 Cookie 注入到 Playwright BrowserContext"""
        cookies_for_pw = []
        for cookie in self.session.cookies:
            if not cookie.domain:
                continue
            pw_cookie = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
            }
            # Playwright 要求 secure 和 sameSite 字段
            if cookie.secure:
                pw_cookie["secure"] = True
            if cookie.expires:
                pw_cookie["expires"] = cookie.expires
            cookies_for_pw.append(pw_cookie)

        if cookies_for_pw:
            context.add_cookies(cookies_for_pw)
            # logger.debug(f"已注入 {len(cookies_for_pw)} 个 Cookie 到浏览器")

    def _bootstrap_browser_session(self, context):
        """在后台触发 SSO 跳转，让浏览器上下文拿到 jwglxt 侧会话"""
        try:
            context.request.get(SSO_BOOTSTRAP_URL, timeout=20000)
        except Exception:
            logger.warning("SSO 引导页加载超时，尝试继续访问系统主页。")

        try:
            context.request.get(INDEX_URL, timeout=20000)
        except Exception:
            pass

    def _setup_page(self, page):
        try:
            page.expose_binding("py_grab_func", self._on_grab_request)
        except Exception:
            pass

    def _wait_and_inject(self, context, inject_js):
        while not self.selected_course:
            if not context.pages:
                logger.error("所有页面已关闭，退出。")
                return

            for pg in list(context.pages):
                try:
                    if pg.is_closed():
                        continue
                    for frame in pg.frames:
                        self._try_inject_frame(frame, inject_js)
                except Exception:
                    pass

            time.sleep(1.5)

    def _try_inject_frame(self, frame, inject_js):
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
                logger.success("抢课助手已注入！请在页面中点击目标课程的【抢课】按钮")
        except Exception:
            pass

    def _on_grab_request(self, source, data):
        name = data.get("course_name", "未知课程")
        xkkh = data.get("xkkh", "")
        logger.info(f"收到选课请求: {name} ({xkkh})")
        self.selected_course = data


def main():
    CourseHunter().run()


if __name__ == "__main__":
    main()
