"""
ZJU CAS 自动登录模块。
实现逆向工程.md 中描述的浙大统一身份认证流程：
  GET 登录页 → 获取 execution token
  GET 公钥 → RSA 加密密码（反转 + textbook RSA）
  POST 登录 → 跟随 302 重定向获取目标系统 Cookie
"""

import re
import requests
from loguru import logger

CAS_LOGIN_URL = "https://zjuam.zju.edu.cn/cas/login"
CAS_PUBKEY_URL = "https://zjuam.zju.edu.cn/cas/v2/getPubKey"
# CAS 标准服务入口（会先落到 jwglxt 的 sso 登录桥接地址）
SERVICE_URL = "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html"
INDEX_URL = "https://zdbk.zju.edu.cn/jwglxt/xtgl/index_initMenu.html?jsdm=06"


def _rsa_encrypt(password: str, modulus_hex: str, exponent_hex: str) -> str:
    """
    还原自教务系统 security.js 的 RSAUtils.encryptedString。
    步骤：反转密码 → 按 2 字节分块构造大整数 → textbook RSA（无填充）→ 十六进制输出。
    """
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)

    # 反转密码
    reversed_pwd = password[::-1]

    # 按 2 字节 (little-endian) 分块
    # 每块最多 2 字节 = 16 bit
    byte_array = [ord(c) for c in reversed_pwd]
    key_len = (n.bit_length() + 7) // 8  # RSA 密钥的字节长度

    # 每块能放 key_len 个字节，但原始 JS 实现中每块放 2 个字符
    # 实际上 JS 的实现是按 key_len*2 位 (十六进制位) 的块大小
    # 简化：每块 key_len 字节，每次取 2 字节组成 16-bit int
    block_size = key_len * 2  # 十六进制位数

    result_parts = []
    i = 0
    while i < len(byte_array):
        # 构造一个大整数块
        block = 0
        j = 0
        while i < len(byte_array) and j < key_len:
            # little-endian: 低字节在前
            block += byte_array[i] << (j * 8)
            i += 1
            j += 1
            if i < len(byte_array) and j < key_len:
                block += byte_array[i] << (j * 8)
                i += 1
                j += 1

        # textbook RSA: crypt = block^e mod n
        crypt = pow(block, e, n)
        # 转十六进制，补齐到 block_size 位
        hex_str = format(crypt, 'x')
        # 长度不足时前面补零（但 JS 原版实现中加密结果可能不补零）
        result_parts.append(hex_str)

    return "".join(result_parts)


def cas_login(username: str, password: str) -> requests.Session:
    """
    执行 CAS 登录，返回已认证的 requests.Session。
    Session 中会包含教务系统所需的全部 Cookie。

    Raises:
        RuntimeError: 登录失败时抛出
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })

    login_url = f"{CAS_LOGIN_URL}?service={SERVICE_URL}"

    # ── Step 1: GET 登录页，提取 execution token ──
    logger.info("正在获取登录页...")
    resp = session.get(login_url)
    resp.raise_for_status()

    match = re.search(r'name="execution"\s+value="([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("无法从登录页提取 execution token，CAS 页面结构可能已变更。")
    execution = match.group(1)

    # ── Step 2: GET RSA 公钥 ──
    logger.info("正在获取 RSA 公钥...")
    resp = session.get(CAS_PUBKEY_URL)
    resp.raise_for_status()
    pub_key = resp.json()
    modulus = pub_key["modulus"]
    exponent = pub_key["exponent"]

    # ── Step 3: 加密密码 ──
    encrypted_password = _rsa_encrypt(password, modulus, exponent)

    # ── Step 4: POST 登录 ──
    logger.info("正在提交登录...")
    form_data = {
        "username": username,
        "password": encrypted_password,
        "authcode": "",
        "execution": execution,
        "_eventId": "submit",
    }
    resp = session.post(login_url, data=form_data, allow_redirects=True)

    # ── Step 5: 判断结果 ──
    # 仅“到了 zdbk 域名”并不等于成功，可能只是被重定向回登录页。
    lower_url = resp.url.lower()
    if "cas/login" not in lower_url and "login_slogin" not in lower_url:
        # 再主动访问一次首页，确认会话在 jwglxt 侧已生效
        verify_resp = session.get(INDEX_URL, allow_redirects=True)
        verify_url = verify_resp.url.lower()
        if "cas/login" not in verify_url and "login_slogin" not in verify_url:
            logger.success(f"登录成功！(用户: {username})")
            return session

    # 失败：尝试提取错误信息
    # err_match = re.search(r'id="errormsg"[^>]*>([^<]+)', resp.text)
    # err_msg = err_match.group(1).strip() if err_match else "未知错误"
    raise RuntimeError(f"登录失败，请重新运行脚本尝试")
