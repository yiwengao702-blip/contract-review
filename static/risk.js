// 文件位置: contract-review/static/risk.js
// 功能说明: 电商业务风控中心前端逻辑。
//          涵盖态势大盘、审核工作台、规则配置、名单库、仿真测试、判定历史六大模块，
//          统一对接 /api/risk/* 接口。所有用户可控文本均经 escapeHtml 处理，避免 XSS。
const API = "";
const SESSION_KEY = "cr_session";

// [安全加固 Phase0] 自动为所有 API 请求注入会话令牌，配合后端 auth_guard 中间件。
// risk.js 通过 api() 助手统一发请求，此处用全局 fetch 包装一次性补齐 Authorization 头。
// 防御性：仅当运行环境本身提供 fetch 时才包裹（无头 DOM 桩、极旧浏览器下安全跳过）。
(function injectAuthHeader() {
  if (typeof window === "undefined" || typeof window.fetch !== "function") return;
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    try {
      const s = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
      if (s && s.token) {
        init.headers = init.headers || {};
        if (typeof init.headers.set === "function") {
          init.headers.set("Authorization", "Bearer " + s.token);
        } else {
          init.headers["Authorization"] = "Bearer " + s.token;
        }
      }
    } catch (e) {}
    return _origFetch(input, init);
  };
})();

// ---------- 基础工具 ----------
const $ = (id) => document.getElementById(id);
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(API + path, opt);
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    throw new Error(j.detail || `请求失败(${r.status})`);
  }
  return r.json();
}
const ACTION_TXT = { pass: "放行", review: "人工审核", block: "拦截" };
const LEVEL_TXT = { high: "高风险", medium: "中风险", low: "低风险" };
function badge(text, cls) { return `<span class="badge ${cls}">${escapeHtml(text)}</span>`; }
function actionBadge(a) { return badge(ACTION_TXT[a] || a, "b-" + a); }
function levelBadge(l) { return badge(LEVEL_TXT[l] || l, "b-" + l); }

// ---------- 鉴权守卫 ----------
(function guard() {
  let sess = null;
  try { sess = JSON.parse(localStorage.getItem(SESSION_KEY) || "null"); } catch (e) {}
  if (!sess || !sess.token) { location.replace("/login?next=/risk"); return; }
  $("userName").textContent = `${escapeHtml(sess.display_name)}（${escapeHtml(sess.role_name)}）`;
  window.__sess = sess;
})();

$("logoutBtn").onclick = () => {
  if (!confirm("确定要退出登录吗？")) return;
  localStorage.removeItem(SESSION_KEY);
  location.replace("/login");
};

// ---------- 标签页切换 ----------
document.querySelectorAll(".rk-side a").forEach(a => {
  a.onclick = () => {
    document.querySelectorAll(".rk-side a").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".rk-tab").forEach(x => x.classList.remove("active"));
    a.classList.add("active");
    const tab = a.dataset.tab;
    $("tab-" + tab).classList.add("active");
    // 进入各页时刷新对应数据
    if (tab === "dashboard") renderDashboard();
    if (tab === "workbench") renderCases();
    if (tab === "rules") renderRules();
    if (tab === "lists") renderLists();
    if (tab === "history") renderHistory();
  };
});

// ======================================================================
// 态势大盘
// ======================================================================
async function renderDashboard() {
  const [dash, cases] = await Promise.all([api("GET", "/api/risk/dashboard"), api("GET", "/api/risk/cases")]);
  const pending = cases.filter(c => c.status === "pending").length;
  $("dashCards").innerHTML = [
    ["总处理事件", dash.total], ["拦截", dash.block], ["人工审核", dash.review],
    ["放行", dash.pass], ["拦截率", dash.intercept_rate + "%"], ["待复核案件", pending],
  ].map(([l, v]) => `<div class="rk-stat"><div class="v">${escapeHtml(v)}</div><div class="l">${escapeHtml(l)}</div></div>`).join("");

  // 风险分布（横向条形）
  const d = dash.risk_distribution;
  const max = Math.max(1, d.high, d.medium, d.low);
  $("distChart").innerHTML = ["high", "medium", "low"].map(k => {
    const w = (d[k] / max) * 100;
    return `<div style="margin:8px 0;"><div class="rk-row" style="justify-content:space-between;">
      <span>${levelBadge(k)}</span><span class="rk-muted">${d[k]}</span></div>
      <div style="background:#eef2fb;height:12px;border-radius:6px;">
      <div style="width:${w}%;height:100%;border-radius:6px;background:${k === "high" ? "#e23b3b" : k === "medium" ? "#f59e0b" : "#16a34a"};"></div></div></div>`;
  }).join("");

  // 规则命中排行
  if (!dash.top_rules.length) {
    $("ruleChart").innerHTML = `<p class="rk-muted">暂无命中记录</p>`;
  } else {
    const mx = Math.max(...dash.top_rules.map(r => r.hits));
    $("ruleChart").innerHTML = dash.top_rules.map(r => {
      const w = (r.hits / mx) * 100;
      return `<div style="margin:7px 0;"><div class="rk-row" style="justify-content:space-between;">
        <span>${escapeHtml(r.rule_name)}</span><span class="rk-muted">${r.hits} 次</span></div>
        <div style="background:#eef2fb;height:10px;border-radius:5px;">
        <div style="width:${w}%;height:100%;border-radius:5px;background:#2f6bff;"></div></div></div>`;
    }).join("");
  }

  // 最近判定
  $("recentDecisions").innerHTML = (dash.recent || []).map(h => `
    <div class="rk-row" style="justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--rk-line);">
      <span>${escapeHtml(h.event_id)} · ${escapeHtml(h.event_type)} · ${escapeHtml(h.user_id)}</span>
      <span>${actionBadge(h.action)} ${levelBadge(h.level)}</span>
    </div>`).join("") || `<p class="rk-muted">暂无判定记录</p>`;
}

$("loadSamples").onclick = async () => {
  $("sampleMsg").textContent = "运行中…";
  try {
    const r = await api("POST", "/api/risk/samples/load");
    $("sampleMsg").textContent = `已接入 ${r.accepted} 条，拦截 ${r.blocked}，转人审 ${r.review}（见审核工作台/判定历史）`;
    renderDashboard();
  } catch (e) { $("sampleMsg").textContent = e.message; }
};
$("resetEngine").onclick = async () => {
  if (!confirm("清空所有近期事件、画像与判定历史？")) return;
  await api("POST", "/api/risk/reset");
  renderDashboard();
};

// ======================================================================
// 审核工作台
// ======================================================================
let selCaseId = null;
async function renderCases() {
  const status = $("caseFilter").value;
  const cases = await api("GET", "/api/risk/cases" + (status ? "?status=" + status : ""));
  if (!cases.length) {
    $("caseList").innerHTML = `<p class="rk-muted">暂无案件</p>`;
    return;
  }
  $("caseList").innerHTML = cases.map(c => `
    <div class="rk-item ${c.case_id === selCaseId ? "sel" : ""}" data-id="${escapeHtml(c.case_id)}">
      <div class="rk-row" style="justify-content:space-between;">
        <b>${escapeHtml(c.case_id)}</b> ${c.status === "pending" ? badge("待复核", "b-pending") : badge("已" + (c.status === "approved" ? "通过" : c.status === "rejected" ? "驳回" : "结案"), "b-done")}
      </div>
      <div class="rk-muted" style="margin-top:4px;">${escapeHtml(c.event_type)} · ${escapeHtml(c.user_id)} · 分 ${c.total_score} ${levelBadge(c.level)}</div>
    </div>`).join("");
  $("caseList").querySelectorAll(".rk-item").forEach(el => {
    el.onclick = () => { selCaseId = el.dataset.id; renderCaseDetail(el.dataset.id); renderCases(); };
  });
}
async function renderCaseDetail(id) {
  const c = await api("GET", "/api/risk/cases/" + id);
  let profileHtml = "";
  try {
    const p = await api("GET", "/api/risk/profiles/" + encodeURIComponent(c.user_id));
    profileHtml = `<div class="rk-kv">
      <b>累计事件</b><span>${p.total_events}</span>
      <b>累计下单</b><span>${p.order_count}</span>
      <b>累计支付</b><span>${p.pay_count}</span>
      <b>使用设备数</b><span>${p.device_ids.length}</span>
      <b>使用IP数</b><span>${p.ips.length}</span>
      <b>被拦截次数</b><span>${p.block_count}</span></div>`;
  } catch (e) { profileHtml = `<p class="rk-muted">无画像</p>`; }

  $("caseDetail").innerHTML = `
    <div class="rk-kv">
      <b>案件ID</b><span>${escapeHtml(c.case_id)}</span>
      <b>关联事件</b><span>${escapeHtml(c.event_id)}</span>
      <b>事件类型</b><span>${escapeHtml(c.event_type)}</span>
      <b>用户</b><span>${escapeHtml(c.user_id)}</span>
      <b>风险分</b><span>${c.total_score}</span>
      <b>风险等级</b><span>${levelBadge(c.level)}</span>
      <b>决策</b><span>${actionBadge(c.decision_action)}</span>
      <b>命中规则</b><span>${escapeHtml((c.hit_rule_ids || []).join(", ") || "—")}</span>
      <b>证据事件</b><span>${escapeHtml((c.evidence_event_ids || []).join(", ") || "—")}</span>
    </div>
    <hr class="rk-hr"/>
    <h3 style="font-size:14px;margin:0 0 8px;">风险成因</h3>
    <ul style="margin:0 0 12px;padding-left:18px;">${(c.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join("") || "<li class='rk-muted'>无</li>"}</ul>
    <h3 style="font-size:14px;margin:0 0 8px;">用户画像</h3>
    ${profileHtml}
    <hr class="rk-hr"/>
    <h3 style="font-size:14px;margin:0 0 8px;">人工复核</h3>
    ${c.status === "pending" ? `
    <div class="rk-form">
      <label>复核人</label><input id="rv_reviewer" placeholder="周明远" value="${escapeHtml(window.__sess.display_name)}"/>
      <label>复核动作</label><select id="rv_action"><option value="approve">通过（风险可接受/已处置）</option><option value="reject">驳回（确认违规）</option><option value="close">结案</option></select>
      <label>复核意见</label><input id="rv_note" placeholder="建议封禁设备并移交安全团队"/>
      <label>处置结果</label><input id="rv_disp" placeholder="已加入设备黑名单"/>
      <div class="rk-row"><button class="rk-btn" id="rv_submit">提交复核</button><span id="rv_msg"></span></div>
    </div>` : `<p class="rk-muted">本案已由 ${escapeHtml(c.reviewer || "—")} 于 ${escapeHtml(c.reviewed_at || "—")} 复核：${escapeHtml(c.review_note || "")} ${escapeHtml(c.disposition || "")}</p>`}
  `;
  const btn = $("rv_submit");
  if (btn) btn.onclick = async () => {
    try {
      await api("POST", "/api/risk/cases/" + id + "/review", {
        reviewer: $("rv_reviewer").value, action: $("rv_action").value,
        note: $("rv_note").value, disposition: $("rv_disp").value,
      });
      $("rv_msg").innerHTML = `<span class="rk-msg ok">复核已提交</span>`;
      renderCases(); renderCaseDetail(id);
    } catch (e) { $("rv_msg").innerHTML = `<span class="rk-msg err">${escapeHtml(e.message)}</span>`; }
  };
}
$("caseFilter").onchange = renderCases;

// ======================================================================
// 规则配置
// ======================================================================
async function renderRules() {
  const data = await api("GET", "/api/risk/rules");
  const facets = data.facets || [];
  $("ruleFacets").innerHTML = facets.map(f =>
    `<div class="rk-stat"><div class="v">${f.total}</div><div class="l">${escapeHtml(f.name)}（启用 ${f.enabled}）</div></div>`).join("");
  $("ruleList").innerHTML = data.rules.map(r => `
    <div class="rk-item" data-id="${escapeHtml(r.id)}">
      <div class="rk-row" style="justify-content:space-between;">
        <b>${escapeHtml(r.name)}</b>
        <span>${levelBadge(r.level)} ${actionBadge(r.action)} <b class="rk-muted">+${r.score}</b></span>
      </div>
      <div class="rk-muted" style="margin-top:4px;">${escapeHtml(r.category)} · 适用：${escapeHtml((r.scope || []).join("/") || "全部")} · ${escapeHtml(r.advice || "")}</div>
      <div class="rk-row" style="margin-top:8px;justify-content:flex-end;">
        <label class="rk-switch"><input type="checkbox" data-toggle="${escapeHtml(r.id)}" ${r.enabled ? "checked" : ""}/> 启用</label>
        <button class="rk-btn ghost" data-edit="${escapeHtml(r.id)}">编辑</button>
        <button class="rk-btn danger" data-del="${escapeHtml(r.id)}">删除</button>
      </div>
    </div>`).join("");
  $("ruleList").querySelectorAll("[data-toggle]").forEach(cb => {
    cb.onchange = async () => {
      try { await api("POST", "/api/risk/rules/" + cb.dataset.toggle + "/toggle", { enabled: cb.checked }); }
      catch (e) { alert(e.message); cb.checked = !cb.checked; }
    };
  });
  $("ruleList").querySelectorAll("[data-del]").forEach(b => {
    b.onclick = async () => {
      if (!confirm("确认删除该规则？")) return;
      try { await api("DELETE", "/api/risk/rules/" + b.dataset.del); renderRules(); }
      catch (e) { alert(e.message); }
    };
  });
  $("ruleList").querySelectorAll("[data-edit]").forEach(b => {
    b.onclick = async () => {
      const id = b.dataset.edit;
      const name = prompt("规则名称："); if (name === null) return;
      const score = prompt("风险分（整数）："); if (score === null) return;
      const level = prompt("风险等级（low/medium/high）：", "medium"); if (level === null) return;
      const action = prompt("建议动作（pass/review/block）：", "review"); if (action === null) return;
      try {
        await api("PUT", "/api/risk/rules/" + id, {
          name, score: parseInt(score, 10) || 0, level, action,
        });
        renderRules();
      } catch (e) { alert(e.message); }
    };
  });
}
$("showAddRule").onclick = () => {
  $("addRuleBox").style.display = $("addRuleBox").style.display === "none" ? "block" : "none";
};
$("addRuleSubmit").onclick = async () => {
  const body = {
    id: $("r_id").value.trim(), name: $("r_name").value.trim(),
    category: $("r_category").value.trim() || "其他",
    scope: $("r_scope").value.split(",").map(s => s.trim()).filter(Boolean),
    score: parseInt($("r_score").value, 10) || 0,
    level: $("r_level").value, action: $("r_action").value,
    advice: $("r_advice").value.trim(),
    match: "all", conditions: [],
  };
  if (!body.id || !body.name) { $("addRuleMsg").innerHTML = `<span class="rk-msg err">ID 与名称必填</span>`; return; }
  try {
    await api("POST", "/api/risk/rules", body);
    $("addRuleMsg").innerHTML = `<span class="rk-msg ok">已新增</span>`;
    renderRules();
  } catch (e) { $("addRuleMsg").innerHTML = `<span class="rk-msg err">${escapeHtml(e.message)}</span>`; }
};

// ======================================================================
// 名单库
// ======================================================================
async function renderLists() {
  const data = await api("GET", "/api/risk/lists");
  $("blackCount").textContent = data.black.length;
  $("whiteCount").textContent = data.white.length;
  const row = (e) => `<div class="rk-row" style="justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--rk-line);">
    <span><b>${escapeHtml(e.value)}</b> <span class="rk-muted">[${escapeHtml(e.dimension)}]</span><br/><span class="rk-muted" style="font-size:12px;">${escapeHtml(e.reason || "")} · ${escapeHtml(e.added_by)}</span></span>
    <button class="rk-btn danger" data-del="${escapeHtml(e.list_type)}|${escapeHtml(e.dimension)}|${escapeHtml(e.value)}">移除</button></div>`;
  $("blackList").innerHTML = data.black.map(row).join("") || `<p class="rk-muted">暂无</p>`;
  $("whiteList").innerHTML = data.white.map(row).join("") || `<p class="rk-muted">暂无</p>`;
  document.querySelectorAll("[data-del]").forEach(b => {
    b.onclick = async () => {
      const [type, dim, val] = b.dataset.del.split("|");
      try { await api("DELETE", "/api/risk/lists", { list_type: type, dimension: dim, value: val }); renderLists(); }
      catch (e) { alert(e.message); }
    };
  });
}
$("addList").onclick = async () => {
  const body = { list_type: $("l_type").value, dimension: $("l_dim").value,
    value: $("l_value").value.trim(), reason: $("l_reason").value.trim(), added_by: window.__sess.display_name };
  if (!body.value) { $("listMsg").innerHTML = `<span class="rk-msg err">值不能为空</span>`; return; }
  try { await api("POST", "/api/risk/lists", body); $("listMsg").innerHTML = `<span class="rk-msg ok">已加入</span>`; renderLists(); }
  catch (e) { $("listMsg").innerHTML = `<span class="rk-msg err">${escapeHtml(e.message)}</span>`; }
};

// ======================================================================
// 仿真测试
// ======================================================================
const SIM_TEMPLATE = {
  event_id: "E-SIM-01", event_type: "marketing", user_id: "U9001",
  timestamp: "2026-09-21 12:00:00",
  device: { device_id: "DEV-SIM-01", ip: "8.8.8.8", is_proxy: true },
  payload: { activity_id: "COUPON-88", activity_type: "领券" },
};
$("simInput").value = JSON.stringify(SIM_TEMPLATE, null, 2);
$("simSample").onclick = async () => {
  try {
    const s = await api("GET", "/api/risk/samples");
    if (s.events && s.events.length) $("simInput").value = JSON.stringify(s.events[0], null, 2);
  } catch (e) { /* 忽略 */ }
};
$("simRun").onclick = async () => {
  let ev;
  try { ev = JSON.parse($("simInput").value); }
  catch (e) { $("simMsg").textContent = "JSON 解析失败：" + e.message; return; }
  $("simMsg").textContent = "运行中…";
  try {
    const r = await api("POST", "/api/risk/simulate", { event: ev });
    const d = r.decision;
    const hits = (d.hits || []).map(h => `
      <tr><td>${escapeHtml(h.rule_name)}</td><td>${h.matched ? badge("命中", "b-block") : badge("未命中", "b-low")}</td>
        <td>+${h.score}</td><td>${levelBadge(h.level)}</td><td class="rk-muted">${escapeHtml(h.detail)}</td></tr>`).join("");
    const f = r.feature;
    $("simResult").innerHTML = `
      <div class="rk-kv" style="margin-bottom:12px;">
        <b>最终决策</b><span>${actionBadge(d.action)}</span>
        <b>综合风险</b><span>${levelBadge(d.level)}</span>
        <b>累计风险分</b><span>${d.total_score}</span>
        <b>名单命中</b><span>${r.list_hit ? badge(r.list_hit, "b-block") : "无"}</span>
      </div>
      <h3 style="font-size:14px;margin:0 0 8px;">特征快照</h3>
      <div class="rk-kv">
        <b>注册次数</b><span>${f.reg_count}</span><b>登录次数</b><span>${f.login_count}</span>
        <b>登录失败</b><span>${f.login_fail_times}</span><b>下单次数</b><span>${f.order_count}</span>
        <b>支付次数</b><span>${f.pay_count}</span><b>营销次数</b><span>${f.marketing_count}</span>
        <b>模拟器</b><span>${f.is_emulator}</span><b>代理</b><span>${f.is_proxy}</span>
        <b>VPN</b><span>${f.is_vpn}</span><b>秒拨</b><span>${f.is_ip_rotation}</span>
        <b>IP风险分</b><span>${f.ip_risk_score}</span><b>账号年龄(天)</b><span>${f.account_age_days}</span>
        <b>设备聚集度</b><span>${f.device_bind_count}</span><b>多IP数</b><span>${f.distinct_ip_count}</span>
      </div>
      <hr class="rk-hr"/>
      <h3 style="font-size:14px;margin:0 0 8px;">规则命中明细（判定链路）</h3>
      <table class="rk"><thead><tr><th>规则</th><th>判定</th><th>分</th><th>等级</th><th>说明</th></tr></thead><tbody>${hits || "<tr><td colspan=5>无规则</td></tr>"}</tbody></table>
      <hr class="rk-hr"/>
      <h3 style="font-size:14px;margin:0 0 8px;">决策依据</h3>
      <ul style="padding-left:18px;">${(d.reasons || []).map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
      <p class="rk-muted">提示：仿真不会写入运行态，可在「态势大盘」点「载入示例事件」查看真实建案效果。</p>`;
    $("simMsg").textContent = "完成";
  } catch (e) { $("simMsg").textContent = e.message; }
};

// ======================================================================
// 判定历史
// ======================================================================
async function renderHistory() {
  const f = $("histFilter").value;
  const list = await api("GET", "/api/risk/history");
  const data = f ? list.filter(h => h.action === f) : list;
  $("histList").innerHTML = data.map(h => `
    <div class="rk-row" style="justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--rk-line);">
      <span><b>${escapeHtml(h.event_id)}</b> · ${escapeHtml(h.event_type)} · ${escapeHtml(h.user_id)}
        ${h.case_id ? `<span class="rk-muted">· 案件 ${escapeHtml(h.case_id)}</span>` : ""}</span>
      <span>${actionBadge(h.action)} ${levelBadge(h.level)} <span class="rk-muted">分 ${h.total_score}</span></span>
    </div>`).join("") || `<p class="rk-muted">暂无记录</p>`;
}
$("histFilter").onchange = renderHistory;

// 首屏渲染
renderDashboard();
