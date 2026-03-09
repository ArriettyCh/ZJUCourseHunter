import importlib.util
import os
import re
import subprocess
import sys


REQUIRED_MODULES = ("requests", "loguru", "playwright", "yaml")

# import 名 → pip 包名（不一致时需要映射）
MODULE_TO_PACKAGE = {"yaml": "pyyaml"}


def _is_module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _run_command(cmd: list[str], desc: str):
    print(f"[env] {desc}...")
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{desc}失败，请检查网络，或更新 Python 环境") from exc


def _load_requirements(requirements_path: str) -> list[str]:
    with open(requirements_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _extract_package_name(requirement: str) -> str:
    # 例: "playwright>=1.40.0" -> "playwright"
    return re.split(r"[<>=!~]", requirement, maxsplit=1)[0].strip().lower()


def _install_missing_python_deps(requirements_path: str, missing_modules: list[str]):
    reqs = _load_requirements(requirements_path)
    # 将 module 名映射为 pip 包名再做匹配
    missing_packages = {MODULE_TO_PACKAGE.get(m, m).lower() for m in missing_modules}
    selected = [req for req in reqs if _extract_package_name(req) in missing_packages]

    targets = selected if selected else missing_modules

    _run_command(
        [sys.executable, "-m", "pip", "install", *targets],
        f"正在安装缺失依赖: {', '.join(missing_modules)}",
    )


def _ensure_playwright_chromium():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        # playwright 包未安装时会在上一阶段处理，这里直接返回。
        return

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        msg = str(exc).lower()
        need_install = (
            "executable doesn't exist" in msg
            or "browser has not been found" in msg
            or "please run the following command" in msg
        )
        if not need_install:
            raise
        _run_command(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            "检测到未安装 Chromium，正在安装 Playwright 浏览器",
        )


def ensure_runtime_ready(base_dir: str):
    requirements_path = os.path.join(base_dir, "requirements.txt")
    if not os.path.exists(requirements_path):
        raise RuntimeError(f"未找到 requirements.txt: {requirements_path}")

    missing = [m for m in REQUIRED_MODULES if not _is_module_available(m)]
    if missing:
        print(f"[env] 缺少依赖: {', '.join(missing)}")
        _install_missing_python_deps(requirements_path, missing)
        missing_after = [m for m in REQUIRED_MODULES if not _is_module_available(m)]
        if missing_after:
            raise RuntimeError(f"依赖安装后仍缺失: {', '.join(missing_after)}")
    else:
        print("[env] Python 依赖检查通过")

    _ensure_playwright_chromium()
