// 文件位置: contract-review/static/login.js
// 功能说明: 登录页逻辑。演示账号点击自动填充、密码显隐、提交登录并写入本地会话。
//          登录成功后跳转主应用；未登录访问主应用会被守卫重定向回本页。
const API = "";
const SESSION_KEY = "cr_session";

// 演示账号（与后端 DEMO_ACCOUNTS 保持一致，仅用于前端列表展示与一键填充）
const DEMO_ACCOUNTS = [
  { account: "chenjingzhi@gmail.com",  password: "chenjingzhi",  role: "平台管理员",   name: "陈敬之" },
  { account: "zhoumingyuan@gmail.com", password: "zhoumingyuan", role: "法务合规经理", name: "周明远" },
  { account: "lisiqi@gmail.com",       password: "lisiqi",       role: "采购业务经办", name: "李思齐" },
];

// 渲染演示账号列表（点击行自动填充账号密码）
function renderDemo() {
  const box = document.getElementById("demoList");
  box.innerHTML = DEMO_ACCOUNTS.map(a => `
    <div class="demo-row" data-account="${a.account}">
      <div>
        <span class="demo-role">${a.role}</span>
        <span class="demo-name">${a.name}</span>
      </div>
      <div class="demo-cred">${a.account}<br />${a.password}</div>
    </div>`).join("");
  box.querySelectorAll(".demo-row").forEach(row => {
    row.onclick = () => {
      const hit = DEMO_ACCOUNTS.find(a => a.account === row.dataset.account);
      document.getElementById("account").value = hit.account;
      document.getElementById("password").value = hit.password;
      document.getElementById("loginError").textContent = "";
    };
  });
}

// 密码显隐切换
document.getElementById("togglePwd").onclick = () => {
  const p = document.getElementById("password");
  p.type = p.type === "password" ? "text" : "password";
};

// 提交登录
document.getElementById("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  const account = document.getElementById("account").value.trim();
  const password = document.getElementById("password").value;
  const errBox = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");

  errBox.textContent = "";
  if (!account || !password) {
    errBox.textContent = "请输入账号与密码";
    return;
  }
  btn.disabled = true;
  btn.textContent = "登录中…";
  try {
    const r = await fetch(API + "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, password }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      errBox.textContent = j.detail || "登录失败，请检查账号密码";
      return;
    }
    const data = await r.json();
    localStorage.setItem(SESSION_KEY, JSON.stringify(data));
    // 支持 ?next=/risk 形式的回跳（风控子系统从登录页返回原位）
    const params = new URLSearchParams(location.search);
    const next = params.get("next");
    location.replace(next && next.startsWith("/") ? next : "/");
  } catch (err) {
    errBox.textContent = "网络异常，请稍后重试";
  } finally {
    btn.disabled = false;
    btn.textContent = "登 录";
  }
};

// 已登录则直接进入主应用，避免重复登录
(function autoSkip() {
  try {
    if (JSON.parse(localStorage.getItem(SESSION_KEY) || "null")) location.replace("/");
  } catch (e) { /* 忽略 */ }
})();

renderDemo();
