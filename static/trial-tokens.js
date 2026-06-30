const adminTokenKey = "pdfExerciseMakerTokenAdmin";
const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
const fragmentToken = (fragment.get("token") || "").trim();
if (fragmentToken) {
  sessionStorage.setItem(adminTokenKey, fragmentToken);
  history.replaceState(null, "", window.location.pathname);
}

const adminToken = sessionStorage.getItem(adminTokenKey) || "";
const content = document.querySelector("#admin-content");
const authError = document.querySelector("#admin-auth-error");
const form = document.querySelector("#trial-token-form");
const recentIp = document.querySelector("#recent-ip");
const boundIp = document.querySelector("#bound-ip");
const usageMode = document.querySelector("#usage-mode");
const maxUses = document.querySelector("#max-uses");
const tokenCount = document.querySelector("#token-count");
const expiresAt = document.querySelector("#expires-at");
const neverExpires = document.querySelector("#never-expires");
const createdPanel = document.querySelector("#created-token");

function adminHeaders(json = false) {
  return {
    "X-Token-Admin": adminToken,
    ...(json ? { "Content-Type": "application/json" } : {}),
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function localInputValue(days) {
  const date = new Date(Date.now() + days * 86400000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "永不过期";
}

function renderTokens(tokens) {
  const rows = tokens.map((token) => {
    const max = token.max_uses === null ? "无限" : token.max_uses;
    const remaining = token.remaining === null ? "无限" : token.remaining;
    const boundLabel = token.bound_ip || "首次访问自动绑定";
    const geoLine = token.bound_ip
      ? `${escapeHtml(token.country || "未知")} · ${escapeHtml(token.as_name || "未知网络")}`
      : "等待首次访问";
    return `
      <tr>
        <td class="mono">${escapeHtml(token.token_prefix)}…</td>
        <td><b>${escapeHtml(boundLabel)}</b><small class="stats-subline">${geoLine}</small></td>
        <td>${token.used_count} / ${token.reserved_count} / ${max}</td>
        <td>${remaining}</td>
        <td>${escapeHtml(formatDate(token.expires_at))}</td>
        <td>${escapeHtml(token.status)}</td>
        <td>${escapeHtml(token.note || "")}<small class="stats-subline mono">${escapeHtml(token.last_job_id || "")}</small></td>
        <td>${token.status === "active" ? `<button class="text-link" data-revoke="${token.id}" type="button">撤销</button>` : ""}</td>
      </tr>`;
  }).join("");
  document.querySelector("#trial-token-list").innerHTML = `
    <table class="stats-table wide">
      <thead><tr><th>Token</th><th>绑定 IP / 来源</th><th>已用 / 预约 / 最大</th><th>剩余</th><th>过期</th><th>状态</th><th>备注 / 最近 Job</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="8" class="muted">暂无试用 token。</td></tr>`}</tbody>
    </table>`;
  document.querySelectorAll("[data-revoke]").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await fetch(`/api/internal/trial-tokens/${button.dataset.revoke}/revoke`, {
        method: "POST",
        headers: adminHeaders(),
      });
      if (response.ok) await loadData();
    });
  });
}

async function loadData() {
  const response = await fetch("/api/internal/trial-tokens", { headers: adminHeaders() });
  if (!response.ok) {
    content.hidden = true;
    authError.hidden = false;
    return;
  }
  const data = await response.json();
  content.hidden = false;
  authError.hidden = true;
  recentIp.innerHTML = `<option value="">请选择或手动输入</option>` + data.recent_ips
    .map((item) => `<option value="${escapeHtml(item.client_ip)}">${escapeHtml(item.client_ip)} · ${escapeHtml(item.country || "未知")} · ${escapeHtml(item.as_name || "未知网络")}</option>`)
    .join("");
  renderTokens(data.tokens);
}

recentIp.addEventListener("change", () => {
  if (recentIp.value) boundIp.value = recentIp.value;
});

usageMode.addEventListener("change", () => {
  const unlimited = usageMode.value === "unlimited";
  maxUses.disabled = unlimited;
  maxUses.required = !unlimited;
});

neverExpires.addEventListener("change", () => {
  expiresAt.disabled = neverExpires.checked;
  expiresAt.required = !neverExpires.checked;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    bound_ip: boundIp.value.trim(),
    max_uses: usageMode.value === "unlimited" ? null : Number(maxUses.value),
    expires_at: neverExpires.checked ? null : new Date(expiresAt.value).toISOString(),
    note: document.querySelector("#token-note").value.trim(),
    count: Number(tokenCount.value || 1),
  };
  const response = await fetch("/api/internal/trial-tokens", {
    method: "POST",
    headers: adminHeaders(true),
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "创建失败");
    return;
  }
  const links = (data.tokens || [data]).map((item) => `${window.location.origin}/#token=${encodeURIComponent(item.token)}`);
  document.querySelector("#created-token-value").value = data.token;
  document.querySelector("#created-token-url").value = links[0];
  document.querySelector("#created-token-list").value = links.join("\n");
  document.querySelector("#copy-created-url").textContent = links.length > 1 ? "复制全部试用链接" : "复制试用链接";
  createdPanel.hidden = false;
  await loadData();
});

document.querySelector("#copy-created-url").addEventListener("click", async () => {
  const allLinks = document.querySelector("#created-token-list").value.trim();
  await navigator.clipboard.writeText(allLinks || document.querySelector("#created-token-url").value);
  document.querySelector("#copy-created-url").textContent = "已复制";
});

document.querySelector("#refresh-tokens").addEventListener("click", loadData);

expiresAt.value = localInputValue(Number(document.querySelector(".token-admin-page").dataset.defaultDays || 7));
if (!adminToken) {
  authError.hidden = false;
} else {
  loadData();
}
