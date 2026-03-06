import requests
import time
from loguru import logger


class CourseGrabber:
    """
    抢课核心引擎。
    接收从浏览器提取的原始 Cookie 字符串和课程数据，
    以 v1.py 验证过的方式构造 requests 请求进行抢课。
    """

    BASE_URL = "https://zdbk.zju.edu.cn/jwglxt/xsxk/zzxkghb_xkBcZyZzxkGhb.html"
    REFERER_TPL = "https://zdbk.zju.edu.cn/jwglxt/xsxk/zzxkghb_cxZzxkGhbIndex.html?gnmkdm=N253530&layout=default&su={su}"

    def __init__(self, cookie_str: str, user_agent: str, su: str):
        """
        Args:
            cookie_str: 原始 Cookie 字符串, 格式 "k1=v1; k2=v2; ..."
            user_agent: 浏览器的 User-Agent（与登录时保持一致）
            su: 学号
        """
        self.su = su
        self.running = False

        self.session = requests.Session()
        # 严格按照 v1.py 经过验证的 Headers 设置
        self.session.headers.update({
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Referer": self.REFERER_TPL.format(su=su),
        })

    def grab(self, course_data: dict, interval: float = 1.0) -> bool:
        """
        开始抢课循环。

        Args:
            course_data: 包含 xn, xq, nj, xkkh, tabname 等字段
            interval: 请求间隔（秒）

        Returns:
            True 表示抢课成功, False 表示因错误终止
        """
        self.running = True
        url = f"{self.BASE_URL}?gnmkdm=N253530&su={self.su}"

        # 严格复用 v1.py 验证过的 6 个字段，不增不减
        form_data = {
            "xn": course_data["xn"],
            "xq": course_data["xq"],
            "nj": course_data["nj"],
            "xkkh": course_data["xkkh"],
            "tabname": course_data.get("tabname", "xkrw2006view"),
            "xkzys": course_data.get("xkzys", "1"),
        }

        course_name = course_data.get("course_name", "未知课程")
        logger.info(f"🎯 目标: {course_name}")
        logger.info(f"📋 选课号: {course_data['xkkh']}")
        logger.info(f"⏱️  请求间隔: {interval}s | 按 Ctrl+C 可随时停止")

        attempt = 0
        while self.running:
            attempt += 1
            try:
                t0 = time.time()
                resp = self.session.post(url, data=form_data, timeout=10)
                elapsed_ms = (time.time() - t0) * 1000

                if resp.status_code != 200:
                    body_preview = resp.text[:200] if resp.text else "(空)"
                    logger.error(f"#{attempt} HTTP {resp.status_code} ({elapsed_ms:.0f}ms) | {body_preview}")

                    if resp.status_code in (901, 403) and attempt == 1:
                        # 第一次遇到 901 时打印调试信息帮助排查
                        logger.debug(f"响应头: {dict(resp.headers)}")
                        logger.debug(f"请求头: {dict(resp.request.headers)}")
                        logger.debug(f"Cookie 长度: {len(resp.request.headers.get('Cookie', ''))}")

                    time.sleep(interval)
                    continue

                result = resp.json()
                msg = result.get("msg", str(result))
                flag = result.get("flag")

                if flag == "1" or "成功" in str(msg):
                    logger.success(f"#{attempt} ✅ 选课成功! ({elapsed_ms:.0f}ms) | {msg}")
                    return True

                if "登录" in str(msg) or "超时" in str(msg):
                    logger.critical(f"#{attempt} Session 过期: {msg}")
                    return False

                logger.warning(f"#{attempt} ⏳ {msg} ({elapsed_ms:.0f}ms)")

            except KeyboardInterrupt:
                raise  # 向上传播，由调用方处理
            except requests.exceptions.Timeout:
                logger.error(f"#{attempt} 请求超时")
            except Exception as e:
                logger.error(f"#{attempt} 异常: {e}")

            time.sleep(interval)

        return False

    def stop(self):
        self.running = False
