const get = id => document.getElementById(id);
const labels = {
  pending: "等待", running: "进行中", completed: "已完成", failed: "失败",
  partial: "部分完成", interrupted: "执行已中断",
  paused: "已暂停", pausing: "等待暂停", detached: "启动器已退出", cancelled: "已取消",
  active: "处理中", preparing: "来源准备中", prepared: "来源就绪", mapping: "映射中", mapped: "映射已完成",
  frozen: "输入已固定", settled: "已结算", held: "受阻", committed: "已提交",
  "manual-held": "待手动开始", released: "已允许处理",
  "waiting-dispatch": "等待调度", "waiting-qualification": "等待来源资格核验",
  "closure-accounting": "待对账", "partially-closed": "已部分结案", deletion_pending: "待完成清理",
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
let obsidianAvailable = false;
let gateway = { state: "checking", ready: false };
let gatewayRevision = 0;
let gatewayCommand;
let gatewayRequestError;
let posting = false;
let refreshing = false;
let runListRevision = 0;
let deleteTarget;
const draftSources = [];
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

function setSelection(id) {
  if (selected === id) return;
  selected = id;
  current = undefined;
  attempt = undefined;
  if (id) localStorage.setItem("hermes-console-run", id);
  else localStorage.removeItem("hermes-console-run");
}

function selectRun(id) {
  setSelection(id);
  render();
  refresh();
}

function rowButton(text, callback, style = "secondary") {
  const button = make("button", text, style);
  button.type = "button";
  button.addEventListener("click", callback);
  return button;
}

function gatewayReady() {
  return gateway.ready && gatewayCommand !== "stop";
}

function render() {
  const busy = runs.some(run => run.launcher_active);
  get("connection").textContent = writable ? "已连接本机" : "历史查看模式";
  get("start").disabled = !writable || !gatewayReady() || busy || posting;
  get("form-hint").textContent = !writable ? "在 Hermes 所在的 WSL 中启动控制台后即可提交。"
    : busy ? "已有控制台任务正在执行。"
    : !gatewayReady() ? "Gateway 派发就绪后可开始处理。"
    : "新建独立测试 Vault，合格页面将发布并同步索引。查询保持关闭。";
  renderWhenChanged("gateway", [gateway, writable, gatewayCommand, gatewayRequestError], renderGateway);
  get("runs-count").textContent = runs.length;
  renderWhenChanged("runs", [runs, selected, writable, posting], () => {
    get("runs").replaceChildren(...(runs.length ? runs.map(run => {
      const isSelected = run.id === selected;
      const row = make("div", undefined, `run-row${isSelected ? " selected" : ""}`);
      row.dataset.runId = run.id;
      const view = rowButton("", () => { showForm(false); selectRun(run.id); }, "run-item");
      view.setAttribute("aria-pressed", String(isSelected));
      view.append(make("strong", run.vault_name), make("small", run.historical ? `历史 · ${run.id}` : run.id));
      const remove = rowButton("删除记录及Vault", () => beginDelete(run), "run-delete");
      remove.setAttribute("aria-label", `删除记录及Vault · ${run.vault_name}`);
      remove.disabled = !writable || run.historical || posting || run.launcher_active;
      if (!writable || run.historical) remove.title = "此记录仅供历史查看";
      else if (run.launcher_active) remove.title = "执行器退出后可删除";
      const actions = make("div", undefined, "run-actions");
      actions.append(badge(run.deletion_pending ? "deletion_pending" : run.state), remove);
      row.append(view, actions);
      return row;
    }) : [make("p", "暂无运行记录", "empty")]));
  });
  if (deleteTarget) {
    const row = runs.find(run => run.id === deleteTarget.id);
    get("delete-pending").hidden = !row?.deletion_pending;
    if (!row && !posting && !get("delete-error").hidden && get("delete-dialog").open) {
      message(`「${deleteTarget.vault_name}」已从运行记录移除。`);
      get("delete-dialog").close();
    }
  }
  get("selected-label").textContent = current ? current.vault_name : "选择一次运行查看节点状态";
  get("run-status").replaceChildren(...(current ? [badge(current.deletion_pending ? "deletion_pending" : current.state)] : []));
  renderWhenChanged("timeline", [selected, current?.nodes, current?.incremental, current?.historical, current?.deletion_pending, busy, posting, gatewayReady(), writable], renderTimeline);
  const pause = current?.pause;
  const pausedAt = pause === "network_recovery" ? "等待网络恢复" : incrementalStageNames[pause] || pause;
  const summary = current ? [
    ["来源", `${current.source_count} 份 PDF`], ["暂停点", pausedAt],
    ["本机启动器", labels[current.launcher_state]],
    ["暂停目标", stageNames[current.pause_request?.node]],
    ["源工作流修订", current.source_revision], ["当前发布", current.release_id || "尚无"],
    ["Provider 索引", current.indexed ? "已就绪" : "尚未就绪"],
    ["查询", current.query_enabled ? "已开启" : "已关闭"],
  ] : [["运行", "未选择"]];
  facts(get("snapshot"), summary);
  renderWhenChanged("vaults", [runs, selected, current?.release_id, current?.indexed, obsidianAvailable], renderVaults);
  renderWhenChanged("attempts", [selected, current?.attempts, attempt], renderAttempts);
  facts(get("native"), current ? [
    ["工作流", current.workflow_id], ["来源看板", current.source_board],
    ["来源阶段", current.source_stage], ["暂停点", pausedAt],
    ["交接观察", waitLabels[current.source_wait?.state]],
    ["最近进展", formatTime(current.source_wait?.last_progress_at)],
    ["观察记录", current.source_wait?.error],
    ["短路径看板", current.short_board], ["短路径回执", current.receipts],
  ] : [["运行", "未选择"]]);
  get("dev-hint").textContent = current?.historical ? "从已保存的运行证据读取。"
    : "与日志同一轮次的数据来自工作流账本及短路径回执。";
}

function canControlGateway(action) {
  return writable && !gatewayCommand && gateway[`can_${action}`] === true;
}

function renderGateway() {
  const [label, tone] = gatewayLabels[gateway.state] || gatewayLabels.unverified;
  const header = get("gateway-top");
  const headerText = `Gateway · ${label}${writable ? " · 管理" : ""}`;
  if (header.textContent !== headerText) header.textContent = headerText;
  header.className = `environment gateway-indicator badge ${tone}`;
  get("workflow-gateway").hidden = !writable || gatewayReady();
  get("workflow-gateway-state").textContent = gatewayCommand === "stop" ? "正在提交 Gateway 停止请求。" : `Gateway ${label}。`;
  const status = get("gateway-badge");
  status.textContent = label;
  status.className = `badge ${tone}`;
  for (const [action, name] of [["start", "启动"], ["stop", "停止"]]) {
    const button = get(`gateway-${action}`);
    button.disabled = !canControlGateway(action);
    button.textContent = gatewayCommand === action ? `正在提交${name}…` : `${name} Gateway`;
  }
  const error = get("gateway-control-error");
  const errors = [gatewayRequestError, !gatewayCommand && gateway.operation_error].filter(Boolean);
  const errorText = [...new Set(errors)].join("\n");
  if (error.textContent !== errorText) error.textContent = errorText;
  facts(get("gateway-facts"), [
    ["进程 PID", gateway.pid],
    ["启动方式", { started: "本次控制台启动", reused: "复用已有进程" }[gateway.ownership]],
    ["状态", gateway.detail],
  ]);
}

async function controlGateway(action) {
  if (!canControlGateway(action)) return;
  if (action === "stop" && !window.confirm("停止后将暂停当前 Hermes 配置的 Gateway 服务和任务派发。确认停止？")) return;
  gatewayCommand = action;
  gatewayRequestError = undefined;
  render();
  try {
    const data = await api(`/api/gateway/${action}`, {});
    gateway = data.gateway;
  } catch (error) {
    gatewayRequestError = error.message;
  } finally {
    gatewayRevision += 1;
    gatewayCommand = undefined;
    render();
    refresh();
  }
}

function renderTimeline() {
  const timeline = get("timeline");
  if (current?.deletion_pending) {
    timeline.replaceChildren(make("p", "请在运行记录中重试删除。", "empty"));
    return;
  }
  if (current?.workflow_mode === "incremental_v1") {
    renderIncremental(timeline);
    return;
  }
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
          || (action.gateway_required !== false && !gatewayReady());
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

const openRecoveryScopes = new Set();
const selectedRecoveryFragments = new Map();
const selectedMapRequests = new Map();
const mapRecoveryNames = {original: "恢复原 Map 卡", supplemental: "定向补跑"};
const mapActions = new Set(["incremental-map-recovery-preview", "incremental-map-recovery-confirm", "incremental-map-supplement-continue"]);
const incrementalStageNames = {...stageNames, closure: "运行收尾"};

function renderIncremental(timeline) {
  const value = current.incremental;
  timeline.dataset.run = selected || "";
  const controls = make("li", undefined, "incremental-scope");
  controls.append(make("strong", "增量运行"), badge(current.state));
  const supplement = value.supplemental_run;
  if (supplement?.source_name) controls.append(make("p", `定向补跑 · ${supplement.source_name}`, "hint"));
  const boundary = value.control.boundary;
  if (boundary?.stage === "network_recovery" && ["paused", "pausing"].includes(current.state)) {
    const phase = boundary.evidence.phase === "coordination" ? "页面协调" : "页面写作";
    controls.append(make("p", `${boundary.scope.scope_id} · ${phase}模型连接连续失败。已保留本轮进度。`, "hint"));
  } else if (boundary) controls.append(make("p", `暂停在${incrementalStageNames[boundary.stage] || boundary.stage}`, "hint"));
  const actions = current.nodes[0].actions;
  const pause = actions.find(action => action.id === "incremental-pause");
  const pauseButton = rowButton("暂停增量运行", () => { if (pause) reenter(pause); });
  pauseButton.disabled = !pause || !writable || posting || runs.some(run => run.launcher_active);
  controls.append(pauseButton);
  for (const action of actions) {
    if (action === pause) continue;
    const button = rowButton(action.label, () => reenter(action));
    button.disabled = !writable || posting || (action.gateway_required !== false && !gatewayReady());
    controls.append(button);
  }
  const stages = make("li", undefined, "incremental-scope");
  stages.append(make("strong", "整体流程"));
  const overall = make("ol", undefined, "incremental-pipeline");
  for (const phase of value.overall_stages || []) overall.append(renderIncrementalPhase(phase));
  stages.append(overall);
  const sources = make("li", undefined, "incremental-scope");
  sources.append(make("strong", "逐文件流程"));
  for (const source of value.sources) {
    const card = make("section", undefined, "source-pipeline-card");
    const row = make("div", undefined, "scope-row");
    row.append(make("strong", source.name), badge(source.display_state || source.state));
    if (source.admission) row.append(badge(source.admission.state === "held" ? "manual-held" : "released"));
    for (const action of source.actions || []) {
      if (action.id !== "incremental-release-source") continue;
      const button = rowButton(action.label, () => reenter(action));
      button.disabled = !writable || posting || (action.gateway_required !== false && !gatewayReady());
      row.append(button);
    }
    card.append(row);
    if (source.failure_reason || source.reason || source.error) card.append(make("p", source.failure_reason || source.reason || source.error, "source-error hint"));
    const pipeline = make("ol", undefined, "incremental-pipeline");
    for (const phase of source.pipeline || []) pipeline.append(renderIncrementalPhase(phase, source));
    card.append(pipeline);
    if (source.related_rounds?.length) card.append(make("p", source.related_rounds.map(round =>
      `参与共享轮次 ${round.round_id}（${round.role === "input" ? "本轮新输入" : "已有上下文"}）· ${round.dependent_page_ids.length} 项依赖页面`).join("；"), "hint"));
    sources.append(card);
  }
  if (!value.sources.length) sources.append(make("p", "等待来源范围登记", "hint"));
  const rounds = make("li", undefined, "incremental-scope");
  rounds.append(make("strong", "交付轮次"));
  for (const round of value.rounds) {
    const row = make("div", undefined, "scope-row");
    row.append(make("span", round.supplement_run_id && round.source_name
      ? `${round.source_name} · ${round.round_id}` : round.round_id), badge(round.delivery_state || (round.active ? "running" : "settled")));
    row.append(make("small", `${round.mapping_count} 份映射 · ${round.page_count} 项页面处置${round.release_id ? ` · ${round.release_id}` : ""}`));
    if (round.independent_section_candidates) {
      row.append(make("small", `写作通过 ${round.original_pages_written} 张整页 · ${round.independent_section_candidates} 张独立小节候选`));
    }
    rounds.append(row);
    const pipeline = make("ol", undefined, "incremental-pipeline");
    for (const phase of round.stages || []) pipeline.append(renderIncrementalPhase(phase));
    rounds.append(pipeline);
  }
  if (!value.rounds.length) rounds.append(make("p", "首份来源映射就绪后可开始轮次", "hint"));
  timeline.replaceChildren(controls, stages, sources, rounds);
}

function renderIncrementalPhase(phase, source) {
  const row = make("li");
  row.append(make("strong", phase.name), badge(phase.state));
  if (phase.counts) row.append(make("small", Object.entries(phase.counts).map(([state, count]) => `${count} ${labels[state] || state}`).join(" · ")));
  if (phase.round_ids?.length) row.append(make("small", `共享轮次 ${phase.round_ids.join("、")}`));
  if (phase.detail) row.append(make("small", phase.detail));
  if (!source && phase.key === "closure") {
    if (phase.reservations) row.append(renderClosureUsage(phase));
    for (const action of phase.actions || []) {
      const button = rowButton(action.label, () => reenter(action));
      button.disabled = !writable || posting || runs.some(run => run.launcher_active);
      row.append(button);
    }
    return row;
  }
  if (phase.boundary) row.append(make("small", phase.network_recovery ? "等待网络恢复" :
    phase.result_state === "completed" ? "本步成果已完成，停在此边界" : "停在此边界"));
  const actions = phase.actions || [];
  const nodeActions = phase.key === "mapping" ? actions.filter(action => mapActions.has(action.id)) : [];
  const scopedActions = actions.filter(action => !nodeActions.includes(action));
  if (nodeActions.length) {
    const rowActions = make("div", undefined, "mapping-actions");
    for (const action of nodeActions) appendIncrementalAction(rowActions, action);
    row.append(rowActions);
  }
  const review = phase.map_recovery;
  const hasMapEvidence = review?.strategies?.length || review?.outcomes?.length || review?.results?.length;
  const scopes = phase.recovery_scopes || [];
  const hasScopeDetails = scopes.some(scope => Object.keys(scope).length) || scopedActions.length;
  if (source && (hasScopeDetails || hasMapEvidence)) {
    const key = `${current.id}:${source.path}:${phase.key}`;
    const detail = recoveryDetails(scopedActions.length ? "查看范围与操作" : "查看证据与处理范围", key);
    if (hasMapEvidence) renderMapRecovery(detail, review, key);
    for (const scope of scopes) {
      const actions = scopedActions.filter(action => action.round_id === (scope.round_id || null));
      if (!Object.keys(scope).length && !actions.length) continue;
      const section = make("div", undefined, "node-recovery-scope");
      if (scope.round_id) section.append(make("strong", `${scope.round_id} · 共享轮次`),
        make("small", `${scope.page_count} 项依赖页面${scope.blocked_pages ? ` · ${scope.blocked_pages.length} 项暂缓` : ""}`));
      if (scope.snapshot_id) section.append(make("small", `来源快照 ${scope.snapshot_id}`));
      if (scope.map_result_id) section.append(make("small", `复用已核验 Map ${scope.map_result_id}`));
      if (scope.task_id) section.append(make("small", `原生任务 ${scope.task_id}`));
      if (scope.error_code) section.append(make("small", scope.error_code));
      if (scope.evidence_refs?.length) {
        const evidence = make("ul");
        for (const ref of scope.evidence_refs) evidence.append(make("li", ref));
        section.append(evidence);
      }
      if (scope.shared_gap_count) section.append(make("small", `该轮另有 ${scope.shared_gap_count} 项共享协调缺口`));
      if (scope.build_run_id) section.append(make("small", `构建 ${scope.build_run_id}`));
      if (scope.release_id) section.append(make("small", `发布 ${scope.release_id}`));
      if (scope.provider_generation) section.append(make("small", `索引 ${scope.provider_generation}`));
      if (scope.blocked_pages?.length) {
        const pages = make("ul");
        for (const page of scope.blocked_pages) {
          const item = make("li", page.name);
          if (page.reason) item.append(make("small", page.reason));
          pages.append(item);
        }
        section.append(pages);
      }
      if (scope.fragments?.length && !actions.some(action => action.fragment_ids)) {
        const fragments = make("details");
        fragments.append(make("summary", `查看 ${scope.fragments.length} 个计划分片`));
        const list = make("ul");
        for (const fragment of scope.fragments) {
          const item = make("li", `${fragment.name} · 分片 ${fragment.ordinal}`);
          item.append(make("small", fragment.request_id));
          list.append(item);
        }
        fragments.append(list);
        section.append(fragments);
      }
      for (const action of actions) appendIncrementalAction(section, action, scope.fragments);
      detail.append(section);
    }
    row.append(detail);
  }
  return row;
}

function recoveryDetails(summary, key) {
  const detail = make("details", undefined, "node-recovery");
  detail.append(make("summary", summary));
  detail.open = openRecoveryScopes.has(key);
  detail.addEventListener("toggle", () => {
    if (detail.isConnected) detail.open ? openRecoveryScopes.add(key) : openRecoveryScopes.delete(key);
  });
  return detail;
}

function appendIncrementalAction(container, action, scopeFragments = []) {
  const allowed = action.fragment_ids;
  const selected = selectedRecoveryFragments.get(action.scope_token) || new Set();
  const preview = action.id === "incremental-map-recovery-preview";
  const enabled = writable && !posting && (preview || !runs.some(run => run.launcher_active))
    && (action.gateway_required === false || gatewayReady());
  const requestIds = action.request_ids;
  const selectionKey = `${action.preview_key}:${action.report_sha256}:${action.strategy}`;
  let requestId = requestIds?.length === 1 ? requestIds[0] : selectedMapRequests.get(selectionKey);
  const button = rowButton(action.label, () => reenter(action, undefined, undefined, allowed ? [...selected] : undefined, requestId));
  const syncDisabled = () => {
    button.disabled = !enabled || Boolean(allowed && !selected.size) || Boolean(requestIds && !requestId);
  };
  syncDisabled();
  if (requestIds?.length > 1) {
    const choice = make("select");
    choice.setAttribute("aria-label", `${mapRecoveryNames[action.strategy]}的原请求`);
    for (const id of ["", ...requestIds]) {
      const entry = make("option", id || "选择原 Map 请求");
      entry.value = id;
      choice.append(entry);
    }
    choice.value = requestId || "";
    choice.disabled = !enabled;
    choice.addEventListener("change", () => {
      requestId = choice.value;
      selectedMapRequests.set(selectionKey, requestId);
      syncDisabled();
    });
    container.append(choice);
  }
  if (allowed) {
    const choices = make("fieldset");
    choices.append(make("legend", "选择要核对原生回执的分片"));
    for (const fragment of scopeFragments.filter(fragment => allowed.includes(fragment.request_id))) {
      const label = make("label");
      const input = make("input");
      input.type = "checkbox";
      input.checked = selected.has(fragment.request_id);
      input.addEventListener("change", () => {
        if (input.checked) selected.add(fragment.request_id);
        else selected.delete(fragment.request_id);
        selectedRecoveryFragments.set(action.scope_token, selected);
        syncDisabled();
      });
      label.append(input, document.createTextNode(`${fragment.name} · 分片 ${fragment.ordinal}`));
      choices.append(label);
    }
    container.append(choices);
  }
  container.append(button);
}

function renderMapRecovery(container, review, key) {
  const allowed = review.strategies?.find(strategy => strategy.allowed);
  if (allowed) container.append(make("p", `${mapRecoveryNames[allowed.id]} · ${allowed.request_ids.length} 个请求符合条件`, "hint"));
  else if (review.strategies?.length) {
    const reasons = [...new Set(review.strategies.map(strategy => strategy.reason).filter(Boolean))];
    for (const reason of reasons) container.append(make("p", reason, "hint"));
  }
  if (review.outcomes?.length) {
    const reused = review.outcomes.filter(item => item.disposition === "reuse").length;
    const evidence = recoveryDetails(`查看 ${review.outcomes.length} 张原 Map 卡的处置 · ${reused} 张可复用`, `${key}:map`);
    const list = make("ul");
    const dispositions = {eligible: "符合恢复条件", reuse: "复用已确认回执", isolated: "隔离待核对"};
    for (const item of review.outcomes) {
      const outcome = make("li", [item.task_id, item.request_id, dispositions[item.disposition] || item.disposition]
        .filter(Boolean).join(" · "));
      if (item.reason) outcome.append(make("small", item.reason));
      list.append(outcome);
    }
    evidence.append(list);
    container.append(evidence);
  }
  for (const result of review.results || []) {
    if (result.request_id) container.append(make("small", `原请求 ${result.request_id}`));
    if (result.supplement_run_id) container.append(make("small", `补跑运行 ${result.supplement_run_id}`));
    for (const id of result.map_result_ids || []) container.append(make("small", `封存 Map ${id}`));
  }
}

function renderClosureUsage(phase) {
  const details = make("details", undefined, "node-recovery");
  details.open = Boolean(phase.review_ready);
  const dispositions = {retained_unconfirmed: "保留预留", settled_actual: "按实际用量结算", settled_zero: "按零用量结算"};
  const reasons = {transport_or_runtime_hold: "调用完成情况待核实", bound_native_response_and_session_usage: "回包和会话用量已匹配",
    bound_local_rejection_without_http_send: "本地拒绝，未发送且会话零用量", ended_attempt_without_audit: "原生任务已结束，缺少调用审计",
    reservation_without_attempt_evidence: "只有预留，缺少执行凭据", returned_usage_unverified: "回包存在，用量凭据待核实",
    recorded_usage_exceeds_reservation: "会话用量超出预留", unconfirmed: "用量待核实"};
  const phases = {mapping: "Map", coordination: "协调", writing: "写作"};
  const entries = Object.entries(phase.reservations);
  details.append(make("summary", `查看 ${entries.length} 笔用量的处置和证据`));
  for (const [id, entry] of entries) {
    const block = make("div", undefined, "node-recovery-scope");
    block.append(make("strong", `${phases[entry.phase] || entry.phase} · ${dispositions[entry.disposition]}`), make("small", id));
    const evidence = entry.evidence || {};
    const reason = reasons[entry.reason] || entry.reason;
    block.append(make("small", [reason, evidence.failure_type?.class, evidence.failure_type?.state].filter(Boolean).join(" · ")));
    if (entry.actual) block.append(make("small", `${entry.actual.calls} 次 · 输入 ${entry.actual.input_tokens} · 输出 ${entry.actual.output_tokens} token`));
    if (evidence.native) block.append(make("small", `原生任务 ${evidence.native.task_id} · 执行 ${evidence.native.run_id} · ${evidence.native.outcome}`));
    if (evidence.observed_session || evidence.session) {
      const session = evidence.session || evidence.observed_session;
      block.append(make("small", `会话 ${session.session_id} · ${session.calls} 次 · 输入 ${session.input_tokens} · 输出 ${session.output_tokens} token`));
    }
    if (evidence.http_journal) block.append(make("small", `HTTP 发送记录 ${evidence.http_journal.send_count} 条`));
    const refs = make("ul");
    for (const proof of [evidence.job, evidence.attempt, evidence.audit, evidence.failure, evidence.native_result, ...(evidence.http_sends || [])]) {
      if (proof?.ref) refs.append(make("li", proof.ref));
    }
    if (refs.childElementCount) block.append(refs);
    details.append(block);
  }
  return details;
}

async function copyVaultDirectory(field) {
  try {
    await navigator.clipboard.writeText(field.value);
    message("已复制目录。可在 Obsidian 的「打开文件夹作为 Vault」中粘贴。");
  } catch {
    field.focus();
    field.select();
    message("已选中目录，请手动复制。");
  }
}

async function openLinuxObsidian(run, action) {
  try {
    await api("/api/runs/" + encodeURIComponent(run.id) + "/obsidian", { action });
    message(action === "manager" ? "已启动 Linux Obsidian 管理器，请选择上面的 Linux 目录。"
      : "已请求 Linux Obsidian 打开此 Vault。首次使用请先在管理器中添加目录。");
  } catch (error) {
    message(error.message, true);
  }
}

function renderVaults() {
  const unique = new Map();
  for (const run of runs) if (!unique.has(run.vault_path)) unique.set(run.vault_path, run);
  get("vault-list").replaceChildren(...(unique.size ? [...unique.values()].map(run => {
    const card = make("article", undefined, "panel");
    const heading = make("div", undefined, "panel-heading");
    heading.append(make("h2", run.vault_name), badge(run.deletion_pending ? "deletion_pending" : run.state));
    const body = make("div", undefined, "vault-card-body");
    const directory = make("input");
    directory.type = "text";
    directory.readOnly = true;
    directory.id = "obsidian-directory-" + run.id;
    directory.value = run.vault_path;
    const directoryLabel = make("label", "Linux Obsidian 目录");
    directoryLabel.htmlFor = directory.id;
    body.append(directoryLabel, directory);
    if (current?.id === run.id && current.release_id) {
      body.append(make("p", `发布 ${current.release_id} · ${current.indexed ? "索引已就绪" : "等待索引"}`, "hint"));
    }
    const manager = rowButton("打开 Linux Obsidian 管理器", () => openLinuxObsidian(run, "manager"), "");
    const open = rowButton("打开已添加的 Vault", () => openLinuxObsidian(run, "open"));
    manager.disabled = open.disabled = !obsidianAvailable || run.deletion_pending;
    const actions = make("div", undefined, "vault-actions");
    actions.append(manager, rowButton("复制目录", () => copyVaultDirectory(directory)), open, rowButton("查看运行", () => {
      selectRun(run.id);
      showForm(false);
      location.hash = "workflow";
    }));
    body.append(actions, make("p", obsidianAvailable
      ? "首次添加时，选「打开文件夹作为 Vault」，按 Ctrl+L 输入上面的目录，或逐级选择文件夹。"
      : "请在安装了 Linux Obsidian 的 WSL 中启动新版控制台。", "hint"));
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
  const revision = gatewayRevision;
  const gatewayLog = api("/api/gateway/log").then(data => {
    if (page() !== "devtools" || revision !== gatewayRevision || gatewayCommand) return;
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
  const revision = runListRevision;
  const requestedGatewayRevision = gatewayRevision;
  try {
    const data = await api("/api/runs");
    if (revision !== runListRevision) return;
    runs = data.runs;
    writable = data.writable;
    if (requestedGatewayRevision === gatewayRevision && !gatewayCommand) gateway = data.gateway;
    obsidianAvailable = data.obsidian_available === true;
    if (!runs.some(run => run.id === selected)) setSelection(runs[0]?.id);
    const id = selected;
    if (id) {
      const snapshot = await api(`/api/runs/${id}`);
      if (revision !== runListRevision) return;
      if (selected === id) {
        if (snapshot.attempts.at(-1)?.id !== current?.attempts.at(-1)?.id) attempt = undefined;
        current = snapshot;
      }
    } else current = undefined;
    render();
    await refreshLogs();
  } catch (error) {
    if (revision !== runListRevision) return;
    get("connection").textContent = "连接异常";
    message(error.message, true);
  } finally {
    refreshing = false;
    if (revision !== runListRevision || requestedGatewayRevision !== gatewayRevision) refresh();
  }
}

function renderDeleteForm() {
  const confirmation = get("delete-confirmation");
  confirmation.disabled = posting;
  get("delete-cancel").disabled = posting;
  const submit = get("delete-submit");
  submit.disabled = posting || confirmation.value !== deleteTarget?.vault_name;
  submit.textContent = posting ? "正在删除…" : "永久删除";
}

function beginDelete(run) {
  deleteTarget = run;
  facts(get("delete-target"), [["Vault 名称", run.vault_name], ["Vault 目录", run.vault_path], ["运行编号", run.id]]);
  get("delete-confirmation").value = "";
  get("delete-error").hidden = true;
  get("delete-pending").hidden = !run.deletion_pending;
  renderDeleteForm();
  get("delete-dialog").showModal();
}

async function deleteRun(event) {
  event.preventDefault();
  const run = deleteTarget;
  const confirmation = get("delete-confirmation").value;
  if (!run || posting || confirmation !== run.vault_name) return;
  posting = true;
  const errorNotice = get("delete-error");
  errorNotice.hidden = true;
  renderDeleteForm();
  render();
  try {
    await api(`/api/runs/${encodeURIComponent(run.id)}/delete`, {confirmation});
    runListRevision += 1;
    runs = runs.filter(item => item.id !== run.id);
    if (selected === run.id) setSelection(runs[0]?.id);
    message(`已删除「${run.vault_name}」的运行记录、Vault 及关联产物。`);
    get("delete-dialog").close();
  } catch (error) {
    errorNotice.textContent = error.message;
    errorNotice.hidden = false;
  } finally {
    posting = false;
    renderDeleteForm();
    render();
    refresh();
  }
}

async function submit(path, body) {
  if (posting) return;
  posting = true;
  render();
  try {
    const data = await api(path, body);
    message(path.endsWith("/pause") ? "暂停已请求，执行器会在指定节点前停下。"
      : data.preview ? "已核对 Map 恢复范围。" : "执行请求已提交。");
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

function reenter(action, manifest, pauseBefore, fragments, requestId) {
  if (action.id.startsWith("incremental-")) {
    if (action.id === "incremental-closure-confirm" &&
        !window.confirm(`确认按已核对的报告结案？\n报告 SHA-256 ${action.report_sha256}`)) return;
    if (action.id === "incremental-map-recovery-confirm") {
      if (!requestId) return;
      const source = action.source_path.split("/").at(-1);
      const stages = action.authorized_stages?.map(stage => incrementalStageNames[stage] || stage).join(" → ");
      const automatic = action.execution_scope === "automatic" && action.strategy === "supplemental";
      if (automatic && !stages) {
        message("请重新预览补跑授权范围。", true);
        return;
      }
      const confirmation = automatic
        ? `确认对「${source}」自动补跑？\n补齐所选的 1 项证据映射请求后，按以下顺序自动推进。\n${stages}\n协调和写作会使用模型。合格页面会发布并同步索引。网络故障或用量待核对时会暂停。`
        : `确认对「${source}」恢复所选映射请求？`;
      if (!window.confirm(confirmation)) return;
    }
    if (action.id === "incremental-map-supplement-continue" &&
        !window.confirm(`确认从补做 Map 停点继续？\n补做运行 ${action.supplement_run_id}\n下一步开始补充轮次的协调和写作。`)) return;
    if (action.scope_token) {
      const scope = action.round_id ? `共享轮次 ${action.round_id}` : action.source_path;
      const fragmentScope = fragments ? `，已选 ${fragments.length} 个分片` : "";
      if (!window.confirm(`${action.label}？\n${scope}${fragmentScope}`)) return;
    }
    submit(`/api/runs/${current.id}/reenter`, {action: action.id, node: action.node, attempt: action.attempt,
      control_revision: action.control_revision, run_revision: action.run_revision,
      source_path: action.source_path, source_sha256: action.source_sha256, retry_kind: action.retry_kind,
      phase: action.phase, round_id: action.round_id, scope_token: action.scope_token, revise_fragments: fragments,
      boundary_fingerprint: action.boundary_fingerprint,
      preview_attempt: action.preview_attempt, report_sha256: action.report_sha256,
      preview_key: action.preview_key, strategy: action.strategy, request_id: requestId || undefined,
      execution_scope: action.execution_scope, authorized_stages: action.authorized_stages,
      supplement_run_id: action.supplement_run_id, supplement_run_revision: action.supplement_run_revision,
      supplement_control_revision: action.supplement_control_revision, recovery_operation_id: action.recovery_operation_id});
    return;
  }
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

function renderSourceDraft() {
  get("source-draft-count").textContent = `${draftSources.length} 个地址`;
  get("source-draft-list").replaceChildren(...(draftSources.length ? draftSources.map((path, index) => {
    const item = make("li");
    const name = path.replace(/\\/g, "/").replace(/\/$/, "").split("/").at(-1) || path;
    const text = make("div");
    text.append(make("strong", name), make("small", path));
    const remove = rowButton("移除", () => { draftSources.splice(index, 1); renderSourceDraft(); });
    remove.setAttribute("aria-label", `移除 ${name}`);
    item.append(text, remove);
    return item;
  }) : [make("li", "加入文件或文件夹后，会在这里逐条显示。", "hint")]));
}

function addDraftSource() {
  const path = get("sources").value.trim();
  if (!path) { get("sources").focus(); return false; }
  if (draftSources.includes(path)) {
    message("这个地址已经在待处理列表中。", true);
    return false;
  }
  draftSources.push(path);
  get("sources").value = "";
  renderSourceDraft();
  return true;
}

get("add-source").addEventListener("click", addDraftSource);
get("sources").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); addDraftSource(); }
});
renderSourceDraft();

get("create").addEventListener("submit", async event => {
  event.preventDefault();
  if (get("sources").value.trim()) addDraftSource();
  if (!draftSources.length) { message("请先把文档或文件夹加入待处理列表。", true); return; }
  const body = {
    sources: [...draftSources],
    vault_path: get("vault-path").value, vault_name: get("vault-name").value,
    pause_before: get("pause-before").value || undefined,
  };
  if (get("workflow-mode").value === "incremental_v1") {
    delete body.pause_before;
    body.workflow_mode = "incremental_v1";
    body.incremental_pause_after = [...get("inc-pauses").selectedOptions].map(option => option.value);
    if (get("inc-manual-sources").checked) body.incremental_manual_sources = true;
  }
  if (await submit("/api/runs", body)) {
    draftSources.splice(0);
    renderSourceDraft();
    event.target.reset();
    get("workflow-mode").dispatchEvent(new Event("change"));
  }
});
get("workflow-mode").addEventListener("change", () => {
  const incremental = get("workflow-mode").value === "incremental_v1";
  get("incremental-options").hidden = !incremental;
  get("pause-before").hidden = incremental;
  get("legacy-pause-label").hidden = incremental;
});
get("toggle-form").addEventListener("click", () => showForm(get("new-panel").hidden));
get("gateway-start").addEventListener("click", () => controlGateway("start"));
get("gateway-stop").addEventListener("click", () => controlGateway("stop"));
get("delete-confirmation").addEventListener("input", renderDeleteForm);
get("delete-form").addEventListener("submit", deleteRun);
get("delete-cancel").addEventListener("click", () => get("delete-dialog").close());
get("delete-dialog").addEventListener("cancel", event => {
  if (posting) event.preventDefault();
});
get("delete-dialog").addEventListener("close", () => {
  const rows = get("runs");
  const row = [...rows.children].find(item => item.dataset.runId === deleteTarget?.id);
  const focus = row?.querySelector(".run-delete:not(:disabled)") ||
    rows.querySelector(".run-row.selected .run-item") || get("history-title");
  deleteTarget = undefined;
  focus.focus();
});
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
