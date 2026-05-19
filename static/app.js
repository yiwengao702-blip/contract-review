// 合同审批审查系统 - 前端逻辑（原生 JS，无构建依赖）
const API = "";

// [安全加固 Phase0] 自动为所有 API 请求注入会话令牌，配合后端 auth_guard 中间件。
// 此前虽登录下发 token，但前端从未携带，鉴权形同虚设；此处统一补齐，且不改动各调用点。
// 防御性：仅当运行环境本身提供 fetch 时才包裹（无头 DOM 桩、极旧浏览器下安全跳过）。
(function injectAuthHeader() {
  if (typeof window === "undefined" || typeof window.fetch !== "function") return;
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    try {
      const s = JSON.parse(localStorage.getItem("cr_session") || "null");
      if (s && s.token) {
        init.headers = init.headers || {};
        if (typeof init.headers.set === "function") {
          init.headers.set("Authorization", "Bearer " + s.token);
        } else {
          init.headers["Authorization"] = "Bearer " + s.token;
        }
      }
    } catch (e) {}
    // [安全加固 Phase0+] 401 兜底：后端会话为纯内存态，服务重启会清空所有令牌。
    // 此时前端 localStorage 里残留的旧 token 已失效，若不加处理，所有受保护接口返回
    // 401，loadRules 等取数失败，页面会静默渲染为空（即用户报的“加载不出来条款”）。
    // 这里统一拦截 401：清除失效会话并跳回登录页，让用户重新获取令牌。
    return _origFetch(input, init).then(function (resp) {
      if (resp && resp.status === 401) {
        try { localStorage.removeItem("cr_session"); } catch (e) {}
        var path = (typeof location !== "undefined" && location.pathname) || "";
        // 已在登录页或正在调用登录接口时，不重复跳转，避免死循环 / 干扰登录报错。
        if (path !== "/login" && !/^\/api\/auth\//.test(String(input))) {
          location.replace("/login");
        }
      }
      return resp;
    });
  };
})();

// ---------------- 会话与角色权限 ----------------
// 会话存于 localStorage；未登录访问主应用一律重定向到登录页。
const SESSION_KEY = "cr_session";
function getSession() {
  try { return JSON.parse(localStorage.getItem(SESSION_KEY) || "null"); } catch (e) { return null; }
}
const session = getSession();
if (!session) location.replace("/login");

// 角色能力矩阵：true 表示该角色具备对应能力
//   admin 平台管理员：全部能力
//   legal 法务合规经理：审查、回写、规则库、失败外发审核
//   biz   采购业务经办：仅上传合同与查看审查结果（不可回写审批）
// dingtalk 这个键控制「失败外发审核」页（把审查失败推送到钉钉群）。
// 刻意不叫 export：工作台的「导出 Markdown」对所有角色开放，两者语义完全不同，
// 共用一个名字迟早会有人误改权限，把导出按钮一起藏掉。
const PERMS = {
  admin: { upload: true, writeback: true, rules: true, dingtalk: true },
  legal: { upload: true, writeback: true, rules: true, dingtalk: true },
  biz:   { upload: true, writeback: false, rules: false, dingtalk: false },
};
const perm = (session && PERMS[session.role])
  || { upload: false, writeback: false, rules: false, dingtalk: false };

// 顶栏用户区：显示当前用户与角色，并提供退出登录
function renderUser() {
  const box = document.getElementById("userBox");
  if (!box || !session) return;
  box.innerHTML = `
    <span class="user-avatar">${esc((session.display_name || "U").slice(0, 1))}</span>
    <span class="user-meta">
      <span class="user-name">${esc(session.display_name || "")}</span>
      <span class="user-role">${esc(session.role_name || "")}</span>
    </span>
    <button class="btn ghost small" id="logoutBtn">退出</button>`;
  document.getElementById("logoutBtn").onclick = openLogoutModal;
}

// ---------------- 退出登录确认 ----------------
// 退出会清掉本地会话，是会话级且不可撤销的操作，所以先确认再执行，
// 而不是点一下就把人踢回登录页。弹窗里把"正要退出哪个身份"摆出来，
// 避免多账号或共用电脑时退错账号。
function openLogoutModal() {
  if (!session) return;
  document.getElementById("logoutAvatar").textContent = (session.display_name || "U").slice(0, 1);
  document.getElementById("logoutName").textContent = session.display_name || "";
  document.getElementById("logoutRole").textContent = session.role_name || "";
  document.getElementById("logoutModal").classList.remove("hidden");
  // 默认焦点给「取消」：退出是有副作用的操作，不该被顺手一个回车触发
  document.getElementById("logoutCancelBtn").focus();
}

function closeLogoutModal() {
  document.getElementById("logoutModal").classList.add("hidden");
}

document.getElementById("logoutCancelBtn").onclick = closeLogoutModal;
// 点遮罩空白处 = 放弃退出
document.getElementById("logoutModal").onclick = (e) => {
  if (e.target.id === "logoutModal") closeLogoutModal();
};
// Esc = 放弃退出（与其他弹窗保持同一交互约定）
document.addEventListener("keydown", (e) => {
  if (document.getElementById("logoutModal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeLogoutModal();
});

document.getElementById("logoutOkBtn").onclick = () => {
  // 只需清会话：本项目其余状态（筛选条件、卡片展开态）都只存在于内存，随页面卸载自然消失
  localStorage.removeItem(SESSION_KEY);
  location.replace("/login");
};

// 按角色应用界面权限
function applyPerms() {
  // 规则与模板页：仅 admin / legal 可见
  document.querySelectorAll('.nav a[data-view="rules"]').forEach(a => {
    a.style.display = perm.rules ? "" : "none";
  });
  // 失败外发审核页：把消息推到钉钉群是对外动作，与规则库同属管理侧能力
  document.querySelectorAll('.nav a[data-view="export"]').forEach(a => {
    a.style.display = perm.dingtalk ? "" : "none";
  });
  // 上传入口
  const up = document.getElementById("uploadBtn");
  if (up) up.style.display = perm.upload ? "" : "none";
  // 拍照识别入口：与上传同权限（本质是另一种"送合同进来"的方式）
  const scan = document.getElementById("scanBtn");
  if (scan) scan.style.display = perm.upload ? "" : "none";
  // 回写按钮：采购业务经办不可回写审批
  const wb = document.getElementById("writeBackBtn");
  if (wb && !perm.writeback) wb.style.display = "none";
  // 无权限时若停留在受限页则回退大盘：
  // 藏掉导航只是不给入口，直接改地址栏 hash 仍能进来，所以这里必须再兜一道
  if (!perm.rules && location.hash.startsWith("#rules")) location.hash = "#dashboard";
  if (!perm.dingtalk && location.hash.startsWith("#export")) location.hash = "#dashboard";
  // 新增示范条款入口：同样受规则库权限约束（采购业务经办不可见）
  const addBtn = document.getElementById("rlAddBtn");
  if (addBtn) addBtn.style.display = perm.rules ? "" : "none";
}
renderUser();
applyPerms();

// ---------------- 工具 ----------------
// HTML 转义：额外转义双引号，因为它要用于 title="" / alt="" 这类属性位置，
// 只转 & < > 会让含引号的文本（如引擎失败原因）把属性截断。
function esc(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function lvClass(lv) { return lv === "high" ? "lv-high" : lv === "medium" ? "lv-medium" : "lv-low"; }
function statusTag(s) {
  const map = { pending: "待处理", parsing: "解析中", checking: "类型校验中", reviewing: "审查中", blocked: "解析受阻", rejected: "已拒绝", completed: "审查完成" };
  return `<span class="tag ${s}">${map[s] || s}</span>`;
}
function riskTag(lv) {
  const txt = { high: "高风险", medium: "中风险", low: "低风险" }[lv] || lv;
  return `<span class="tag ${lvClass(lv)}">${txt}</span>`;
}
// 回写状态中文映射（后端返回英文枚举：not_written / writing / success / failed）
const WRITE_BACK_STATUS = {
  not_written: "未回写",
  writing: "回写中",
  success: "回写成功",
  failed: "回写失败",
};

// ---------------- 业务类型选择弹窗 ----------------
// 替代浏览器原生 prompt：以自定义弹窗 + 下拉框选择业务类型，
// 返回 Promise<string|null>，用户取消时 resolve(null)。
let __bizResolver = null;

function pickBizType(fileName) {
  return new Promise((resolve) => {
    __bizResolver = resolve;
    document.getElementById("bizFileName").textContent = fileName || "—";
    document.getElementById("bizModal").classList.remove("hidden");
    document.getElementById("bizTypeSelect").focus();
  });
}

function closeBizModal(value) {
  document.getElementById("bizModal").classList.add("hidden");
  const r = __bizResolver;
  __bizResolver = null;
  if (r) r(value);
}

// 弹窗按钮与交互绑定
document.getElementById("bizOkBtn").onclick = () =>
  closeBizModal(document.getElementById("bizTypeSelect").value);
document.getElementById("bizCancelBtn").onclick = () => closeBizModal(null);
// 点击遮罩空白处等同于取消
document.getElementById("bizModal").onclick = (e) => {
  if (e.target.id === "bizModal") closeBizModal(null);
};
// Esc 取消 / Enter 确认
document.addEventListener("keydown", (e) => {
  if (document.getElementById("bizModal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeBizModal(null);
  if (e.key === "Enter") closeBizModal(document.getElementById("bizTypeSelect").value);
});

// ---------------- 视图路由 ----------------
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  document.getElementById(name).classList.remove("hidden");
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.view === name));
}
function route() {
  const h = location.hash || "#dashboard";
  if (h.startsWith("#workbench")) {
    const id = new URLSearchParams(h.split("?")[1] || "").get("task");
    showView("workbench");
    if (id) loadWorkbench(id);
  } else if (h.startsWith("#rules")) {
    showView("rules");
    loadRules();
  } else if (h.startsWith("#export")) {
    // 直接改 hash 进来的（未经导航点击）也要过一遍权限，不能只靠藏导航
    if (!perm.dingtalk) { location.hash = "#dashboard"; return; }
    showView("export");
    loadExport();
  } else {
    showView("dashboard");
    loadTasks();
  }
}
window.addEventListener("hashchange", route);

// ---------------- 大盘 ----------------
// 统计概览：合同总数 / 待审任务 / 高风险合同 / 本周审结
function computeStats(rows) {
  const now = new Date();
  const dow = now.getDay() || 7; // 周一为 1，周日为 7
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (dow - 1));
  let total = rows.length, pending = 0, high = 0, week = 0;
  for (const t of rows) {
    const st = t.status;
    // 待审：尚未进入条款审查完成态的任务
    if (["pending", "parsing", "checking", "reviewing"].includes(st)) pending++;
    // 高风险：已完成审查且综合风险为 high
    if (t.report && t.report.overall_level === "high") high++;
    // 本周审结：完成态且更新时间落在本周一 00:00 之后
    if (st === "completed") {
      const d = new Date((t.updated_at || "").replace(" ", "T"));
      if (!isNaN(d) && d >= monday) week++;
    }
  }
  return { total, pending, high, week };
}

function renderStats(rows) {
  const s = computeStats(rows);
  const cards = [
    { ico: "📁", val: s.total, label: "合同总数", cls: "blue" },
    { ico: "⏳", val: s.pending, label: "待审任务", cls: "amber" },
    { ico: "⚠️", val: s.high, label: "高风险合同", cls: "red" },
    { ico: "✅", val: s.week, label: "本周审结", cls: "green" },
  ];
  document.getElementById("statsBar").innerHTML = cards.map(c => `
    <div class="stat-card ${c.cls}">
      <div class="stat-ico">${c.ico}</div>
      <div class="stat-meta">
        <div class="stat-val">${c.val}</div>
        <div class="stat-label">${c.label}</div>
      </div>
    </div>`).join("");
}

async function loadTasks() {
  const rows = await (await fetch(API + "/api/tasks")).json();
  renderStats(rows);
  const fr = document.getElementById("filterRisk").value;
  const fs = document.getElementById("filterStatus").value;
  const data = rows.filter(t =>
    (!fr || (t.report && t.report.overall_level === fr)) &&
    (!fs || t.status === fs));
  const body = document.getElementById("taskBody");
  document.getElementById("emptyHint").style.display = data.length ? "none" : "block";
  body.innerHTML = data.map(t => `
    <tr>
      <td>${esc(t.filename)}</td>
      <td>${esc(t.contract_type)}</td>
      <td>${t.meta && t.meta.amount ? Number(t.meta.amount).toLocaleString() + " " + (t.meta.currency || "元") : "—"}</td>
      <td>${statusTag(t.status)}</td>
      <td>${t.report ? riskTag(t.report.overall_level) : "—"}</td>
      <td>${esc(t.created_at)}</td>
      <td>
        ${(t.status === "completed" || t.status === "rejected")
          ? `<span class="link" onclick="location.hash='#workbench?task=${t.task_id}'">进入工作台</span>`
          : statusTag(t.status)}
        ${t.status === "blocked" ? ` <span class="link" onclick="reupload('${t.task_id}')">重试</span>` : ""}
      </td>
    </tr>`).join("");
}

document.getElementById("uploadBtn").onclick = () => document.getElementById("fileInput").click();
document.getElementById("fileInput").onchange = async (e) => {
  const file = e.target.files[0];
  // 允许重复上传同一文件：先把 input 值清空（拿到文件引用后不影响后续提交）
  e.target.value = "";
  if (!file) return;

  // 弹窗选择业务类型（下拉框）；取消则放弃本次上传
  const bizType = await pickBizType(file.name);
  if (bizType === null) return;

  const fd = new FormData();
  fd.append("file", file);
  fd.append("contract_type", bizType || "未分类");
  fd.append("auto_run", "true");
  const r = await fetch(API + "/api/tasks/upload", { method: "POST", body: fd });
  const task = await r.json();
  if (task.status === "blocked") {
    alert("解析受阻：" + task.error + "\n可点击「重试」更换文件。");
  } else if (task.status === "rejected") {
    alert("已拦截：该文件未判定为合同类文档，未进入条款审查。\n原因：" + (task.error || ""));
  }
  location.hash = "#workbench?task=" + task.task_id;
};
document.getElementById("filterRisk").onchange = loadTasks;
document.getElementById("filterStatus").onchange = loadTasks;

async function reupload(taskId) {
  // 阻塞任务重试：弹系统文件框选新文件，连同任务 id 一起提交给后端，
  // 由后端用新文件重跑同一套「文档闸门 → 规则引擎 → 语义审查」链路。
  // 不能复用页面隐藏的 fileInput（它已绑定 upload 分支的逻辑），故此处新建临时 input。
  const inp = document.createElement("input");
  inp.type = "file";
  inp.onchange = async () => {
    const f = inp.files[0];
    if (!f) return;                       // 用户取消选择时不提交，避免误建空任务
    const fd = new FormData(); fd.append("file", f);
    try {
      await fetch(API + `/api/tasks/${taskId}/retry`, { method: "POST", body: fd });
    } catch (e) {
      alert("重试提交失败：" + e.message);
      return;
    }
    location.hash = "#workbench?task=" + taskId;
  };
  inp.click();
}

// ---------------- 双栏工作台 ----------------
async function loadWorkbench(id) {
  const t = await (await fetch(API + "/api/tasks/" + id)).json();
  window.__currentTask = t;
  // 闸门拦截视图：两类都走这里，但语义完全不同，必须分开措辞——
  //   rejected     = 证据充分，判定"不是合同"（用户传错文件）
  //   blocked+undetermined = 证据矛盾（自述是合同但正文无要件，典型是拍照件识别失败），
  //                          机器判不了，只能请人来看 —— 这条要给「重申」出口
  if (t.status === "rejected") {
    renderGateBlock(t, "not_contract");
    return;
  }
  if (t.status === "blocked" && (t.doc_check || {}).verdict === "undetermined") {
    renderGateBlock(t, "undetermined");
    return;
  }
  document.getElementById("riskSummary").innerHTML =
    `<b>综合风险：</b>${t.report ? riskTag(t.report.overall_level) : "—"}<br>${esc(t.report ? t.report.summary : t.error || "")}`;
  renderContract(t);
  renderRiskCards(t);
  // 回写/导出按钮
  document.getElementById("writeBackBtn").onclick = () => doWriteback(id);
  document.getElementById("exportMdBtn").onclick = () => exportMarkdown(t);
  document.getElementById("printBtn").onclick = () => window.print();
}

function renderRejected(t) {
  const dc = t.doc_check || {};
  const signals = [
    ["合同标题特征", dc.title_hit || "未命中"],
    ["签约主体标识", dc.party_hit || "未命中"],
    ["条款结构（第X条）", (dc.clause_count ?? 0) + " 处"],
    ["合同要素命中", (dc.element_hits || []).join("、") || "无"],
    ["签署要素", dc.sign_hit ? "已命中" : "未命中"],
    ["非合同负向特征", (dc.negative_hits || []).slice(0, 8).join("、") || "无"],
  ];
  const rows = signals.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  document.getElementById("riskSummary").innerHTML =
    `<b>审查状态：</b>${statusTag("rejected")}<br>本文件未被判定为合同类文档，已跳过条款审查以避免误报。`;
  document.getElementById("contractView").innerHTML = `
    <div class="reject-box">
      <h3>⚠ 已拦截非合同文档</h3>
      <p class="reject-reason">${esc(t.error || "文档未被判定为合同，未进入条款审查。")}</p>
      <p class="reject-score">合同特征得分 <b>${Number(dc.score ?? 0).toFixed(1)}</b> / 阈值 ${Number(dc.threshold ?? 6).toFixed(1)}</p>
      <h4>判定依据（可解释）</h4>
      <table class="signal-table"><tbody>${rows}</tbody></table>
    </div>`;
  document.getElementById("riskCards").innerHTML = `
    <div class="rcard">
      <div class="rc-head"><span>提示</span></div>
      <div class="rc-reason">请确认上传的是合同/协议类文件（需含甲方/乙方、条款编号、价款/违约等要素）。
      若确为合同，请检查文档是否完整或重新上传正确的合同文本。</div>
    </div>`;
  // 拦截场景下禁用回写/导出按钮
  document.getElementById("writeBackBtn").onclick = () => alert("非合同文档无需回写。");
  document.getElementById("exportMdBtn").onclick = () => alert("非合同文档无审查报告可导出。");
  document.getElementById("printBtn").onclick = () => window.print();
}

function buildHighlighted(text, findings) {
  const hits = findings
    .filter(f => f.start_offset != null && f.end_offset != null)
    .sort((a, b) => a.start_offset - b.start_offset);
  let html = "", cursor = 0;
  for (const f of hits) {
    if (f.start_offset < cursor) continue; // 跳过重叠
    html += esc(text.slice(cursor, f.start_offset));
    const cls = f.level === "high" ? "risk-high" : f.level === "medium" ? "risk-medium" : "risk-low";
    html += `<mark class="${cls}" data-fid="${f.finding_id}">${esc(text.slice(f.start_offset, f.end_offset))}</mark>`;
    cursor = f.end_offset;
  }
  html += esc(text.slice(cursor));
  return html;
}

function renderContract(t) {
  const view = document.getElementById("contractView");
  if (t.status !== "completed") {
    view.innerHTML = `<i>状态：${statusTag(t.status)}${t.error ? "，错误：" + esc(t.error) : ""}。解析受阻请返回大盘重试。</i>`;
    return;
  }
  view.innerHTML = buildHighlighted(t.full_text, t.findings);
  view.querySelectorAll("mark").forEach(m => {
    m.onclick = () => focusCard(m.dataset.fid);
  });
}

function renderRiskCards(t) {
  const box = document.getElementById("riskCards");
  if (!t.findings || !t.findings.length) {
    box.innerHTML = `<div class="rcard">未发现明确风险点。</div>`;
    return;
  }
  const order = { high: 0, medium: 1, low: 2 };
  const fs = [...t.findings].sort((a, b) => order[a.level] - order[b.level]);
  box.innerHTML = fs.map(f => `
    <div class="rcard ${f.level}" data-fid="${f.finding_id}" id="card-${f.finding_id}">
      <div class="rc-head"><span>${riskTag(f.level)} ${esc(f.category)}</span></div>
      <div class="rc-reason">${esc(f.reason)}</div>
      ${f.legal_basis ? `<div class="rc-law">依据：${esc(f.legal_basis)}</div>` : ""}
      ${f.recommendation ? `<div class="rc-rec"><span class="rc-copy" onclick="copyRec('${f.finding_id}')">复制</span><b>建议修改：</b>\n${esc(f.recommendation)}</div>` : ""}
    </div>`).join("");
  box.querySelectorAll(".rcard").forEach(c => {
    c.onclick = (e) => {
      if (e.target.classList.contains("rc-copy")) return;
      focusMark(c.dataset.fid);
    };
  });
}

function focusMark(fid) {
  const m = document.querySelector(`mark[data-fid="${fid}"]`);
  if (m) { m.scrollIntoView({ behavior: "smooth", block: "center" }); m.classList.add("flash"); setTimeout(() => m.classList.remove("flash"), 1200); }
}
function focusCard(fid) {
  const c = document.getElementById("card-" + fid);
  if (c) { c.scrollIntoView({ behavior: "smooth", block: "center" }); c.classList.add("active"); setTimeout(() => c.classList.remove("active"), 1200); }
}
function copyRec(fid) {
  const f = window.__currentTask.findings.find(x => x.finding_id === fid);
  if (f && f.recommendation) navigator.clipboard.writeText(f.recommendation);
}

async function doWriteback(id) {
  // 权限双保险：无回写权限的角色直接拦截（采购业务经办仅上传与查看）
  if (!perm.writeback) {
    alert("当前角色（" + (session ? session.role_name : "") + "）无审批回写权限。");
    return;
  }
  const note = document.getElementById("legalNote").value;
  const st = document.getElementById("writeBackStatus");
  st.textContent = "回写中...";
  const r = await fetch(API + `/api/tasks/${id}/writeback`, { method: "POST" });
  const res = await r.json();
  st.innerHTML = `回写状态：<b>${WRITE_BACK_STATUS[res.status] || res.status}</b>${res.error ? "（" + esc(res.error) + "）" : ""}`;
}

function exportMarkdown(t) {
  if (!t.report) return;
  const rp = t.report;
  let md = `## 合同审查意见：${rp.contract_name}\n\n- 业务类型：${rp.contract_type}\n- 综合风险等级：**${rp.overall_level}**\n\n> ${rp.summary}\n\n### 风险清单\n`;
  rp.findings.forEach((f, i) => {
    md += `${i + 1}. **[${f.level}] ${f.category}** —— ${f.reason}\n${f.recommendation ? "   - 建议修改：" + f.recommendation + "\n" : ""}`;
  });
  const note = document.getElementById("legalNote").value;
  if (note) md += `\n### 法务批注\n${note}\n`;
  const blob = new Blob([md], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = rp.contract_name + "_审查意见.md";
  a.click();
}

// ---------------- 合规规则与模板 ----------------
// 三个标签页（审查规则库 / 标准示范条款库 / 主体资质库）共用左侧两张分面卡与右上搜索框。
// 页面状态：
//   tab  当前标签页；f 各分面当前选中项（键随标签页变化）；kw 搜索词；open 已展开的行键集合
const RL_STATE = { data: null, tab: "rules", f: {}, kw: "", open: new Set() };
applyAddBtnForTab(RL_STATE.tab);   // 初始：审查规则库标签下隐藏新增按钮

// 风险等级中文与排序权重
const LV_TEXT = { high: "高风险", medium: "中风险", low: "低风险" };
const LV_ORDER = { high: 0, medium: 1, low: 2 };
// 规则类型 -> 人类可读的判定方式说明（展开行展示）
const RULE_TYPE_TEXT = {
  meta_missing: "元数据字段缺失检查：合同要素未被抽取/未约定即报警",
  meta_or_regex: "复合判定：元数据缺失 或 正文命中敏感表述，任一命中即报警",
  text_regex: "正文（或限定条款类别）正则命中即报警",
  text_absent: "条款存在但缺少必备要件才报警（避免只要出现该措辞就误报）",
  clause_missing: "条款整体缺失才报警（按条款类别或关键措辞判断）",
};

// 各标签页在 /api/rules/config 的 counts 里对应的字段名（用于统计条）
const CNT_KEY = { rules: "rules", clauses: "clauses", quals: "qualifications" };
const RL_SEARCH_PH = {
  rules: "搜索规则名称 / 编号",
  clauses: "搜索条款名称 / 正文",
  quals: "搜索核验项名称 / 编号",
};

// 计算「标准示范条款库」中被规则引用的条款键集合
function clauseRefKeys() {
  const d = RL_STATE.data;
  return new Set(((d && d.rules) || []).map(r => r.recommend_key).filter(Boolean));
}

// 按标签页构造左侧两张分面卡定义：
//   { key 状态键, title 卡片标题, allLabel 全部项文案, allCount 全部计数, items 子项, risk 是否展示风险徽标 }
function facetDefs() {
  const d = RL_STATE.data;
  const group = (arr, getter) => {
    const m = new Map();
    for (const x of arr) { const k = getter(x) || "其他"; m.set(k, (m.get(k) || 0) + 1); }
    return [...m].map(([name, count]) => ({ name, count }));
  };

  if (RL_STATE.tab === "rules") {
    const dims = (d.facets.dimensions || []).map(x => ({
      name: x.name, count: x.total, high: x.high, medium: x.medium,
    }));
    return [
      { key: "dim", title: "审查维度", allLabel: "全部维度", allCount: d.counts.rules, items: dims, risk: true },
      { key: "biz", title: "业务类型覆盖", allLabel: "全部类型", allCount: d.counts.rules,
        items: (d.facets.biz_types || []).map(x => ({ name: x.name, count: x.total })) },
    ];
  }
  if (RL_STATE.tab === "clauses") {
    const refs = clauseRefKeys();
    const used = d.clauses.filter(c => refs.has(c.key)).length;
    return [
      { key: "dim", title: "关联维度", allLabel: "全部维度", allCount: d.clauses.length,
        items: group(d.clauses, c => c.category) },
      { key: "ref", title: "引用情况", allLabel: "全部条款", allCount: d.clauses.length,
        items: [{ name: "已关联规则", count: used }, { name: "通用条款", count: d.clauses.length - used }] },
    ];
  }
  return [
    { key: "dim", title: "审查维度", allLabel: "全部维度", allCount: d.qualifications.length,
      items: group(d.qualifications, q => q.category) },
    { key: "src", title: "数据来源", allLabel: "全部来源", allCount: d.qualifications.length,
      items: group(d.qualifications, q => q.source) },
  ];
}

// 渲染顶部筛选工具条（替代原左侧分面栏）：
//   ① 风险分级速筛胶囊  ② 两组筛选 chips  ③ 结果统计  ④ 清空筛选
// 与旧分面栏的差别：chips 按计数降序排列（规则最多的维度自动浮到最前），
// 且选中态用实心深底而非浅蓝底 —— 横向铺开时，浅底色块在浅色背景上几乎看不出选中。
function renderFilterBar() {
  const defs = facetDefs();
  const chipBoxes = [document.getElementById("rlDimChips"), document.getElementById("rlBizChips")];
  const labels = [document.getElementById("rlDimLabel"), document.getElementById("rlBizLabel")];

  defs.forEach((def, idx) => {
    const box = chipBoxes[idx];
    if (!box) return;
    labels[idx].textContent = def.title;
    const cur = RL_STATE.f[def.key] || "";
    const items = [...def.items].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
    const parts = [`<button class="rl-fchip ${cur === "" ? "active" : ""}" data-fkey="${def.key}" data-fval="">
        全部<span class="rl-fchip-n">${def.allCount}</span></button>`];
    for (const it of items) {
      // 风险圆点：与规则卡的分级口径一致，只标高风险/中风险，避免整行都是彩点
      const dots = def.risk
        ? `${it.high ? `<i class="rl-fdot high" title="高风险 ${it.high} 条"></i>` : ""}`
          + `${it.medium ? `<i class="rl-fdot medium" title="中风险 ${it.medium} 条"></i>` : ""}`
        : "";
      parts.push(`<button class="rl-fchip ${cur === it.name ? "active" : ""}"
          data-fkey="${def.key}" data-fval="${esc(it.name)}">${dots}${esc(it.name)}<span class="rl-fchip-n">${it.count}</span></button>`);
    }
    box.innerHTML = parts.join("");
  });

  // 风险分级速筛：只用于「审查规则库」（条款/资质没有 level 字段）
  const lvBox = document.getElementById("rlLvChips");
  if (RL_STATE.tab === "rules") {
    const dist = { high: 0, medium: 0, low: 0 };
    for (const r of (RL_STATE.data.rules || [])) dist[r.level] = (dist[r.level] || 0) + 1;
    const cur = RL_STATE.f.lv || "";
    lvBox.classList.remove("hidden");
    lvBox.innerHTML = ["high", "medium", "low"].map(k =>
      `<button class="rl-lv-chip ${k} ${cur === k ? "active" : ""}" data-lv="${k}"
         title="${cur === k ? "取消只看" : "只看"}${LV_TEXT[k]}"><i class="rl-lv-dot"></i>${LV_TEXT[k]}
         <span class="rl-fchip-n">${dist[k] || 0}</span></button>`).join("");
  } else {
    lvBox.innerHTML = "";
    lvBox.classList.add("hidden");
  }

  // 结果统计 + 清空按钮显隐（有任一筛选条件生效才出现）
  const shown = filterRows().length;
  const totalAll = (RL_STATE.data.counts || {})[CNT_KEY[RL_STATE.tab]] || 0;
  const filtering = !!(RL_STATE.f[defs[0].key] || RL_STATE.f[defs[1].key]
    || RL_STATE.f.lv || RL_STATE.kw.trim());
  document.getElementById("rlResultStat").innerHTML = filtering
    ? `筛出 <b>${shown}</b> / ${totalAll} 条`
    : `共 <b>${totalAll}</b> 条`;
  document.getElementById("rlClearBtn").classList.toggle("hidden", !filtering);

  // 事件绑定（每次重渲染都重建，避免残留闭包指向旧状态）
  document.querySelectorAll(".rl-chips .rl-fchip").forEach(btn => {
    btn.onclick = () => {
      RL_STATE.f[btn.dataset.fkey] = btn.dataset.fval;
      RL_STATE.open.clear();
      renderFilterBar();
      renderGrid();
    };
  });
  document.querySelectorAll("#rlLvChips .rl-lv-chip").forEach(btn => {
    btn.onclick = () => {
      RL_STATE.f.lv = RL_STATE.f.lv === btn.dataset.lv ? "" : btn.dataset.lv;
      RL_STATE.open.clear();
      renderFilterBar();
      renderGrid();
    };
  });
}

// 风险分级徽标
function lvPill(lv) {
  const k = LV_TEXT[lv] ? lv : "low";
  return `<span class="rl-pill ${k}">${LV_TEXT[k]}</span>`;
}

// 适用范围：全部类型规则优先展示为「全部类型」，其余按业务类型顿号连接
function scopeText(scope) {
  const arr = scope || [];
  if (!arr.length || arr.includes("全部类型")) return "全部类型";
  return arr.join("、");
}

// 启用开关（复选框 + 自绘滑轨）
function switchHtml(ruleId, on) {
  return `<label class="rl-switch"><input type="checkbox" ${on ? "checked" : ""}
    data-rule="${esc(ruleId)}"><span class="rl-slider"></span></label>`;
}

// 按标签页与分面/搜索条件过滤数据行
function filterRows() {
  const d = RL_STATE.data;
  const kw = RL_STATE.kw.trim().toLowerCase();
  const hit = (s) => String(s == null ? "" : s).toLowerCase().includes(kw);
  const tab = RL_STATE.tab;

  if (tab === "rules") {
    return [...(d.rules || [])]
      .filter(r =>
        (!RL_STATE.f.dim || (r.category || "其他") === RL_STATE.f.dim) &&
        (!RL_STATE.f.biz || (r.scope || []).includes(RL_STATE.f.biz)) &&
        (!RL_STATE.f.lv || r.level === RL_STATE.f.lv) &&
        (!kw || hit(r.id) || hit(r.name) || hit(r.category) || hit(r.match_mode)))
      .sort((a, b) => (LV_ORDER[a.level] ?? 3) - (LV_ORDER[b.level] ?? 3)
        || String(a.id).localeCompare(String(b.id)));
  }
  if (tab === "clauses") {
    const refs = clauseRefKeys();
    return (d.clauses || []).filter(c => {
      const state = refs.has(c.key) ? "已关联规则" : "通用条款";
      return (!RL_STATE.f.dim || c.category === RL_STATE.f.dim)
        && (!RL_STATE.f.ref || state === RL_STATE.f.ref)
        && (!kw || hit(c.title) || hit(c.key) || hit(c.text) || hit(c.category));
    });
  }
  return (d.qualifications || []).filter(q =>
    (!RL_STATE.f.dim || (q.category || "其他") === RL_STATE.f.dim)
    && (!RL_STATE.f.src || (q.source || "其他") === RL_STATE.f.src)
    && (!kw || hit(q.id) || hit(q.name) || hit(q.basis) || hit(q.source)));
}

// 卡片内的「键：值」小项（适用范围 / 匹配方式 / 数据来源 等）
function rcKv(k, v) {
  return `<span class="rl-rc-kv"><em>${k}</em>${esc(v)}</span>`;
}

// 生成一张卡片（含可选的展开详情）
// 与旧表格行的差别：编号/风险等级/启用开关收到卡片头部一行，规则名独立成标题，
// 元信息退为次要行 —— 扫视时先看到"这条规则管什么"，而不是先看到一列编号。
function cardHtml(tab, item, open) {
  const key = item.id || item.key;

  if (tab === "rules") {
    return `<article class="rl-rc lv-${esc(item.level || "low")}${open ? " open" : ""}" data-key="${esc(key)}">
      <div class="rl-rc-head">
        <span class="rl-mono rl-rc-id">${esc(key)}</span>
        ${lvPill(item.level)}
        ${switchHtml(item.id, item.enabled !== false)}
      </div>
      <h3 class="rl-rc-name">${esc(item.name)}</h3>
      <div class="rl-rc-meta">
        <span class="rl-chip">${esc(item.category || "其他")}</span>
        ${rcKv("适用范围", scopeText(item.scope))}
        ${rcKv("匹配方式", item.match_mode || "—")}
      </div>
      <div class="rl-rc-foot">
        <span class="rl-rc-toggle">${open ? "收起判定依据 ▴" : "展开判定依据 ▾"}</span>
        <span class="rl-rc-op">只读</span>
      </div>
      ${open ? `<div class="rl-rc-detail">
        <div class="rl-detail-row"><span class="rl-detail-k">风险成因</span><span>${esc(item.reason || "—")}</span></div>
        <div class="rl-detail-row"><span class="rl-detail-k">合规依据</span><span>${esc(item.legal_basis || "—")}</span></div>
        <div class="rl-detail-row"><span class="rl-detail-k">判定方式</span><span>${esc(RULE_TYPE_TEXT[item.type] || item.type || "—")}</span></div>
        <div class="rl-detail-row"><span class="rl-detail-k">关联条款</span><span>${item.recommend_key
          ? `<span class="rl-mono">${esc(item.recommend_key)}</span>` : "无（使用通用建议）"}</span></div>
      </div>` : ""}
    </article>`;
  }

  if (tab === "clauses") {
    const ids = ((RL_STATE.data.rules) || [])
      .filter(r => r.recommend_key === item.key).map(r => r.id);
    return `<article class="rl-rc lv-neutral${open ? " open" : ""}" data-key="${esc(key)}">
      <div class="rl-rc-head">
        <span class="rl-mono rl-rc-id">${esc(key)}</span>
        <span class="rl-chip">${esc(item.category || "其他")}</span>
        <span class="rl-link" data-copy="${esc(item.key)}">复制正文</span>
      </div>
      <h3 class="rl-rc-name">${esc(item.title)}</h3>
      <p class="rl-rc-quote">${esc(item.text.replace(/\s+/g, " "))}</p>
      <div class="rl-rc-meta">
        ${rcKv("引用规则", ids.length ? ids.join("、") : "通用条款")}
      </div>
      <div class="rl-rc-foot">
        <span class="rl-rc-toggle">${open ? "收起示范条款 ▴" : "展开示范条款 ▾"}</span>
        <span class="rl-rc-op">只读</span>
      </div>
      ${open ? `<div class="rl-rc-detail">
        <div class="rl-detail-row"><span class="rl-detail-k">示范条款</span><pre class="rl-pre">${esc(item.text)}</pre></div>
      </div>` : ""}
    </article>`;
  }

  return `<article class="rl-rc lv-${esc(item.level || "low")}${open ? " open" : ""}" data-key="${esc(key)}">
    <div class="rl-rc-head">
      <span class="rl-mono rl-rc-id">${esc(key)}</span>
      ${lvPill(item.level)}
    </div>
    <h3 class="rl-rc-name">${esc(item.name)}</h3>
    <div class="rl-rc-meta">
      <span class="rl-chip">${esc(item.category || "其他")}</span>
      ${rcKv("数据来源", item.source || "—")}
      ${rcKv("状态", item.enabled === false ? "已停用" : "启用中")}
    </div>
    <div class="rl-rc-foot">
      <span class="rl-rc-toggle">${open ? "收起核验依据 ▴" : "展开核验依据 ▾"}</span>
      <span class="rl-rc-op">只读</span>
    </div>
    ${open ? `<div class="rl-rc-detail">
      <div class="rl-detail-row"><span class="rl-detail-k">核验依据</span><span>${esc(item.basis || "—")}</span></div>
      <div class="rl-detail-row"><span class="rl-detail-k">数据来源</span><span>${esc(item.source || "—")}</span></div>
    </div>` : ""}
  </article>`;
}

function renderGrid() {
  const tab = RL_STATE.tab;
  const rows = filterRows();

  document.getElementById("rlSearch").placeholder = RL_SEARCH_PH[tab];
  document.getElementById("rlGrid").innerHTML =
    rows.map(x => cardHtml(tab, x, RL_STATE.open.has(x.id || x.key))).join("");

  // 空状态文案：区分"筛选后为空"与"该业务类型暂无专属规则"两种语义
  const empty = document.getElementById("rlEmpty");
  empty.style.display = rows.length ? "none" : "block";
  if (!rows.length) {
    empty.textContent = (tab === "rules" && RL_STATE.f.biz)
      ? `「${RL_STATE.f.biz}」类型暂无专属规则：系统对该业务类型仍会执行全部「全部类型」规则，此处为空表示尚无针对性规则项。`
      : "未匹配到任何记录，可调整上方筛选条件，或点击「清空筛选」后重试。";
  }

  bindCardEvents();
}

// 卡片交互：展开详情、切换启用、复制条款、点击开关不触发展开
function bindCardEvents() {
  document.querySelectorAll("#rlGrid article.rl-rc").forEach(card => {
    card.onclick = (e) => {
      if (e.target.closest(".rl-switch")) return;      // 开关自己处理
      if (e.target.closest("[data-copy]")) return;     // 复制链接自己处理
      const key = card.dataset.key;
      RL_STATE.open.has(key) ? RL_STATE.open.delete(key) : RL_STATE.open.add(key);
      renderGrid();
    };
  });

  document.querySelectorAll("#rlGrid .rl-switch input").forEach(inp => {
    inp.onchange = async () => {
      const id = inp.dataset.rule;
      const next = inp.checked;
      try {
        const r = await fetch(API + `/api/rules/${encodeURIComponent(id)}/toggle`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "切换失败");
        // 就地更新本地数据，避免整表重取导致展开状态与滚动位置丢失
        const item = (RL_STATE.data.rules || []).find(x => x.id === id);
        if (item) item.enabled = j.enabled;
        flashHint(`规则 ${id} 已${j.enabled ? "启用" : "停用"}，后续审查任务即时生效。`, j.enabled ? "ok" : "warn");
      } catch (err) {
        inp.checked = !next;                       // 失败回滚，避免出现"看着关掉了其实没关"
        flashHint("切换失败：" + err.message, "err");
      }
    };
  });

  document.querySelectorAll("#rlGrid [data-copy]").forEach(el => {
    el.onclick = (e) => {
      e.stopPropagation();
      const c = (RL_STATE.data.clauses || []).find(x => x.key === el.dataset.copy);
      if (!c) return;
      navigator.clipboard.writeText(c.text).then(
        () => flashHint("示范条款正文已复制到剪贴板。", "ok"),
        () => flashHint("复制失败，请手动选中文案复制。", "err"));
    };
  });
}

// 页头右侧的轻提示（保存/切换结果反馈，不打断操作）
let __rlHintTimer = null;
function flashHint(msg, kind) {
  const el = document.getElementById("clauseHint");
  if (!el) return;
  el.textContent = msg;
  el.className = "field-hint rl-hint " + (kind || "");
  clearTimeout(__rlHintTimer);
  __rlHintTimer = setTimeout(() => {
    el.className = "field-hint rl-hint";
    el.textContent = "留空的待填项建议用【】标注，便于法务逐项确认。";
  }, 3600);
}

async function loadRules() {
  try {
    const d = await (await fetch(API + "/api/rules/config")).json();
    RL_STATE.data = d;
    document.getElementById("rlN-rules").textContent = d.counts.rules;
    document.getElementById("rlN-clauses").textContent = d.counts.clauses;
    document.getElementById("rlN-quals").textContent = d.counts.qualifications;
    // 新增条款弹窗的维度下拉：取自规则库实际维度，保证与左侧分面口径一致
    document.getElementById("clauseCategory").innerHTML =
      (d.facets.dimensions || []).map(x => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join("");
    renderFilterBar();
    renderGrid();
  } catch (e) {
    flashHint("规则库加载失败：" + e.message, "err");
  }
}

// 标签页切换
document.querySelectorAll("#rlTabs .rl-tab").forEach(btn => {
  btn.onclick = () => {
    RL_STATE.tab = btn.dataset.tab;
    RL_STATE.f = {};          // 分面键随标签页不同，切换时重置筛选
    RL_STATE.open.clear();
    document.querySelectorAll("#rlTabs .rl-tab").forEach(b => b.classList.toggle("active", b === btn));
    applyAddBtnForTab(RL_STATE.tab);
    renderFilterBar();
    renderGrid();
  };
});

// 新增按钮始终可见：「添加条款」是用户高频意图，不应因当前标签被隐藏。
// rules 标签下点它自动跳到「标准示范条款库」并弹窗；clauses / quals 标签下直接弹对应窗。
function applyAddBtnForTab(tab) {
  const addBtn = document.getElementById("rlAddBtn");
  if (tab === "rules") {
    addBtn.style.display = "";
    addBtn.textContent = "＋ 新增示范条款";
    addBtn.onclick = () => {
      document.querySelector('#rlTabs .rl-tab[data-tab="clauses"]').click();
      openClauseModal();
    };
  } else if (tab === "clauses") {
    addBtn.style.display = "";
    addBtn.textContent = "＋ 新增示范条款";
    addBtn.onclick = openClauseModal;
  } else {
    addBtn.style.display = "";
    addBtn.textContent = "＋ 新增主体资质";
    addBtn.onclick = openQualModal;
  }
}

// 搜索输入：只重渲染主区 + 统计（不重渲染 chips，避免输入时 chip 闪烁抢焦点）
document.getElementById("rlSearch").oninput = (e) => {
  RL_STATE.kw = e.target.value;
  RL_STATE.open.clear();
  renderFilterBar();
  renderGrid();
};

// 清空筛选：一次把所有条件（分面 / 风险等级 / 搜索词）复位
document.getElementById("rlClearBtn").onclick = () => {
  RL_STATE.f = {};
  RL_STATE.kw = "";
  RL_STATE.open.clear();
  document.getElementById("rlSearch").value = "";
  renderFilterBar();
  renderGrid();
};

document.getElementById("rlRefreshBtn").onclick = () => {
  RL_STATE.open.clear();
  loadRules();
  flashHint("规则库已刷新。", "ok");
};

// ---------------- 新增示范条款弹窗 ----------------
function openClauseModal() {
  document.getElementById("clauseTitle").value = "";
  document.getElementById("clauseText").value = "";
  document.getElementById("clauseModal").classList.remove("hidden");
  document.getElementById("clauseTitle").focus();
}
function closeClauseModal() {
  document.getElementById("clauseModal").classList.add("hidden");
}

document.getElementById("rlAddBtn").onclick = openClauseModal;
document.getElementById("clauseCancelBtn").onclick = closeClauseModal;
document.getElementById("clauseModal").onclick = (e) => {
  if (e.target.id === "clauseModal") closeClauseModal();
};
document.getElementById("clauseOkBtn").onclick = async () => {
  const title = document.getElementById("clauseTitle").value.trim();
  const category = document.getElementById("clauseCategory").value;
  const text = document.getElementById("clauseText").value.trim();
  if (!title || !text) {
    flashHint("条款名称与示范条款正文不能为空。", "err");
    return;
  }
  try {
    const r = await fetch(API + "/api/rules/clauses", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, category, text }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "保存失败");
    closeClauseModal();
    // 保存成功后切到条款库标签页，让用户立刻看到新增结果
    document.querySelector('#rlTabs .rl-tab[data-tab="clauses"]').click();
    await loadRules();
    flashHint(`已新增示范条款「${j.title}」，可用于同维度风险的建议。`, "ok");
  } catch (err) {
    flashHint("保存失败：" + err.message, "err");
  }
};
// Esc 关闭 / Ctrl+Enter 保存
document.addEventListener("keydown", (e) => {
  if (document.getElementById("clauseModal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeClauseModal();
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) document.getElementById("clauseOkBtn").click();
});

// ---------------- 新增主体资质核验项弹窗 ----------------
function openQualModal() {
  document.getElementById("qualName").value = "";
  document.getElementById("qualBasis").value = "";
  document.getElementById("qualSource").value = "";
  document.getElementById("qualLevel").value = "medium";
  document.getElementById("qualModal").classList.remove("hidden");
  document.getElementById("qualName").focus();
}
function closeQualModal() {
  document.getElementById("qualModal").classList.add("hidden");
}
document.getElementById("qualCancelBtn").onclick = closeQualModal;
document.getElementById("qualModal").onclick = (e) => {
  if (e.target.id === "qualModal") closeQualModal();
};
document.getElementById("qualOkBtn").onclick = async () => {
  const name = document.getElementById("qualName").value.trim();
  const category = document.getElementById("qualCategory").value;
  const level = document.getElementById("qualLevel").value;
  const basis = document.getElementById("qualBasis").value.trim();
  const source = document.getElementById("qualSource").value.trim();
  if (!name || !basis) {
    flashHint("核验项名称与核验要点不能为空。", "err");
    return;
  }
  try {
    const r = await fetch(API + "/api/rules/qualifications", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, category, level, basis, source }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "保存失败");
    closeQualModal();
    document.querySelector('#rlTabs .rl-tab[data-tab="quals"]').click();
    await loadRules();
    flashHint(`已新增主体资质「${j.name}」（${j.id}），后续审查即时生效。`, "ok");
  } catch (err) {
    flashHint("保存失败：" + err.message, "err");
  }
};
// Esc 关闭 / Ctrl+Enter 保存（资质弹窗）
document.addEventListener("keydown", (e) => {
  if (document.getElementById("qualModal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeQualModal();
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) document.getElementById("qualOkBtn").click();
});

// ---------------- 拍照 / 图片识别（支持多张照片 = 多页合同） ----------------
// 交互链路：探测引擎能力 → 逐张添加照片（拍照 / 相册多选 / 拖拽）
//          → 整批送后端 OCR（每页都有体检与尝试链路）→ 逐页校对 → 提交审查
// 设计取舍：
//   ① 一页一张卡片、一页一个校对框：纸质合同的多页是"同一份合同的不同页"，
//      页码必须看得见、能调序、能单独重拍；否则第 3 页拍糊了只能整份重来。
//   ② 提交时传「逐页正文数组」而不是前端拼好的长字符串：页与页之间的分隔符
//      决定了后端能否还原出真实页码，这个约定只能有一处定义（在 ocr_service 里），
//      前端另存一份实现，一旦漂移风险卡片就会指错页。
//   ③ 留空页不参与审查：纸质合同确有空白页/封面页，强行要求填写等于逼用户编内容；
//      但要明确告诉用户哪几页是空的，别让"漏了一页"悄悄溜进审查。
//   ④ 留空页仍然占页码：提交时数组中该页传空字符串，由后端保留分页位置，
//      使「第 N 页」始终等于「第 N 张照片」。若把空页删掉再提交，后续页的
//      页码会整体前移，用户照报告页码去翻照片就对不上了。
const SCAN = { pages: [], maxPages: 20, replaceId: null, seq: 0, probe: null };
// 页对象结构：{ id, file, url, status: pending|loading|done|error, result, text, error }

const scanPageById = (id) => SCAN.pages.find((p) => p.id === id);
const scanPageNo = (id) => SCAN.pages.findIndex((p) => p.id === id) + 1;
const scanHasText = (p) => !!(p.text || "").trim();
// 可重试 = 该页还没有正文，且上一次识别确实没成功（成功过的不重试，免得覆盖用户清空的选择）
const scanIsRetryable = (p) =>
  !scanHasText(p) && p.status !== "loading" && !(p.result && p.result.ok);

function setScanHint(msg, kind) {
  const el = document.getElementById("scanHint");
  if (!el) return;
  el.textContent = msg;
  el.className = "field-hint scan-hint " + (kind || "");
}

function openScanModal() {
  SCAN.pages = [];
  SCAN.seq = 0;
  SCAN.replaceId = null;
  syncScanUI();
  setScanHint("点击「拍照」逐页拍，或用「选择图片」一次多选；各页正文会按页码顺序合并成一份合同。", "");
  document.getElementById("scanModal").classList.remove("hidden");
  loadOcrProbe();
}

function closeScanModal() {
  document.getElementById("scanModal").classList.add("hidden");
  // 及时释放本地预览地址，长时间反复拍照不会持续占用内存
  SCAN.pages.forEach((p) => URL.revokeObjectURL(p.url));
}

// 添加一批照片：本地立即出预览（不等识别），再整批送去识别
async function addScanFiles(files) {
  const list = Array.from(files || []).filter(
    (f) => f && (f.type.startsWith("image/") || /\.(png|jpe?g|bmp|tiff?|webp)$/i.test(f.name || ""))
  );
  if (!list.length) {
    setScanHint("没有选中可用的图片（仅支持 JPG / PNG / BMP / TIFF / WEBP）。", "warn");
    return;
  }

  const room = SCAN.maxPages - SCAN.pages.length;
  if (room <= 0) {
    setScanHint(`已达 ${SCAN.maxPages} 页上限，请先删除部分照片再添加。`, "warn");
    return;
  }
  const take = list.slice(0, room);
  const dropped = list.length - take.length;

  const fresh = take.map((file) => ({
    id: ++SCAN.seq,
    file,
    url: URL.createObjectURL(file),
    status: "pending",
    result: null,
    text: "",
    error: "",
  }));
  SCAN.pages.push(...fresh);
  syncScanUI();
  if (dropped) {
    setScanHint(`已忽略 ${dropped} 张：一次最多 ${SCAN.maxPages} 页，可分两批提交。`, "warn");
  }
  await recognizeScanPages(fresh);
}

// 引擎能力现状：打开弹窗即调用，避免"识别失败后才发现这台机器没装引擎"
// force=true 走服务端强制重探（绕过进程内缓存），对应界面上的「重新检测」按钮：
// 引擎常常是"服务已经开着的时候"才装好的，若不强制刷新，用户会一直看到旧的"不可用"，
// 只能重启服务才生效 —— 那种"我明明装好了你却说没装"的体验必须避免。
async function loadOcrProbe(force = false) {
  const box = document.getElementById("scanProviders");
  box.innerHTML = `<div class="scan-providers-loading">正在检测本机可用的识别引擎…</div>`;
  try {
    const url = API + "/api/ocr/probe" + (force ? "?refresh=1" : "");
    const d = await (await fetch(url)).json();
    SCAN.probe = d;
    if (d.max_pages) SCAN.maxPages = d.max_pages;   // 页数上限以服务端为准，避免前后端各写一个数
    // manual 是兜底通道，不参与"自动识别是否可用"的判断，故不在此列表展示
    const items = (d.providers || []).filter(p => p.name !== "manual");
    const head = d.auto_available
      ? `<span class="scan-ok">✓ 自动识别可用：${esc(d.active_label)}（一次最多 ${SCAN.maxPages} 页）</span>`
      : `<span class="scan-warn">⚠ 本机无可用自动识别引擎：多张照片仍可添加，但需在各页校对框人工录入</span>`;
    box.innerHTML =
      `<div class="scan-providers-head"><span class="scan-providers-title">识别引擎</span>${head}
         <button type="button" class="scan-reprobe" id="scanReprobe">重新检测</button></div>
       <div class="scan-providers-list">${items.map(p => `
         <span class="scan-provider ${p.available ? "on" : "off"}"
               title="${esc(p.reason)}${p.hint ? "｜修复：" + esc(p.hint) : ""}">
           <i class="scan-dot"></i>${esc(p.label)}<em>${p.available ? "可用" : "不可用"}</em>
         </span>`).join("")}</div>
       ${d.auto_available ? "" : `<div class="scan-providers-tip">
         刚装完引擎或中文包？本机服务无需重启，点「重新检测」即可生效。
         <span class="scan-providers-meta">当前解释器：${esc(d.python_exe || "")}｜检测时间：${esc(d.checked_at || "")}</span>
       </div>`}`;
    // 按钮在 innerHTML 重建后才存在，故在渲染完成后绑定
    const btn = document.getElementById("scanReprobe");
    if (btn) btn.onclick = () => loadOcrProbe(true);
  } catch (e) {
    box.innerHTML = `<div class="scan-warn">引擎探测失败：${esc(e.message)}</div>`;
  }
}

// 重建页列表。只在"结构变化"时调用（增删页、调序、识别状态变化）；
// 用户打字只刷新汇总，不重建卡片 —— 重建会丢焦点和光标位置。
function renderScanPages() {
  const box = document.getElementById("scanPages");
  if (!SCAN.pages.length) {
    box.innerHTML = `<div class="scan-empty">尚未添加任何照片。点击上方「拍照」逐页拍摄，或使用「选择图片」一次多选；也可直接将照片拖拽至此处。</div>`;
    return;
  }
  box.innerHTML = SCAN.pages.map(scanPageHTML).join("");
  // 正文用 value 赋值而非写进 HTML：识别文本里出现 < 、& 也不会破坏 DOM 结构
  SCAN.pages.forEach((p) => {
    const ta = box.querySelector(`textarea[data-id="${p.id}"]`);
    if (ta) ta.value = p.text || "";
  });
}

// 单页卡片：缩略图 + 体检 + 识别过程 + 可编辑正文，页码即合同页序
function scanPageHTML(p, i) {
  const no = i + 1;
  const r = p.result || {};
  const q = r.quality || null;
  const recognized = p.status === "done" && r.ok;
  const failed = p.status === "error" || (p.status === "done" && !r.ok);
  const cls = p.status === "loading" ? "is-loading" : recognized ? "is-ok" : failed ? "is-fail" : "";

  let status;
  if (p.status === "loading") {
    status = `<span class="scan-page-status loading">⏳ 识别中…</span>`;
  } else if (p.status === "error") {
    status = `<span class="scan-page-status fail">✕ 识别请求失败：${esc(p.error)}</span>`;
  } else if (recognized) {
    status = `<span class="scan-page-status ok">✓ 已识别 ${r.chars || 0} 字`
      + `${r.provider_label ? "（" + esc(r.provider_label) + "）" : ""}</span>`;
  } else if (p.status === "done") {
    status = `<span class="scan-page-status fail">⚠ 未能自动识别${r.reason ? "：" + esc(r.reason) : ""}</span>`;
  } else {
    status = `<span class="scan-page-status">待识别</span>`;
  }

  const chips = q ? [
    q.width && q.height ? `${q.width}×${q.height}` : "",
    q.sharpness ? `清晰度 ${q.sharpness}` : "",
    q.brightness ? `亮度 ${q.brightness}` : "",
    q.contrast ? `对比度 ${q.contrast}` : "",
  ].filter(Boolean) : [];

  const warns = (q && q.warnings) || [];
  const warnHTML = warns.length
    ? `<ul class="scan-warn-list">${warns.map((w) => `<li>⚠ ${esc(w)}</li>`).join("")}</ul>`
    : q ? `<div class="scan-good">✓ 照片质量各项指标正常</div>` : "";

  // 识别过程默认折叠；只在"需要交代为什么没认出来"时展开，否则纯占版面
  const attempts = r.attempts || [];
  const attemptsHTML = attempts.length
    ? `<details class="scan-attempts"${recognized ? "" : " open"}>
         <summary>识别过程（${attempts.length} 个引擎）</summary>
         <div class="scan-attempt-list">${attempts.map(scanAttemptHTML).join("")}</div>
       </details>`
    : "";

  return `<div class="scan-page ${cls}">
    <div class="scan-page-head">
      <span class="scan-page-no">第 ${no} 页</span>
      <img class="scan-page-thumb" src="${p.url}" alt="第 ${no} 页照片" />
      <div class="scan-page-info">
        ${status}
        ${chips.length ? `<div class="scan-meta">${chips.map((c) => `<span class="scan-chip">${esc(c)}</span>`).join("")}</div>` : ""}
        ${warnHTML}
        ${attemptsHTML}
      </div>
      <div class="scan-page-ops">
        <button data-op="up" data-id="${p.id}" ${i === 0 ? "disabled" : ""} title="上移一页">↑</button>
        <button data-op="down" data-id="${p.id}" ${i === SCAN.pages.length - 1 ? "disabled" : ""} title="下移一页">↓</button>
        <button data-op="replace" data-id="${p.id}" title="重拍或替换这一页">↻ 重拍</button>
        <button data-op="remove" data-id="${p.id}" title="删除这一页">✕</button>
      </div>
    </div>
    <textarea class="note scan-page-text" data-id="${p.id}"
      placeholder="第 ${no} 页正文：自动识别结果可在此校对；未能识别时请对照照片人工录入。留空表示该页无正文，不参与审查。"></textarea>
  </div>`;
}

// 引擎尝试链路：逐条说明"为什么没识别出来"
function scanAttemptHTML(a) {
  const statusText = { skipped: "跳过", error: "出错", empty: "未识别到文字", ok: "成功" };
  const cls = statusText[a.status] ? a.status : "";
  return `<div class="scan-attempt ${cls}">
    <span class="scan-attempt-name">${esc(a.label)}</span>
    <span class="scan-attempt-status">${esc(statusText[a.status] || a.status)}</span>
    <span class="scan-attempt-reason">${esc(a.reason || (a.chars != null ? `识别 ${a.chars} 字` : ""))}</span>
  </div>`;
}

// 结构变化：重建卡片 + 刷新汇总
function syncScanUI() {
  renderScanPages();
  refreshScanState();
}

// 汇总与按钮状态：几页有正文、哪几页是空的、现在能不能提交，都算清楚后一次性呈现
function refreshScanState() {
  const total = SCAN.pages.length;
  const typed = SCAN.pages.filter(scanHasText).length;
  const pending = SCAN.pages.filter((p) => p.status === "loading" || p.status === "pending").length;
  const allOk = total > 0 && typed === total && !pending;
  // 空页分两种：识别没成功（要提示重拍/补录）与用户主动留空（无需提示）
  const blanks = SCAN.pages.map((p, i) => ({ no: i + 1, p })).filter((b) => !scanHasText(b.p));
  const failedNos = blanks
    .filter((b) => b.p.status === "error" || (b.p.result && !b.p.result.ok))
    .map((b) => b.no);

  const box = document.getElementById("scanSummary");
  if (!total) {
    box.className = "scan-summary hidden";
    box.innerHTML = "";
  } else {
    box.className = "scan-summary" + (allOk ? " all-ok" : "");
    const head = [`共 ${total} 页`, `已填写 ${typed} 页`];
    if (pending) head.push(`${pending} 页识别中`);
    let html = `<div>${allOk ? "✓ " : ""}${head.join(" · ")}</div>`;
    if (blanks.length) {
      html += `<div class="scan-summary-gap">第 ${blanks.map((b) => b.no).join("、")} 页尚无正文`
        + (failedNos.length ? `（其中第 ${failedNos.join("、")} 页自动识别未成功）` : "")
        + ` → 可对照照片在该页校对框录入；确实为空白页的可留空，留空页不参与审查，`
        + `但页码会为它保留位置（「第 N 页」始终等于第 N 张照片）。</div>`;
    }
    box.innerHTML = html;
  }

  // 提交闸门：至少一页有正文，且没有还在识别中的页（避免把提交到一半的正文送去审查）
  document.getElementById("scanSubmitBtn").disabled = typed === 0 || pending > 0;
  document.getElementById("scanRetryBtn").classList.toggle(
    "hidden", SCAN.pages.filter(scanIsRetryable).length === 0);
}

// 把焦点交给指定页的校对框：识别失败时最该做的事就是让人开始录入
function focusScanText(id) {
  const ta = document.querySelector(`#scanPages textarea[data-id="${id}"]`);
  if (ta) { ta.focus(); ta.scrollIntoView({ block: "center" }); }
}

// 一批照片一次请求：后端逐页处理并返回逐页结果，页序与 targets 顺序严格对应。
// 之所以按"批"而不按"张"发请求，是因为一次拍照/选图往往就是一页一张，
// 合并成一次往返更快；而逐页重试时批里只有一张，走的是同一段代码。
async function recognizeScanPages(targets) {
  const pages = (targets || []).filter(Boolean);
  if (!pages.length) return;

  pages.forEach((p) => { p.status = "loading"; p.error = ""; });
  syncScanUI();
  setScanHint(pages.length > 1 ? `正在识别 ${pages.length} 张照片，请稍候…` : "正在识别图片文字，请稍候…", "");

  try {
    const fd = new FormData();
    pages.forEach((p) => fd.append("file", p.file));
    const r = await fetch(API + "/api/ocr/recognize", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "识别失败");
    const results = j.pages || [];
    pages.forEach((p, i) => {
      const one = results[i];
      if (!one) { p.status = "error"; p.error = "服务端未返回该页结果"; return; }
      p.result = one;
      p.status = "done";
      // 直接覆盖正文是安全的：识别只发生在"该页还没有正文"这一前提下
      // （新添加的页、以及被判定为可重试的空页），不会冲掉已校对好的内容。
      p.text = one.text || "";
    });
  } catch (e) {
    pages.forEach((p) => { p.status = "error"; p.error = e.message; });
  }

  syncScanUI();
  // 若这一批是因为"引擎/语言包不可用"而失败的，顺手强制重探一次能力条：
  // 常见情形是用户开着页面去装引擎，装完直接回来点重试 —— 此时若能力条还挂着旧的
  // "不可用"，就会自相矛盾（候选引擎明明刚装好）。重探只起一个子进程，代价可忽略。
  if (pages.some((p) => looksLikeEngineIssue(p.result && p.result.reason))) loadOcrProbe(true);
  announceScanResult(pages);
}

// 判断失败原因是不是"环境缺引擎/缺语言包"，而不是"这张照片拍得不好"。
// 这两类的出路完全不同（去装引擎 vs 重拍），前端据此决定要不要重探能力并提示用户。
function looksLikeEngineIssue(reason) {
  const s = String(reason || "");
  return ["未安装", "不可用", "缺少中文语言包", "chi_sim", "traineddata"].some((k) => s.includes(k));
}

// 把这一批的识别结果用一句话交代清楚：成功了几张、哪一张没成、接下来该干什么
function announceScanResult(pages) {
  const okPages = pages.filter((p) => p.status === "done" && p.result && p.result.ok);
  const bad = pages.filter((p) => !(p.status === "done" && p.result && p.result.ok));
  if (!bad.length) {
    const chars = okPages.reduce((n, p) => n + ((p.result.chars) || 0), 0);
    setScanHint(`${okPages.length} 张照片识别完成，共 ${chars} 字。请逐页校对有无错字后再提交审查。`, "ok");
    return;
  }
  // 降级不是死路：原因、重拍建议与出路一次说清，并把焦点交给那一页的校对框
  const first = bad[0];
  const why = first.error || (first.result && first.result.reason) || "未识别出文字";
  const how = (first.result && first.result.hint) || "";
  const no = scanPageNo(first.id);
  setScanHint(`第 ${no} 页未能自动识别：${why}${how ? " " + how : ""}`, "warn");
  focusScanText(first.id);
}

document.getElementById("scanBtn").onclick = openScanModal;
document.getElementById("scanCancelBtn").onclick = closeScanModal;
document.getElementById("scanModal").onclick = (e) => {
  if (e.target.id === "scanModal") closeScanModal();
};

// 三个入口分开绑定：
//   scanInput        拍照（capture，手机直接调起后置摄像头，连拍多次即连续续页）
//   scanInputMulti   相册多选（multiple，一次加多页）—— 与 capture 不能共用同一个 input
//   scanInputReplace 指定页重拍（点某页的「↻ 重拍」后落到这里）
const scanDrop = document.getElementById("scanDrop");
document.getElementById("scanShootBtn").onclick = (e) => {
  e.stopPropagation();
  document.getElementById("scanInput").click();
};
document.getElementById("scanPickBtn").onclick = (e) => {
  e.stopPropagation();
  document.getElementById("scanInputMulti").click();
};
// 点空白处默认走"多选"，因为整块区域更像"从相册添照片"，而拍照有专属按钮
scanDrop.onclick = () => document.getElementById("scanInputMulti").click();
scanDrop.addEventListener("dragover", (e) => {
  e.preventDefault();
  scanDrop.classList.add("dragging");
});
scanDrop.addEventListener("dragleave", () => scanDrop.classList.remove("dragging"));
scanDrop.addEventListener("drop", async (e) => {
  e.preventDefault();
  scanDrop.classList.remove("dragging");
  await addScanFiles(e.dataTransfer.files);
});

// 注意：FileList 是"活"的，清空 input.value 会同时清空它，
// 所以必须先把文件取成数组，再清空 input（否则重复选同一张会没有反应）
document.getElementById("scanInput").onchange = async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  await addScanFiles(files);
};
document.getElementById("scanInputMulti").onchange = async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  await addScanFiles(files);
};
document.getElementById("scanInputReplace").onchange = async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  const p = scanPageById(SCAN.replaceId);
  SCAN.replaceId = null;
  if (!files.length || !p) return;
  // 换图即放弃这一页原有的识别结果与正文：用户点「重拍」就是要重来
  URL.revokeObjectURL(p.url);
  p.file = files[0];
  p.url = URL.createObjectURL(p.file);
  p.result = null;
  p.text = "";
  p.error = "";
  await recognizeScanPages([p]);
};

// 页内操作（上移/下移/重拍/删除）：用事件委托，卡片重建后无需重新绑定
document.getElementById("scanPages").onclick = (e) => {
  const btn = e.target.closest("button[data-op]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const i = SCAN.pages.findIndex((p) => p.id === id);
  if (i < 0) return;
  const op = btn.dataset.op;
  if (op === "remove") {
    const [gone] = SCAN.pages.splice(i, 1);
    URL.revokeObjectURL(gone.url);
    syncScanUI();
    setScanHint(`已移除原第 ${i + 1} 页，后续页序已自动前移。`, "");
  } else if (op === "up" && i > 0) {
    [SCAN.pages[i - 1], SCAN.pages[i]] = [SCAN.pages[i], SCAN.pages[i - 1]];
    syncScanUI();
  } else if (op === "down" && i < SCAN.pages.length - 1) {
    [SCAN.pages[i + 1], SCAN.pages[i]] = [SCAN.pages[i], SCAN.pages[i + 1]];
    syncScanUI();
  } else if (op === "replace") {
    SCAN.replaceId = id;
    document.getElementById("scanInputReplace").click();
  }
};

// 逐页校对框可编辑：只刷新汇总与按钮，绝不重建卡片（重建会让光标跳出输入框）
document.getElementById("scanPages").oninput = (e) => {
  const ta = e.target.closest("textarea[data-id]");
  if (!ta) return;
  const p = scanPageById(Number(ta.dataset.id));
  if (!p) return;
  p.text = ta.value;
  refreshScanState();
};

// 重试未识别页：只重跑"没有正文且上次识别失败"的页，不会覆盖已校对的内容
document.getElementById("scanRetryBtn").onclick = async () => {
  const targets = SCAN.pages.filter(scanIsRetryable);
  if (!targets.length) { setScanHint("当前没有需要重试的页。", ""); return; }
  await recognizeScanPages(targets);
};

document.getElementById("scanSubmitBtn").onclick = async () => {
  const pages = SCAN.pages.map((p) => p.text || "");
  if (!pages.some((t) => t.trim())) {
    setScanHint("还没有任何一页填写了正文，无法提交审查。", "err");
    return;
  }
  const blankNos = pages.map((t, i) => (t.trim() ? null : i + 1)).filter(Boolean);
  if (blankNos.length &&
      !confirm(`第 ${blankNos.join("、")} 页没有正文，这几页不会参与审查；\n页码仍按照片顺序保留，其余页的页码不受影响。确定继续？`)) {
    return;
  }

  const btn = document.getElementById("scanSubmitBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  // 单页时沿用相册文件名便于追溯；多页时相机文件名（IMG_2031）没有信息量，改用统一名称
  const first = SCAN.pages[0];
  const base = SCAN.pages.length > 1
    ? "拍照识别合同"
    : ((first.file.name || "").replace(/\.[^.]+$/, "") || "拍照识别合同");

  try {
    const r = await fetch(API + "/api/ocr/submit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pages,   // 传逐页正文：页间分隔符由后端统一定义，前端不参与拼接
        contract_type: document.getElementById("scanBizType").value,
        filename: base + "（拍照识别）",
      }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "提交失败");
    closeScanModal();
    if (j.status === "blocked") {
      alert("解析受阻：" + j.error);
    } else if (j.status === "rejected") {
      alert("已拦截：识别出的正文未判定为合同类文档，未进入条款审查。\n原因：" + (j.error || ""));
    }
    location.hash = "#workbench?task=" + j.task_id;
  } catch (e) {
    setScanHint("提交失败：" + e.message, "err");
  } finally {
    btn.textContent = "提交审查";
    refreshScanState();
  }
};

// Esc 关闭拍照弹窗
document.addEventListener("keydown", (e) => {
  if (document.getElementById("scanModal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeScanModal();
});

// ---------------- 失败外发审核（钉钉） ----------------
// 链路：审查失败 → 条件门禁筛噪 → 责任人确认 → 推送钉钉群 → 留痕
//
// 前端在这条链路里只做两件事：把"当前条件是什么"和"这条为什么值得发"如实呈现，
// 以及把责任人的确认动作传下去。条件判定与消息正文一律由服务端算——
// 判定明细要可审计、正文要在入队那一刻冻结，前端重复实现只会造成前后端各说各话。
const EXPORT_STATE = { config: null, records: [], stats: {}, filter: "", test: null, audit: [] };

// 状态展示元数据（与 services/export_queue.py 的 _STATUS_LABEL 一一对应）
const EX_STATUS = {
  filtered:       { label: "条件未通过", cls: "filtered" },
  pending:        { label: "待人工审核", cls: "pending" },
  pushed:         { label: "已推送",     cls: "pushed" },
  push_failed:    { label: "推送失败",   cls: "failed" },
  human_rejected: { label: "已驳回",     cls: "rejected" },
  superseded:     { label: "已失效",     cls: "superseded" },
};
// 失败类型标签。默认值与后端 failure_gate 的取值对应，
// 载入时会被 /api/export/config 下发的 options.kinds 覆盖——
// 服务端将来新增一种失败类型，前端不改代码也能正确显示中文名。
const EX_KIND_LABEL = { blocked: "解析受阻", rejected: "非合同被拒" };
// 需要人处理的状态：这两类才出现在「待人工审核」区
const EX_OPEN = ["pending", "push_failed"];

// 审计动作的可读名。这里与 EX_KIND_LABEL 的处理方式不同——
// 审计动作是系统内部事件的编码，不参与业务判定，改由前端命名；
// 遇到未知动作时原样显示编码（而不是显示"未知"），保证新加的动作也可追溯。
const EX_ACTION_LABEL = {
  enqueue:          "进入待审",
  filtered:         "条件拦下",
  push:             "审核推送",
  retry_push:       "重试推送",
  reject:           "人工驳回",
  revoke:           "自动作废",
  condition_update: "条件变更",
  test_channel:     "通道测试",
  trim:             "容量清理",
};
// 动作的语气：对外动作（真发过/被拦）用实色，配置类动作淡处理，便于快速扫读
const EX_ACTION_TONE = {
  push: "real", retry_push: "real", reject: "real",
  filtered: "mute", revoke: "mute",
  condition_update: "cfg", test_channel: "cfg", trim: "cfg", enqueue: "queue",
};

function exHint(msg, kind) {
  const el = document.getElementById("exCondHint");
  if (!el) return;
  el.textContent = msg;
  el.className = "field-hint ex-hint " + (kind || "");
}

function exStatusPill(status) {
  const s = EX_STATUS[status] || { label: status, cls: "" };
  return `<span class="ex-status ${s.cls}">${esc(s.label)}</span>`;
}

function exSourceLabel(src) {
  return { upload: "上传文档", scan: "拍照识别" }[src] || src || "—";
}

async function loadExport() {
  try {
    const [cfg, q] = await Promise.all([
      fetch(API + "/api/export/config").then(r => r.json()),
      fetch(API + "/api/export/queue").then(r => r.json()),
    ]);
    EXPORT_STATE.config = cfg;
    EXPORT_STATE.records = q.records || [];
    EXPORT_STATE.stats = q.stats || {};
    EXPORT_STATE.audit = q.audit || [];
    // 以服务端下发的标签为准（服务端是失败类型的定义方，前端不重复定义业务含义）
    ((cfg.options || {}).kinds || []).forEach(k => { EX_KIND_LABEL[k.value] = k.label; });
    renderConditionForm();
    renderRecords();
  } catch (e) {
    exHint("加载外发配置失败：" + e.message, "err");
  }
}

// 条件表单：把当前生效条件回填到输入项
function renderConditionForm() {
  const wrap = (EXPORT_STATE.config || {}).condition;
  if (!wrap) return;
  const cond = wrap.condition || {};
  const opts = (EXPORT_STATE.config || {}).options || {};

  // 失败类型（复选）：勾选项与"是否有专属说明"都在这里
  document.getElementById("exKinds").innerHTML = (opts.kinds || []).map(k => `
    <label class="ex-check-line" title="${esc(k.hint || "")}">
      <input type="checkbox" data-exkind="${esc(k.value)}"
        ${(cond.kinds || []).includes(k.value) ? "checked" : ""} />
      <span>${esc(k.label)}</span>
    </label>`).join("");
  document.getElementById("exKindTip").textContent =
    (opts.kinds || []).map(k => `${k.label}：${k.hint}`).join("　");

  // 正文来源（复选，全不勾 = 不限）
  document.getElementById("exSources").innerHTML = (opts.sources || []).map(s => `
    <label class="ex-check-line">
      <input type="checkbox" data-exsource="${esc(s.value)}"
        ${(cond.sources || []).includes(s.value) ? "checked" : ""} />
      <span>${esc(s.label)}</span>
    </label>`).join("");

  document.getElementById("exMinRetries").value = cond.min_retries || 0;
  document.getElementById("exExtensions").value = (cond.extensions || []).join(", ");
  document.getElementById("exKeywords").value = (cond.reason_keywords || []).join(", ");
  document.getElementById("exDedupe").checked = cond.dedupe !== false;
  document.getElementById("exCondLabels").innerHTML =
    (wrap.labels || []).map(t => `<span class="ex-label-pill">${esc(t)}</span>`).join("");
}

// 读取表单 → 提交给后端规范化（分隔符兼容、非法值丢弃都在后端做，前端不做二次解释）
function readConditionForm() {
  const kinds = [...document.querySelectorAll("[data-exkind]")]
    .filter(i => i.checked).map(i => i.dataset.exkind);
  const sources = [...document.querySelectorAll("[data-exsource]")]
    .filter(i => i.checked).map(i => i.dataset.exsource);
  return {
    kinds,
    sources,
    min_retries: Number(document.getElementById("exMinRetries").value || 0),
    extensions: document.getElementById("exExtensions").value,
    reason_keywords: document.getElementById("exKeywords").value,
    dedupe: document.getElementById("exDedupe").checked,
  };
}

function renderRecords() {
  const open = EXPORT_STATE.records.filter(r => EX_OPEN.includes(r.status));
  const done = EXPORT_STATE.records.filter(r => !EX_OPEN.includes(r.status));
  document.getElementById("exPendingN").textContent = String(open.length);

  const box = document.getElementById("exPendingList");
  box.innerHTML = open.length
    ? open.map(r => exportCard(r, "open")).join("")
    : `<div class="ex-empty">当前没有待审核的失败任务。审查失败并经条件门禁筛除噪声后，相关任务会自动出现在此，等待责任人确认推送。</div>`;

  // 状态筛选 chips（带计数，便于一眼看清各类留痕有多少）
  const st = EXPORT_STATE.stats || {};
  const chips = [["", "全部", done.length]]
    .concat(["pushed", "human_rejected", "filtered", "superseded"]
      .map(k => [k, EX_STATUS[k].label, st[k] || 0]));
  document.getElementById("exStatusChips").innerHTML = chips.map(([k, label, n]) => `
    <button class="ex-chip ${EXPORT_STATE.filter === k ? "active" : ""}" data-exfilter="${k}">
      ${esc(label)}<em>${n}</em>
    </button>`).join("");

  const shown = EXPORT_STATE.filter ? done.filter(r => r.status === EXPORT_STATE.filter) : done;
  document.getElementById("exHistoryN").textContent = String(shown.length);
  document.getElementById("exHistoryList").innerHTML = shown.length
    ? shown.map(r => exportCard(r, "done")).join("")
    : `<div class="ex-empty">暂无记录。已处理与已拦截的失败任务将在此汇总留痕。</div>`;

  renderAudit();
  bindExportEvents();
}

// 审计留痕：回答"为什么变成了现在这个状态"。
// 上两块列表展示的是每条记录的结果，这里展示的是产生这些结果的动作序列——
// 外发钉钉不可撤回，事后追责与复盘只能靠这份流水。
function renderAudit() {
  const rows = EXPORT_STATE.audit || [];
  const box = document.getElementById("exAuditList");
  if (!box) return;
  document.getElementById("exAuditN").textContent = String(rows.length);
  if (!rows.length) {
    box.innerHTML = `<div class="ex-empty">暂无审计记录。所有外发动作的判定与确认流水将在此留存，便于事后追溯。</div>`;
    return;
  }
  box.innerHTML = rows.map(a => {
    const act = a.action || "";
    const label = EX_ACTION_LABEL[act] || act || "—";
    const tone = EX_ACTION_TONE[act] || "";
    return `<div class="ex-audit-row">
      <span class="ex-audit-ts">${esc(a.ts || "")}</span>
      <span class="ex-audit-act ${esc(tone)}">${esc(label)}</span>
      <span class="ex-audit-op">${esc(a.operator || "system")}</span>
      <span class="ex-audit-detail" title="${esc(a.detail || "")}">${esc(a.detail || "")}</span>
    </div>`;
  }).join("");
}

// 单条外发记录卡片。
// mode=open 是可操作态（确认推送/驳回/重试），mode=done 是只读留痕。
function exportCard(rec, mode) {
  const canAct = mode === "open";
  const retryable = rec.status === "push_failed";
  const kind = EX_KIND_LABEL[rec.failure_kind] || rec.failure_kind;

  // 逐条条件判定：让审核人看到"为什么这条值得发"，而不是只给个通过/不通过
  const gateRows = ((rec.gate || {}).items || []).map(it => `
    <div class="ex-gate-row ${it.skipped ? "skip" : it.passed ? "ok" : "bad"}">
      <span class="ex-gate-mark">${it.skipped ? "·" : it.passed ? "✓" : "✗"}</span>
      <span class="ex-gate-label">${esc(it.label)}</span>
      <span class="ex-gate-exp">${esc(it.expected)}</span>
      <span class="ex-gate-act">${esc(it.actual)}</span>
    </div>`).join("");

  const push = rec.push;
  const pushLine = push
    ? `<div class="ex-push ${push.ok ? "ok" : "bad"}">
         ${push.ok ? "✓ 推送成功" : "✗ 推送失败"}（${esc(push.mode === "real" ? "真实" : "模拟")}，
         ${esc(push.pushed_at || "")}）${push.detail ? " —— " + esc(push.detail) : ""}
       </div>`
    : "";

  const ops = canAct ? `
    <div class="ex-ops">
      <input class="ex-note" data-exnote="${esc(rec.record_id)}"
        placeholder="审核意见（驳回时必填理由）" value="${esc(rec.review_note || "")}" />
      <button class="btn primary" data-exact="approve" data-exid="${esc(rec.record_id)}">
        ${retryable ? "↻ 重试推送" : "确认推送"}</button>
      <button class="btn" data-exact="reject" data-exid="${esc(rec.record_id)}">驳回</button>
      <a class="ex-link" href="#workbench?task=${esc(rec.task_id)}">查看任务</a>
    </div>` : `
    <div class="ex-ops">
      ${rec.reviewed_by ? `<span class="ex-rev">审核：${esc(rec.reviewed_by)}</span>` : ""}
      ${rec.review_note ? `<span class="ex-note-text">${esc(rec.review_note)}</span>` : ""}
      <a class="ex-link" href="#workbench?task=${esc(rec.task_id)}">查看任务</a>
    </div>`;

  return `
  <div class="ex-item ${EX_STATUS[rec.status] ? EX_STATUS[rec.status].cls : ""}">
    <div class="ex-item-head">
      <span class="ex-kind">${esc(kind)}</span>
      <span class="ex-item-name">${esc(rec.filename)}</span>
      <span class="ex-item-meta">${esc(rec.contract_type)} · ${esc(exSourceLabel(rec.source))}
        · 重试 ${rec.retries} 次 · ${esc(rec.created_at)}</span>
      ${exStatusPill(rec.status)}
    </div>
    <div class="ex-item-reason">失败原因：${esc(rec.reason || "（无）")}</div>
    <div class="ex-gate">
      <div class="ex-gate-sum">${esc((rec.gate || {}).summary || "")}</div>
      ${gateRows}
    </div>
    ${pushLine}
    <details class="ex-detail">
      <summary>预览将推送的消息（入队时已冻结，与实际发出内容完全一致）</summary>
      <pre class="ex-msg">${esc(rec.message || "")}</pre>
    </details>
    ${ops}
  </div>`;
}

function bindExportEvents() {
  const operator = (session && session.display_name) || "";

  document.querySelectorAll("[data-exfilter]").forEach(btn => {
    btn.onclick = () => {
      EXPORT_STATE.filter = btn.dataset.exfilter;
      renderRecords();
    };
  });

  document.querySelectorAll("[data-exact]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.exid;
      const noteEl = document.querySelector(`[data-exnote="${id}"]`);
      const note = (noteEl && noteEl.value || "").trim();
      const isReject = btn.dataset.exact === "reject";
      const rec = EXPORT_STATE.records.find(r => r.record_id === id) || {};

      if (isReject && !note) {
        // 驳回必须留理由：否则队列被无声清空后，"为什么没发出去"就查不到了
        exHint("驳回必须填写理由（用于回溯「为何该失败未外发」）", "err");
        if (noteEl) noteEl.focus();
        return;
      }
      if (!isReject && rec.status === "pending") {
        // 对外动作，且发出去就收不回来，所以人工再确认一次
        if (!confirm(`确认将这条失败提醒推送到钉钉群？\n\n任务：${rec.filename || ""}\n类型：${EX_KIND_LABEL[rec.failure_kind] || rec.failure_kind}`)) return;
      }

      const action = isReject ? "reject" : (rec.status === "push_failed" ? "retry" : "approve");
      btn.disabled = true;
      try {
        const r = await fetch(`${API}/api/export/${id}/${action}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operator, note }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "操作失败");
        // 结果按真实推送成败分别提示：模拟模式下"成功"不等于对方收到了
        if (action === "reject") exHint("已驳回，理由已留痕。", "ok");
        else if (j.status === "pushed") {
          exHint(j.push && j.push.mode === "real" ? "已推送到钉钉群。" : "已记录为推送成功（当前为模拟模式，未真正发出）。", "ok");
        } else {
          exHint("推送失败：" + ((j.push && j.push.detail) || ""), "err");
        }
        await refreshExportQueue();
      } catch (e) {
        exHint("操作失败：" + e.message, "err");
      } finally {
        btn.disabled = false;
      }
    };
  });
}

// 只刷新队列（不动条件表单），避免保存一次条件就把人正在填的输入冲掉
async function refreshExportQueue() {
  const q = await (await fetch(API + "/api/export/queue")).json();
  EXPORT_STATE.records = q.records || [];
  EXPORT_STATE.stats = q.stats || {};
  EXPORT_STATE.audit = q.audit || [];
  renderRecords();
}

document.getElementById("exScanBtn").onclick = async () => {
  const btn = document.getElementById("exScanBtn");
  btn.disabled = true;
  try {
    const r = await fetch(API + "/api/export/scan", { method: "POST" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "扫描失败");
    exHint(`扫描 ${j.scanned} 个任务：新增待审核 ${j.queued} 条，`
      + `条件拦下 ${j.filtered} 条，队列内已有 ${j.already_in_queue} 条，`
      + `非失败任务 ${j.non_failure} 个。`, j.queued ? "ok" : "");
    await refreshExportQueue();
  } catch (e) {
    exHint("扫描失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
};

document.getElementById("exTestBtn").onclick = async () => {
  const btn = document.getElementById("exTestBtn");
  btn.disabled = true;
  try {
    const r = await fetch(API + "/api/export/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator: (session && session.display_name) || "" }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "测试失败");
    // 通道状态卡已按需求移除，测试结果改走条件卡提示条，反馈不丢
    exHint(`通道测试${j.ok ? "通过" : "失败"}（${j.mode === "real" ? "已真实发送" : "模拟发送"}）${j.detail ? "：" + j.detail : ""}`,
      j.ok ? "ok" : "err");
  } catch (e) {
    exHint("通道测试失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
};

document.getElementById("exCondSave").onclick = async () => {
  try {
    const r = await fetch(API + "/api/export/condition", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readConditionForm()),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "保存失败");
    EXPORT_STATE.config.condition = j;
    renderConditionForm();
    exHint("条件已保存。改动只影响后续扫描，已有记录的判定明细保持不变。", "ok");
  } catch (e) {
    exHint("保存失败：" + e.message, "err");
  }
};

// 恢复默认：走同一条保存接口，只是把出厂默认值填进表单再提交，
// 保证"界面上看到的"和"实际生效的"一定一致
document.getElementById("exCondReset").onclick = async () => {
  const def = ((EXPORT_STATE.config || {}).condition || {}).defaults;
  if (!def) return;
  if (!confirm("恢复为出厂默认条件（只纳入「解析受阻」）？")) return;
  try {
    const r = await fetch(API + "/api/export/condition", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(def),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "恢复失败");
    EXPORT_STATE.config.condition = j;
    renderConditionForm();
    exHint("已恢复默认条件。", "ok");
  } catch (e) {
    exHint("恢复失败：" + e.message, "err");
  }
};

// 启动
route();
