const state = { deferredPrompt: null };
const $ = selector => document.querySelector(selector);
const formatNumber = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? "--" : Number(value).toFixed(digits);
const formatPercent = value => value == null || !Number.isFinite(Number(value)) ? "--" : `${Number(value).toFixed(2)}%`;
const formatReturn = value => value == null || !Number.isFinite(Number(value)) ? "--" : `${(Number(value) * 100).toFixed(2)}%`;
const tone = value => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));

async function api(path) {
  const response = await fetch(path, {cache: "no-store", headers: {"Accept": "application/json"}});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderTop5(payload) {
  $("#top5-updated").textContent = `数据更新时间：${payload.updated_at || "--"}`;
  $("#top5-empty").hidden = payload.items.length > 0;
  $("#top5-cards").innerHTML = payload.items.map(item => `
    <article class="stock-card">
      <div class="stock-card-head">
        <span class="rank">${escapeHtml(item.rank)}</span>
        <div class="stock-title"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.symbol)} · ${escapeHtml(item.selection_type)}</small></div>
        <span class="score">${formatNumber(item.score, 1)}</span>
      </div>
      <div class="stock-core">
        <div><span>当前价</span><strong>${formatNumber(item.price)}</strong></div>
        <div><span>涨幅</span><strong class="${tone(item.change_percent)}">${formatPercent(item.change_percent)}</strong></div>
        <div><span>综合得分</span><strong>${formatNumber(item.score, 1)}</strong></div>
      </div>
      <details><summary>展开详情</summary>
        <div class="detail-grid">
          <div><span>所属板块</span><strong>${escapeHtml(item.sector || "--")}</strong></div>
          <div><span>量比</span><strong>${formatNumber(item.details.volume_ratio)}</strong></div>
          <div><span>换手率</span><strong>${formatPercent(item.details.turnover_rate)}</strong></div>
          <div><span>VWAP</span><strong>${escapeHtml(item.details.vwap_status || "--")}</strong></div>
        </div>
        <p>${escapeHtml(item.reason || "暂无详细理由")}</p>
      </details>
      <p class="risk">风险：${escapeHtml(item.risk || "模型结果不代表未来收益")}</p>
    </article>`).join("");
  $("#top5-table").innerHTML = payload.items.map(item => `<tr>
    <td>${escapeHtml(item.rank)}</td><td>${escapeHtml(item.symbol)}</td><td>${escapeHtml(item.name)}</td>
    <td>${formatNumber(item.price)}</td><td class="${tone(item.change_percent)}">${formatPercent(item.change_percent)}</td>
    <td>${formatNumber(item.score, 1)}</td><td>${escapeHtml(item.selection_type)}</td><td>${escapeHtml(item.risk || "--")}</td>
  </tr>`).join("");
}

function renderReviews(payload) {
  $("#daily-content").innerHTML = payload.items.length ? payload.items.map(item => `
    <article class="review-item"><div><span>排名</span><strong>${escapeHtml(item.rank)}</strong></div>
    <div><span>股票</span><strong>${escapeHtml(item.name)} ${escapeHtml(item.symbol)}</strong></div>
    <div><span>收盘收益</span><strong class="${tone(item.close_return)}">${formatReturn(item.close_return)}</strong></div>
    <div><span>模拟收益</span><strong class="${tone(item.simulated_return)}">${formatReturn(item.simulated_return)}</strong></div>
    <div><span>状态</span><strong>${escapeHtml(item.review_status)}</strong></div>
    <div><span>结论</span><strong>${escapeHtml(item.conclusion || "等待真实数据")}</strong></div></article>`).join("") : '<div class="empty-state">暂无每日复盘记录。</div>';
}

function renderHistory(payload) {
  const completed = payload.items.filter(item => item.review_status === "完成" && Number.isFinite(Number(item.close_return)));
  const wins = completed.filter(item => Number(item.close_return) > 0).length;
  const average = completed.length ? completed.reduce((sum, item) => sum + Number(item.close_return), 0) / completed.length : null;
  $("#history-metrics").innerHTML = [
    ["历史样本", payload.items.length], ["完成复盘", completed.length],
    ["收盘上涨率", completed.length ? `${(wins / completed.length * 100).toFixed(1)}%` : "--"],
    ["平均收益", formatReturn(average)]
  ].map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
  $("#history-list").innerHTML = payload.items.slice(0, 20).map(item => `
    <article class="review-item"><div><span>日期</span><strong>${escapeHtml(item.recommendation_date)}</strong></div>
    <div><span>股票</span><strong>${escapeHtml(item.name)} ${escapeHtml(item.symbol)}</strong></div>
    <div><span>入选类型</span><strong>${escapeHtml(item.selection_type)}</strong></div>
    <div><span>收盘收益</span><strong class="${tone(item.close_return)}">${formatReturn(item.close_return)}</strong></div></article>`).join("");
}

async function loadAll() {
  $("#refresh-button").disabled = true;
  try {
    const [top5, daily, history, status] = await Promise.all([
      api("/api/top5"), api("/api/reviews/daily"), api("/api/reviews/history?limit=100"), api("/api/status")
    ]);
    renderTop5(top5); renderReviews(daily); renderHistory(history);
    $("#system-status").innerHTML = `<p>服务器：已连接</p><p>服务器时间：${escapeHtml(status.server_time)}</p><p>待复盘：${escapeHtml(status.pending_reviews)}</p><p>最近扫描：${escapeHtml(status.latest_scan?.status || "暂无")}</p>`;
    $("#online-status").textContent = "已连接";
    $("#online-status").style.color = "var(--green)";
  } catch (error) {
    $("#offline-banner").hidden = false;
    $("#online-status").textContent = "离线";
    $("#system-status").textContent = "无法连接线上服务，请检查网络。";
  } finally {
    $("#refresh-button").disabled = false;
    setTimeout(() => $("#splash").classList.add("hidden"), 250);
  }
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === button.dataset.view));
  scrollTo({top: 0, behavior: "smooth"});
}));

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault(); state.deferredPrompt = event; $("#install-button").hidden = false;
});
$("#install-button").addEventListener("click", async () => {
  if (!state.deferredPrompt) return;
  state.deferredPrompt.prompt();
  await state.deferredPrompt.userChoice;
  state.deferredPrompt = null; $("#install-button").hidden = true;
});
window.addEventListener("appinstalled", () => { $("#install-button").hidden = true; });

const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
$("#install-help").textContent = standalone
  ? "应用已以独立窗口运行。"
  : isIos
    ? "请点击Safari分享按钮，再选择“添加到主屏幕”。"
    : "Chrome或Edge支持时，页面会显示“安装应用”按钮；其他浏览器可继续作为普通网站使用。";

window.addEventListener("offline", () => { $("#offline-banner").hidden = false; $("#online-status").textContent = "离线"; });
window.addEventListener("online", () => { $("#offline-banner").hidden = true; loadAll(); });
$("#refresh-button").addEventListener("click", loadAll);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/service-worker.js", {scope: "/"}).then(registration => {
    registration.addEventListener("updatefound", () => {
      const worker = registration.installing;
      worker.addEventListener("statechange", () => {
        if (worker.state === "installed" && navigator.serviceWorker.controller) $("#update-banner").hidden = false;
      });
    });
  });
  navigator.serviceWorker.addEventListener("controllerchange", () => location.reload());
}
$("#update-button").addEventListener("click", async () => {
  const registration = await navigator.serviceWorker.getRegistration();
  registration?.waiting?.postMessage("SKIP_WAITING");
});

loadAll();
