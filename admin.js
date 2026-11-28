let dashboardData = null;

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;")
        .replace(/\u2B50\uFE0F?/g, "\u2605");
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString("ru-RU");
}

function formatStars(value) {
    return Number(value || 0).toLocaleString("ru-RU", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
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

/* =====================================================================
   PATRICK HUNTER — визуальный слой
   Графики на чистом SVG (без Chart.js), счётчики, тема, подсветка и т.д.
   ===================================================================== */

const REDUCED = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
const SVGNS = "http://www.w3.org/2000/svg";
let uid = 0;

function S(tag, attrs, parent) {
    const el = document.createElementNS(SVGNS, tag);
    for (const k in (attrs || {})) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
}

function ico(name, cls) {
    return `<svg class="ico ${cls === undefined ? "inline-ico" : cls}"><use href="#i-${name}"/></svg>`;
}

function pill(kind, text) {
    return `<span class="pill ${kind}"><i></i>${escapeHtml(text)}</span>`;
}

function compact(n) {
    n = Number(n) || 0;
    const a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "k";
    return String(Math.round(n * 100) / 100);
}

/* ---------------- счётчики ---------------- */

function countTo(el, to, fmt, dur) {
    if (!el) return;
    to = Number(to) || 0;
    fmt = fmt || formatNumber;
    const from = el._v === undefined ? 0 : el._v;
    const render = (v) => { el.textContent = fmt(v); };
    cancelAnimationFrame(el._raf);
    el._v = to;
    if (REDUCED || from === to) { render(to); return; }
    const t0 = performance.now();
    dur = dur || 1300;
    const step = (now) => {
        const p = Math.min(1, (now - t0) / dur);
        const e = p >= 1 ? 1 : 1 - Math.pow(2, -10 * p);
        render(from + (to - from) * e);
        if (p < 1) el._raf = requestAnimationFrame(step);
    };
    el._raf = requestAnimationFrame(step);
}

/* ---------------- тултип ---------------- */

const tip = { el: null };

function tipShow(html, x, y) {
    if (!tip.el) tip.el = document.getElementById("tip");
    if (!tip.el) return;
    tip.el.innerHTML = html;
    tip.el.classList.add("on");
    const w = tip.el.offsetWidth, h = tip.el.offsetHeight;
    let left = x + 16, top = y + 16;
    if (left + w > innerWidth - 8) left = x - w - 16;
    if (top + h > innerHeight - 8) top = y - h - 16;
    tip.el.style.left = Math.max(8, left) + "px";
    tip.el.style.top = Math.max(8, top) + "px";
}

function tipHide() {
    if (tip.el) tip.el.classList.remove("on");
}

/* ---------------- сглаживание (монотонный кубический сплайн) ---------------- */

function smoothPath(pts) {
    const n = pts.length;
    if (!n) return "";
    if (n === 1) return `M${pts[0][0]},${pts[0][1]}`;
    if (n === 2) return `M${pts[0][0]},${pts[0][1]}L${pts[1][0]},${pts[1][1]}`;
    const dx = [], m = [], t = new Array(n);
    for (let i = 0; i < n - 1; i++) {
        dx[i] = pts[i + 1][0] - pts[i][0];
        m[i] = dx[i] ? (pts[i + 1][1] - pts[i][1]) / dx[i] : 0;
    }
    t[0] = m[0];
    t[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) {
        if (m[i - 1] * m[i] <= 0) t[i] = 0;
        else t[i] = 3 * (dx[i - 1] + dx[i]) /
            ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]);
    }
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < n - 1; i++) {
        const x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1];
        d += `C${(x0 + dx[i] / 3).toFixed(2)},${(y0 + t[i] * dx[i] / 3).toFixed(2)} ` +
             `${(x1 - dx[i] / 3).toFixed(2)},${(y1 - t[i + 1] * dx[i] / 3).toFixed(2)} ${x1.toFixed(2)},${y1.toFixed(2)}`;
    }
    return d;
}

function stepPath(pts) {
    if (!pts.length) return "";
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 1; i < pts.length; i++) d += `H${pts[i][0]}V${pts[i][1]}`;
    return d;
}

function niceTicks(max, count) {
    if (!(max > 0)) return { max: 1, ticks: [0, 1] };
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const top = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = 0; v <= top + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return { max: top, ticks };
}

/* ---------------- спарклайн ---------------- */

function renderSpark(el, values, animate) {
    if (!el) return;
    el.classList.remove("in");
    el.innerHTML = "";
    values = (values || []).map(Number);
    if (values.length < 2 || values.every(v => v === 0)) {
        el.innerHTML = `<div class="spark-empty">нет данных</div>`;
        return;
    }
    const W = 100, H = 34, pad = 3;
    const max = Math.max(...values), min = Math.min(...values);
    const span = max - min || 1;
    const pts = values.map((v, i) => [
        (i / (values.length - 1)) * W,
        pad + (1 - (v - min) / span) * (H - pad * 2)
    ]);
    const id = "spg" + (++uid);
    const svg = S("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" }, el);
    const defs = S("defs", {}, svg);
    const g = S("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    S("stop", { offset: "0%", style: "stop-color:var(--fg);stop-opacity:.22" }, g);
    S("stop", { offset: "100%", style: "stop-color:var(--fg);stop-opacity:0" }, g);
    const wrap = S("g", { class: "sp-wrap" }, svg);
    const line = smoothPath(pts);
    S("path", { class: "sp-area", d: `${line}L${W},${H}L0,${H}Z`, fill: `url(#${id})` }, wrap);
    S("path", { class: "sp-line", d: line }, wrap);
    const last = pts[pts.length - 1];
    const dot = document.createElement("i");
    dot.className = "sp-dot";
    dot.style.top = (last[1] / H * 100) + "%";
    el.appendChild(dot);
    if (animate === false || REDUCED) el.classList.add("in");
    else requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("in")));
}

function replaySparks(root) {
    root.querySelectorAll(".spark").forEach(el => {
        if (!el.querySelector("svg")) return;
        el.classList.remove("in");
        void el.offsetWidth;
        el.classList.add("in");
    });
}

/* ---------------- линейный график ---------------- */
/*
   cfg: {
     labels: [...],
     series: [{name, values, cls, area, stepped, fmt}],
     normalize: каждая серия в своём масштабе (для разных единиц),
     xFmt(label), tipTitle(label), yFmt(v), legend: DOM-элемент, height
   }
*/

function lineChart(box, cfg) {
    if (!box) return;
    box._cfg = cfg;
    if (!box._hidden) box._hidden = new Set();
    buildLegend(box);
    drawLine(box, cfg.animate !== false);
    if (!box._ro && window.ResizeObserver) {
        box._ro = new ResizeObserver(() => {
            const w = box.clientWidth;
            if (w && Math.abs(w - (box._w || 0)) > 2) drawLine(box, false);
        });
        box._ro.observe(box);
    }
}

function buildLegend(box) {
    const cfg = box._cfg;
    const lg = cfg.legend;
    if (!lg) return;
    lg.innerHTML = "";
    cfg.series.forEach((s, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "lg" + (box._hidden.has(i) ? " off" : "");
        const svg = S("svg", { viewBox: "0 0 18 8" });
        S("line", { x1: 0, y1: 4, x2: 18, y2: 4, class: "c-line " + s.cls }, svg);
        b.appendChild(svg);
        const sp = document.createElement("span");
        sp.textContent = s.name;
        b.appendChild(sp);
        b.onclick = () => {
            if (box._hidden.has(i)) box._hidden.delete(i); else box._hidden.add(i);
            if (box._hidden.size >= cfg.series.length) box._hidden.clear();
            buildLegend(box);
            drawLine(box, false);
        };
        lg.appendChild(b);
    });
}

function drawLine(box, animate) {
    const cfg = box._cfg;
    const W = box.clientWidth || 640;
    const H = box.clientHeight || 260;
    box._w = box.clientWidth;
    box.innerHTML = "";

    const n = (cfg.labels || []).length;
    if (n < 2) {
        box.innerHTML = `<div class="empty">Недостаточно данных для графика</div>`;
        return;
    }

    const m = { l: cfg.normalize ? 8 : 42, r: 16, t: 14, b: 26 };
    const iw = Math.max(10, W - m.l - m.r);
    const ih = Math.max(10, H - m.t - m.b);
    const X = (i) => m.l + (i / (n - 1)) * iw;

    const vis = cfg.series.map((s, i) => ({ s, i })).filter(o => !box._hidden.has(o.i));

    let ticks = null, yMax = 1;
    if (!cfg.normalize) {
        let mx = 0;
        vis.forEach(o => o.s.values.forEach(v => { if (v > mx) mx = v; }));
        const nt = niceTicks(mx, Math.max(2, Math.round(ih / 52)));
        ticks = nt.ticks; yMax = nt.max;
    }

    const svg = S("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, box);
    const id = ++uid;
    const defs = S("defs", {}, svg);
    const grad = S("linearGradient", { id: "ag" + id, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    S("stop", { offset: "0%", style: "stop-color:var(--fg);stop-opacity:.20" }, grad);
    S("stop", { offset: "100%", style: "stop-color:var(--fg);stop-opacity:0" }, grad);
    const clip = S("clipPath", { id: "cp" + id }, defs);
    const clipRect = S("rect", { x: 0, y: -20, width: animate && !REDUCED ? 0 : W, height: H + 40 }, clip);

    /* сетка + ось Y */
    const gridLines = cfg.normalize ? [0, .25, .5, .75, 1] : ticks.map(t => t / yMax);
    gridLines.forEach((f, k) => {
        const y = m.t + ih - f * ih;
        S("line", { class: "c-grid", x1: m.l, x2: m.l + iw, y1: y, y2: y }, svg);
        if (!cfg.normalize) {
            const t = S("text", { class: "c-axis", x: m.l - 9, y: y + 3.5, "text-anchor": "end" }, svg);
            t.textContent = (cfg.yFmt || compact)(ticks[k]);
        }
    });

    /* ось X */
    const xt = Math.min(n, Math.max(2, Math.floor(iw / 78)));
    for (let k = 0; k < xt; k++) {
        const i = Math.round(k * (n - 1) / (xt - 1));
        const t = S("text", {
            class: "c-axis", x: X(i), y: H - 6,
            "text-anchor": k === 0 ? "start" : k === xt - 1 ? "end" : "middle"
        }, svg);
        t.textContent = (cfg.xFmt || String)(cfg.labels[i]);
    }

    /* серии */
    const plot = S("g", { "clip-path": `url(#cp${id})` }, svg);
    const Ys = {};
    vis.forEach(({ s, i }) => {
        let sMax = yMax;
        if (cfg.normalize) sMax = Math.max(...s.values, 1) * 1.08;
        const pts = s.values.map((v, k) => [X(k), m.t + ih - (v / sMax) * ih]);
        Ys[i] = { pts, sMax };
        const d = s.stepped ? stepPath(pts) : smoothPath(pts);
        if (s.area) {
            S("path", {
                class: "c-area", d: `${d}L${pts[n - 1][0]},${m.t + ih}L${pts[0][0]},${m.t + ih}Z`,
                fill: `url(#ag${id})`
            }, plot);
        }
        S("path", { class: "c-line " + s.cls, d }, plot);
    });

    /* маркер последней точки */
    if (vis.length) {
        const o = vis[0], p = Ys[o.i].pts[n - 1];
        S("circle", { class: "c-end-ring", cx: p[0], cy: p[1], r: 4 }, svg);
        S("circle", { class: "c-end", cx: p[0], cy: p[1], r: 3.5 }, svg);
    }

    /* наведение */
    const cross = S("line", { class: "c-cross", y1: m.t, y2: m.t + ih, x1: 0, x2: 0, style: "opacity:0" }, svg);
    const dots = vis.map(({ i }) => S("circle", { class: "c-pt c-end", r: 4.5, style: "opacity:0" }, svg));
    const hit = S("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);

    const move = (ev) => {
        const r = svg.getBoundingClientRect();
        const px = (ev.clientX - r.left) * (W / r.width);
        const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / iw) * (n - 1))));
        const x = X(i);
        cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.style.opacity = 1;
        let rows = "";
        vis.forEach(({ s, i: si }, k) => {
            const y = Ys[si].pts[i][1];
            dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", y); dots[k].style.opacity = 1;
            const v = s.values[i];
            rows += `<div class="t-r"><span>${escapeHtml(s.name)}</span><b>${escapeHtml(s.fmt ? s.fmt(v) : formatNumber(v))}</b></div>`;
        });
        const title = (cfg.tipTitle || cfg.xFmt || String)(cfg.labels[i]);
        tipShow(`<div class="t-h">${escapeHtml(title)}</div>${rows}`, ev.clientX, ev.clientY);
    };
    const leave = () => {
        cross.style.opacity = 0;
        dots.forEach(d => { d.style.opacity = 0; });
        tipHide();
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", leave);

    /* анимация «прорисовки» */
    if (animate && !REDUCED) {
        const fin = () => clipRect.setAttribute("width", W);
        try {
            const a = clipRect.animate([{ width: "0px" }, { width: W + "px" }],
                { duration: 1500, easing: "cubic-bezier(.22,.8,.24,1)", fill: "forwards" });
            a.onfinish = fin;
            a.oncancel = fin;
        } catch (_) { fin(); }
        setTimeout(fin, 1700);
    }
}

/* ---------------- кольцевая диаграмма ---------------- */

function renderDonut(box, items, centerLabel, animate) {
    if (!box) return;
    const total = items.reduce((a, b) => a + b.value, 0);
    const R = 76, C = 2 * Math.PI * R;
    box.innerHTML = "";

    const wrap = document.createElement("div");
    wrap.className = "donut";
    const svg = S("svg", { viewBox: "0 0 190 190" }, wrap);
    S("circle", { class: "bg", cx: 95, cy: 95, r: R }, svg);

    const arcs = [];
    let acc = 0;
    if (total > 0) {
        items.forEach((it, i) => {
            if (it.value <= 0) return;
            const len = (it.value / total) * C;
            const gap = Math.min(4, len * .4);
            const c = S("circle", {
                class: "arc c-line " + it.cls, cx: 95, cy: 95, r: R,
                "stroke-dasharray": `0 ${C}`, "stroke-dashoffset": -acc, "stroke-linecap": "butt"
            }, svg);
            c.dataset.tip = `${it.name}|${it.fmt(it.value)}|${(it.value / total * 100).toFixed(1)}%`;
            arcs.push({ c, len: Math.max(0, len - gap) });
            acc += len;
        });
    }
    const center = document.createElement("div");
    center.className = "donut-center";
    center.innerHTML = `<b id="donut-total">0</b><span>${escapeHtml(centerLabel)}</span>`;
    wrap.appendChild(center);

    const list = document.createElement("div");
    list.className = "src-list";
    items.forEach(it => {
        const pct = total > 0 ? it.value / total * 100 : 0;
        const row = document.createElement("div");
        row.className = "src-row";
        row.innerHTML = `<i class="sw ${it.bg}"></i><span>${escapeHtml(it.name)}</span>
            <b>${escapeHtml(it.fmt(it.value))}</b><em>${pct.toFixed(1)}%</em>
            <div class="src-bar"><i class="${it.bg}" data-w="${pct}"></i></div>`;
        list.appendChild(row);
    });

    box.appendChild(wrap);
    box.appendChild(list);
    const fill = () => {
        arcs.forEach(a => a.c.setAttribute("stroke-dasharray", `${a.len} ${C - a.len}`));
        list.querySelectorAll(".src-bar i").forEach(b => { b.style.width = b.dataset.w + "%"; });
    };
    if (animate === false || REDUCED) {
        wrap.classList.add("static");
        list.classList.add("static");
        center.querySelector("b").textContent = formatStars(total);
        fill();
    } else {
        countTo(center.querySelector("b"), total, formatStars, 1500);
        requestAnimationFrame(() => requestAnimationFrame(fill));
    }
}

/* ---------------- календарь активности ---------------- */

function renderHeatmap(box, byDate, animate) {
    if (!box) return;
    box.classList.toggle("static", animate === false);
    const avail = box.clientWidth || 900;
    const WEEKS = Math.max(12, Math.min(53, Math.floor((avail - 34 + 4) / 20)));
    const sub = document.getElementById("heat-sub");
    if (sub) sub.textContent = `Все действия по дням · последние ${WEEKS} нед.`;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const dow = (today.getDay() + 6) % 7; // пн = 0
    const start = new Date(today);
    start.setDate(start.getDate() - dow - (WEEKS - 1) * 7);

    const key = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const vals = [];
    for (let i = 0; i < WEEKS * 7; i++) {
        const d = new Date(start); d.setDate(start.getDate() + i);
        const v = byDate[key(d)] || 0;
        if (v > 0 && d <= today) vals.push(v);
    }
    vals.sort((a, b) => a - b);
    const q = (p) => vals.length ? vals[Math.min(vals.length - 1, Math.floor(vals.length * p))] : 1;
    const t1 = q(.25), t2 = q(.5), t3 = q(.8);
    const level = (v) => !v ? 0 : v <= t1 ? 1 : v <= t2 ? 2 : v <= t3 ? 3 : 4;

    const MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
    let months = "", prev = -1, cells = "";
    for (let w = 0; w < WEEKS; w++) {
        const d0 = new Date(start); d0.setDate(start.getDate() + w * 7);
        if (d0.getMonth() !== prev) {
            months += `<span style="grid-column:${w + 1}">${MONTHS[d0.getMonth()]}</span>`;
            prev = d0.getMonth();
        }
        for (let r = 0; r < 7; r++) {
            const d = new Date(start); d.setDate(start.getDate() + w * 7 + r);
            if (d > today) { cells += `<i class="hm-cell future"></i>`; continue; }
            const v = byDate[key(d)] || 0;
            const label = d.toLocaleDateString("ru-RU", { day: "numeric", month: "long", weekday: "short" });
            cells += `<i class="hm-cell h${level(v)}" style="--d:${Math.round((w * 14 + r * 4))}ms" data-tip="${label}|${formatNumber(v)} действий"></i>`;
        }
    }
    box.innerHTML = `<div class="hm-wrap">
        <div class="hm-days"><span></span><span>Пн</span><span></span><span>Ср</span><span></span><span>Пт</span><span></span></div>
        <div><div class="hm-months" style="grid-template-columns:repeat(${WEEKS},16px)">${months}</div>
        <div class="hm-grid">${cells}</div></div></div>`;
}

/* ---------------- глобальный UI ---------------- */

function setupVisualLayer() {
    /* тема */
    try {
        const saved = localStorage.getItem("ph-theme");
        if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
    } catch (_) {}
    const tb = document.getElementById("theme-btn");
    if (tb) tb.onclick = () => {
        const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem("ph-theme", next); } catch (_) {}
    };

    /* подсветка карточек и фон за курсором */
    let raf = 0;
    document.addEventListener("pointermove", (e) => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
            raf = 0;
            const t = e.target.closest && e.target.closest("[data-tip]");
            if (t) {
                const p = t.dataset.tip.split("|");
                tipShow(`<div class="t-h">${escapeHtml(p[0])}</div>` +
                    p.slice(1).map((x, i) => i === 0 && p.length > 2
                        ? `<div class="t-r"><b>${escapeHtml(x)}</b></div>` : `<div class="t-r"><span>${escapeHtml(x)}</span></div>`).join(""),
                    e.clientX, e.clientY);
            } else if (!e.target.closest || !e.target.closest("svg")) {
                tipHide();
            }
        });
    });

    /* часы */
    const clock = document.getElementById("clock");
    const tick = () => { if (clock) clock.textContent = new Date().toLocaleTimeString("ru-RU"); };
    tick();
    setInterval(tick, 1000);

    /* диапазон hero-графика */
    document.querySelectorAll("#hero-range button").forEach(b => b.onclick = () => {
        heroRange = Number(b.dataset.r);
        document.querySelectorAll("#hero-range button").forEach(x => x.classList.toggle("active", x === b));
        if (dashboardData) renderHeroChart(true);
    });

    placeIndicator();
    window.addEventListener("resize", placeIndicator);
}

function placeIndicator() {
    const act = document.querySelector(".nav-item.active");
    const ind = document.querySelector(".nav-indicator");
    if (!act || !ind) return;
    ind.style.setProperty("--ny", act.offsetTop + "px");
    ind.style.height = act.offsetHeight + "px";
}

let splashStart = performance.now();
let splashDone = false;

function hideSplash() {
    if (splashDone) return;
    splashDone = true;
    const el = document.getElementById("splash");
    if (!el) return;
    const wait = Math.max(0, 1500 - (performance.now() - splashStart));
    setTimeout(() => el.classList.add("out"), wait);
    setTimeout(() => el.remove(), wait + 900);
}

/* ---------------- данные для графиков ---------------- */

let heroRange = 90;
let firstRender = true;

function dayKeyOf(v) {
    const d = new Date(v);
    if (Number.isNaN(d.getTime())) return null;
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function shortDate(k) {
    const p = String(k).split("-");
    return p.length === 3 ? `${p[2]}.${p[1]}` : String(k);
}

function longDate(k) {
    const p = String(k).split("-");
    if (p.length !== 3) return String(k);
    return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric", weekday: "short" });
}

/* ежедневные суммы по всем аккаунтам, без пропусков по датам */
function dailySeries() {
    const g = {};
    (dashboardData.daily_stats || []).forEach(r => {
        const k = r.stat_date;
        if (!k) return;
        const o = g[k] || (g[k] = { clicks: 0, tasks: 0, mini_apps: 0, daily: 0, promos_found: 0, promos: 0, stars: 0 });
        o.clicks += Number(r.clicks || 0);
        o.tasks += Number(r.tasks_completed || 0);
        o.mini_apps += Number(r.mini_apps_completed || 0);
        o.daily += Number(r.daily_bonuses || 0);
        o.promos_found += Number(r.promo_codes_found || 0);
        o.promos += Number(r.promo_codes_activated || 0);
        o.stars += Number(r.total_stars || 0);
    });
    const keys = Object.keys(g).sort();
    if (!keys.length) return { labels: [], g };
    const labels = [];
    const a = new Date(keys[0] + "T00:00:00"), b = new Date(keys[keys.length - 1] + "T00:00:00");
    for (let d = new Date(a), i = 0; d <= b && i < 800; d.setDate(d.getDate() + 1), i++) {
        const k = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        labels.push(k);
        if (!g[k]) g[k] = { clicks: 0, tasks: 0, mini_apps: 0, daily: 0, promos_found: 0, promos: 0, stars: 0 };
    }
    return { labels, g };
}

/* суммарный баланс по дням (последний снимок аккаунта, протянутый вперёд) */
function balanceSeries() {
    const snaps = (dashboardData.balance_snapshots || []).slice()
        .sort((a, b) => new Date(a.captured_at) - new Date(b.captured_at));
    if (!snaps.length) return [];
    const byDay = {};
    snaps.forEach(s => {
        const k = dayKeyOf(s.captured_at);
        if (!k) return;
        (byDay[k] || (byDay[k] = {}))[s.account_id] = Number(s.balance || 0);
    });
    const keys = Object.keys(byDay).sort();
    const cur = {}, out = [];
    const a = new Date(keys[0] + "T00:00:00"), b = new Date(keys[keys.length - 1] + "T00:00:00");
    for (let d = new Date(a), i = 0; d <= b && i < 800; d.setDate(d.getDate() + 1), i++) {
        const k = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        if (byDay[k]) Object.assign(cur, byDay[k]);
        out.push(Object.values(cur).reduce((x, y) => x + y, 0));
    }
    return out;
}

function renderHeroChart(animate) {
    const { labels, g } = dailySeries();
    const box = document.getElementById("hero-chart");
    let acc = 0;
    const cum = labels.map(k => (acc += g[k].stars));
    const from = Math.max(0, labels.length - heroRange);
    lineChart(box, {
        labels: labels.slice(from),
        series: [{ name: "Всего Stars", values: cum.slice(from), cls: "c-s0", area: true, fmt: (v) => formatStars(v) + " ★" }],
        xFmt: shortDate, tipTitle: longDate, yFmt: compact,
        animate: animate === undefined ? firstRender : animate
    });
}

function renderSparks(animate) {
    const { labels, g } = dailySeries();
    const take = (key) => labels.slice(-30).map(k => g[k][key]);
    const map = {
        stars: take("stars"), clicks: take("clicks"), tasks: take("tasks"), daily: take("daily"),
        mini_apps: take("mini_apps"), promos_found: take("promos_found"),
        balance: balanceSeries().slice(-30)
    };
    document.querySelectorAll(".spark[data-spark]").forEach(el => renderSpark(el, map[el.dataset.spark], animate));
}

function renderTicker() {
    const box = document.getElementById("ticker");
    const track = document.getElementById("ticker-track");
    const logs = (dashboardData.logs || []).slice(0, 14);
    if (!box || !track) return;
    if (!logs.length) { box.classList.add("hidden"); return; }
    const html = logs.map(l => {
        const t = l.created_at ? new Date(l.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "";
        return `<span class="ticker-item"><b>${escapeHtml(t)}</b>${escapeHtml(l.message || l.event_type || "событие")}</span>`;
    }).join("");
    track.innerHTML = html + html;
    track.style.animationDuration = Math.max(30, logs.length * 5) + "s";
    box.classList.remove("hidden");
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
    hideSplash();
    $("refresh-btn").classList.remove("loading");
    $("loading").classList.add("hidden");
    $("content").classList.add("hidden");
    $("error").classList.remove("hidden");
    $("error-message").textContent = message;
}

function hideError() {
    $("error").classList.add("hidden");
}

function showContent() {
    hideSplash();
    $("refresh-btn").classList.remove("loading");
    $("loading").classList.add("hidden");
    $("content").classList.remove("hidden");
}

async function loadDashboard() {
    hideError();
    $("refresh-btn").classList.add("loading");

    if (!dashboardData) {
        $("loading").classList.remove("hidden");
        $("content").classList.add("hidden");
    }

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

        // сначала показываем контент: графики меряют ширину контейнеров
        showContent();
        renderAll();
        loadAccountsInfo().then(() => { renderAccounts(); renderAccountsTable(); });

        $("last-update").textContent =
            `Обновлено: ${new Date().toLocaleTimeString("ru-RU")}`;

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
    renderTicker();
    renderCharts();
}

function renderTotals() {
    const totals = dashboardData.totals || {};
    const star = (v) => formatStars(v) + " ★";
    const signed = (v) => (v >= 0 ? "+" : "−") + formatStars(Math.abs(v)) + " ★";

    countTo($("total-stars"), totals.stars, formatStars, 1900);
    countTo($("total-clicks"), totals.clicks, formatNumber);
    countTo($("total-tasks"), totals.tasks, formatNumber);
    countTo($("total-daily"), totals.daily, formatNumber);
    countTo($("total-promos-found"), totals.promos_found, formatNumber);
    countTo($("total-promos-activated"), totals.promos_activated, formatNumber);

    // BALANCE — данные приходят из /api/admin/overview
    const balance = dashboardData.balance || {};

    const earned = Number(balance.event_earnings ?? totals.stars ?? 0);
    const real = Number(
        balance.real_balance ??
        (dashboardData.real_balance || {}).current_balance ??
        0
    );
    const delta = Number(
        balance.today_delta ??
        (dashboardData.real_balance || {}).delta_today ??
        dashboardData.balance_delta_today ??
        0
    ) || 0;

    countTo($("event-earnings"), earned, star);
    countTo($("real-balance"), real, star);
    countTo($("balance-delta"), delta, signed);

    // шкала изменения баланса: влево — минус, вправо — плюс
    const fill = $("delta-fill");
    if (fill) {
        const frac = Math.min(1, Math.abs(delta) / Math.max(5, real * 0.15));
        fill.style.width = (frac * 50) + "%";
        fill.style.left = (delta >= 0 ? 50 : 50 - frac * 50) + "%";
    }

    // доля активированных промокодов
    const found = Number(totals.promos_found || 0);
    const act = Number(totals.promos_activated || 0);
    const ratio = found ? Math.min(100, act / found * 100) : 0;
    if ($("promo-ratio-fill")) $("promo-ratio-fill").style.width = ratio + "%";
    if ($("promo-ratio-text")) $("promo-ratio-text").textContent = found ? ratio.toFixed(0) + "%" : "—";

    // чипы под главным числом
    const ds = dailySeries();
    let best = { k: null, v: 0 };
    ds.labels.forEach(k => { if (ds.g[k].stars > best.v) best = { k, v: ds.g[k].stars }; });
    const days = ds.labels.length;
    if ($("hero-chip-avg")) $("hero-chip-avg").innerHTML =
        days ? `в среднем <b>${formatStars(Number(totals.stars || 0) / days)}</b> ★ / день` : "в среднем —";
    if ($("hero-chip-best")) $("hero-chip-best").innerHTML =
        best.k ? `рекорд <b>${formatStars(best.v)}</b> ★ · ${shortDate(best.k)}` : "рекорд —";
    if ($("hero-chip-days")) $("hero-chip-days").innerHTML = `<b>${days}</b> дн. данных`;

    countTo($("activity-clicks"), totals.clicks, formatNumber);
    countTo($("activity-tasks"), totals.tasks, formatNumber);
    countTo($("activity-mini-apps"), totals.mini_apps, formatNumber);
    countTo($("activity-daily"), totals.daily, formatNumber);
}

// ---------------- Прокси аккаунтов ----------------

const PROXY_LABELS = {
    ok: "Работает", slow: "Медленный", dead: "Не работает",
    none: "Без прокси", unknown: "Не проверен",
};

function proxyBadge(p) {
    const st = (p && p.status) || "unknown";
    const cls = st === "ok" ? "online" : st === "dead" ? "offline" : st === "slow" ? "warning" : "neutral";
    return `<span class="badge ${cls}">${PROXY_LABELS[st] || st}</span>`;
}

function proxyAddress(p) {
    if (!p || !p.configured) {
        const ip = p && p.direct_ip ? ` · ${escapeHtml(p.direct_ip)}` : "";
        return `<span class="proxy-sub">прямое соединение (IP Render${ip})</span>`;
    }
    const via = p.route === "direct"
        ? `<div class="proxy-sub">${ico("alert")}сейчас напрямую, IP Render${p.direct_ip ? ": " + escapeHtml(p.direct_ip) : ""}${p.failed_over ? " (авария прокси)" : ""}</div>`
        : "";
    const ip = p.exit_ip && p.route !== "direct" ? `<div class="proxy-sub">IP выхода: ${escapeHtml(p.exit_ip)}</div>` : "";
    return `<div>${escapeHtml(p.display || "—")}${p.auth ? " " + ico("key") : ""}</div>${via}${ip}`;
}

function proxyDetails(p) {
    if (!p) return "";
    const parts = [];
    if (p.latency_ms != null) parts.push(`до Telegram ${p.latency_ms} мс`);
    if (p.client_ping_ms != null) parts.push(`пинг аккаунта ${p.client_ping_ms} мс`);
    if (p.error) parts.push(escapeHtml(p.error));
    if (p.checked_at) parts.push(`проверено ${formatDate(p.checked_at)}`);
    return parts.length ? `<div class="proxy-sub">${parts.join(" · ")}</div>` : "";
}

async function checkProxies(accountNumber) {
    const buttons = document.querySelectorAll("[data-proxy-check], #proxy-check-all");
    buttons.forEach(b => { b.disabled = true; });
    $("proxy-status").textContent = "Проверяю прокси…";
    try {
        const r = await fetch(`/api/admin/proxies/check?account=${accountNumber || 0}`,
            { method: "POST", credentials: "same-origin" });
        if (!r.ok) {
            let m = "HTTP " + r.status;
            try { m = (await r.json()).detail || m; } catch (_) {}
            throw new Error(m);
        }
        const data = await r.json();
        (data.accounts || []).forEach(a => {
            const row = (dashboardData.accounts || []).find(x => Number(x.account_number) === a.account);
            if (row) row.proxy = a.proxy;
        });
        $("proxy-status").textContent = "";
        renderAccounts();
        renderAccountsTable();
    } catch (e) {
        $("proxy-status").textContent = "Не удалось проверить прокси: " + e.message;
    } finally {
        // кнопки в таблице пересоздаются, а «Проверить все» — статичная: включаем обратно всегда
        document.querySelectorAll("[data-proxy-check], #proxy-check-all").forEach(b => { b.disabled = false; });
    }
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
                    <span class="st">★</span> ${formatStars(item.stars)}
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
                    <span class="st">★</span> ${formatStars(item.stars)}
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

            let icon = "list";

            const type =
                String(log.event_type || "")
                    .toLowerCase();

            const isErr = type.includes("error") ||
                String(log.level || "").toLowerCase() === "error";

            if (isErr) {
                icon = "alert";
            } else if (type.includes("promo")) {
                icon = "ticket";
            } else if (type.includes("click")) {
                icon = "cursor";
            } else if (type.includes("task")) {
                icon = "check";
            } else if (type.includes("daily")) {
                icon = "gift";
            }

            return `
                <div class="event-row">

                    <div class="event-icon ${isErr ? "err" : ""}">
                        ${ico(icon, "")}
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
                                ? `<span class="log-err">${escapeHtml(log.error)}</span>`
                                : ""
                        }

                    </div>

                </div>
            `;
        }).join("");
}

function renderCharts() {
    const anim = firstRender;

    renderHeroChart();
    renderSparks(anim);
    renderActivityChart();
    renderStarsChart(anim);
    renderHeatmapChart(anim);

    firstRender = false;
}

function renderActivityChart() {
    const { labels, g } = dailySeries();
    const v = (key) => labels.map(d => g[d][key]);

    lineChart($("activity-chart"), {
        labels,
        xFmt: shortDate,
        tipTitle: longDate,
        legend: $("activity-legend"),
        animate: firstRender,
        series: [
            { name: "Клики",     values: v("clicks"),    cls: "c-s0", area: true },
            { name: "Задания",   values: v("tasks"),     cls: "c-s1" },
            { name: "Mini Apps", values: v("mini_apps"), cls: "c-s1 c-d1" },
            { name: "Daily",     values: v("daily"),     cls: "c-s2" },
            { name: "Promo",     values: v("promos"),    cls: "c-s0 c-d2" }
        ]
    });
}

function renderStarsChart(anim) {
    const t = dashboardData.totals || {};
    const f = (v) => formatStars(v) + " ★";

    renderDonut($("stars-chart"), [
        { name: "Клики",     value: Number(t.stars_from_clicks || 0), cls: "c-s0", bg: "bg-s0", fmt: f },
        { name: "Задания",   value: Number(t.stars_from_tasks  || 0), cls: "c-s1", bg: "bg-s1", fmt: f },
        { name: "Промокоды", value: Number(t.stars_from_promos || 0), cls: "c-s2", bg: "bg-s2", fmt: f },
        { name: "Daily",     value: Number(t.stars_from_daily  || 0), cls: "c-s3", bg: "bg-s3", fmt: f }
    ], "всего ★", anim);
}

function renderHeatmapChart(anim) {
    const { labels, g } = dailySeries();
    const byDate = {};

    labels.forEach(k => {
        const o = g[k];
        byDate[k] = o.clicks + o.tasks + o.mini_apps + o.daily + o.promos;
    });

    renderHeatmap($("heatmap"), byDate, anim);
}

function setupNavigation() {
    const buttons =
        document.querySelectorAll(".nav-item:not(.nav-link)");

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

        session: {
            title: "Сессия",
            subtitle: "Генерация строки сессии Telethon"
        },

        withdraw: {
            title: "Выводы",
            subtitle: "Автовывод звёзд подарком другу"
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

                placeIndicator();
                $("crumb").textContent = section;

                document
                    .querySelectorAll(".section")
                    .forEach(item =>
                        item.classList.remove("active")
                    );

                const target =
                    $(`section-${section}`);

                if (target) {
                    target.classList.add("active");
                    replaySparks(target);
                }

                if (section === "learning") {
                    loadKnowledge();
                }
                if (section === "accounts") {
                    loadAccountsInfo().then(() => renderAccountsTable());
                }
                if (section === "control") {
                    loadControl(true);
                }
                if (section === "resources") {
                    loadResources();
                }
                if (section === "withdraw") {
                    loadWithdraw();
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
    flow: "Сценарии",
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

async function loadControl(force) {
    try {
        const [r] = await Promise.all([
            fetch("/api/admin/jobs", { credentials: "same-origin", cache: "no-store" }),
            loadAccountsInfo()
        ]);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        ctlData = data;
        $("ctl-status").textContent = (data.accounts || []).length ? "" : "Аккаунты ещё не запущены.";

        // не перерисовываем, если ничего не изменилось (иначе лагает и сбивает ввод)
        const sig = JSON.stringify(data) + "|" + Math.floor(Date.now() / 60000) + "|" +
            JSON.stringify(Object.values(accInfo).map(i => [i.balance, i.floods_1h, i.floods_24h,
                i.withdrawn_stars, i.has_avatar, i.username, i.ready, i.referral_link]));
        if (force || sig !== ctlSig) {
            ctlSig = sig;
            renderControl();
        }
    } catch (e) {
        $("ctl-status").textContent = "Не удалось загрузить: " + e.message;
    }
    if (!ctlTimer) {
        ctlTimer = setInterval(() => {
            const ae = document.activeElement;
            const typing = ae && /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName) &&
                ae.closest && ae.closest("#section-control");
            if (document.hidden || typing) return;
            if (document.getElementById("section-control").classList.contains("active")) loadControl();
            else if (document.getElementById("section-accounts").classList.contains("active")) {
                loadAccountsInfo().then(() => renderAccountsTable());
            }
        }, 15000);
    }
}

/* =====================================================================
   АККАУНТЫ: данные и помощники
   ===================================================================== */

let accInfo = {};          // номер аккаунта -> живые данные из /api/admin/accounts-info
let ctlData = null;        // ответ /api/admin/jobs
let ctlSel = null;         // выбранный аккаунт
let ctlTab = "sched";      // выбранная вкладка карточки
let showHiddenAcc = false; // показывать ли скрытые аккаунты
let ctlSig = "";

function hiddenAccs() {
    try { return new Set(JSON.parse(localStorage.getItem("ph-hidden-acc") || "[]").map(Number)); }
    catch (_) { return new Set(); }
}

function setAccHidden(n, on) {
    const s = hiddenAccs();
    if (on) s.add(Number(n)); else s.delete(Number(n));
    try { localStorage.setItem("ph-hidden-acc", JSON.stringify([...s])); } catch (_) {}
}

async function loadAccountsInfo() {
    try {
        const r = await fetch("/api/admin/accounts-info", { credentials: "same-origin", cache: "no-store" });
        if (!r.ok) return false;
        const d = await r.json();
        const next = {};
        (d.accounts || []).forEach(a => { next[Number(a.account)] = a; });
        accInfo = next;
        return true;
    } catch (_) {
        return false;
    }
}

function accNick(n) {
    const i = accInfo[n];
    return i && i.username ? "@" + i.username : "";
}

function accFullName(n) {
    const i = accInfo[n];
    return i ? [i.first_name, i.last_name].filter(Boolean).join(" ") : "";
}

function avatarHtml(n, name, size) {
    const i = accInfo[n];
    const ch = String(accFullName(n) || name || "?").trim().charAt(0).toUpperCase() || "?";
    const img = i && i.has_avatar
        ? `<img src="/api/admin/account-avatar?account=${Number(n)}" alt="" loading="lazy" onerror="this.remove()">` : "";
    return `<span class="av av-${size}"><b>${escapeHtml(ch)}</b>${img}</span>`;
}

function statusOf(dead, ready, connected) {
    if (dead) return { cls: "bad", dot: "bad", text: "Остановлен" };
    if (ready === false) return { cls: "warn", dot: "warn", text: "Запускается" };
    if (connected === false) return { cls: "off", dot: "off", text: "Оффлайн" };
    return { cls: "ok", dot: "on", text: "Онлайн" };
}

function statusPill(st) {
    return `<span class="spill ${st.cls}"><i></i>${st.text}</span>`;
}

function copyText(text, btn) {
    const done = () => {
        if (!btn) return;
        const old = btn.dataset.old || btn.textContent;
        btn.dataset.old = old;
        btn.textContent = "Скопировано";
        setTimeout(() => { btn.textContent = old; }, 1400);
    };
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, () => {});
        return;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (_) {}
    ta.remove();
}

function openAccount(n, tab) {
    ctlSel = Number(n);
    if (tab) ctlTab = tab;
    const btn = document.querySelector('.nav-item[data-section="control"]');
    if (btn) btn.click();
}

function kv(label, value, sub) {
    return `<div class="kv"><i>${escapeHtml(label)}</i><b>${value}</b>${sub ? `<span>${sub}</span>` : ""}</div>`;
}

function hiddenToggleHtml(count) {
    if (!count && !showHiddenAcc) return "";
    return `<button class="acc-chip ghost" data-toggle-hidden>${showHiddenAcc ? "Прятать скрытые" : `Скрытые (${count})`}</button>`;
}

/* =====================================================================
   СТРАНИЦА «АККАУНТЫ»: карточки всех аккаунтов
   ===================================================================== */

function renderAccounts() {
    const container = $("accounts-preview");
    const hid = hiddenAccs();
    const accounts = (dashboardData.accounts || [])
        .filter(a => showHiddenAcc || !hid.has(Number(a.account_number)));

    if (!accounts.length) {
        container.innerHTML = `<div class="empty">Аккаунтов нет</div>`;
        return;
    }

    container.innerHTML = accounts.map(account => {
        const n = Number(account.account_number);
        const info = accInfo[n] || {};
        const st = statusOf(info.dead, info.ready, account.connected === true);
        const name = account.name || `Аккаунт ${n}`;
        return `
            <div class="account-row" data-open-acc="${n}" style="cursor:pointer">
                <div class="account-info">
                    ${avatarHtml(n, name, 32)}
                    <div class="account-name">${escapeHtml(name)}</div>
                </div>
                <div class="account-status ${st.cls === "ok" ? "online" : "offline"}">
                    ${statusPill(st)}
                    ${proxyBadge(account.proxy)}
                </div>
            </div>`;
    }).join("");

    container.querySelectorAll("[data-open-acc]").forEach(el => {
        el.onclick = () => openAccount(el.dataset.openAcc);
    });
}

function renderAccountsTable() {
    const box = $("accounts-grid");
    if (!box || !dashboardData) return;

    const hid = hiddenAccs();
    const all = dashboardData.accounts || [];
    const hiddenCount = all.filter(a => hid.has(Number(a.account_number))).length;
    const list = all.filter(a => showHiddenAcc || !hid.has(Number(a.account_number)));

    const tg = $("acc-toggle-hidden");
    if (tg) {
        tg.classList.toggle("hidden", !hiddenCount && !showHiddenAcc);
        tg.textContent = showHiddenAcc ? "Прятать скрытые" : `Скрытые (${hiddenCount})`;
        tg.onclick = () => { showHiddenAcc = !showHiddenAcc; renderAccountsTable(); renderAccounts(); };
    }
    const all2 = $("proxy-check-all");
    if (all2) all2.onclick = () => checkProxies(0);

    if (!list.length) {
        box.innerHTML = `<div class="empty">${all.length ? "Все аккаунты скрыты" : "Аккаунтов нет"}</div>`;
        return;
    }

    box.innerHTML = list.map(account => {
        const n = Number(account.account_number);
        const i = accInfo[n] || {};
        const isHidden = hid.has(n);
        const st = statusOf(i.dead, i.ready, account.connected === true);
        const name = account.name || `Аккаунт ${n}`;
        const nick = accNick(n);
        const sub = [accFullName(n), nick, i.tg_id ? "ID " + i.tg_id : ""].filter(Boolean).join(" · ");
        const flood = i.floods_24h != null ? String(i.floods_24h) : "—";
        const out = i.withdrawn_stars != null ? formatStars(i.withdrawn_stars) + " ★" : "—";
        const bal = i.balance != null && i.ready ? formatStars(i.balance) + " ★" : "—";
        const p = account.proxy || {};
        return `
        <article class="mini ${i.dead ? "is-dead" : ""} ${isHidden ? "is-hidden" : ""}">
            <div class="mini-top">
                ${avatarHtml(n, name, 44)}
                <div class="mini-id">
                    <b>${escapeHtml(name)}</b>
                    <span>${escapeHtml(sub || "данные появятся после запуска")}</span>
                </div>
                ${statusPill(st)}
            </div>
            ${i.dead ? `<div class="mini-dead">${ico("x", "")}${escapeHtml(i.dead)}</div>` : ""}
            <div class="mini-stats">
                <div><i>Баланс</i><b>${bal}</b></div>
                <div><i>FloodWait 24ч</i><b>${flood}</b></div>
                <div><i>Выведено</i><b>${out}</b></div>
            </div>
            <div class="mini-line">
                ${proxyBadge(p)}
                <span>${p.route === "proxy" ? "через прокси" : "напрямую (Render)"}${p.latency_ms != null ? " · " + p.latency_ms + " мс" : ""}</span>
            </div>
            <div class="mini-actions">
                <button class="kn-btn kn-primary" data-open-acc="${n}">Управлять</button>
                <button class="kn-btn" data-proxy-check="${n}">Проверить прокси</button>
                <button class="kn-btn" data-hide-acc="${n}" data-on="${isHidden ? 0 : 1}">${isHidden ? "Показать" : "Скрыть"}</button>
            </div>
        </article>`;
    }).join("");

    box.querySelectorAll("[data-open-acc]").forEach(b => b.onclick = () => openAccount(b.dataset.openAcc));
    box.querySelectorAll("[data-proxy-check]").forEach(b => b.onclick = () => checkProxies(Number(b.dataset.proxyCheck)));
    box.querySelectorAll("[data-hide-acc]").forEach(b => b.onclick = () => {
        setAccHidden(b.dataset.hideAcc, b.dataset.on === "1");
        renderAccountsTable();
        renderAccounts();
    });
}

/* =====================================================================
   СТРАНИЦА «УПРАВЛЕНИЕ»: выбрал аккаунт → вкладка → настройка
   ===================================================================== */

const CTL_TABS = [
    ["sched", "Расписание"],
    ["proxy", "Прокси"],
    ["warm", "Прогрев"],
    ["prot", "Защита"],
    ["wd", "Вывод"],
];

function renderControl() {
    const d = ctlData;
    const sw = $("acc-switch");
    const list = $("ctl-list");
    if (!d || !sw || !list) return;

    const hid = hiddenAccs();
    const all = d.accounts || [];
    const vis = all.filter(a => showHiddenAcc || !hid.has(a.account));
    const hiddenCount = all.filter(a => hid.has(a.account)).length;

    if (!vis.find(a => a.account === ctlSel)) ctlSel = vis.length ? vis[0].account : null;

    sw.innerHTML = vis.map(a => {
        const st = statusOf(a.dead, a.ready, true);
        const name = a.label;
        const nick = accNick(a.account);
        return `<button class="acc-chip ${a.account === ctlSel ? "active" : ""} ${a.dead ? "dead" : ""}" data-sel="${a.account}">
            ${avatarHtml(a.account, name, 28)}
            <span><b>${escapeHtml(name)}</b><em>${escapeHtml(nick || st.text)}</em></span>
            <i class="sdot ${st.dot}"></i>
        </button>`;
    }).join("") + hiddenToggleHtml(hiddenCount);

    const a = vis.find(x => x.account === ctlSel);
    list.innerHTML = a
        ? accountCard(a, d.titles || {})
        : `<div class="panel"><div class="empty">${all.length ? "Все аккаунты скрыты" : "Аккаунты ещё не запущены"}</div></div>`;

    bindControlUI();
}

function accountCard(a, titles) {
    const n = a.account;
    const i = accInfo[n] || {};
    const p = a.protection || {};
    const w = a.warmup || {};
    const px = a.proxy || {};
    const st = statusOf(a.dead, a.ready, true);
    const full = accFullName(n);
    const nick = accNick(n);
    const isHidden = hiddenAccs().has(n);

    // Запущен
    let started = "—", startedSub = "";
    if (i.started_at) {
        started = escapeHtml(formatDate(i.started_at));
        const up = (Date.now() - new Date(i.started_at).getTime()) / 1000;
        if (up > 0 && a.ready) startedSub = "работает " + fmtSec(up);
    } else if (!a.ready && !a.dead) {
        startedSub = "запускается…";
    }

    // FloodWait
    const f24 = p.floods_24h != null ? p.floods_24h : i.floods_24h;
    const f1 = p.floods_1h != null ? p.floods_1h : i.floods_1h;
    let floodSub = f1 != null ? `за час: ${f1}` : "";
    const lf = p.last_flood;
    if (lf) {
        const ago = i.last_flood && i.last_flood.ago_sec != null ? `, ${fmtSec(i.last_flood.ago_sec)} назад` : "";
        floodSub += ` · последний ${lf.sec} с${ago}`;
    } else if (f24 != null) {
        floodSub += " · не было";
    }

    const wLabel = w.enabled && w.active ? `день ${w.day}/${w.days}` : (w.enabled ? "завершён" : "выключен");
    const wSub = w.enabled && w.active ? (w.mode === "ultra" ? "ультра" : "обычный") + ` · ${Math.round((w.factor || 0) * 100)}% нагрузки` : "полная нагрузка";

    const cells = [
        kv("Запущен", started, escapeHtml(startedSub)),
        kv("Баланс", i.balance != null && a.ready ? formatStars(i.balance) + " ★" : "—",
            i.friends != null ? `друзей ${i.friends}${typeof i.activated === "number" ? ` · активировали ${i.activated}` : ""}` : ""),
        kv("Выведено", i.withdrawn_stars != null ? formatStars(i.withdrawn_stars) + " ★" : "—",
            i.withdrawn_count != null ? `выводов: ${i.withdrawn_count}` : "нет данных"),
        kv("FloodWait / 24ч", f24 != null ? String(f24) : "—", escapeHtml(floodSub)),
        kv("Маршрут", px.route === "proxy" ? "Прокси" : "Напрямую",
            escapeHtml(PROXY_LABELS[px.status || "unknown"] + (px.latency_ms != null ? " · " + px.latency_ms + " мс" : ""))),
        kv("Прогрев", escapeHtml(wLabel), escapeHtml(wSub)),
        kv("Защита", escapeHtml(p.state_title || "—"),
            p.cooldown_left > 0 ? "КД ещё " + fmtSec(p.cooldown_left) : "ограничений нет"),
        kv("Telegram ID", i.tg_id ? String(i.tg_id) : "—", nick ? escapeHtml(nick) : ""),
    ].join("");

    const ref = i.referral_link
        ? `<div class="refrow"><label>Реф. ссылка</label>
              <input readonly value="${escapeHtml(i.referral_link)}" onfocus="this.select()">
              <button class="kn-btn" data-copy="${escapeHtml(i.referral_link)}">Копировать</button></div>`
        : `<div class="refrow"><label>Реф. ссылка</label><span class="muted">появится после запуска аккаунта</span></div>`;

    const dead = a.dead
        ? `<div class="banner bad">${ico("x", "")}<div><b>Аккаунт остановлен:</b> ${escapeHtml(a.dead)}.
            Нужна новая сессия (вкладка «Сессия»), после замены TELEGRAM_SESSION_${n} на Render аккаунт запустится заново.
            Мёртвый аккаунт можно скрыть с экрана кнопкой справа.</div></div>` : "";

    const tabs = CTL_TABS.map(([k, t]) =>
        `<button class="tab ${k === ctlTab ? "active" : ""}" data-tab="${k}">${t}</button>`).join("");

    let body = "";
    if (ctlTab === "sched") body = schedPanel(a, titles);
    else if (ctlTab === "proxy") body = routePanel(a);
    else if (ctlTab === "warm") body = warmupPanel(a);
    else if (ctlTab === "prot") body = protectionPanel(a);
    else body = withdrawPanel(a);

    return `<div class="acc-card">
        <div class="acc-head">
            ${avatarHtml(n, a.label, 72)}
            <div class="acc-title">
                <h2>${escapeHtml(a.label)}</h2>
                <div class="acc-sub">
                    ${full ? `<span><b>${escapeHtml(full)}</b></span>` : ""}
                    ${nick ? `<span>${escapeHtml(nick)}</span>` : `<span>ник не задан</span>`}
                    ${i.tg_id ? `<span>ID <b>${i.tg_id}</b></span>` : ""}
                </div>
            </div>
            <div class="acc-head-right">
                ${statusPill(st)}
                <button class="kn-btn" data-hide-acc="${n}" data-on="${isHidden ? 0 : 1}"
                    title="Скрывает аккаунт с экрана в этом браузере. Сам аккаунт не затрагивается.">${isHidden ? "Показать" : "Скрыть"}</button>
            </div>
        </div>
        ${dead}
        <div class="kv-grid">${cells}</div>
        ${timelineHtml(n, i, a)}
        ${ref}
        <div class="tabs">${tabs}</div>
        <div class="tab-body">${body}</div>
    </div>`;
}

function bindControlUI() {
    document.querySelectorAll("#acc-switch [data-sel]").forEach(b => b.onclick = () => {
        ctlSel = Number(b.dataset.sel);
        renderControl();
    });
    document.querySelectorAll("#acc-switch [data-toggle-hidden]").forEach(b => b.onclick = () => {
        showHiddenAcc = !showHiddenAcc;
        renderControl();
    });
    document.querySelectorAll("#ctl-list [data-tab]").forEach(b => b.onclick = () => {
        ctlTab = b.dataset.tab;
        renderControl();
    });
    document.querySelectorAll("#ctl-list [data-copy]").forEach(b => b.onclick = () => copyText(b.dataset.copy, b));
    document.querySelectorAll("#ctl-list [data-hide-acc]").forEach(b => b.onclick = () => {
        setAccHidden(b.dataset.hideAcc, b.dataset.on === "1");
        if (b.dataset.on === "1") ctlSel = null;
        renderControl();
        renderAccounts();
        renderAccountsTable();
    });
    document.querySelectorAll("#ctl-list [data-wm]").forEach(el => el.onclick = () => {
        const n = el.dataset.n;
        $(`wmode-${n}`).value = el.dataset.wm;
        document.querySelectorAll(`#ctl-list [data-wm][data-n="${n}"]`)
            .forEach(x => x.classList.toggle("active", x === el));
        const days = $(`wdays-${n}`);
        if (days) days.max = el.dataset.wm === "ultra" ? 90 : 60;
    });
    document.querySelectorAll("#ctl-list [data-goto-wd]").forEach(b => b.onclick = () => {
        const btn = document.querySelector('.nav-item[data-section="withdraw"]');
        if (btn) btn.click();
    });
    document.querySelectorAll("#ctl-list button[data-job]").forEach(b => b.onclick = () => runJob(b));
    document.querySelectorAll("#ctl-list button[data-probe]").forEach(b => b.onclick = () => probeWithdraw(b));
    bindControlPanels();
}


/* ===== Хронология аккаунта: живой отсчёт + ручные даты (хранятся в этом браузере) ===== */
function accMeta(n) {
    try { return (JSON.parse(localStorage.getItem("ph-acc-meta") || "{}"))[n] || {}; } catch (e) { return {}; }
}
function setAccMeta(n, key, val) {
    try {
        const all = JSON.parse(localStorage.getItem("ph-acc-meta") || "{}");
        all[n] = all[n] || {};
        if (val === "" || val == null) delete all[n][key]; else all[n][key] = val;
        localStorage.setItem("ph-acc-meta", JSON.stringify(all));
    } catch (e) {}
}
function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
          m = Math.floor(sec % 3600 / 60), s = sec % 60, z = x => String(x).padStart(2, "0");
    return (d ? d + " д " : "") + z(h) + ":" + z(m) + ":" + z(s);
}
function timelineHtml(n, i, a) {
    const m = accMeta(n);
    const startMs = i.started_at ? new Date(i.started_at).getTime() : 0;
    const live = startMs && a.ready;
    const prior = Number(m.worked_days || 0) * 86400;
    const addedDef = i.added_at ? String(i.added_at).slice(0, 10) : "";
    return `<div class="timeline" data-acc="${n}">
        <div class="tl-cell"><i>Запущен (отсчёт)</i>
            <b class="tl-live" ${live ? `data-up="${startMs}"` : ""}>${live ? fmtClock((Date.now() - startMs) / 1000) : "не запущен"}</b>
            <span>${startMs ? "с " + escapeHtml(formatDate(i.started_at)) : "время запуска появится после старта"}</span></div>
        <div class="tl-cell"><i>Проработал всего</i>
            <b class="tl-live" ${live ? `data-up="${startMs}"` : ""} data-off="${prior}">${fmtClock(prior + (live ? (Date.now() - startMs) / 1000 : 0))}</b>
            <span class="tl-inp">до запуска, дней: <input type="number" min="0" step="1" data-meta="worked_days" value="${escapeHtml(m.worked_days || "")}" placeholder="0"></span></div>
        <div class="tl-cell"><i>Добавлен</i>
            <input type="date" data-meta="added" value="${escapeHtml(m.added || addedDef)}">
            <span>дата добавления в воронку</span></div>
        <div class="tl-cell"><i>Регистрация аккаунта</i>
            <input type="date" data-meta="reg" value="${escapeHtml(m.reg || "")}">
            <span class="tl-age" data-reg="${escapeHtml(m.reg || "")}">${regAge(m.reg)}</span></div>
    </div>`;
}
function regAge(d) {
    if (!d) return "укажите вручную";
    const days = Math.floor((Date.now() - new Date(d).getTime()) / 86400000);
    return days >= 0 ? `аккаунту ${days} дн.` : "дата в будущем";
}
document.addEventListener("change", e => {
    const t = e.target;
    if (!t.dataset || !t.dataset.meta) return;
    const n = t.closest(".timeline").dataset.acc;
    setAccMeta(n, t.dataset.meta, t.value);
    if (t.dataset.meta === "reg") t.closest(".timeline").querySelector(".tl-age").textContent = regAge(t.value);
    if (t.dataset.meta === "worked_days") {
        const el = t.closest(".timeline").querySelectorAll("[data-off]")[0];
        if (el) el.dataset.off = Number(t.value || 0) * 86400;
    }
});
setInterval(() => {
    if (document.hidden) return;
    document.querySelectorAll(".tl-live[data-up]").forEach(el => {
        el.textContent = fmtClock(Number(el.dataset.off || 0) + (Date.now() - Number(el.dataset.up)) / 1000);
    });
}, 1000);

async function postJson(url, body) {
    const r = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
    return data;
}

function fmtSec(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec >= 86400) return `${Math.floor(sec / 86400)} дн ${Math.floor((sec % 86400) / 3600)} ч`;
    if (sec >= 3600) return `${Math.floor(sec / 3600)} ч ${Math.floor((sec % 3600) / 60)} мин`;
    if (sec >= 60) return `${Math.floor(sec / 60)} мин`;
    return `${sec} с`;
}

function lr(label, html) {
    return `<div class="lr"><span>${escapeHtml(label)}</span><b>${html}</b></div>`;
}

/* ---------------- Расписание ---------------- */

function schedPanel(a, titles) {
    const jobs = Object.entries(a.jobs || {}).map(([job, s]) => {
        const title = titles[job] || job;
        const st = s.running ? pill("run", "выполняется")
            : (s.status === "error" ? pill("bad", "ошибка")
                : (s.status ? pill("ok", "ок") : pill("idle", "ещё не было")));
        const btn = job === "clicker" ? "Сделать клик"
            : job === "tasks" ? "Сделать задания"
            : job === "daily" ? "Забрать ежедневку" : "Запустить обход";
        let plan = "";
        if (job === "clicker" && s.plan && s.plan.total) {
            const pct = Math.min(100, Math.round(s.plan.done / s.plan.total * 100));
            plan = `<div class="plan"><div class="pbar"><i style="width:${pct}%"></i></div>
                <span>план дня ${s.plan.done}/${s.plan.total}</span></div>`;
        }
        return `<div class="job">
            <div class="job-top"><b>${escapeHtml(title)}</b>${st}</div>
            ${plan}
            ${lr("Следующий", escapeHtml(fmtRel(s.next)))}
            ${lr("Последний", escapeHtml(fmtRel(s.last)))}
            ${s.message ? `<div class="job-msg">${escapeHtml(s.message)}</div>` : ""}
            <button class="kn-btn kn-primary" data-acc="${a.account}" data-job="${job}"
                ${(!a.ready || s.running || !s.manual) ? "disabled" : ""}>${btn}</button>
        </div>`;
    }).join("");
    return `<div class="jobs-grid">${jobs || `<div class="empty">Задач нет</div>`}</div>`;
}

/* ---------------- Прокси ---------------- */

const MODE_TITLES = { auto: "Авто", on: "Только прокси", off: "Напрямую" };
const MODE_HINTS = {
    auto: "Идёт через прокси; если прокси не работает — сам переходит на IP Render и возвращается, когда прокси стабильно здоров 10 минут.",
    on: "Только через прокси. Если прокси упадёт — аккаунт останется без связи (аварийного перехода нет).",
    off: "Прокси выключен: аккаунт работает с IP Render, что бы ни случилось с прокси.",
};

function routePanel(a) {
    const n = a.account;
    const p = a.proxy || {};
    const mode = p.mode || "auto";
    const has = !!p.configured;
    const routeNow = p.route === "proxy" ? "Через прокси" : p.route === "direct" ? "Напрямую (IP Render)" : "—";
    const src = p.url_source === "admin" ? "задан в админке"
        : p.url_source === "env" ? "из переменной ACCOUNT_N_PROXY" : "—";
    const ping = [
        p.latency_ms != null ? `до Telegram ${p.latency_ms} мс` : "",
        p.client_ping_ms != null ? `аккаунт ${p.client_ping_ms} мс` : "",
    ].filter(Boolean).join(" · ") || "—";
    const exitIp = p.route === "direct" ? (p.direct_ip || "—") : (p.exit_ip || "—");

    const seg = ["auto", "on", "off"].map(m =>
        `<button class="seg-btn ${m === mode ? "active" : ""}" data-pmode="${m}" data-acc="${n}"
            ${(a.dead || (m !== "off" && !has)) ? "disabled" : ""}>${MODE_TITLES[m]}</button>`).join("");

    const fo = p.failed_over
        ? `<div class="banner warn">${ico("alert", "")}<div>Авария прокси: аккаунт временно на IP Render. Вернётся на прокси сам,
           когда тот проработает стабильно ${Math.round((p.failback_sec || 600) / 60)} мин.</div></div>` : "";
    const err = p.error ? `<div class="banner warn">${ico("alert", "")}<div>${escapeHtml(p.error)}</div></div>` : "";
    const ev = (p.events || []).slice().reverse().slice(0, 3)
        .map(e => `<div class="ev">${escapeHtml(formatDate(e.at))} — ${escapeHtml(e.text)}</div>`).join("");

    return `<div class="two-col">
        <div>
            <h4 class="pn-h">Состояние</h4>
            ${lr("Сейчас", escapeHtml(routeNow))}
            ${lr("Прокси", has ? escapeHtml(p.display || "—") + (p.auth ? " " + ico("key") : "") : "не задан")}
            ${lr("Статус", proxyBadge(p))}
            ${lr("IP выхода", escapeHtml(exitIp))}
            ${lr("Пинг", escapeHtml(ping))}
            ${lr("Проверен", p.checked_at ? escapeHtml(formatDate(p.checked_at)) : "—")}
            ${lr("Источник", escapeHtml(src))}
            ${fo}${err}
            ${ev ? `<div class="evs">${ev}</div>` : ""}
            <div class="btn-row"><button class="kn-btn" data-pcheck="${n}">Проверить сейчас</button></div>
        </div>
        <div>
            <h4 class="pn-h">Режим маршрута</h4>
            <div class="seg">${seg}</div>
            <p class="hint">${escapeHtml(MODE_HINTS[mode])}</p>
            <h4 class="pn-h">${has ? "Заменить прокси" : "Добавить прокси"}</h4>
            <input class="kn-search full" type="text" id="purl-${n}" placeholder="socks5://user:pass@host:port" autocomplete="off">
            <div class="btn-row">
                <button class="kn-btn kn-primary" data-purl="${n}" ${a.dead ? "disabled" : ""}>Сохранить прокси</button>
                ${p.url_source === "admin" ? `<button class="kn-btn" data-purl-clear="${n}">Убрать адрес</button>` : ""}
                ${has && mode !== "off" ? `<button class="kn-btn" data-pmode="off" data-acc="${n}">Отключить прокси</button>` : ""}
            </div>
            ${p.url_source === "env" ? `<p class="hint">Адрес задан переменной окружения. Чтобы убрать его совсем, удалите ACCOUNT_${n}_PROXY на Render или выберите «Напрямую».</p>` : ""}
        </div>
    </div>`;
}

/* ---------------- Защита ---------------- */

function protectionPanel(a) {
    const p = a.protection;
    if (!p) return `<div class="empty">Нет данных о защите аккаунта</div>`;
    const glyph = { ACTIVE: "●", COOLDOWN: "■", RECOVERING: "◐", OFFLINE: "○", AUTH_ERROR: "✕" }[p.state] || "•";
    const fl = p.last_flood
        ? `${p.last_flood.sec} с на «${escapeHtml(String(p.last_flood.action))}»` : "не было";
    const budgets = Object.values(p.budgets || {}).map(b => {
        const pct = b.cap ? Math.min(100, Math.round(b.used / b.cap * 100)) : 0;
        return `<div class="bud"><div class="bud-top"><span>${escapeHtml(b.title)}</span>
            <b>${b.used} / ${b.cap === null ? "∞" : b.cap}</b></div>
            <div class="pbar"><i style="width:${pct}%"></i></div></div>`;
    }).join("");
    const alerts = [];
    if (p.cooldown_left > 0) alerts.push(`КД ещё ${fmtSec(p.cooldown_left)}`);
    if (p.recovering_left > 0) alerts.push(`восстановление ещё ${fmtSec(p.recovering_left)}`);
    if (p.penalty_left > 0) alerts.push(`штрафной режим ещё ${fmtSec(p.penalty_left)}`);
    const peer = p.peer_flood
        ? `<div class="banner bad">${ico("alert", "")}<div>Был PeerFlood — проверьте аккаунт в @SpamBot.</div></div>` : "";

    return `<div class="two-col">
        <div>
            <h4 class="pn-h">Состояние</h4>
            <div class="big-state">${glyph} ${escapeHtml(p.state_title)}</div>
            ${alerts.length ? `<p class="hint">${alerts.join(" · ")}</p>` : ""}
            ${peer}
            ${lr("FloodWait за час", String(p.floods_1h))}
            ${lr("FloodWait за сутки", String(p.floods_24h))}
            ${lr("Последний", fl)}
            ${lr("Нагрузка", Math.round(p.load_factor * 100) + "%")}
            ${lr("Замедление", "×" + p.slowdown)}
            ${lr("Паузы", "×" + p.pace)}
            <div class="btn-row"><button class="kn-btn" data-pclear="${a.account}"
                ${(p.cooldown_left || p.recovering_left || p.penalty_left) ? "" : "disabled"}>Сбросить КД / штраф</button></div>
        </div>
        <div>
            <h4 class="pn-h">Бюджеты на сегодня</h4>
            ${budgets || `<div class="empty">Нет данных</div>`}
        </div>
    </div>`;
}

/* ---------------- Прогрев ---------------- */

function warmupPanel(a) {
    const n = a.account;
    const w = a.warmup || {};
    const ultra = w.mode === "ultra";
    let head;
    if (w.enabled && w.active) {
        const till = w.ends_at ? ` · до ${escapeHtml(formatDate(w.ends_at))}` : "";
        const pct = w.days ? Math.min(100, Math.round(((w.day - 1) / w.days) * 100)) : 0;
        let extra = `нагрузка ${Math.round(w.factor * 100)}%`;
        if (ultra) {
            const phase = w.phase === "lurk"
                ? `затишье (ещё ${fmtSec(w.lurk_left_sec)}): только чтение канала` : "рост по кривой";
            const quiet = w.quiet ? ` · тихие часы ${escapeHtml(w.quiet)} (UTC${w.tz_offset >= 0 ? "+" : ""}${w.tz_offset})` : "";
            extra = `${phase} · нагрузка ${Math.round(w.factor * 100)}% · замедление ×${w.slowdown}${quiet}`;
        }
        head = `<div class="warm-state on">
            <div class="warm-top">${ico(ultra ? "shield" : "leaf", "")}<b>${ultra ? "Ультра-прогрев" : "Прогрев"}: день ${w.day} из ${w.days}</b><span>${till}</span></div>
            <div class="pbar big"><i style="width:${pct}%"></i></div>
            <div class="warm-sub">${extra}</div></div>`;
    } else if (w.enabled) {
        head = `<div class="warm-state done">${ico("check", "")}<b>Прогрев завершён (${w.days} дн.)</b><span>работает на полной нагрузке</span></div>`;
    } else {
        head = `<div class="warm-state">${ico("leaf", "")}<b>Прогрев выключен</b><span>аккаунт работает на полной нагрузке</span></div>`;
    }
    const cur = w.enabled && w.mode ? w.mode : "normal";
    const days = w.enabled && w.days ? w.days : 5;
    return `${head}
        <h4 class="pn-h">Режим прогрева</h4>
        <div class="mode-cards">
            <div class="mode-card ${cur === "normal" ? "active" : ""}" data-wm="normal" data-n="${n}">
                <b>Обычный</b><span>Нагрузка плавно растёт с 20% до 100%. До 60 дней.</span></div>
            <div class="mode-card ${cur === "ultra" ? "active" : ""}" data-wm="ultra" data-n="${n}">
                <b>Ультра</b><span>Для хрупких аккаунтов: сначала только чтение канала, затем рост с 5%, тихие часы, строгий прокси. До 90 дней.</span></div>
        </div>
        <input type="hidden" id="wmode-${n}" value="${cur}">
        <div class="btn-row">
            <label class="inline-f">Дней <input class="kn-search" type="number" min="1" max="${cur === "ultra" ? 90 : 60}" value="${days}" id="wdays-${n}"></label>
            <button class="kn-btn kn-primary" data-wstart="${n}" ${a.dead ? "disabled" : ""}>${w.enabled && w.active ? "Начать заново" : "Запустить прогрев"}</button>
            ${w.enabled ? `<button class="kn-btn" data-wstop="${n}">Выключить</button>` : ""}
        </div>
        <p class="hint">Промокоды и чеки ловятся и во время прогрева (в ультра — с дневным лимитом). Применяется сразу.</p>`;
}

/* ---------------- Вывод ---------------- */

function withdrawPanel(a) {
    const i = accInfo[a.account] || {};
    return `<div class="two-col">
        <div>
            <h4 class="pn-h">Звёзды</h4>
            ${lr("Баланс", i.balance != null && a.ready ? formatStars(i.balance) + " ★" : "—")}
            ${lr("Друзей", i.friends != null ? String(i.friends) : "—")}
            ${lr("Активировали бота", i.activated != null ? String(i.activated) : "—")}
            ${lr("Выведено всего", i.withdrawn_stars != null ? formatStars(i.withdrawn_stars) + " ★" : "—")}
            ${lr("Количество выводов", i.withdrawn_count != null ? String(i.withdrawn_count) : "—")}
        </div>
        <div>
            <h4 class="pn-h">Действия</h4>
            <p class="hint">Вывод подарком — на вкладке «Выводы». Проба безопасна: она нажимает минимальную сумму, только если бот гарантированно откажет.</p>
            <div class="btn-row">
                <button class="kn-btn kn-primary" data-goto-wd>Открыть «Выводы»</button>
                <button class="kn-btn" data-probe="${a.account}" ${a.ready ? "" : "disabled"}>Проба вывода</button>
            </div>
        </div>
    </div>`;
}

async function ctlAction(fn, okText) {
    $("ctl-status").textContent = "Применяю…";
    try {
        const data = await fn();
        $("ctl-status").textContent = (okText || "Готово")
            + (data && data.saved === false ? " (БД недоступна — настройка действует до перезапуска)" : "");
    } catch (e) {
        $("ctl-status").textContent = "Не вышло: " + e.message;
    }
    loadControl(true);
}

function bindControlPanels() {
    document.querySelectorAll("#ctl-list [data-pmode]").forEach(b => b.onclick = () => {
        b.disabled = true;
        ctlAction(() => postJson(`/api/admin/proxy/mode?account=${b.dataset.acc}&mode=${b.dataset.pmode}`),
            "Режим: " + MODE_TITLES[b.dataset.pmode]);
    });
    document.querySelectorAll("#ctl-list [data-pcheck]").forEach(b => b.onclick = () => {
        b.disabled = true;
        ctlAction(() => postJson(`/api/admin/proxies/check?account=${b.dataset.pcheck}`), "Проверка выполнена");
    });
    document.querySelectorAll("#ctl-list [data-purl]").forEach(b => b.onclick = () => {
        const url = ($(`purl-${b.dataset.purl}`).value || "").trim();
        if (!url) { $("ctl-status").textContent = "Введите адрес прокси"; return; }
        b.disabled = true;
        ctlAction(() => postJson("/api/admin/proxy/url", { account: Number(b.dataset.purl), url }), "Прокси сохранён и применён");
    });
    document.querySelectorAll("#ctl-list [data-purl-clear]").forEach(b => b.onclick = () => {
        b.disabled = true;
        ctlAction(() => postJson("/api/admin/proxy/url", { account: Number(b.dataset.purlClear), url: "" }), "Адрес из админки убран");
    });
    document.querySelectorAll("#ctl-list [data-wstart]").forEach(b => b.onclick = () => {
        const days = Number($(`wdays-${b.dataset.wstart}`).value);
        const mode = $(`wmode-${b.dataset.wstart}`).value;
        const cap = mode === "ultra" ? 90 : 60;
        if (!(days >= 1 && days <= cap)) { $("ctl-status").textContent = `Дней прогрева: от 1 до ${cap}`; return; }
        b.disabled = true;
        ctlAction(async () => {
            const r = await postJson("/api/admin/warmup", { account: Number(b.dataset.wstart), days, mode });
            if (r && r.warning) alert(r.warning);
            return r;
        }, `${mode === "ultra" ? "УЛЬТРА-прогрев" : "Прогрев"} запущен на ${days} дн.`);
    });
    document.querySelectorAll("#ctl-list [data-pclear]").forEach(b => b.onclick = () => {
        if (!confirm("Сбросить КД и режим восстановления? Делайте это, только если уверены, что ограничения у аккаунта нет.")) return;
        b.disabled = true;
        ctlAction(() => postJson(`/api/admin/protection/clear?account=${b.dataset.pclear}`), "КД и штраф сброшены");
    });
    document.querySelectorAll("#ctl-list [data-wstop]").forEach(b => b.onclick = () => {
        b.disabled = true;
        ctlAction(() => postJson("/api/admin/warmup", { account: Number(b.dataset.wstop), days: 0 }), "Прогрев выключен");
    });
}

async function probeWithdraw(btn) {
    btn.disabled = true;
    $("ctl-status").textContent = "Проба вывода идёт…";
    try {
        const r = await fetch(`/api/admin/probe-withdraw?account=${btn.dataset.probe}`,
            { method: "POST", credentials: "same-origin" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        $("ctl-status").textContent = "Проба вывода: " + (await r.json()).result;
    } catch (e) {
        $("ctl-status").textContent = "Проба не удалась: " + e.message;
    }
    btn.disabled = false;
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
        setTimeout(() => loadControl(true), 2500);
    } catch (e) {
        $("ctl-status").textContent = "Не удалось запустить: " + e.message;
        btn.disabled = false;
    }
}

// ---------------- Ресурсы ----------------

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
                card("Цена аккаунта (память)", `${a.per_account_steady_mb} МБ${a.per_account_is_floor ? " *" : ""}`,
                    a.per_account_is_floor
                        ? `* заложено с запасом; замер ${a.per_account_measured_mb} МБ — ниже шума`
                        : `в покое · на пике ${a.per_account_peak_mb} МБ`),
                card("Пик памяти", `${a.rss_peak_mb} МБ`, `в покое ~${a.rss_steady_mb} МБ`),
                card("CPU средний / пик", `${a.cpu_avg_pct}% / ${a.cpu_peak_pct}%`, `на аккаунт ~${a.per_account_cpu_pct}%`),
                card("Потянет аккаунтов", `${a.recommended_total}`, `можно добавить ещё: <b>${a.can_add}</b>`),
                card("Разбивка лимита", `${a.max_by_memory_peak} / ${a.max_by_memory_steady} / ${a.max_by_cpu}`, "по памяти (пик) / по памяти (покой) / по CPU"),
            );
        }
        $("res-cards").innerHTML = cards.join("");
        $("res-notes").innerHTML = (a.notes || []).map(n => `<div>• ${escapeHtml(n)}</div>`).join("");

        const hist = d.history || [];
        const t = (ts) => new Date(ts);

        lineChart($("res-chart"), {
            labels: hist.map(h => h.ts),
            normalize: true,
            legend: $("res-legend"),
            xFmt: (ts) => t(ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }),
            tipTitle: (ts) => t(ts).toLocaleString("ru-RU", {
                day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"
            }),
            series: [
                { name: "Память",   values: hist.map(h => Number(h.rss)),      cls: "c-s0", area: true, fmt: v => v.toFixed(0) + " МБ" },
                { name: "CPU",      values: hist.map(h => Number(h.cpu)),      cls: "c-s1 c-d1",           fmt: v => v.toFixed(1) + " %" },
                { name: "Аккаунты", values: hist.map(h => Number(h.accounts)), cls: "c-s2", stepped: true, fmt: v => String(v) }
            ]
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
        setupVisualLayer();

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


// ---------------- Выводы ----------------
let wdData = null, wdFilter = "all", wdTimer = null, wdSel = {}, wdRefreshed = {}, wdMode = {}, wdFriend = {};

async function wdRefreshProfile(acc) {
    $("wd-status").textContent = "Читаю профиль в боте…";
    try {
        const r = await fetch(`/api/admin/refresh-profile?account=${acc}`, { method: "POST", credentials: "same-origin" });
        if (!r.ok) throw new Error("HTTP " + r.status);
    } catch (e) {
        $("wd-status").textContent = "Не удалось прочитать профиль: " + e.message;
        return;
    }
    $("wd-status").textContent = "";
    await loadWithdraw(true);
}

const WD_STATUS = {
    running: ["идёт", "wd-b-run"], success: ["успешно", "wd-b-ok"],
    unverified: ["не подтверждено", "wd-b-warn"], failed: ["ошибка", "wd-b-bad"]
};

function wdBadge(st) {
    const [t, c] = WD_STATUS[st] || [st, ""];
    return `<span class="wd-badge ${c}">${t}</span>`;
}

function wdSteps(steps) {
    return (steps || []).map(s =>
        `<div class="wd-step ${s.ok ? "" : "wd-step-bad"}"><span>${escapeHtml(s.t)}</span>
         ${s.ok ? "✓" : "✗"} <b>${escapeHtml(s.step)}</b> ${escapeHtml(s.detail || "")}</div>`).join("");
}

async function loadWithdraw(noAuto) {
    try {
        const r = await fetch("/api/admin/withdrawals", { credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        wdData = await r.json();
        $("wd-status").textContent = "";
        renderWithdraw();
        // после рестарта данные профиля пусты до первого чтения — читаем один раз за визит
        if (!noAuto) {
            for (const a of wdData.accounts) {
                if (a.ready && !a.known && !wdRefreshed[a.account]) {
                    wdRefreshed[a.account] = true;
                    wdRefreshProfile(a.account);
                    break;
                }
            }
        }
    } catch (e) {
        $("wd-status").textContent = "Не удалось загрузить: " + e.message;
    }
    if (!wdTimer) {
        wdTimer = setInterval(() => {
            const open = document.getElementById("section-withdraw").classList.contains("active");
            if (open && (wdData && wdData.running)) loadWithdraw();
        }, 3000);
    }
}

function renderWithdraw() {
    const d = wdData, sm = d.summary;
    $("wd-sub").textContent = "Вывод себе или подарком другу (любой Telegram ID). Друг: Вывод звезд → Подарить другу → ID → Подтвердить";
    $("wd-stats").innerHTML = [
        ["Успешных", sm.success], ["Выведено ★", formatStars(sm.stars_out)],
        ["Не подтверждено", sm.unverified], ["Ошибок", sm.failed]
    ].map(([k, v]) => `<div class="res-card"><div class="res-k">${k}</div><div class="res-v">${v}</div></div>`).join("");

    const run = d.running;
    $("wd-running").innerHTML = run ? `<div class="wd-run"><b>${pill("run", "идёт")} ${escapeHtml(run.label)}: ${run.gift_emoji}
        ${escapeHtml(run.gift_name)} ${run.stars}★ → ${run.mode === "self" ? "себе" : escapeHtml(run.friend_id)}</b>${wdSteps(run.steps)}</div>` : "";

    $("wd-accs").innerHTML = d.accounts.map(a => {
        const sel = wdSel[a.account];
        const mode = wdMode[a.account] || "friend";
        const fid = wdFriend[a.account] ?? d.friend_id;
        const gifts = d.gifts.map(g => {
            const price = mode === "self" ? g.self_stars : g.stars;
            const lowFriends = a.known && a.activated < a.min_activated;
            const why = !d.enabled ? "автовывод выключен" : !a.ready ? "аккаунт запускается"
                : !a.known ? "нет данных профиля"
                : lowFriends ? `нужно ${a.min_activated} друзей (есть ${a.activated})`
                : run ? "идёт другой вывод"
                : !price ? "недоступно в этом режиме"
                : a.balance < price ? `не хватает ${(price - a.balance).toFixed(2)}★` : "";
            const can = !why;
            return `<div class="wd-gift ${can ? "can" : "cant"} ${sel === g.code ? "sel" : ""}" data-acc="${a.account}"
                data-code="${g.code}" data-can="${can ? 1 : 0}">
                <div class="wd-emoji">${g.emoji}</div><div class="wd-gname">${escapeHtml(g.name)}</div>
                <div class="wd-price">${price || "—"}${price ? "★" : ""}</div><div class="wd-why">${can ? "можно вывести" : escapeHtml(why)}</div></div>`;
        }).join("");
        const g = d.gifts.find(x => x.code === sel);
        return `<div class="ctl-acc"><h3>${escapeHtml(a.label)}</h3>
            <div class="ctl-line">${a.known
                ? `Баланс: <b>${a.balance}★</b> · друзей: ${a.friends} · активировали: ${a.activated}`
                : `<b>Данных профиля пока нет</b> (после рестарта читаются из бота)`}
            <button class="kn-btn proxy-btn" data-refresh="${a.account}" ${a.ready ? "" : "disabled"}>Обновить из бота</button></div>
            <div class="wd-mode">
                <button class="kn-chip ${mode === "friend" ? "active" : ""}" data-mode="${a.account}:friend">Другу</button>
                <button class="kn-chip ${mode === "self" ? "active" : ""}" data-mode="${a.account}:self">★ Себе</button>
                ${mode === "friend" ? `<input class="kn-search wd-fid" data-fid="${a.account}" value="${escapeHtml(fid)}"
                    placeholder="Telegram ID друга" inputmode="numeric" autocomplete="off">` : ""}
            </div>
            <div class="wd-grid">${gifts}</div>
            <button class="kn-btn kn-primary" data-go="${a.account}" ${(g && !run) ? "" : "disabled"}>
                ${g ? `Вывести ${g.emoji} ${mode === "self" ? g.self_stars : g.stars}★ ${mode === "self" ? "себе" : "другу " + escapeHtml(fid)}` : "Выбери подарок"}</button></div>`;
    }).join("");
    document.querySelectorAll(".wd-gift[data-can='1']").forEach(el => el.onclick = () => {
        wdSel[el.dataset.acc] = el.dataset.code; renderWithdraw();
    });
    document.querySelectorAll("button[data-go]").forEach(b => b.onclick = () => startWithdraw(b.dataset.go));
    document.querySelectorAll("button[data-mode]").forEach(b => b.onclick = () => {
        const [acc, m] = b.dataset.mode.split(":");
        wdMode[acc] = m; wdSel[acc] = null; renderWithdraw();
    });
    document.querySelectorAll("input[data-fid]").forEach(i => i.oninput = () => { wdFriend[i.dataset.fid] = i.value.trim(); });
    document.querySelectorAll("button[data-refresh]").forEach(b => b.onclick = () => wdRefreshProfile(b.dataset.refresh));

    let rows = d.history;
    if (wdFilter === "success") rows = rows.filter(h => h.status === "success");
    if (wdFilter === "problems") rows = rows.filter(h => h.status === "failed" || h.status === "unverified");
    $("wd-history").innerHTML = rows.length ? `<table class="wd-table"><thead><tr><th>Когда</th><th>Акк.</th>
        <th>Подарок</th><th>Баланс</th><th>Статус</th></tr></thead><tbody>` + rows.map(h => `<tr>
        <td>${formatDate(h.created_at)}</td><td>#${h.account_number ?? ""}</td>
        <td>${escapeHtml(h.gift_emoji || "")} ${escapeHtml(h.gift_name || "")} ${h.stars}★ → ${h.mode === "self" ? "себе" : escapeHtml(h.friend_id || "")}</td>
        <td>${h.balance_before ?? "?"} → ${h.balance_after ?? "?"}</td>
        <td>${wdBadge(h.status)}${h.error ? `<div class="wd-err">${escapeHtml(h.error)}</div>` : ""}
        <details><summary>шаги</summary>${wdSteps(h.steps)}${h.bot_response ? `<div class="wd-step">Ответ бота: ${escapeHtml(h.bot_response)}</div>` : ""}</details></td></tr>`).join("")
        + "</tbody></table>" : `<div class="kn-status">Выводов пока нет</div>`;
}

document.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-wf]");
    if (!b) return;
    wdFilter = b.dataset.wf;
    document.querySelectorAll("button[data-wf]").forEach(x => x.classList.toggle("active", x === b));
    if (wdData) renderWithdraw();
});

async function startWithdraw(acc) {
    const code = wdSel[acc];
    const g = wdData.gifts.find(x => x.code === code);
    if (!g) return;
    const mode = wdMode[acc] || "friend";
    const fid = (wdFriend[acc] ?? wdData.friend_id) || "";
    const price = mode === "self" ? g.self_stars : g.stars;
    if (mode === "friend" && !/^\d{5,13}$/.test(fid)) { $("wd-status").textContent = "Укажи Telegram ID друга (только цифры)"; return; }
    if (!confirm(`Вывести ${g.emoji} ${g.name} за ${price}★ ${mode === "self" ? "СЕБЕ" : "другу " + fid}?\nЗвёзды будут списаны по-настоящему.`)) return;
    try {
        const r = await fetch(`/api/admin/withdraw?account=${acc}&gift=${encodeURIComponent(code)}&mode=${mode}&friend_id=${encodeURIComponent(mode === "friend" ? fid : "")}`,
            { method: "POST", credentials: "same-origin" });
        if (!r.ok) {
            let m = "HTTP " + r.status;
            try { m = (await r.json()).detail || m; } catch (_) {}
            throw new Error(m);
        }
        wdSel[acc] = null;
        $("wd-status").textContent = "Вывод запущен";
    } catch (e) {
        $("wd-status").textContent = "Не удалось запустить: " + e.message;
    }
    loadWithdraw();
}


// ---------------- Генерация сессии ----------------
let sgMode = "phone", sgSid = null, sgTimer = null;

function sgReset() {
    clearInterval(sgTimer); sgTimer = null; sgSid = null;
    $("sg-step").innerHTML = ""; $("sg-result").style.display = "none"; $("sg-session").value = "";
    $("sg-start").disabled = false;
}

async function sgApi(path, body, method = "POST") {
    const r = await fetch("/api/admin/session/" + path, {
        method, credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: method === "POST" && body ? JSON.stringify(body) : undefined
    });
    let j = {}; try { j = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    return j;
}

function sgRender(st) {
    const step = $("sg-step");
    if (st.state === "need_code") {
        step.innerHTML = `<div>Код отправлен в Telegram (или SMS). Введи его:</div>
            <input id="sg-in" class="kn-search" placeholder="Код" autocomplete="off" inputmode="numeric">
            <button id="sg-ok" class="kn-btn kn-primary">Подтвердить</button>`;
        $("sg-ok").onclick = () => sgSubmit("code", { code: $("sg-in").value });
    } else if (st.state === "need_password") {
        step.innerHTML = `<div>На аккаунте включён облачный пароль (2FA):</div>
            <input id="sg-in" type="password" class="kn-search" placeholder="Пароль" autocomplete="off">
            <button id="sg-ok" class="kn-btn kn-primary">Войти</button>`;
        $("sg-ok").onclick = () => sgSubmit("password", { password: $("sg-in").value });
    } else if (st.state === "qr_wait") {
        if (!$("sg-qr")) {
            step.innerHTML = `<div>Telegram → Настройки → Устройства → Подключить устройство → наведи на код.<br>
                Код обновляется сам каждые ~25 секунд.</div><div id="sg-qr" class="sg-qr"></div>`;
        }
        if ($("sg-qr").dataset.url !== st.qr_url) {
            $("sg-qr").dataset.url = st.qr_url; $("sg-qr").innerHTML = "";
            if (window.QRCode) new QRCode($("sg-qr"), { text: st.qr_url, width: 220, height: 220 });
            else $("sg-qr").textContent = st.qr_url;
        }
    } else if (st.state === "done") {
        clearInterval(sgTimer); sgTimer = null; step.innerHTML = "";
        $("sg-session").value = st.session; $("sg-result").style.display = "block";
        $("sg-status").textContent = "Готово. Скопируй строку — на сервере она уже не хранится после закрытия вкладки/отмены.";
        $("sg-api-hash").value = "";
        fetch("/api/admin/session/cancel?sid=" + encodeURIComponent(sgSid), { method: "POST", credentials: "same-origin" });
    } else if (st.state === "error" || st.state === "gone") {
        clearInterval(sgTimer); sgTimer = null; step.innerHTML = "";
        $("sg-status").textContent = "Ошибка: " + (st.error || "попытка истекла"); $("sg-start").disabled = false;
    }
}

async function sgSubmit(path, body) {
    $("sg-status").textContent = "…";
    try {
        const st = await sgApi(path, { sid: sgSid, ...body });
        $("sg-status").textContent = "";
        sgRender(st);
    } catch (e) { $("sg-status").textContent = "Ошибка: " + e.message; }
}

async function sgStart() {
    sgReset();
    $("sg-status").textContent = "Запрашиваю у Telegram…";
    $("sg-start").disabled = true;
    try {
        const st = await sgApi("start", {
            mode: sgMode, api_id: $("sg-api-id").value, api_hash: $("sg-api-hash").value, phone: $("sg-phone").value
        });
        sgSid = st.sid; $("sg-status").textContent = "";
        sgRender(st);
        if (sgMode === "qr") {
            sgTimer = setInterval(async () => {
                try { sgRender(await sgApi("status?sid=" + encodeURIComponent(sgSid), null, "GET")); } catch (_) {}
            }, 2000);
        }
    } catch (e) {
        $("sg-status").textContent = "Ошибка: " + e.message; $("sg-start").disabled = false;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    $("sg-start").onclick = sgStart;
    $("sg-copy").onclick = async () => {
        try { await navigator.clipboard.writeText($("sg-session").value); $("sg-status").textContent = "Скопировано"; }
        catch (_) { $("sg-session").select(); }
    };
    document.querySelectorAll("button[data-sgm]").forEach(b => b.onclick = () => {
        sgMode = b.dataset.sgm;
        document.querySelectorAll("button[data-sgm]").forEach(x => x.classList.toggle("active", x === b));
        $("sg-phone").style.display = sgMode === "phone" ? "" : "none";
        $("sg-start").textContent = sgMode === "phone" ? "Отправить код" : "Показать QR";
        if (sgSid) fetch("/api/admin/session/cancel?sid=" + encodeURIComponent(sgSid), { method: "POST", credentials: "same-origin" });
        sgReset(); $("sg-status").textContent = "";
    });
});
