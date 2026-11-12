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

        control: {
            title: "Управление",
            subtitle:
                "Расписание и ручной запуск задач"
        },

        resources: {
            title: "Ресурсы",
            subtitle:
                "Память, CPU и сколько аккаунтов потянет сервис"
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
                if (section === "control") {
                    loadControl();
                }
                if (section === "resources") {
                    loadResources();
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
    ad: "Реклама",
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

// ---------------- Управление ----------------

let ctlTimer = null;

function fmtRel(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    const diff = Math.round((d - Date.now()) / 1000);
    const abs = Math.abs(diff);
    const h = Math.floor(abs / 3600), m = Math.floor((abs % 3600) / 60);
    const span = h ? `${h} ч ${m} мин` : `${m} мин`;
    const at = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    return diff >= 0 ? `через ${span} (${at})` : `${span} назад (${at})`;
}

async function loadControl() {
    try {
        const r = await fetch("/api/admin/jobs", { credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        $("ctl-status").textContent = data.accounts.length ? "" : "Аккаунты ещё не запущены.";
        $("ctl-list").innerHTML = data.accounts.map(a => {
            const jobs = Object.entries(a.jobs).map(([job, s]) => {
                const title = (data.titles || {})[job] || job;
                const plan = job === "clicker" && s.plan && s.plan.total
                    ? ` · план дня ${s.plan.done}/${s.plan.total}` : "";
                const st = s.running ? "⏳ выполняется"
                    : (s.status === "error" ? "⚠️ ошибка" : (s.status ? "✅ ок" : "—"));
                const btn = job === "clicker" ? "Сделать клик"
                    : job === "tasks" ? "Сделать задания"
                    : job === "daily" ? "Забрать ежедневку" : "Запустить обход";
                return `<div class="ctl-job">
                    <div class="ctl-job-head"><b>${escapeHtml(title)}</b><span>${st}</span></div>
                    <div class="ctl-line">Следующий запуск: ${escapeHtml(fmtRel(s.next))}${plan}</div>
                    <div class="ctl-line">Последний: ${escapeHtml(fmtRel(s.last))}${s.message ? " — " + escapeHtml(s.message) : ""}</div>
                    <button class="kn-btn kn-primary" data-acc="${a.account}" data-job="${job}"
                        ${(!a.ready || s.running || !s.manual) ? "disabled" : ""}>${btn}</button>
                </div>`;
            }).join("");
            return `<div class="ctl-acc"><h3>${escapeHtml(a.label)} ${a.ready ? "" : "<i>(запускается…)</i>"}</h3>
                <div class="ctl-jobs">${jobs}</div></div>`;
        }).join("");
        document.querySelectorAll("#ctl-list button[data-job]").forEach(b => b.onclick = () => runJob(b));
    } catch (e) {
        $("ctl-status").textContent = "Не удалось загрузить: " + e.message;
    }
    if (!ctlTimer) {
        ctlTimer = setInterval(() => {
            if (document.getElementById("section-control").classList.contains("active")) loadControl();
        }, 15000);
    }
}

async function runJob(btn) {
    btn.disabled = true;
    try {
        const r = await fetch(`/api/admin/run?account=${btn.dataset.acc}&job=${btn.dataset.job}`,
            { method: "POST", credentials: "same-origin" });
        if (!r.ok) {
            let m = "HTTP " + r.status;
            try { m = (await r.json()).detail || m; } catch (_) {}
            throw new Error(m);
        }
        $("ctl-status").textContent = "Запущено. Результат придёт в лог-группу Telegram.";
        setTimeout(loadControl, 2500);
    } catch (e) {
        $("ctl-status").textContent = "Не удалось запустить: " + e.message;
        btn.disabled = false;
    }
}

// ---------------- Ресурсы ----------------

let resChart = null;

async function loadResources() {
    const hours = $("res-hours").value;
    try {
        const r = await fetch("/api/admin/resources?hours=" + hours, { credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        const a = d.analysis;
        $("res-status").textContent = "";
        const card = (t, v, sub) => `<div class="res-card"><div class="res-t">${t}</div><div class="res-v">${v}</div><div class="res-s">${sub || ""}</div></div>`;
        const cards = [
            card("Память сейчас", `${a.current.rss} МБ`, `лимит ${a.limits.ram_mb} МБ · база без аккаунтов ${a.base_rss_mb} МБ`),
            card("CPU сейчас", `${a.current.cpu}%`, `лимит ≈ ${a.limits.cpu_cores * 100}% ядра`),
            card("Аккаунтов запущено", a.current.accounts, `asyncio-задач: ${a.current.tasks}`),
        ];
        if (a.recommended_total !== undefined) {
            cards.push(
                card("Цена аккаунта (память)", `${a.per_account_steady_mb} МБ`, `в покое · на пике ${a.per_account_peak_mb} МБ`),
                card("Пик памяти", `${a.rss_peak_mb} МБ`, `в покое ~${a.rss_steady_mb} МБ`),
                card("CPU средний / пик", `${a.cpu_avg_pct}% / ${a.cpu_peak_pct}%`, `на аккаунт ~${a.per_account_cpu_pct}%`),
                card("Потянет аккаунтов", `${a.recommended_total}`, `можно добавить ещё: <b>${a.can_add}</b>`),
                card("Разбивка лимита", `${a.max_by_memory_peak} / ${a.max_by_memory_steady} / ${a.max_by_cpu}`, "по памяти (пик) / по памяти (покой) / по CPU"),
            );
        }
        $("res-cards").innerHTML = cards.join("");
        $("res-notes").innerHTML = (a.notes || []).map(n => `<div>• ${escapeHtml(n)}</div>`).join("");

        const hist = d.history || [];
        const labels = hist.map(s => new Date(s.ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }));
        const ds = [
            { label: "Память, МБ", data: hist.map(s => s.rss), borderColor: "#7c5cff", tension: .25, pointRadius: 0, yAxisID: "y" },
            { label: "CPU, %", data: hist.map(s => s.cpu), borderColor: "#3cc878", tension: .25, pointRadius: 0, yAxisID: "y1" },
            { label: "Аккаунты", data: hist.map(s => s.accounts), borderColor: "#ffaa00", stepped: true, pointRadius: 0, yAxisID: "y2" },
        ];
        if (resChart) resChart.destroy();
        resChart = new Chart($("res-chart"), {
            type: "line", data: { labels, datasets: ds },
            options: {
                responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
                scales: {
                    y: { position: "left", title: { display: true, text: "МБ" } },
                    y1: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "CPU %" } },
                    y2: { display: false, min: 0 },
                },
            },
        });
    } catch (e) {
        $("res-status").textContent = "Не удалось загрузить: " + e.message;
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
    $("res-hours").onchange = loadResources;
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
