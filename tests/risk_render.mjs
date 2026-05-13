// 文件位置: contract-review/tests/risk_render.mjs
// 功能说明: 电商风控前端渲染与接口契约自检（需先启动服务，连接 http://127.0.0.1:18090）。
//          校验页面可访问、静态脚本含 XSS 转义、各 /api/risk/* 接口返回契约正确，
//          以及仿真接口能给出合法决策。与 contract-review 的 render_check.mjs 同构。
const BASE = process.env.BASE || "http://127.0.0.1:18090";

let PASS = 0, FAIL = 0;
const ok = (n, c) => { if (c) { PASS++; console.log(`  ✅ ${n}`); } else { FAIL++; console.log(`  ❌ ${n}`); } };

async function get(path) {
  const r = await fetch(BASE + path);
  return { status: r.status, body: r.status === 200 ? await r.text() : "" };
}
async function getJson(path) {
  const r = await fetch(BASE + path);
  return { status: r.status, json: r.status === 200 ? await r.json() : null };
}
async function postJson(path, body) {
  const r = await fetch(BASE + path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : "{}",
  });
  return { status: r.status, json: r.status === 200 ? await r.json() : null };
}

async function main() {
  console.log("【页面与静态资源】");
  const page = await get("/risk");
  ok("风控页可访问(200)", page.status === 200);
  ok("页面含侧边导航与标题", page.body.includes("rk-side") && page.body.includes("电商业务风控中心"));
  ok("页面挂载 risk.js", page.body.includes("/static/risk.js"));
  const js = await get("/static/risk.js");
  ok("risk.js 可访问(200)", js.status === 200);
  ok("risk.js 含 XSS 转义函数 escapeHtml", js.body.includes("escapeHtml"));

  console.log("【态势大盘】");
  const dash = await getJson("/api/risk/dashboard");
  ok("dashboard 200", dash.status === 200);
  ok("dashboard 含 total/intercept_rate/top_rules",
     dash.json && "total" in dash.json && "intercept_rate" in dash.json && Array.isArray(dash.json.top_rules));

  console.log("【示例事件接入】");
  const load = await postJson("/api/risk/samples/load");
  ok("samples/load 200", load.status === 200);
  ok("samples/load 返回 accepted>0", load.json && load.json.accepted > 0);

  console.log("【案件 / 规则 / 名单】");
  const cases = await getJson("/api/risk/cases");
  ok("cases 200 且为数组", cases.status === 200 && Array.isArray(cases.json));
  const rules = await getJson("/api/risk/rules");
  ok("rules 200 且含 rules/facets", rules.status === 200 && Array.isArray(rules.json.rules) && Array.isArray(rules.json.facets));
  const lists = await getJson("/api/risk/lists");
  ok("lists 200 且含 black/white", lists.status === 200 && Array.isArray(lists.json.black) && Array.isArray(lists.json.white));

  console.log("【仿真回溯】");
  const sim = await postJson("/api/risk/simulate", { event: {
    event_id: "E-TEST-XSS", event_type: "marketing", user_id: "U<script>alert(1)</script>",
    timestamp: "2026-09-21 12:00:00",
    device: { device_id: "DEV-T", ip: "9.9.9.9", is_proxy: true },
    payload: { activity_id: "C", activity_type: "领券" },
  }});
  ok("simulate 200", sim.status === 200);
  ok("simulate 返回决策动作合法", sim.json && ["pass", "review", "block"].includes(sim.json.decision.action));
  ok("simulate 含逐条规则命中明细", sim.json && Array.isArray(sim.json.decision.hits));

  console.log("【判定历史】");
  const hist = await getJson("/api/risk/history");
  ok("history 200 且为数组", hist.status === 200 && Array.isArray(hist.json));

  console.log(`\n结果：通过 ${PASS} / 失败 ${FAIL}`);
  process.exit(FAIL ? 1 : 0);
}
main().catch(e => { console.error("运行异常:", e); process.exit(1); });
