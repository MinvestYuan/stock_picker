// 报告前端逻辑：从 #report-data 的 JSON payload 渲染所有动态内容并初始化图表。
const DATA = JSON.parse(document.getElementById('report-data').textContent);

const annualReturns = DATA.annual_returns;
const tvEquityData = DATA.tv_equity_series;
const chartColors = DATA.colors_map;
const tvDrawdownSeries = DATA.tv_drawdown_series;
const tvMonthlyData = DATA.tv_monthly_data;
const tvReturnDistData = DATA.tv_return_dist_data;
const latestKMetadata = DATA.latest_k_metadata;
const latestKFallback = DATA.latest_k_fallback;
const mtdReturnsReport = DATA.mtd_returns;
const defaultYear = DATA.default_year;

// ===== 数值格式化（与 Python 端口径一致）=====
function fmtPct(v) {
    return Math.abs(v) < 10 ? (v * 100).toFixed(1) + '%' : (v * 100).toFixed(0) + '%';
}

// ===== 渲染：顶部日期、信号区 =====
function renderHeader() {
    document.getElementById('date-range').textContent = DATA.date_range;
    document.getElementById('signal-asof').textContent = DATA.asof_date_str;
    document.getElementById('signal-next-date').textContent = DATA.next_date_str;

    const container = document.getElementById('signals-container');
    if (DATA.next_picks && DATA.next_picks.length > 0) {
        const cards = DATA.next_picks.map(p =>
            `<div class="metric-card" style="text-align:center;">
                <div class="metric-value" style="font-size:1.25rem;">${p.ticker}</div>
                <div class="metric-label" style="margin-top:0.5rem;">总分 ${p.total_score.toFixed(3)}</div>
                <div style="font-size:0.6875rem;color:var(--text-muted);margin-top:0.25rem;">动量 ${p.momentum_score.toFixed(3)} · RRG ${p.rrg_score.toFixed(3)}</div>
                <div style="font-size:0.6875rem;color:var(--text-subtle);margin-top:0.25rem;">Close/EMA50 ${p.close_over_ema50.toFixed(3)}</div>
            </div>`
        ).join('');
        container.innerHTML = `<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">${cards}</div>`;
    } else {
        container.innerHTML =
            `<div style="text-align:center;padding:1.5rem;color:var(--text-muted);font-size:0.875rem;background:var(--surface);border-radius:12px;border:1px dashed var(--border-strong);">持现金（QQQ 50MA &lt; 200MA）或当前无可选股</div>`;
    }
}

// ===== 渲染：KPI 卡片 =====
function renderKPIs() {
    const container = document.getElementById('kpi-container');
    container.innerHTML = DATA.performance_kpis.map(k =>
        `<div class="metric-card">
            <div class="metric-label">${k.label}</div>
            <div class="metric-value ${k.text_color}">${k.value}</div>
        </div>`
    ).join('');
}

// ===== 渲染：策略 vs 基准对比表 =====
function renderComparison() {
    const tbody = document.getElementById('comparison-tbody');
    tbody.innerHTML = DATA.comparison_rows.map(r =>
        `<tr class="${r.row_cls}">
            <td class="font-medium"><span class="legend-dot" style="background:${r.dot_color}"></span>${r.name}</td>
            <td class="${r.ret_cls}">${r.total_return}</td>
            <td class="${r.cagr_cls}">${r.cagr}</td>
            <td class="val-neg">${r.max_drawdown}</td>
            <td class="val-neutral">${r.sharpe}</td>
            <td class="val-neutral">${r.win_rate}</td>
        </tr>`
    ).join('');
}

// ===== 渲染：MTD 徽章初值 =====
function renderMTDBadge() {
    const mtdVal = DATA.portfolio_mtd * 100;
    const cls = mtdVal >= 0 ? 'val-pos' : 'val-neg';
    document.getElementById('mtd-live').innerHTML =
        `<span class="${cls}">${mtdVal.toFixed(1)}%</span><span class="mtd-live-tag">实时</span>`;
}

// ===== 渲染：年份下拉 + 月度卡片 =====
function renderMonthlyCards() {
    const sel = document.getElementById('year-select');
    sel.innerHTML = DATA.years.map(y =>
        `<option value="${y}"${y === DATA.default_year ? ' selected' : ''}>${y}</option>`
    ).join('');

    const container = document.getElementById('monthly-cards');
    container.innerHTML = DATA.monthly_data.map(m => {
        const retClass = m.monthly_return > 0 ? 'ret-badge-pos' : 'ret-badge-neg';
        const retStr = (m.monthly_return * 100 >= 0 ? '+' : '') + (m.monthly_return * 100).toFixed(2) + '%';
        const details = DATA.details_by_month[m.month] || [];
        let tableHtml;
        if (details.length > 0) {
            const rows = details.map(d => {
                const tRetClass = d.monthly_return > 0 ? 'val-pos' : 'val-neg';
                const tRetStr = (d.monthly_return * 100 >= 0 ? '+' : '') + (d.monthly_return * 100).toFixed(2) + '%';
                return `<tr>
                    <td class="font-mono font-semibold">${d.ticker}</td>
                    <td>${d.buy_price.toFixed(2)}</td>
                    <td>${d.sell_price.toFixed(2)}</td>
                    <td class="${tRetClass}">${tRetStr}</td>
                </tr>`;
            }).join('');
            tableHtml = `<table>
                <thead><tr><th>标的</th><th>买入价</th><th>卖出价</th><th>回报</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
        } else {
            tableHtml = '<div class="text-xs text-zinc-400 mt-1">现金持仓</div>';
        }
        return `<div class="month-card" data-year="${m.year}" data-month="${m.month}">
            <div class="flex justify-between items-start mb-1">
                <div>
                    <div class="font-semibold text-[13px] tracking-tight">${m.month}</div>
                    <div class="text-[10px] text-zinc-400 mt-0.5">${m.buy_date} → ${m.sell_date}</div>
                </div>
                <div class="ret-badge ${retClass}">${retStr}</div>
            </div>
            ${tableHtml}
        </div>`;
    }).join('');
}

const TV_BASE = {
    layout: { background: { color: 'transparent' }, textColor: '#71717a', fontSize: 11 },
    grid: { vertLines: { color: 'rgba(24,24,27,0.04)' }, horzLines: { color: 'rgba(24,24,27,0.04)' } },
    timeScale: { borderColor: 'rgba(24,24,27,0.08)', timeVisible: false, secondsVisible: false },
    crosshair: { mode: 0, vertLine: { color: 'rgba(24,24,27,0.15)', width: 1, style: 3 }, horzLine: { color: 'rgba(24,24,27,0.15)', width: 1, style: 3 } },
};

function createTVChart(container, height, extra = {}) {
    if (!container || typeof LightweightCharts === 'undefined') return null;
    return LightweightCharts.createChart(container, {
        width: container.clientWidth || 800,
        height: container.clientHeight || height,
        ...TV_BASE,
        rightPriceScale: { borderColor: '#e5e7eb', ...(extra.rightPriceScale || {}) },
        ...extra,
    });
}

function bindChartResize(chart, container, height) {
    const resize = () => chart.resize(container.clientWidth || 800, container.clientHeight || height);
    window.addEventListener('resize', resize);
    setTimeout(() => { resize(); try { chart.timeScale().fitContent(); } catch(e) {} }, 80);
}

function showChartError(container, msg) {
    container.innerHTML = `<div style="padding:12px;color:#b91c1c;font-size:12px;background:#fef2f2;border-radius:6px;">${msg}</div>`;
}

function initMultiLineChart(containerId, seriesMap, options = {}) {
    const container = document.getElementById(containerId);
    if (!container || !seriesMap || Object.keys(seriesMap).length === 0) return;
    const height = options.height || 240;
    try {
        const chart = createTVChart(container, height, options.chartOpts || {});
        if (!chart || typeof chart.addLineSeries !== 'function') {
            showChartError(container, options.errorMsg || '图表初始化失败');
            return;
        }
        Object.keys(seriesMap).forEach(name => {
            const data = seriesMap[name];
            if (!data || data.length === 0) return;
            const lw = options.lineWidth ? options.lineWidth(name) : (name === '策略' ? 2 : 1.5);
            chart.addLineSeries({ color: chartColors[name] || '#6b7280', lineWidth: lw, title: name }).setData(data);
        });
        bindChartResize(chart, container, height);
    } catch (err) {
        console.error(containerId, err);
        showChartError(container, options.errorMsg || '图表创建出错');
    }
}

function initHistogramChart(containerId, data, options = {}) {
    const container = document.getElementById(containerId);
    if (!container || !data || data.length === 0) return;
    const height = options.height || 240;
    try {
        const chart = createTVChart(container, height, options.chartOpts || {});
        const series = chart.addHistogramSeries({ title: options.title || '', color: options.color });
        series.setData(data);
        bindChartResize(chart, container, height);
    } catch (err) {
        console.error(containerId, err);
        showChartError(container, options.errorMsg || '图表创建出错');
    }
}

function waitForCharts(callback, container, failMsg) {
    if (typeof LightweightCharts !== 'undefined' && typeof LightweightCharts.createChart === 'function') {
        callback();
        return;
    }
    let attempts = 0;
    const iv = setInterval(() => {
        attempts++;
        if (typeof LightweightCharts !== 'undefined' && typeof LightweightCharts.createChart === 'function') {
            clearInterval(iv);
            callback();
        } else if (attempts > 12) {
            clearInterval(iv);
            if (container) container.innerHTML = `<div style="padding:12px;color:#666;font-size:12px;">${failMsg}</div>`;
        }
    }, 100);
}

function filterByYear() {
    const select = document.getElementById('year-select');
    const year = select.value;
    const allCards = Array.from(document.querySelectorAll('.month-card'));
    const container = document.getElementById('monthly-cards');

    allCards.forEach(card => card.style.display = 'none');

    let yearCards = [];
    if (year) {
        yearCards = allCards.filter(card => card.dataset.year === year);
        yearCards.sort((a, b) => b.dataset.month.localeCompare(a.dataset.month));
    } else {
        yearCards = allCards;
    }

    yearCards.forEach(card => {
        container.appendChild(card);
        card.style.display = 'block';
    });

    const retDisplay = document.getElementById('year-return-display');
    if (retDisplay) {
        if (year && annualReturns[year] !== undefined) {
            const ret = annualReturns[year];
            const retStr = (ret * 100).toFixed(2) + '%';
            const retClass = ret >= 0 ? 'val-pos' : 'val-neg';
            retDisplay.innerHTML = `<span class="${retClass}">年收益 ${retStr}</span>`;
        } else {
            retDisplay.innerHTML = '';
        }
    }
}

function initTradingViewEquity() {
    const container = document.getElementById('tv-equity-chart');
    waitForCharts(() => initMultiLineChart('tv-equity-chart', tvEquityData, {
        height: 400,
        lineWidth: (name) => name === '策略' ? 3 : 2,
        chartOpts: { rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 } }, legend: { visible: true, position: 'top' } },
        errorMsg: '累计权益曲线创建出错，请刷新重试。',
    }), container, 'TradingView 图表库加载失败，请检查网络后刷新。');
}

function initTradingViewDrawdown() {
    initMultiLineChart('tv-drawdown-chart', tvDrawdownSeries, { errorMsg: '回撤图表创建出错，请刷新。' });
}

function initTradingViewMonthly() {
    const coloredData = tvMonthlyData.map(d => ({ time: d.time, value: d.value, color: d.value >= 0 ? '#059669' : '#e11d48' }));
    initHistogramChart('tv-monthly-chart', coloredData, { title: '策略月度回报', errorMsg: '月度回报图表创建出错，请刷新。' });
}

function initTradingViewReturnDist() {
    const coloredData = tvReturnDistData.map(d => ({
        time: d.time,
        value: d.value,
        color: ((d.time - 100000) / 100) >= 0 ? '#059669' : '#e11d48',
    }));
    initHistogramChart('tv-return-dist-chart', coloredData, {
        title: '回报分布',
        chartOpts: { timeScale: { ...TV_BASE.timeScale, tickMarkFormatter: (time) => ((time - 100000) / 100).toFixed(1) + '%' } },
        errorMsg: '回报分布图表出错。',
    });
}

async function fetchYahooCloses(ticker, startDateStr) {
    const start = Math.floor(new Date(startDateStr).getTime() / 1000);
    const end = Math.floor(Date.now() / 1000);
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${ticker}?interval=1d&period1=${start}&period2=${end}`;
    const proxy = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(url);
    const resp = await fetch(proxy, { cache: 'no-cache' });
    if (!resp.ok) throw new Error('proxy fail');
    const json = await resp.json();
    const result = json.chart.result[0];
    const timestamps = result.timestamp;
    const closes = result.indicators.quote[0].close;
    const series = [];
    for (let i = 0; i < timestamps.length; i++) {
        if (closes[i] == null) continue;
        const dateStr = new Date(timestamps[i] * 1000).toISOString().slice(0, 10);
        series.push({ time: dateStr, value: closes[i] });
    }
    return series;
}

function updateMTDLive(ticker, mtd) {
    if (!window.liveMTDs) window.liveMTDs = { ...mtdReturnsReport };
    window.liveMTDs[ticker] = mtd;
    if (window.refreshMTD) window.refreshMTD();
}

async function initLatestKChart() {
    const container = document.getElementById('tv-latest-kchart');
    if (!container || !latestKMetadata?.tickers?.length) return;
    try {
        const chart = createTVChart(container, 260, {
            timeScale: { ...TV_BASE.timeScale, rightOffset: 3, barSpacing: 18, minBarWidth: 4 },
        });
        if (!chart) return;
        const colors = ['#059669', '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899'];
        const seriesMap = {};
        latestKMetadata.tickers.forEach((t, i) => {
            const s = chart.addLineSeries({ color: colors[i % colors.length], lineWidth: 2, title: t });
            seriesMap[t] = s;
            if (latestKFallback?.[t]) s.setData(latestKFallback[t]);
        });
        try { chart.timeScale().fitContent(); } catch(e) {}
        if (window.liveMTDs === undefined) window.liveMTDs = { ...mtdReturnsReport };
        window.refreshMTD = function() {
            const vals = Object.values(window.liveMTDs);
            const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
            const el = document.getElementById('mtd-live');
            if (el) {
                const pctStr = (avg * 100).toFixed(1);
                const numClass = avg >= 0 ? 'val-pos' : 'val-neg';
                el.innerHTML = `<span class="${numClass}">${pctStr}%</span><span class="mtd-live-tag">实时</span>`;
            }
        };
        window.refreshMTD();
        await Promise.all(latestKMetadata.tickers.map(async (t) => {
            try {
                const firstOpen = (latestKMetadata.firstOpens || latestKMetadata.firstCloses || {})[t];
                const closes = await fetchYahooCloses(t, latestKMetadata.startDate);
                if (closes.length > 0 && firstOpen) {
                    const seriesData = closes.map(d => ({ time: d.time, value: d.value / firstOpen - 1 }));
                    seriesMap[t].setData(seriesData);
                    updateMTDLive(t, seriesData[seriesData.length - 1].value);
                }
            } catch (e) {
                console.warn('Live fetch failed for ' + t, e);
            }
        }));
        bindChartResize(chart, container, 260);
    } catch (err) {
        console.error('TV latest K error:', err);
        container.innerHTML = '<div style="padding:4px;color:#666;font-size:10px;">走势图表出错，使用报告数据。</div>';
    }
}

function initializeAll() {
    renderHeader();
    renderKPIs();
    renderComparison();
    renderMTDBadge();
    renderMonthlyCards();

    const sel = document.getElementById('year-select');
    if (sel) {
        const opts = Array.from(sel.options).map(o => o.value);
        sel.value = opts.includes(defaultYear) ? defaultYear : (opts[0] || '');
        filterByYear();
    }
    setTimeout(initTradingViewEquity, 120);
    setTimeout(initTradingViewDrawdown, 150);
    setTimeout(initTradingViewMonthly, 150);
    setTimeout(initTradingViewReturnDist, 180);
    setTimeout(initLatestKChart, 150);
}

initializeAll();
