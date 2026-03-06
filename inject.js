// ZJU Course Hunter - Injection Script
// 在教务系统选课页面的每个"选课"按钮旁注入"抢课"按钮
(function () {
    // 防止重复注入（基于 DOM 属性，比 window 变量更持久）
    if (document.documentElement.getAttribute("data-zju-injected") === "true") return;
    document.documentElement.setAttribute("data-zju-injected", "true");
    window.__zju_hunter_injected = true;

    // 仅在包含选课内容的页面生效
    if (!document.getElementById("contentBox") && !document.querySelector(".xuanke")) return;

    // ── 样式 ──
    const style = document.createElement("style");
    style.textContent = `
        .zju-hunter-btn {
            margin-left: 6px !important;
            background: #d9534f !important;
            border: 1px solid #d43f3a !important;
            color: #fff !important;
            font-weight: bold;
            padding: 4px 10px;
            border-radius: 3px;
            cursor: pointer;
            position: relative;
            z-index: 999;
        }
        .zju-hunter-btn:hover { background: #c9302c !important; }

        /* 自定义确认弹窗（不使用 confirm()，因为 Playwright 会自动关闭原生弹窗） */
        .hunter-overlay {
            position: fixed; inset: 0;
            background: rgba(0,0,0,0.5);
            z-index: 2147483647;
            display: flex; align-items: center; justify-content: center;
        }
        .hunter-modal {
            background: #fff; border-radius: 8px; padding: 24px 28px;
            min-width: 360px; max-width: 480px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            font-family: -apple-system, "Microsoft YaHei", sans-serif;
        }
        .hunter-modal h3 {
            margin: 0 0 16px; font-size: 18px; color: #333;
        }
        .hunter-modal .info-table {
            width: 100%; border-collapse: collapse; margin-bottom: 20px;
        }
        .hunter-modal .info-table td {
            padding: 6px 8px; font-size: 14px; color: #555;
            border-bottom: 1px solid #eee;
        }
        .hunter-modal .info-table td:first-child {
            font-weight: 600; color: #333; white-space: nowrap; width: 80px;
        }
        .hunter-modal .btn-row {
            display: flex; justify-content: flex-end; gap: 10px;
        }
        .hunter-modal .btn-row button {
            padding: 8px 24px; border-radius: 4px; border: none;
            font-size: 14px; cursor: pointer; font-weight: 500;
        }
        .hunter-modal .btn-cancel {
            background: #e0e0e0; color: #333;
        }
        .hunter-modal .btn-cancel:hover { background: #d0d0d0; }
        .hunter-modal .btn-confirm {
            background: #d9534f; color: #fff;
        }
        .hunter-modal .btn-confirm:hover { background: #c9302c; }
    `;
    document.head.appendChild(style);

    // ── 工具函数 ──

    /** 从多个位置尝试获取学号 */
    function getSu() {
        const el = document.getElementById("sessionUserKey");
        if (el) return el.value;
        try {
            const parentEl = window.parent.document.getElementById("sessionUserKey");
            if (parentEl) return parentEl.value;
        } catch (_) {}
        return new URLSearchParams(window.location.search).get("su") || "";
    }

    /** 获取全局选课参数 */
    function getGlobalParams() {
        const v = (id) => document.getElementById(id)?.value || "";
        return { xn: v("xn"), xq: v("xq"), nj: v("nj"), su: getSu() };
    }

    /** 调用 Python 暴露的函数（兼容 iframe 场景） */
    function callPython(payload) {
        if (window.py_grab_func) return window.py_grab_func(payload);
        if (window.parent?.py_grab_func) return window.parent.py_grab_func(payload);
    }

    /**
     * 从按钮所在行和面板提取课程详细信息
     * HTML 结构:
     *   panel-heading > .kcmc > a (课程名)  i (学分)
     *   tr > .jsxm a (教师)  .sksj (上课时间)  .skdd (地点)  .rsxx (余量)
     */
    function extractCourseInfo(btn) {
        const tr = btn.closest("tr");
        const panel = btn.closest(".panel");

        const xkkh = btn.getAttribute("data-xkkh") || "";
        const tabname = btn.getAttribute("data-tabname") || "xkrw2006view";

        let name = "未知课程", code = "", credits = "";
        if (panel) {
            const kcmcSpan = panel.querySelector(".panel-heading .kcmc");
            if (kcmcSpan) {
                const a = kcmcSpan.querySelector("a");
                if (a) name = a.innerText.trim();
                const i = kcmcSpan.querySelector("i");
                if (i) credits = i.innerText.trim();
                const raw = kcmcSpan.childNodes[0]?.textContent?.trim();
                if (raw) code = raw;
            }
        }

        let teacher = "", schedule = "", location = "", capacity = "";
        if (tr) {
            teacher = tr.querySelector(".jsxm")?.innerText?.trim() || "";
            schedule = tr.querySelector(".sksj")?.innerText?.trim() || "";
            location = tr.querySelector(".skdd")?.innerText?.trim() || "";
            capacity = tr.querySelector(".rsxx")?.innerText?.trim() || "";
        }

        return { xkkh, tabname, name, code, credits, teacher, schedule, location, capacity };
    }

    /**
     * 显示自定义确认弹窗（返回 Promise<boolean>）
     * 不使用原生 confirm()，因为 Playwright 会自动拦截并关闭原生对话框。
     */
    function showConfirmModal(info) {
        return new Promise((resolve) => {
            const overlay = document.createElement("div");
            overlay.className = "hunter-overlay";

            // 构建信息行
            const rows = [
                ["课程", info.name],
                info.code   ? ["代码", info.code] : null,
                ["选课号", info.xkkh],
                info.teacher  ? ["教师", info.teacher] : null,
                info.schedule ? ["时间", info.schedule] : null,
                info.location ? ["地点", info.location] : null,
                info.capacity ? ["余量", info.capacity] : null,
            ].filter(Boolean);

            const tableHTML = rows
                .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`)
                .join("");

            overlay.innerHTML = `
                <div class="hunter-modal">
                    <h3>确认开始抢课？</h3>
                    <table class="info-table">${tableHTML}</table>
                    <p style="font-size:13px;color:#888;margin:0 0 16px;">
                        确认后浏览器将关闭，脚本进入自动抢课模式。按 Ctrl+C 可随时停止。
                    </p>
                    <div class="btn-row">
                        <button class="btn-cancel" id="hunter-cancel">取消</button>
                        <button class="btn-confirm" id="hunter-confirm">开始抢课</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            function cleanup(result) {
                overlay.remove();
                resolve(result);
            }

            overlay.querySelector("#hunter-cancel").onclick = () => cleanup(false);
            overlay.querySelector("#hunter-confirm").onclick = () => cleanup(true);
            // 点击遮罩层关闭
            overlay.addEventListener("click", (e) => {
                if (e.target === overlay) cleanup(false);
            });
        });
    }

    // ── 注入按钮 ──

    function injectButton(originalBtn) {
        if (originalBtn.getAttribute("data-zju-injected") === "true") return;
        originalBtn.setAttribute("data-zju-injected", "true");

        const btn = document.createElement("button");
        btn.className = "btn btn-sm zju-hunter-btn";
        btn.textContent = "抢课";
        btn.type = "button";

        btn.addEventListener("click", async function (e) {
            e.preventDefault();
            e.stopPropagation();

            const info = extractCourseInfo(originalBtn);
            const confirmed = await showConfirmModal(info);
            if (!confirmed) return;

            const global = getGlobalParams();
            callPython({
                ...global,
                xkkh: info.xkkh,
                tabname: info.tabname,
                course_name: info.name,
                xkzys: "1",
            });
        });

        if (originalBtn.parentNode) {
            originalBtn.parentNode.insertBefore(btn, originalBtn.nextSibling);
        }
    }

    function scanAndInject() {
        document.querySelectorAll("button.xuanke").forEach(injectButton);
    }

    // 立即扫描 + 监听动态展开
    scanAndInject();
    const target = document.getElementById("contentBox") || document.body;
    new MutationObserver(scanAndInject).observe(target, { childList: true, subtree: true });
})();
