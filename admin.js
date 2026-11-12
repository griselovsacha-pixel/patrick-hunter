let dashboardData = null;
let activityChart = null;
let starsChart = null;

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString("ru-RU");
}

function formatStars(value) {
    return Number(value || 0).toFixed(2);
}

function formatDate(value) {
    if (!value) return "—";

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return escapeHtml(value);
    }

    return date.toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit"
    });
}

function accountName(accountId) {
    if (!dashboardData || !accountId) {
        return "—";
    }

    const account = dashboardData.accounts.find(
        (item) => item.id === accountId
    );

    if (!account) {
        return "—";
    }

    return account.name ||
        `Аккаунт ${account.account_number}`;
}

function showError(message) {
    $("loading").classList.add("hidden");
    $("content").classList.add("hidden");
    $("error").classList.remove("hidden");
    $("error-message").textContent = message;
}

function hideError() {
    $("error").classList.add("hidden");
}

function showContent() {
    $("loading").classList.add("hidden");
    $("content").classList.remove("hidden");
}

async function loadDashboard() {
    hideError();

    $("loading").classList.remove("hidden");
    $("content").classList.add("hidden");

    try {
        const response = await fetch(
            "/api/admin/overview",
            {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store"
            }
        );

        if (response.status === 401) {
            throw new Error(
                "Неверные данные администратора или истёк вход."
            );
        }

        if (!response.ok) {
            let message = `HTTP ${response.status}`;

            try {
                const data = await response.json();

                if (data.detail) {
                    message = data.detail;
                }
            } catch (_) {}

            throw new Error(message);
        }

        dashboardData = await response.json();

        renderAll();

        $("last-update").textContent =
            `Обновлено: ${new Date().toLocaleTimeString("ru-RU")}`;

        showContent();

    } catch (error) {
        console.error(error);

        showError(
            error.message ||
            "Не удалось загрузить статистику."
        );
    }
}

function renderAll() {
    renderTotals();
    renderAccounts();
    renderAccountsTable();
    renderActivity();
    renderEvents();
    renderLogs();
    renderCharts();
}

function renderTotals() {
    const totals = dashboardData.totals || {};

    $("total-stars").textContent =
        formatStars(totals.stars);

    $("total-clicks").textContent =
        formatNumber(totals.clicks);

    $("total-tasks").textContent =
        formatNumber(totals.tasks);

    $("total-daily").textContent =
        formatNumber(totals.daily);

    $("total-promos-found").textContent =
        formatNumber(totals.promos_found);

    $("total-promos-activated").textContent =
        formatNumber(totals.promos_activated);

    // BALANCE — данные приходят из /api/admin/overview
    const balance = dashboardData.balance || {};

    if ($("event-earnings")) {
        $("event-earnings").textContent =
            `${formatStars(
                balance.event_earnings ??
                totals.stars ??
                0
            )} ⭐`;
    }

    if ($("real-balance")) {
        $("real-balance").textContent =
            `${formatStars(
                balance.real_balance ??
                dashboardData.real_balance ??
                0
            )} ⭐`;
    }

    if ($("balance-delta")) {
        const delta =
            balance.today_delta ??
            dashboardData.balance_delta_today ??
            0;

        $("balance-delta").textContent =
            `${Number(delta || 0) >= 0 ? "+" : ""}${formatStars(delta)} ⭐`;
    }

    $("activity-clicks").textContent =
        formatNumber(totals.clicks);

    $("activity-tasks").textContent =
        formatNumber(totals.tasks);

    $("activity-mini-apps").textContent =
        formatNumber(totals.mini_apps);

    $("activity-daily").textContent =
        formatNumber(totals.daily);
}

function renderAccounts() {
    const container = $("accounts-preview");
    const accounts = dashboardData.accounts || [];

    if (!accounts.length) {
        container.innerHTML =
            `<div class="empty">Аккаунтов нет</div>`;
        return;
    }

    container.innerHTML = accounts.map(account => {

        const online = account.connected === true;

        const name =
            account.name ||
            `Аккаунт ${account.account_number}`;

        return `
            <div class="account-row">

                <div class="account-info">

                    <div class="account-number">
                        #${escapeHtml(account.account_number)}
                    </div>

                    <div class="account-name">
                        ${escapeHtml(name)}
                    </div>

                </div>

                <div class="account-status ${online ? "online" : "offline"}">

                    <span class="account-status-dot"></span>

                    ${online ? "Онлайн" : "Оффлайн"}

                </div>

            </div>
        `;
    }).join("");
}

function renderAccountsTable() {
    const tbody = $("accounts-table");
    const accounts = dashboardData.accounts || [];

    if (!accounts.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="5">
                    <div class="empty">
                        Аккаунтов нет
                    </div>
                </td>
            </tr>
        `;

        return;
    }

    tbody.innerHTML = accounts.map(account => {

        const online =
            account.connected === true;

        return `
            <tr>

                <td>
                    #${escapeHtml(account.account_number)}
                </td>

                <td>
                    ${escapeHtml(
                        account.name || "Без имени"
                    )}
                </td>

                <td>
                    <span class="badge ${
                        online
                            ? "online"
                            : "offline"
                    }">
                        ${online ? "Онлайн" : "Оффлайн"}
                    </span>
                </td>

                <td>
                    ${formatDate(
                        account.last_connected_at
                    )}
                </td>

                <td>
                    ${formatDate(
                        account.updated_at
                    )}
                </td>

            </tr>
        `;
    }).join("");
}

function renderActivity() {
    const clicks = dashboardData.clicks || [];
    const tasks = dashboardData.tasks || [];

    const clickTable = $("clicks-table");

    if (!clicks.length) {
        clickTable.innerHTML = `
            <tr>
                <td colspan="3">
                    <div class="empty">
                        Кликов пока нет
                    </div>
                </td>
            </tr>
        `;
    } else {
        clickTable.innerHTML = clicks.map(item => `
            <tr>

                <td>
                    ${escapeHtml(
                        accountName(item.account_id)
                    )}
                </td>

                <td>
                    ${formatDate(item.clicked_at)}
                </td>

                <td>
                    ⭐ ${formatStars(item.stars)}
                </td>

            </tr>
        `).join("");
    }

    const taskTable = $("tasks-table");

    if (!tasks.length) {
        taskTable.innerHTML = `
            <tr>
                <td colspan="4">
                    <div class="empty">
                        Выполненных заданий пока нет
                    </div>
                </td>
            </tr>
        `;
    } else {
        taskTable.innerHTML = tasks.map(item => `
            <tr>

                <td>
                    ${escapeHtml(
                        accountName(item.account_id)
                    )}
                </td>

                <td>
                    ${escapeHtml(
                        item.task_type || "task"
                    )}
                </td>

                <td>
                    ${formatDate(item.completed_at)}
                </td>

                <td>
                    ⭐ ${formatStars(item.stars)}
                </td>

            </tr>
        `).join("");
    }
}

function renderEvents() {
    const container = $("events-preview");
    const logs = dashboardData.logs || [];

    if (!logs.length) {
        container.innerHTML =
            `<div class="empty">Событий пока нет</div>`;
        return;
    }

    container.innerHTML =
        logs.slice(0, 8).map(log => {

            let icon = "📋";

            const type =
                String(log.event_type || "")
                    .toLowerCase();

            if (type.includes("promo")) {
                icon = "🎟️";
            } else if (type.includes("click")) {
                icon = "🖱️";
            } else if (type.includes("task")) {
                icon = "✅";
            } else if (type.includes("daily")) {
                icon = "🎁";
            } else if (type.includes("error")) {
                icon = "❌";
            }

            return `
                <div class="event-row">

                    <div class="event-icon">
                        ${icon}
                    </div>

                    <div class="event-text">

                        <div class="event-message">
                            ${escapeHtml(
                                log.message || "Событие"
                            )}
                        </div>

                        <div class="event-time">
                            ${formatDate(log.created_at)}
                        </div>

                    </div>

                </div>
            `;
        }).join("");
}

function renderLogs() {
    const container = $("logs-container");
    const logs = dashboardData.logs || [];

    if (!logs.length) {
        container.innerHTML =
            `<div class="empty">Логов пока нет</div>`;
        return;
    }

    container.innerHTML =
        logs.map(log => {

            const level =
                String(log.level || "info")
                    .toLowerCase();

            return `
                <div class="log-row">

                    <div class="log-time">
                        ${formatDate(log.created_at)}
                    </div>

                    <div class="log-level ${escapeHtml(level)}">
                        ${escapeHtml(level)}
                    </div>

                    <div class="log-type">
                        ${escapeHtml(
                            log.event_type || "app"
                        )}
                    </div>

                    <div class="log-message">

                        ${escapeHtml(
                            log.message || ""
                        )}

                        ${
                            log.error
                                ? `<br><span style="color:#ff5c6c">
                                    ${escapeHtml(log.error)}
                                   </span>`
                                : ""
                        }

                    </div>

                </div>
            `;
        }).join("");
}

function renderCharts() {
    if (typeof Chart === "undefined") {
        console.warn("Chart.js не загружен.");
        return;
    }

    renderActivityChart();
    renderStarsChart();
}

function renderActivityChart() {
    const stats = dashboardData.daily_stats || [];
    const grouped = {};

    stats.forEach(row => {

        const date = row.stat_date || "unknown";

        if (!grouped[date]) {
            grouped[date] = {
                clicks: 0,
                tasks: 0,
                mini_apps: 0,
                daily: 0,
                promos: 0
            };
        }

        grouped[date].clicks +=
            Number(row.clicks || 0);

        grouped[date].tasks +=
            Number(row.tasks_completed || 0);

        grouped[date].mini_apps +=
            Number(row.mini_apps_completed || 0);

        grouped[date].daily +=
            Number(row.daily_bonuses || 0);

        grouped[date].promos +=
            Number(row.promo_codes_activated || 0);
    });

    const labels = Object.keys(grouped).sort();

    const values = key =>
        labels.map(date => grouped[date][key]);

    if (activityChart) {
        activityChart.destroy();
    }

    activityChart = new Chart(
        $("activity-chart"),
        {
            type: "line",

            data: {
                labels,

                datasets: [
                    {
                        label: "Клики",
                        data: values("clicks"),
                        tension: 0.35,
                        borderWidth: 2
                    },
                    {
                        label: "Задания",
                        data: values("tasks"),
                        tension: 0.35,
                        borderWidth: 2
                    },
                    {
                        label: "Mini Apps",
                        data: values("mini_apps"),
                        tension: 0.35,
                        borderWidth: 2
                    },
                    {
                        label: "Daily",
                        data: values("daily"),
                        tension: 0.35,
                        borderWidth: 2
                    },
                    {
                        label: "Promo",
                        data: values("promos"),
                        tension: 0.35,
                        borderWidth: 2
                    }
                ]
            },

            options: {
                responsive: true,
                maintainAspectRatio: false,

                interaction: {
                    mode: "index",
                    intersect: false
                },

                plugins: {
                    legend: {
                        labels: {
                            color: "#8b95a7",
                            boxWidth: 12
                        }
                    }
                },

                scales: {
                    x: {
                        ticks: {
                            color: "#8b95a7"
                        },

                        grid: {
                            color: "rgba(255,255,255,0.04)"
                        }
                    },

                    y: {
                        beginAtZero: true,

                        ticks: {
                            color: "#8b95a7"
                        },

                        grid: {
                            color: "rgba(255,255,255,0.04)"
                        }
                    }
                }
            }
        }
    );
}

function renderStarsChart() {
    const totals = dashboardData.totals || {};

    if (starsChart) {
        starsChart.destroy();
    }

    starsChart = new Chart(
        $("stars-chart"),
        {
            type: "doughnut",

            data: {
                labels: [
                    "Клики",
                    "Задания",
                    "Промокоды",
                    "Daily"
                ],

                datasets: [
                    {
                        data: [
                            Number(
                                totals.stars_from_clicks || 0
                            ),

                            Number(
                                totals.stars_from_tasks || 0
                            ),

                            Number(
                                totals.stars_from_promos || 0
                            ),

                            Number(
                                totals.stars_from_daily || 0
                            )
                        ],

                        borderWidth: 0
                    }
                ]
            },

            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "65%",

                plugins: {
                    legend: {
                        position: "bottom",

                        labels: {
                            color: "#8b95a7",
                            boxWidth: 12,
                            padding: 15
                        }
                    }
                }
            }
        }
    );
}

function setupNavigation() {
    const buttons =
        document.querySelectorAll(".nav-item");

    const sections = {
        dashboard: {
            title: "Дашборд",
            subtitle:
                "Статистика Patrick Hunter"
        },

        accounts: {
            title: "Аккаунты",
            subtitle:
                "Состояние Telegram аккаунтов"
        },

        activity: {
            title: "Активность",
            subtitle:
                "Клики, задания и бонусы"
        },

        logs: {
            title: "Логи",
            subtitle:
                "Последние события приложения"
        },

        learning: {
            title: "Обучение",
            subtitle:
                "Что отдаёт бот: кнопки, тексты, алерты"
        }
    };

    buttons.forEach(button => {

        button.addEventListener(
            "click",
            () => {

                buttons.forEach(item =>
                    item.classList.remove("active")
                );

                button.classList.add("active");

                const section =
                    button.dataset.section;

                document
                    .querySelectorAll(".section")
                    .forEach(item =>
                        item.classList.remove("active")
                    );

                const target =
                    $(`section-${section}`);

                if (target) {
                    target.classList.add("active");
                }

                if (section === "learning") {
                    loadKnowledge();
                }

                if (sections[section]) {
                    $("page-title").textContent =
                        sections[section].title;

                    $("page-subtitle").textContent =
                        sections[section].subtitle;
                }
            }
        );
    });
}

// ---------------- Обучение ----------------

const KN_KINDS = {
    all: "Все",
    screen: "Экраны",
    task: "Задания",
    notice: "Плашки",
    alert: "Алерты",
    transition: "Кнопки → результат"
};
let knData = { items: [], counts: {}, total: 0 };
let knKind = "all";

async function loadKnowledge() {
    const status = $("kn-status");
    status.textContent = "Загрузка…";
    try {
        const r = await fetch("/api/admin/knowledge", {
            credentials: "same-origin", cache: "no-store"
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        knData = await r.json();
        status.textContent = knData.total
            ? ""
            : "Пока пусто: записи появятся, когда бот начнёт получать сообщения от @patrickstarsrobot.";
        renderKnowledge();
    } catch (e) {
        status.textContent = "Не удалось загрузить: " + e.message;
    }
}

function renderKnowledge() {
    const chips = $("kn-chips");
    chips.innerHTML = Object.entries(KN_KINDS).map(([k, name]) => {
        const n = k === "all" ? knData.total : (knData.counts[k] || 0);
        return `<button class="kn-chip ${knKind === k ? "active" : ""}" data-k="${k}">${name} <b>${n}</b></button>`;
    }).join("");
    chips.querySelectorAll(".kn-chip").forEach(b => b.onclick = () => {
        knKind = b.dataset.k;
        renderKnowledge();
    });

    const q = ($("kn-search").value || "").toLowerCase();
    const items = knData.items.filter(it => {
        if (knKind !== "all" && it.kind !== knKind) return false;
        if (!q) return true;
        return JSON.stringify([it.title, it.sample_text, it.data]).toLowerCase().includes(q);
    });

    $("kn-list").innerHTML = items.map(it => {
        const d = it.data || {};
        const btns = (d.buttons || []).map(b =>
            `<span class="kn-b" title="${escapeHtml(b.data || b.url || "")}">${escapeHtml(b.text)} <i>${escapeHtml(b.type)}</i></span>`
        ).join("");
        let flow = "";
        if (it.kind === "transition") {
            const b = d.button || {};
            const res = d.outcome === "alert" ? "алерт"
                      : d.outcome === "skipped" ? "не нажимал" : "экран";
            flow = `<div class="kn-flow">«${escapeHtml(d.screen_title || "")}» → <b>${escapeHtml(b.text || "")}</b> `
                 + `<i>${escapeHtml(b.type || "")}${b.data ? " · " + escapeHtml(b.data) : ""}</i> → `
                 + `${escapeHtml(res)}: ${escapeHtml(d.outcome_title || "")}</div>`;
        }
        return `<details class="kn-item">
            <summary>
                <span class="kn-kind kn-${it.kind}">${escapeHtml(KN_KINDS[it.kind] || it.kind)}</span>
                <span class="kn-title">${escapeHtml(it.title || "(без заголовка)")}</span>
                <span class="kn-meta">×${it.seen_count} · ${escapeHtml(formatDate(it.last_seen))}</span>
            </summary>
            ${flow}
            ${it.sample_text ? `<pre class="kn-text">${escapeHtml(it.sample_text)}</pre>` : ""}
            ${btns ? `<div class="kn-btns">${btns}</div>` : ""}
        </details>`;
    }).join("") || `<div class="kn-status">Ничего не найдено</div>`;
}

async function knFetchExport(format) {
    const r = await fetch("/api/admin/knowledge/export?format=" + format, { credentials: "same-origin" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r;
}

async function knDownload(format) {
    try {
        const r = await knFetchExport(format);
        const blob = await r.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `patrick-knowledge.${format === "md" ? "md" : "json"}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
    } catch (e) {
        $("kn-status").textContent = "Ошибка выгрузки: " + e.message;
    }
}

async function knCopy() {
    try {
        const text = await (await knFetchExport("md")).text();
        await navigator.clipboard.writeText(text);
        $("kn-status").textContent = "Скопировано (" + text.length + " символов) — вставь в чат с Claude.";
    } catch (e) {
        $("kn-status").textContent = "Не удалось скопировать: " + e.message;
    }
}

let knPoll = null;

async function knCrawlStatus() {
    try {
        const r = await fetch("/api/admin/knowledge/crawl-status", { credentials: "same-origin", cache: "no-store" });
        if (!r.ok) return;
        const s = await r.json();
        const el = $("kn-status");
        if (s.running) {
            el.textContent = "Обход идёт: " + (s.progress || "…");
        } else {
            if (knPoll) { clearInterval(knPoll); knPoll = null; loadKnowledge(); }
            if (s.last_summary) el.textContent = `Последний обход (${s.last_run}): ${s.last_summary}`;
        }
    } catch (_) {}
}

async function knCrawl() {
    try {
        const r = await fetch("/api/admin/knowledge/crawl", { method: "POST", credentials: "same-origin" });
        if (!r.ok) {
            let m = "HTTP " + r.status;
            try { m = (await r.json()).detail || m; } catch (_) {}
            throw new Error(m);
        }
        $("kn-status").textContent = "Обход запущен, он займёт несколько минут…";
        if (knPoll) clearInterval(knPoll);
        knPoll = setInterval(knCrawlStatus, 5000);
    } catch (e) {
        $("kn-status").textContent = "Не удалось запустить обход: " + e.message;
    }
}

function setupKnowledge() {
    $("kn-crawl").onclick = knCrawl;
    $("kn-reload").onclick = loadKnowledge;
    $("kn-json").onclick = () => knDownload("json");
    $("kn-md").onclick = () => knDownload("md");
    $("kn-copy").onclick = knCopy;
    $("kn-search").addEventListener("input", renderKnowledge);
}

document.addEventListener(
    "DOMContentLoaded",
    () => {

        setupNavigation();
        setupKnowledge();

        $("refresh-btn").addEventListener(
            "click",
            loadDashboard
        );

        loadDashboard();

        setInterval(
            loadDashboard,
            60000
        );
    }
);
