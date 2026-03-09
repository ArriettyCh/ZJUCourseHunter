import os
import sys
import json
import signal
import threading
import time
from typing import Optional
from bootstrap_env import ensure_runtime_ready

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ensure_runtime_ready(BASE_DIR)

import yaml
from playwright.sync_api import sync_playwright
from loguru import logger
from auth import cas_login
from grabber import CourseGrabber

# ── 配置 ──
logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

# ── 全局退出控制 ──
_shutdown_event = threading.Event()


def _handle_shutdown(signum, frame):
    sig_name = signal.Signals(signum).name
    if _shutdown_event.is_set():
        print()
        logger.warning(f"再次收到 {sig_name}，强制退出")
        os._exit(1)
    print()
    logger.info(f"收到 {sig_name}，正在优雅退出...")
    _shutdown_event.set()


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
INJECT_JS_PATH = os.path.join(BASE_DIR, "inject.js")
CREDENTIALS_PATH = os.path.join(BASE_DIR, "data/credentials.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # YAML 的 >- 折叠会保留换行为空格，去掉多余空白
    urls = cfg.get("urls", {})
    for key in urls:
        if isinstance(urls[key], str):
            urls[key] = urls[key].replace("\n", "").replace(" ", "")

    return cfg


CFG = load_config()
GRAB_CFG = CFG.get("grab", {})
URL_CFG = CFG.get("urls", {})

COURSE_SELECT_URL = URL_CFG.get(
    "course_select",
    "https://zdbk.zju.edu.cn/jwglxt/xsxk/zzxkghb_cxZzxkGhbIndex.html?gnmkdm=N253530&layout=default&su={su}",
)
SSO_BOOTSTRAP_URL = URL_CFG.get(
    "sso_bootstrap",
    "https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html",
)
INDEX_URL = URL_CFG.get(
    "index",
    "https://zdbk.zju.edu.cn/jwglxt/xtgl/index_initMenu.html?jsdm=06",
)


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
    os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
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
        self._inject_js = ""
        self._inject_lock = threading.Lock()

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

        if _shutdown_event.is_set():
            return

        if not creds:
            username, password = prompt_credentials()
            self.su = username
            try:
                self.session = cas_login(username, password)
            except RuntimeError as e:
                logger.error(f"登录失败: {e}")
                return
            save_credentials(username, password)

        if _shutdown_event.is_set():
            return

        # ── 阶段 1: 打开 Playwright 浏览器，注入 Cookie，直接进选课页 ──
        with open(INJECT_JS_PATH, "r", encoding="utf-8") as f:
            self._inject_js = f.read()

        try:
            self._run_browser_stage()
        except Exception:
            if _shutdown_event.is_set():
                return
            raise

    def _close_browser(self, browser):
        try:
            browser.close()
        except Exception:
            pass

    def _run_browser_stage(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(no_viewport=True)

            self._inject_cookies(context)

            context.on("page", self._setup_page)
            page = context.new_page()
            self._setup_page(page)

            logger.info("正在初始化浏览器登录态...")
            self._bootstrap_browser_session(context)

            if _shutdown_event.is_set():
                self._close_browser(browser)
                return

            target_url = COURSE_SELECT_URL.format(su=self.su)
            logger.info("正在打开选课页面...")
            try:
                page.goto(target_url, wait_until="domcontentloaded")
            except Exception:
                pass

            logger.info("请在页面中展开课程，点击红色的【抢课】按钮。")

            # ── 阶段 2: 等待用户选课（事件驱动 + 轻量保底轮询） ──
            self._wait_for_selection(context)

            if _shutdown_event.is_set() or not self.selected_course:
                self._close_browser(browser)
                if _shutdown_event.is_set():
                    logger.info("已取消选课，再见")
                return

            course = self.selected_course
            logger.info(f"目标锁定: {course.get('course_name', '未知课程')}")

            self._close_browser(browser)
            logger.info("浏览器已关闭，进入纯请求模式。\n")

            # ── 阶段 3: 抢课 ──
            grabber = CourseGrabber(self.session, self.su, _shutdown_event)
            success = grabber.grab(
                course,
                interval=GRAB_CFG.get("interval", 1.0),
                jitter=GRAB_CFG.get("jitter", 0.3),
                max_attempts=GRAB_CFG.get("max_attempts", 0),
                request_timeout=GRAB_CFG.get("request_timeout", 10),
            )

            if success:
                logger.success("抢课成功！")
            elif _shutdown_event.is_set():
                logger.info("用户停止抢课，再见")
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
            if cookie.secure:
                pw_cookie["secure"] = True
            if cookie.expires:
                pw_cookie["expires"] = cookie.expires
            cookies_for_pw.append(pw_cookie)

        if cookies_for_pw:
            context.add_cookies(cookies_for_pw)

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

        page.on("framenavigated", lambda frame: self._on_frame_navigated(frame))

    def _on_frame_navigated(self, frame):
        """当 frame 导航完成时尝试注入，替代轮询"""
        if self.selected_course:
            return
        with self._inject_lock:
            self._try_inject_frame(frame, self._inject_js)

    def _wait_for_selection(self, context):
        """
        等待用户选课。
        注入主要由 framenavigated 事件驱动，此处仅做保底轮询
        （处理事件可能错过的 frame，如页面在事件注册前就已加载的情况）。
        """
        self._scan_all_frames(context)

        while not self.selected_course and not _shutdown_event.is_set():
            if not context.pages:
                logger.error("所有页面已关闭，退出。")
                return
            _shutdown_event.wait(timeout=3)
            if not _shutdown_event.is_set():
                self._scan_all_frames(context)

    def _scan_all_frames(self, context):
        """扫描所有页面的所有 frame，尝试注入"""
        for pg in list(context.pages):
            try:
                if pg.is_closed():
                    continue
                for frame in pg.frames:
                    self._try_inject_frame(frame, self._inject_js)
            except Exception:
                pass

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
    try:
        CourseHunter().run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"未预期的异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
