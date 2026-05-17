// 文件位置: contract-review/tests/render_check.mjs
// 功能说明: 规则页「卡片网格」渲染自检。
//           把 static/app.js 在最小 DOM stub 里执行一遍，用 /api/rules/config 的真实数据
//           调用 cardHtml() 生成 HTML，逐项断言结构与文案。
//           之所以不用浏览器截图验证：本机无 agent-browser，而这套前端是纯字符串模板，
//           直接在 Node 里跑渲染函数就能覆盖"字段名写错 / 转义漏了 / 标签不闭合"这类真实故障。
//           运行：node tests/render_check.mjs   （需服务已在 18090 运行）
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const BASE = "http://127.0.0.1:18090";

// [适配 auth_guard] 项目现已对 /api/* 强制校验 Bearer 令牌（sakura_base.PathAuthMiddleware）。
// 自检作为一个"字段契约红线"测试，需要在真实鉴权环境下跑通，因此先以演示账号登录拿令牌，
// 再把它同时喂给：① app.js 内部 fetch（经 localStorage 会话）、② 本测试直接发的业务请求。
// 不采用"把业务接口设为公开"的做法——那会回退到此前 P0 级越权漏洞。
const _login = await fetch(`${BASE}/api/login`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ account: "zhoumingyuan@gmail.com", password: "zhoumingyuan" }),
});
if (!_login.ok) throw new Error("自检登录失败: HTTP " + _login.status);
const TOKEN = (await _login.json()).token;
// 本测试直接发起的业务请求统一带上令牌（app.js 内部的 fetch 由下方 sessionStub 经注入逻辑补齐）
const authFetch = (url, opt = {}) =>
  fetch(url, { ...opt, headers: { ...(opt.headers || {}), Authorization: "Bearer " + TOKEN } });

// ---------------- 最小 DOM stub ----------------
// 只实现 app.js 顶层会碰到的接口；不需要真实布局能力。
const store = new Map();
function mkEl(id = "") {
  const el = {
    id, innerHTML: "", textContent: "", className: "", value: "", placeholder: "",
    style: {}, checked: false, disabled: false, dataset: {}, src: "",
    // classList 真实记录类名（而不是 no-op）：弹窗开关就是"增删 hidden"，
    // 用空实现替身根本测不出"点了退出到底有没有弹出确认框"。
    _cls: new Set(),
    classList: {
      add: (c) => el._cls.add(c),
      remove: (c) => el._cls.delete(c),
      toggle: (c, on) => (on === undefined
        ? (el._cls.has(c) ? el._cls.delete(c) : el._cls.add(c))
        : (on ? el._cls.add(c) : el._cls.delete(c))),
      contains: (c) => el._cls.has(c),
    },
    closest: () => null, querySelector: () => mkEl(), querySelectorAll: () => [],
    focus() {}, click() {}, addEventListener() {}, removeAttribute() {}, setAttribute() {},
    appendChild() {}, remove() {},
  };
  return el;
}
const documentStub = {
  getElementById: (id) => {
    if (!store.has(id)) store.set(id, mkEl(id));
    return store.get(id);
  },
  querySelector: () => mkEl(),
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => mkEl(),
};

// ---------------- 执行 app.js，取回待测函数 ----------------
// app.js 顶层会读会话并可能跳转登录页，所以 localStorage / location 都要给可用替身，
// 否则模块加载阶段就会抛错，测不到后面的渲染函数。
// 退出确认要靠"会话被清掉 / 页面被跳转"这两个副作用来断言，所以它们必须可观测
const removedKeys = [];
const nav = { to: null };
const sessionStub = JSON.stringify({ role: "legal", display_name: "自检", role_name: "法务合规经理", token: TOKEN });
const src = fs.readFileSync(path.join(ROOT, "static/app.js"), "utf8");
const build = new Function(
  "document", "window", "navigator", "location", "alert", "URL", "Blob",
  "setTimeout", "clearTimeout", "localStorage", "fetch",
  `${src}
    return { cardHtml, rcKv, scopeText, lvPill, esc, RL_STATE, LV_TEXT,
             openLogoutModal, closeLogoutModal,
             exportCard, renderConditionForm, renderRecords, renderAudit,
             EX_STATUS, EX_OPEN, EX_KIND_LABEL, EX_ACTION_LABEL, EX_ACTION_TONE,
             EXPORT_STATE, exStatusPill };`,
);
// app.js 里 API = ""，请求走相对路径；Node 里相对路径无 base，
// 所以这里把 fetch 包一层补全成绝对地址，让顶层的 loadTasks() 也能正常跑完。
// 同时补上 Bearer 令牌：app.js 在 Node 桩里调用的是"裸 fetch"（即本桩参数），
// 不会走真正的 window.fetch 包裹逻辑，所以令牌必须在这里直接注入，否则会被 auth_guard 拒掉。
const fetchStub = (u, opt = {}) => {
  const opt2 = { ...opt, headers: { ...(opt.headers || {}), Authorization: "Bearer " + TOKEN } };
  return fetch(typeof u === "string" && u.startsWith("http") ? u : BASE + u, opt2);
};
const app = build(
  documentStub, { addEventListener() {} }, { clipboard: { writeText: async () => {} } },
  { replace: (u) => { nav.to = u; }, hash: "" },
  () => {}, { createObjectURL: () => "" }, function () {}, () => 0, () => {},
  { getItem: () => sessionStub, setItem() {}, removeItem: (k) => removedKeys.push(k) },
  fetchStub,
);

// ---------------- 取真实数据 ----------------
const data = await (await authFetch(`${BASE}/api/rules/config`)).json();
app.RL_STATE.data = data;

let fail = 0;
const check = (name, ok, extra = "") => {
  console.log(`  ${ok ? "✓" : "✗"} ${name}${extra ? "  " + extra : ""}`);
  if (!ok) fail++;
};

// 逐项断言三个标签页的卡片渲染
const cases = [
  ["rules", data.rules, "RL_LEVEL"],
  ["clauses", data.clauses, null],
  ["quals", data.qualifications, null],
];

for (const [tab, items, _x] of cases) {
  console.log(`\n=== ${tab} 卡片渲染（共 ${items.length} 条）===`);
  let undefinedCnt = 0, unbalanced = 0, emptyName = 0, noKey = 0;
  const sample = [];
  for (const it of items) {
    for (const open of [false, true]) {
      const h = app.cardHtml(tab, it, open);
      if (/undefined|NaN|\[object Object\]/.test(h)) undefinedCnt++;
      const o = (h.match(/<article\b/g) || []).length, c = (h.match(/<\/article>/g) || []).length;
      if (o !== 1 || c !== 1) unbalanced++;
      if (!/data-key="[^"]+"/.test(h)) noKey++;
      if (/rl-rc-name">\s*</.test(h)) emptyName++;
    }
    if (sample.length < 2) sample.push(app.cardHtml(tab, it, false));
  }
  check(`${tab}: 无 undefined/NaN/[object Object]`, undefinedCnt === 0, `(${undefinedCnt} 处异常)`);
  check(`${tab}: article 标签闭合且唯一`, unbalanced === 0, `(${unbalanced} 条异常)`);
  check(`${tab}: 每卡都有 data-key`, noKey === 0);
  check(`${tab}: 卡片名称非空`, emptyName === 0);
  console.log(`  样例第 1 张（${tab}，折叠态）:`);
  console.log(sample[0].split("\n").map(l => "    " + l.trim()).join("\n"));
}

// ---------------- 字段覆盖：模板引用的字段必须都在 API 契约里 ----------------
console.log("\n=== 模板字段 vs API 契约 ===");
const contract = {
  rules: new Set(Object.keys(data.rules[0])),
  clauses: new Set(Object.keys(data.clauses[0])),
  quals: new Set(Object.keys(data.qualifications[0])),
};
// 从 cardHtml 源码里抓 item.xxx / item["xxx"] 的字段名，按 tab 归类
const body = src.slice(src.indexOf("function cardHtml("), src.indexOf("function renderGrid()"));
const refs = [...body.matchAll(/item\.([a-z_]+)/g)].map(m => m[1]);
const uniq = [...new Set(refs)];
const allFields = new Set([...contract.rules, ...contract.clauses, ...contract.quals]);
const unknown = uniq.filter(f => !allFields.has(f));
check("cardHtml 引用的字段均在接口返回中", unknown.length === 0, unknown.length ? "未定义: " + unknown.join(", ") : `(${uniq.length} 个字段)`);

// ---------------- 转义安全检查 ----------------
console.log("\n=== XSS 转义 ===");
const evil = { id: 'R-1"><script>x</script>', name: '<img src=x onerror=alert(1)>',
  category: '"><b>', scope: ['<script>'], level: 'high', match_mode: '<i>',
  enabled: true, reason: '"><svg>', legal_basis: '<b>', type: 'x', recommend_key: '"><u>' };
const h = app.cardHtml("rules", evil, true);
check("尖括号被转义，未生成真实标签", !/<script|<img|<svg|<b>|<i>|<u>/.test(h));
check("& 被转义", h.includes("&lt;") && h.includes("&gt;"));
// 属性值中的引号必须转成实体：否则 data-key 会被提前闭合，点击卡片时找不到对应键
const dk = h.match(/data-key="([^"]*)"/);
check("属性值内引号已实体化（data-key 不会被截断）", !!dk && dk[1].includes("&quot;"),
  dk ? `data-key="${dk[1].slice(0, 26)}"` : "未取到 data-key");
// 开关的 data-rule 走同一条转义路径，一并验
const dr = h.match(/data-rule="([^"]*)"/);
check("data-rule 同样被实体化", !!dr && dr[1].includes("&quot;"));

// ---------------- 退出登录确认 ----------------
// 这里要验的核心不是"弹窗长得对"，而是"没确认就绝不动会话"——
// 那才是加确认框的全部意义所在。
console.log("\n=== 退出登录确认 ===");
const el = (id) => documentStub.getElementById(id);
const modal = el("logoutModal");
app.openLogoutModal();
check("点击退出后弹出确认框", !modal.classList.contains("hidden"));
check("确认框里显示的是当前账号",
  el("logoutName").textContent === "自检" && el("logoutRole").textContent === "法务合规经理",
  `${el("logoutName").textContent} / ${el("logoutRole").textContent}`);
check("头像取姓名首字", el("logoutAvatar").textContent === "自");
check("仅弹确认时不动会话（未清缓存、未跳转）",
  removedKeys.length === 0 && nav.to === null,
  `removed=${JSON.stringify(removedKeys)} nav=${nav.to}`);
app.closeLogoutModal();
check("取消后确认框关闭", modal.classList.contains("hidden"));
check("取消后会话仍在", removedKeys.length === 0 && nav.to === null);
// 再走一次真正的确认：只有这一步才允许清会话并跳回登录页
el("logoutOkBtn").onclick();
check("确认后才清掉会话", removedKeys.includes("cr_session"), JSON.stringify(removedKeys));
check("确认后跳回登录页", nav.to === "/login", `-> ${nav.to}`);

// ---------------- 失败外发审核页 ----------------
// 这一段的价值在于"真的把渲染函数跑起来"：本轮就靠它抓到 EX_KIND_LABEL
// 被引用却从未定义（用了但没声明的标识符，只有执行到那一行才炸）。
console.log("\n=== 失败外发审核：先造一条真实失败任务 ===");
const fd = new FormData();
fd.append("file", new Blob([Buffer.from("%PDF-1.4\n" + "\u0000\u0001 not a pdf ".repeat(40))],
  { type: "application/pdf" }), "自检_无法解析.pdf");
fd.append("contract_type", "采购");
fd.append("auto_run", "true");
await authFetch(`${BASE}/api/tasks/upload`, { method: "POST", body: fd });
await authFetch(`${BASE}/api/export/scan`, { method: "POST" });

const exCfg = await (await authFetch(`${BASE}/api/export/config`)).json();
const exQ = await (await authFetch(`${BASE}/api/export/queue`)).json();
app.EXPORT_STATE.config = exCfg;
app.EXPORT_STATE.records = exQ.records || [];
app.EXPORT_STATE.stats = exQ.stats || {};
const pending = (exQ.records || []).filter(r => r.status === "pending");
check("失败任务按默认条件进入待审队列", pending.length >= 1,
  `待审 ${pending.length} / 记录 ${exQ.records.length} 条`);

// 字段契约：exportCard 里 rec.xxx 引用的字段必须真实存在于接口返回中，
// 否则就是"字段改名了但模板没跟着改"，界面上会静默变成空白
console.log("\n=== 外发卡片字段契约 ===");
const cardSrc = src.slice(src.indexOf("function exportCard("), src.indexOf("function bindExportEvents()"));
const recRefs = [...new Set([...cardSrc.matchAll(/\brec\.([a-z_]+)/g)].map(m => m[1]))];
const recKeys = new Set(Object.keys(pending[0] || (exQ.records || [])[0] || {}));
const missFields = recRefs.filter(f => !recKeys.has(f));
check("exportCard 引用的字段都在接口返回中", missFields.length === 0,
  missFields.length ? "未定义: " + missFields.join(", ") : `(${recRefs.length} 个字段)`);

console.log("\n=== 失败类型与状态元数据覆盖 ===");
const kinds = (exCfg.options || {}).kinds || [];
const unmappedKinds = kinds.filter(k => !app.EX_KIND_LABEL[k.value]);
check("服务端每种失败类型都有中文标签", unmappedKinds.length === 0,
  unmappedKinds.length ? "缺: " + unmappedKinds.map(k => k.value).join(", ") : `${kinds.length} 种`);
// 页面绝不能出现原始英文状态码：接口返回的每个状态都必须在 EX_STATUS 里有中文元数据
const rawStatuses = [...new Set((exQ.records || []).map(r => r.status))].filter(s => !app.EX_STATUS[s]);
check("接口返回的状态都有展示元数据", rawStatuses.length === 0, rawStatuses.join(", "));

console.log("\n=== 待审卡片渲染 ===");
if (pending[0]) {
  const rec = pending[0];
  const h = app.exportCard(rec, "open");
  check("卡片无 undefined/NaN/[object Object]", !/undefined|NaN|\[object Object\]/.test(h));
  check("有「确认推送」与「驳回」按钮",
    /data-exact="approve"/.test(h) && /data-exact="reject"/.test(h));
  check("展示逐条条件判定明细（不是只给个通过/不通过）",
    (h.match(/ex-gate-row/g) || []).length >= 1,
    `${(h.match(/ex-gate-row/g) || []).length} 条`);
  check("展示入队时冻结的消息正文",
    /<pre class="ex-msg">/.test(h) && /合同审查/.test(h));
  check("失败类型显示中文名而非原始 kind 值",
    h.includes(app.EX_KIND_LABEL[rec.failure_kind] || "?"),
    `${rec.failure_kind} -> ${app.EX_KIND_LABEL[rec.failure_kind]}`);

  const doneRec = { ...rec, status: "pushed", reviewed_by: "自检", review_note: "已确认",
    push: { ok: true, mode: "simulated", pushed_at: "2026-09-19 10:00:00", detail: "模拟发送" } };
  const hp = app.exportCard(doneRec, "done");
  check("已推送卡片为只读（无操作按钮）", !/data-exact=/.test(hp));
  check("已推送卡片写明推送结果与模式", /推送成功/.test(hp) && /模拟/.test(hp));
  const hf = app.exportCard({ ...rec, status: "push_failed" }, "open");
  check("推送失败卡片按钮文案为「重试推送」", /↻ 重试推送/.test(hf));
  check("驳回理由输入框存在（驳回必须留痕）", /data-exnote="/.test(h));
}

console.log("\n=== 列表分区渲染 ===");
app.renderRecords();
check("待审数量与数据一致",
  documentStub.getElementById("exPendingN").textContent === String(pending.length),
  `${documentStub.getElementById("exPendingN").textContent} vs ${pending.length}`);
check("有数据时待审区渲染出卡片",
  /ex-item/.test(documentStub.getElementById("exPendingList").innerHTML));
// 空态要给引导文案，否则用户看到空白不知道"是没失败还是坏了"
app.EXPORT_STATE.records = [];
app.renderRecords();
check("无记录时待审区给出引导文案",
  /没有待审核的失败任务/.test(documentStub.getElementById("exPendingList").innerHTML));
check("无记录时留痕区显示「暂无记录」",
  /暂无记录/.test(documentStub.getElementById("exHistoryList").innerHTML));
app.EXPORT_STATE.records = exQ.records || [];

console.log("\n=== 外发卡片 XSS 转义 ===");
const evilRec = {
  record_id: 'X-1"><script>x</script>', task_id: 'T"><b>', filename: '<img src=x onerror=alert(1)>',
  contract_type: '"><i>', source: "upload", failure_kind: "blocked", status: "pending",
  reason: '"><svg>', retries: 1, created_at: '"><u>', reviewed_by: '"><em>', review_note: '"><strong>',
  message: "<script>steal()</script>",
  gate: { summary: '"><sup>',
    items: [{ label: "<b>", expected: "<i>", actual: "<u>", passed: true, skipped: false }] },
  push: { ok: false, mode: "real", pushed_at: '"><q>', detail: "<mark>" },
};
const eh = app.exportCard(evilRec, "open");
check("尖括号被实体化，未生成真实标签",
  !/<script|<img|<svg|<b>|<i>|<u>|<em>|<strong>|<mark>|<sup>|<q>/.test(eh));
check("& 被转义", eh.includes("&lt;") && eh.includes("&gt;"));
// data-exid 决定"点确认推送是对哪条记录操作"，被提前闭合就会操作错记录
const exIdAttr = eh.match(/data-exid="([^"]*)"/);
check("data-exid 内引号已实体化", !!exIdAttr && exIdAttr[1].includes("&quot;"),
  exIdAttr ? `data-exid="${exIdAttr[1].slice(0, 24)}"` : "未取到 data-exid");
const exNoteAttr = eh.match(/data-exnote="([^"]*)"/);
check("data-exnote 同样已实体化", !!exNoteAttr && exNoteAttr[1].includes("&quot;"));

console.log("\n=== 审计留痕 ===");
// 后端 _log() 会写 9 种 action；前端每种都必须有中文标签，否则界面上出现英文动作名。
// 这条断言的意义是"后端新增 action 时前端不会静默漏翻"——缺一个就红。
const ACTIONS = ["enqueue", "filtered", "push", "retry_push", "reject",
                 "revoke", "condition_update", "test_channel", "trim"];
const noLabel = ACTIONS.filter(a => typeof app.EX_ACTION_LABEL[a] !== "string" || !app.EX_ACTION_LABEL[a]);
check("后端 9 种审计动作全部有中文标签", noLabel.length === 0, noLabel.join(",") || "无缺失");
// 标签值必须真是中文，防止把 key 名抄成 value（"push: push" 这种等于没翻）
const notCn = ACTIONS.filter(a => !/[\u4e00-\u9fff]/.test(app.EX_ACTION_LABEL[a] || ""));
check("标签值确为中文而非动作名本身", notCn.length === 0, notCn.join(",") || "无异常");

app.EXPORT_STATE.audit = [
  { action: "enqueue", target: "R-1", operator: "系统", detail: "解析受阻，条件通过后入队", ts: "2026-09-19 09:00:00" },
  { action: "revoke", target: "R-1", operator: "系统", detail: "任务重新审查通过，该记录自动作废", ts: "2026-09-19 09:05:00" },
];
app.renderAudit();
check("审计条数与数据一致",
  documentStub.getElementById("exAuditN").textContent === "2",
  documentStub.getElementById("exAuditN").textContent);
const auditHtml = documentStub.getElementById("exAuditList").innerHTML;
check("渲染出审计行", (auditHtml.match(/ex-audit-row/g) || []).length === 2);
check("动作显示为中文标签（未漏翻）",
  auditHtml.includes("进入待审") && auditHtml.includes("自动作废"));
check("动作原始英文名不出现在界面上",
  !/>enqueue</.test(auditHtml) && !/>revoke</.test(auditHtml));
check("操作人留痕（含系统）", auditHtml.includes("系统"));
check("详情文本可见", auditHtml.includes("条件通过后入队"));

app.EXPORT_STATE.audit = [];
app.renderAudit();
check("无审计时给出说明文案",
  /暂无审计记录/.test(documentStub.getElementById("exAuditList").innerHTML));
check("无审计时计数归零", documentStub.getElementById("exAuditN").textContent === "0");

// 审计行含 title 属性（长详情靠悬停补全），属性内引号若不转义会被提前闭合
app.EXPORT_STATE.audit = [{
  action: 'x"><script>y</script>', operator: '<img src=x onerror=alert(1)>',
  detail: '"><svg>', ts: '"><i>', target: '"><u>',
}];
app.renderAudit();
const auditEvil = documentStub.getElementById("exAuditList").innerHTML;
check("恶意审计字段不生成真实标签",
  !/<script|<img|<svg|<i>|<u>/.test(auditEvil));
check("title 属性内引号已实体化", auditEvil.includes("&quot;"));
check("未知 action 退化为原值展示（不显示 undefined）",
  !auditEvil.includes("undefined"));
app.EXPORT_STATE.audit = [];

console.log("\n" + "=".repeat(62));
if (fail) { console.log(`渲染自检：失败 ${fail} 项`); process.exit(1); }
console.log("渲染自检：全部通过");
