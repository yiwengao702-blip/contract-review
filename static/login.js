// 文件位置: contract-review/static/login.js
// 功能说明: 登录 / 注册页逻辑。同一张卡片支持「账号登录」与「自助注册」两个视图，
//          登录成功或注册成功均写入本地会话并进入主应用；未登录访问主应用会被守卫重定向回本页。
const API = "";
const SESSION_KEY = "cr_session";

// 当前视图：login / register
let authView = "login";

// 切换登录 / 注册视图（同步标签高亮、标题与两处错误提示）
function showView(view) {
  authView = view;
  const isLogin = view === "login";
  document.getElementById("loginView").classList.toggle("hidden", !isLogin);
  document.getElementById("registerView").classList.toggle("hidden", isLogin);
  document.querySelectorAll("#loginTabs .ltab").forEach(b => {
    b.classList.toggle("active", b.dataset.view === view);
  });
  document.getElementById("cardTitle").textContent = isLogin ? "账号登录" : "注册账号";
  document.getElementById("loginError").textContent = "";
  document.getElementById("regError").textContent = "";
}

// 标签栏 + 底部链接切换视图
document.querySelectorAll("#loginTabs .ltab").forEach(b => {
  b.onclick = () => showView(b.dataset.view);
});
document.getElementById("toRegister").onclick = (e) => { e.preventDefault(); showView("register"); };
document.getElementById("toLogin").onclick = (e) => { e.preventDefault(); showView("login"); };

// 密码显隐切换（登录 / 注册各一个）
document.getElementById("togglePwd").onclick = () => {
  const p = document.getElementById("password");
  p.type = p.type === "password" ? "text" : "password";
};
document.getElementById("toggleRegPwd").onclick = () => {
  const p = document.getElementById("regPassword");
  p.type = p.type === "password" ? "text" : "password";
};

// 写入会话并进入主应用（登录与注册共用）
function enterApp(data, next) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(data));
  location.replace(next && next.startsWith("/") ? next : "/");
}

// 表单提交：按当前视图分流到登录或注册
document.getElementById("authForm").onsubmit = async (e) => {
  e.preventDefault();
  if (authView === "login") return doLogin();
  return doRegister();
};

// 账号登录
async function doLogin() {
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
    // 支持 ?next=/risk 形式的回跳（风控子系统从登录页返回原位）
    const params = new URLSearchParams(location.search);
    enterApp(data, params.get("next"));
  } catch (err) {
    errBox.textContent = "网络异常，请稍后重试";
  } finally {
    btn.disabled = false;
    btn.textContent = "登 录";
  }
}

// 自助注册（成功即自动登录）
async function doRegister() {
  const account = document.getElementById("regAccount").value.trim();
  const display_name = document.getElementById("regName").value.trim();
  const role = document.getElementById("regRole").value || "biz";
  const password = document.getElementById("regPassword").value;
  const confirm = document.getElementById("regConfirm").value;
  const errBox = document.getElementById("regError");
  const btn = document.getElementById("regBtn");

  errBox.textContent = "";
  if (!account || !display_name || !password || !confirm) {
    errBox.textContent = "请完整填写注册信息";
    return;
  }
  if (password !== confirm) {
    errBox.textContent = "两次输入的密码不一致";
    return;
  }
  btn.disabled = true;
  btn.textContent = "注册中…";
  try {
    const r = await fetch(API + "/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, display_name, password, role }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      errBox.textContent = j.detail || "注册失败，请稍后重试";
      return;
    }
    const data = await r.json();
    enterApp(data, null);
  } catch (err) {
    errBox.textContent = "网络异常，请稍后重试";
  } finally {
    btn.disabled = false;
    btn.textContent = "注 册";
  }
}

// 已登录则直接进入主应用，避免重复登录
(function autoSkip() {
  try {
    if (JSON.parse(localStorage.getItem(SESSION_KEY) || "null")) location.replace("/");
  } catch (e) { /* 忽略 */ }
})();
