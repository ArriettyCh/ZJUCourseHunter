import random
import threading
import requests
import time
from loguru import logger


class CourseGrabber:
    """
    抢课核心引擎。
    直接复用 auth.py 登录后的 requests.Session（Cookie 已在 Session 中），
    避免任何 Cookie 格式转换。
    """

    GRAB_URL = "https://zdbk.zju.edu.cn/jwglxt/xsxk/zzxkghb_xkBcZyZzxkGhb.html"
    REFERER_TPL = (
        "https://zdbk.zju.edu.cn/jwglxt/xsxk/"
        "zzxkghb_cxZzxkGhbIndex.html?gnmkdm=N253530&layout=default&su={su}"
    )

    def __init__(self, session: requests.Session, su: str,
                 shutdown_event: threading.Event | None = None):
        self.session = session
        self.su = su
        self.running = False
        self._shutdown = shutdown_event or threading.Event()

        self.session.headers.update({
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.REFERER_TPL.format(su=su),
        })

    def grab(
        self,
        course_data: dict,
        interval: float = 1.0,
        jitter: float = 0.3,
        max_attempts: int = 0,
        request_timeout: int = 10,
    ) -> bool:
        """
        开始抢课循环。

        Args:
            course_data: 包含 xn, xq, nj, xkkh, tabname 等字段
            interval: 基础请求间隔（秒）
            jitter: 随机抖动幅度（秒），实际间隔 = interval ± jitter
            max_attempts: 最大重试次数，0 表示无限制
            request_timeout: 单次请求超时（秒）

        Returns:
            True 表示抢课成功, False 表示因错误终止
        """
        self.running = True
        url = f"{self.GRAB_URL}?gnmkdm=N253530&su={self.su}"

        form_data = {
            "xn": course_data["xn"],
            "xq": course_data["xq"],
            "nj": course_data["nj"],
            "xkkh": course_data["xkkh"],
            "tabname": course_data.get("tabname", "xkrw2006view"),
            "xkzys": course_data.get("xkzys", "1"),
        }

        course_name = course_data.get("course_name", "未知课程")

        logger.info(f"目标: {course_name}")
        logger.info(f"选课号: {course_data['xkkh']}")
        if course_data.get("semester"):
            logger.info(f"学期: {course_data['semester']}")
        if course_data.get("teacher"):
            logger.info(f"教师: {course_data['teacher']}")
        if course_data.get("schedule"):
            logger.info(f"时间: {course_data['schedule']}")
        if course_data.get("location"):
            logger.info(f"地点: {course_data['location']}")
        limit_desc = f"最多 {max_attempts} 次" if max_attempts > 0 else "无限制"
        logger.info(f"请求间隔: {interval}±{jitter}s | 重试: {limit_desc} | 按 Ctrl+C 可随时停止\n")

        attempt = 0
        while self.running and not self._shutdown.is_set():
            attempt += 1

            if max_attempts > 0 and attempt > max_attempts:
                logger.error(f"已达到最大重试次数 ({max_attempts})，停止抢课。")
                return False

            try:
                t0 = time.time()
                resp = self.session.post(url, data=form_data, timeout=request_timeout)
                elapsed_ms = (time.time() - t0) * 1000

                if resp.status_code != 200:
                    body_preview = resp.text[:200] if resp.text else "(空)"
                    logger.error(f"#{attempt} HTTP {resp.status_code} ({elapsed_ms:.0f}ms) | {body_preview}")
                    self._sleep_with_jitter(interval, jitter)
                    continue

                result = resp.json()
                msg = result.get("msg", str(result))
                flag = result.get("flag")

                if flag == "1" or "成功" in str(msg):
                    logger.success(f"#{attempt} 选课成功! ({elapsed_ms:.0f}ms) | {msg}")
                    return True

                if "登录" in str(msg) or "超时" in str(msg):
                    logger.critical(f"#{attempt} Session 过期: {msg}")
                    return False

                logger.warning(f"#{attempt} {msg} ({elapsed_ms:.0f}ms)")

            except requests.exceptions.Timeout:
                logger.error(f"#{attempt} 请求超时")
            except Exception as e:
                logger.error(f"#{attempt} 异常: {e}")

            self._sleep_with_jitter(interval, jitter)

        return False

    def _sleep_with_jitter(self, interval: float, jitter: float):
        actual = max(0.1, interval + random.uniform(-jitter, jitter))
        self._shutdown.wait(timeout=actual)

    def stop(self):
        self.running = False
