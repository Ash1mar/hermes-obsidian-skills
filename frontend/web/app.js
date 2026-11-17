const get = id => document.getElementById(id);
const labels = {
  pending: "等待", running: "进行中", completed: "已完成", failed: "失败",
  partial: "部分完成", interrupted: "执行已中断",
  paused: "已暂停", pausing: "等待暂停", detached: "启动器已退出", cancelled: "已取消",
};
const waitLabels = { waiting: "等待交接", detached: "等待已结束", handoff: "交接就绪", failed: "来源交接失败" };
const fileLabels = { ready: "就绪", failed: "转换失败", pending: "未结算", running: "处理中", queued: "排队中",
  waiting: "待派发", awaiting_reconciliation: "等待执行回执" };
const pages = { workflow: "工作流", vaults: "Vault", devtools: "DevTools" };
const gatewayLabels = {
  checking: ["检查中", "pending"], starting: ["正在启动", "running"], ready: ["已就绪", "completed"],
  degraded: ["已就绪（部分平台异常）", "partial"], stopped: ["已停止", "failed"],
  failed: ["启动或运行失败", "failed"], draining: ["正在排空", "partial"],
  dispatch_disabled: ["派发已关闭", "partial"], unverified: ["状态待确认", "partial"],
  disabled: ["历史查看模式", "pending"],
};
const stageNames = {
  bootstrap: "创建 Vault", prepare: "来源准备", exact: "精确计划与交接", topics: "主题提取", mapping: "证据映射",
  coordination: "页面协调", writing: "分段写作", "formal-build": "正式构建", release: "发布与索引", acceptance: "最终验收",
};
let runs = [];
let current;
let selected = localStorage.getItem("hermes-console-run");
let attempt;
let writable = false;
let gateway = { state: "checking", ready: false };
let posting = false;
let refreshing = false;
const rendered = new Map();

function make(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function badge(state) {
  return make("span", labels[state] || state || "等待", `badge ${state || "pending"}`);
}

function renderWhenChanged(key, data, render) {
  const signature = JSON.stringify(data);
  if (rendered.get(key) === signature) return;
  render();
  rendered.set(key, signature);
}

function message(text, error = false) {
  const notice = get("notice");
  notice.textContent = text;
  notice.hidden = !text;
  notice.classList.toggle("error", error);
}

async function api(path, body) {
  const response = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function facts(element, entries) {
  element.replaceChildren(...entries.flatMap(([name, value]) =>
    [make("dt", name), make("dd", value ?? "—")]));
}

function formatTime(value) {
  return Number.isFinite(value) ? new Date(value * 1000).toLocaleString("zh-CN", {hour12: false}) : undefined;
}

function showForm(open) {
  get("new-panel").hidden = !open;
  get("toggle-form").textContent = open ? "收起新建" : "＋ 新建运行";
  get("toggle-form").setAttribute("aria-expanded", String(open));
}

function page() {
  const value = location.hash.slice(1);
  return Object.hasOwn(pages, value) ? value : "workflow";
}

function route() {
  const view = page();
  get("breadcrumb").textContent = pages[view];
  for (const name of Object.keys(pages)) get(name).hidden = name !== view;
  for (const link of document.querySelectorAll("nav [data-page]")) {
    if (link.dataset.page === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (view === "devtools") refreshLogs().catch(error => message(error.message, true));
}

function selectRun(id) {
  if (selected !== id) {
    selected = id;
    current = undefined;
    attempt = undefined;
    localStorage.setItem("hermes-console-run", id);
    render();
  }
  refresh();
}

function rowButton(text, callback, style = "secondary") {
  const button = make("button", text, style);
  button.type = "button";
  button.addEventListener("click", callback);
  return button;
}

function render() {
  const busy = runs.some(run => run.launcher_active);
  get("connection").textContent = writable ? "已连接本机" : "历史查看模式";
  get("start").disabled = !writable || !gateway.ready || busy || posting;
  get("form-hint").textContent = !writable ? "在 Hermes 所在的 WSL 中启动控制台后即可提交。"
    : busy ? "已有控制台任务正在执行。"
    : !gateway.ready ? "等待 Hermes Gateway 派发就绪，可在 DevTools 查看启动状态。"
    : "新建独立测试 Vault，合格页面将发布并同步索引。查询保持关闭。";
  renderWhenChanged("gateway", gateway, renderGateway);
  get("runs-count").textContent = runs.length;
  renderWhenChanged("runs", [runs, selected], () => {
    get("runs").replaceChildren(...(runs.length ? runs.map(run => {
      const button = rowButton("", () => { showForm(false); selectRun(run.id); },
        `run-item${run.id === selected ? " selected" : ""}`);
      button.setAttribute("aria-pressed", String(run.id === selected));
      const title = make("span");
      title.append(make("strong", run.vault_name), badge(run.state));
      button.append(title, make("small", run.historical ? `历史 · ${run.id}` : run.id));
      return button;
    }) : [make("p", "暂无运行记录", "empty")]));
  });
  get("selected-label").textContent = current ? current.vault_name : "选择一次运行查看节点状态";
  get("run-status").replaceChildren(...(current ? [badge(current.state)] : []));
  renderWhenChanged("timeline", [selected, current?.nodes, current?.historical, busy, posting, gateway.ready, writable], renderTimeline);
  const summary = current ? [
    ["来源", `${current.source_count} 份 PDF`], ["源工作流暂停", current.pause],
    ["本机启动器", labels[current.launcher_state]],
    ["暂停目标", stageNames[current.pause_request?.node]],
    ["源工作流修订", current.source_revision], ["当前发布", current.release_id || "尚无"],
    ["Provider 索引", current.indexed ? "已就绪" : "尚未就绪"],
    ["查询", current.query_enabled ? "已开启" : "已关闭"],
  ] : [["运行", "未选择"]];
  facts(get("snapshot"), summary);
  renderWhenChanged("vaults", [runs, selected, current?.release_id, current?.indexed], renderVaults);
  renderWhenChanged("attempts", [selected, current?.attempts, attempt], renderAttempts);
  facts(get("native"), current ? [
    ["工作流", current.workflow_id], ["来源看板", current.source_board],
    ["来源阶段", current.source_stage], ["暂停点", current.pause],
    ["交接观察", waitLabels[current.source_wait?.state]],
    ["最近进展", formatTime(current.source_wait?.last_progress_at)],
    ["观察记录", current.source_wait?.error],
    ["短路径看板", current.short_board], ["短路径回执", current.receipts],
  ] : [["运行", "未选择"]]);
  get("dev-hint").textContent = current?.historical ? "从已保存的运行证据读取。"
    : "与日志同一轮次的数据来自工作流账本及短路径回执。";
}

function renderGateway() {
  const [label, tone] = gatewayLabels[gateway.state] || gatewayLabels.unverified;
  const header = get("gateway-top");
  header.textContent = `Gateway · ${label}`;
  header.className = `environment gateway-indicator badge ${tone}`;
  const status = get("gateway-badge");
  status.textContent = label;
  status.className = `badge ${tone}`;
  facts(get("gateway-facts"), [
    ["进程 PID", gateway.pid],
    ["启动方式", { started: "本次控制台启动", reused: "复用已有进程" }[gateway.ownership]],
    ["状态", gateway.detail],
  ]);
}

function renderTimeline() {
  const timeline = get("timeline");
  const sameRun = timeline.dataset.run === (selected || "");
  const open = new Set(sameRun ? [...timeline.querySelectorAll("details[open]")].map(item => item.dataset.node) : []);
  const manifests = new Map(sameRun ? [...timeline.querySelectorAll("[data-manifest]")].map(item =>
    [item.dataset.manifest, item.value]) : []);
  const pauses = new Map(sameRun ? [...timeline.querySelectorAll("[data-next-pause]")].map(item =>
    [item.dataset.nextPause, item.value]) : []);
  timeline.dataset.run = selected || "";
  const nodes = current?.nodes || Object.values(stageNames).map(name => ({ name, state: "pending" }));
  timeline.replaceChildren(...nodes.map((node, index) => {
    const item = make("li");
    const pin = make("span", node.state === "completed" ? "✓" : String(index + 1), `step-pin ${node.state}`);
    pin.setAttribute("aria-hidden", "true");
    const line = make("div", undefined, "step-line");
    line.append(make("strong", node.name), badge(node.state));
    item.append(pin, line);
    if (node.detail) item.append(make("p", node.detail, "step-info"));
    if (node.key) {
      const detail = make("details");
      detail.dataset.node = node.key;
      detail.open = open.has(node.key) || ["failed", "paused", "pausing"].includes(node.state)
        || node.actions?.some(action => action.id === "attach-source");
      detail.append(make("summary", "节点详情"), make("p", node.description, "hint"));
      if (node.error) detail.append(make("p", node.error, "hint"));
      if (node.sources?.length) {
        const files = make("ul", undefined, "source-list");
        files.append(...node.sources.map(file => {
          const line = make("li", `${file.name} · ${fileLabels[file.status] || file.status}`);
          const started = formatTime(file.observed_running_at);
          if (started || file.error) line.append(make("small", [started && `观察到执行 ${started}`, file.error].filter(Boolean).join(" · ")));
          return line;
        }));
        detail.append(files);
      }
      const actions = node.actions || [];
      for (const action of actions) {
        const row = make("div", undefined, "action-line");
        let input;
        if (action.manifest) {
          input = make("input");
          input.placeholder = "映射恢复 JSON 路径";
          input.setAttribute("aria-label", "映射恢复 JSON 路径");
          input.dataset.manifest = action.id;
          input.value = manifests.get(action.id) || "";
          row.append(input);
        }
        let nextPause;
        if (action.pending_pause) {
          row.append(make("span", `保留暂停：${stageNames[action.pending_pause]}`, "hint"));
        } else if (action.pause_targets?.length) {
          nextPause = make("select");
          nextPause.setAttribute("aria-label", "下一暂停节点");
          nextPause.dataset.nextPause = `${action.attempt}:${action.id}`;
          for (const key of ["", ...action.pause_targets]) {
            const choice = make("option", key ? `暂停在${stageNames[key]}前` : "选择下一暂停点（可选）");
            choice.value = key;
            nextPause.append(choice);
          }
          nextPause.value = pauses.get(nextPause.dataset.nextPause) || "";
          row.append(nextPause);
        }
        const button = rowButton(action.label, () => reenter(action, input?.value, nextPause?.value), "secondary");
        button.disabled = !writable || posting || (action.id !== "pause" && runs.some(run => run.launcher_active))
          || (action.gateway_required !== false && !gateway.ready);
        row.append(button);
        detail.append(row);
      }
      if (!actions.length) {
        const reason = current.historical ? "历史记录仅供查看。"
          : current.pause_request?.node ? `已请求在${stageNames[current.pause_request.node]}前暂停。`
          : current.launcher_active ? "到达安全检查点后会更新可用操作。"
          : node.key === "exact" ? "短路径保留源工作流的 exact_plan 暂停。"
          : "当前回执没有可从此处执行的恢复操作。";
        detail.append(make("p", reason, "hint"));
      }
      item.append(detail);
    }
    return item;
  }));
}

function renderVaults() {
  const unique = new Map();
  for (const run of runs) if (!unique.has(run.vault_path)) unique.set(run.vault_path, run);
  get("vault-list").replaceChildren(...(unique.size ? [...unique.values()].map(run => {
    const card = make("article", undefined, "panel");
    const heading = make("div", undefined, "panel-heading");
    heading.append(make("h2", run.vault_name), badge(run.state));
    const body = make("div", undefined, "vault-card-body");
    body.append(make("p", run.vault_path));
    if (current?.id === run.id && current.release_id) {
      body.append(make("p", `发布 ${current.release_id} · ${current.indexed ? "索引已就绪" : "等待索引"}`, "hint"));
    }
    const open = make("a", "在 Obsidian 中打开", "button");
    open.href = `obsidian://open?vault=${encodeURIComponent(run.vault_name)}`;
    body.append(open, rowButton("查看运行", () => {
      selectRun(run.id);
      showForm(false);
      location.hash = "workflow";
    }), make("p", "请先在 Obsidian 中按此名称注册该 Vault。", "hint"));
    card.append(heading, body);
    return card;
  }) : [make("p", "提交首次运行后，这里会显示对应 Vault。", "empty")]));
}

function renderAttempts() {
  const entries = current?.attempts || [];
  const select = get("attempts");
  if (!entries.length) {
    select.replaceChildren(make("option", "暂无执行记录"));
    select.disabled = true;
    get("log").textContent = "选择一条运行查看日志。";
    return;
  }
  if (!entries.some(entry => entry.id === attempt)) attempt = entries.at(-1).id;
  select.disabled = false;
  select.replaceChildren(...entries.map((entry, index) => {
    const outcome = entry.returncode === 75 ? " · 观察退出" : entry.returncode ? ` · 退出码 ${entry.returncode}` : "";
    const option = make("option", `${index + 1}. ${entry.label || (entry.action === "start" ? "原始运行" : "恢复交付")}${outcome}`);
    option.value = entry.id;
    return option;
  }));
  select.value = attempt;
}

async function refreshLog() {
  if (!current?.id || !attempt || page() !== "devtools") return;
  const id = current.id;
  const entry = attempt;
  const data = await api(`/api/runs/${id}/log?attempt=${encodeURIComponent(entry)}`);
  if (id !== selected || entry !== attempt) return;
  showLog(get("log"), data.text || "执行器尚未写入输出。");
}

function showLog(log, text) {
  if (log.textContent === text) return;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 25;
  log.textContent = text;
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

async function refreshLogs() {
  if (page() !== "devtools") return;
  const gatewayLog = api("/api/gateway/log").then(data => {
    if (page() !== "devtools") return;
    const fallback = gateway.ownership === "reused" ? "本次控制台复用了已有 Gateway。"
      : gateway.state === "disabled" ? "历史查看模式未启动 Gateway。"
      : gateway.detail || "等待 Gateway 启动日志。";
    showLog(get("gateway-log"), data.text || fallback);
  });
  await Promise.all([refreshLog(), gatewayLog]);
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await api("/api/runs");
    runs = data.runs;
    writable = data.writable;
    gateway = data.gateway;
    if (!runs.some(run => run.id === selected)) {
      selected = runs[0]?.id;
      attempt = undefined;
    }
    const id = selected;
    if (id) {
      const snapshot = await api(`/api/runs/${id}`);
      if (selected === id) {
        if (snapshot.attempts.at(-1)?.id !== current?.attempts.at(-1)?.id) attempt = undefined;
        current = snapshot;
      }
    } else current = undefined;
    render();
    await refreshLogs();
  } catch (error) {
    get("connection").textContent = "连接异常";
    message(error.message, true);
  } finally {
    refreshing = false;
  }
}

async function submit(path, body) {
  if (posting) return;
  posting = true;
  render();
  try {
    const data = await api(path, body);
    message(path.endsWith("/pause") ? "暂停已请求，执行器会在指定节点前停下。" : "执行请求已提交。");
    showForm(false);
    selectRun(data.id);
    return true;
  } catch (error) {
    message(error.message, true);
    return false;
  } finally {
    posting = false;
    render();
    refresh();
  }
}

function reenter(action, manifest, pauseBefore) {
  if (action.manifest && !manifest?.trim()) {
    message("请填写映射恢复 JSON 清单路径。", true);
    return;
  }
  if (action.id === "pause") {
    submit(`/api/runs/${current.id}/pause`, {node: action.node, attempt: action.attempt});
    return;
  }
  const target = pauseBefore || action.pending_pause;
  const scope = target ? `执行器将在${stageNames[target]}前暂停。` : "满足发布条件时，本轮可以继续到索引和验收。";
  if (!window.confirm(`确认对「${current.vault_name}」执行“${action.label}”？${scope}`)) return;
  submit(`/api/runs/${current.id}/reenter`, {
    action: action.id, node: action.node, attempt: action.attempt, manifest, prior_entry: action.prior_entry,
    pause_before: pauseBefore || undefined,
  });
}

for (const [key, name] of Object.entries(stageNames)) {
  const option = make("option", name);
  option.value = key;
  get("pause-before").append(option);
}

get("create").addEventListener("submit", async event => {
  event.preventDefault();
  const body = {
    sources: get("sources").value.split(/\r?\n/).map(path => path.trim()).filter(Boolean),
    vault_path: get("vault-path").value, vault_name: get("vault-name").value,
    pause_before: get("pause-before").value || undefined,
  };
  if (await submit("/api/runs", body)) event.target.reset();
});
get("toggle-form").addEventListener("click", () => showForm(get("new-panel").hidden));
get("attempts").addEventListener("change", event => {
  attempt = event.target.value;
  refreshLog().catch(error => message(error.message, true));
});
window.addEventListener("hashchange", route);
if (selected) showForm(false);
route();
render();
refresh();
setInterval(refresh, 2500);
