const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const DEFAULT_TZ = "America/Bahia";
const DASHBOARD_REFRESH_MS = 60_000;

let dashboardUiState = {
  query: "",
  filter: "all",
};

let dashboardRefreshInFlight = false;
let dashboardRefreshTimer = null;

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function getJSON(path, optional = false) {
  try {
    const separator = path.includes("?") ? "&" : "?";
    const response = await fetch(`${path}${separator}v=${Date.now()}`, {
      cache: "no-store",
      headers: { "Cache-Control": "no-cache" },
    });
    if (!response.ok) throw new Error(`Falha HTTP ${response.status}`);
    return response.json();
  } catch (error) {
    if (optional) return null;
    throw error;
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function getJSONRetry(path, {
  attempts = 3,
  delayMs = 1200,
  optional = false,
} = {}) {
  let lastError = null;

  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const data = await getJSON(path, false);
      if (data !== null && data !== undefined) return data;
    } catch (error) {
      lastError = error;
    }

    if (attempt < attempts) {
      await sleep(delayMs * attempt);
    }
  }

  if (optional) return null;
  throw lastError || new Error(`Não foi possível carregar ${path}`);
}

function fmtDate(value, fallback = "—", timeZone = DEFAULT_TZ) {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone,
  }).format(date);
}

function fmtRelative(value) {
  if (!value) return "—";
  const ms = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ms)) return "—";
  const minutes = Math.max(0, Math.floor(ms / 60000));
  if (minutes < 1) return "agora";
  if (minutes < 60) return `há ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `há ${hours} h`;
  const days = Math.floor(hours / 24);
  return `há ${days} ${days === 1 ? "dia" : "dias"}`;
}

function localParts(date = new Date(), timeZone = DEFAULT_TZ) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  return Object.fromEntries(parts.filter(p => p.type !== "literal").map(p => [p.type, p.value]));
}

function localDayKey(date, timeZone = DEFAULT_TZ) {
  const p = localParts(date, timeZone);
  return `${p.year}-${p.month}-${p.day}`;
}

function hhmmToMinutes(value = "00:00") {
  const [h, m] = value.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}

function isInsideWindow(config, now = new Date()) {
  const tz = config.fuso_horario || DEFAULT_TZ;
  const parts = localParts(now, tz);
  const current = Number(parts.hour) * 60 + Number(parts.minute);
  const start = hhmmToMinutes(config.monitoramento?.inicio || "08:00");
  const end = hhmmToMinutes(config.monitoramento?.fim || "20:00");
  return current >= start && current < end;
}

function statusFor(process, config) {
  const failures = Number(process.consecutive_failures || 0);
  const now = new Date();
  const tz = config.fuso_horario || DEFAULT_TZ;
  const changeHours = Number(config.dashboard?.destaque_alteracao_horas ?? 24);
  const lateAfter = Number(config.dashboard?.atraso_apos_minutos ?? 75);
  const recentChange = process.last_change_at &&
    (now.getTime() - new Date(process.last_change_at).getTime()) <= changeHours * 3600000;

  if (failures > 0) {
    return { key: "error", label: "Erro de consulta", detail: `${failures} falha(s) consecutiva(s)` };
  }

  if (recentChange) {
    return { key: "change", label: "Alteração detectada", detail: fmtRelative(process.last_change_at) };
  }

  const inside = isInsideWindow(config, now);
  if (!inside) {
    return { key: "paused", label: "Fora da janela", detail: "Retoma às 08:07" };
  }

  if (!process.last_success_at) {
    return { key: "waiting", label: "Aguardando verificação", detail: "Sem consulta bem-sucedida registrada" };
  }

  const ageMinutes = (now.getTime() - new Date(process.last_success_at).getTime()) / 60000;
  const today = localDayKey(now, tz);
  const successDay = localDayKey(new Date(process.last_success_at), tz);
  const start = hhmmToMinutes(config.monitoramento?.inicio || "08:00");
  const p = localParts(now, tz);
  const current = Number(p.hour) * 60 + Number(p.minute);

  // No começo da manhã, a última consulta legítima pode ser a do dia anterior.
  if (successDay !== today && current <= start + lateAfter) {
    return { key: "waiting", label: "Aguardando ciclo", detail: "Primeira verificação do dia ainda pode ocorrer" };
  }

  if (ageMinutes > lateAfter) {
    return { key: "delayed", label: "Monitor atrasado", detail: `Último sucesso ${fmtRelative(process.last_success_at)}` };
  }

  return { key: "operational", label: "Operacional", detail: `Verificado ${fmtRelative(process.last_success_at)}` };
}

function processHref(id) {
  return `./?processo=${encodeURIComponent(id)}`;
}

function statusBadge(status, compact = false) {
  return `<span class="state-badge state-${status.key}${compact ? " compact" : ""}"><span class="state-dot"></span>${escapeHtml(status.label)}</span>`;
}

function mergeConfiguredProcesses(config, summary) {
  const summaryMap = new Map((summary?.processes || []).map(p => [p.id, p]));
  return (config.processos || [])
    .filter(p => p.ativo !== false)
    .map(p => {
      const current = summaryMap.get(p.id) || {};
      return {
        id: p.id,
        numero: p.numero,
        nome: p.nome || p.numero,
        grupo: p.grupo || "Geral",
        url: p.url,
        ativo: p.ativo !== false,
        initialized: false,
        counts: { protocolos: null, andamentos: null },
        consecutive_failures: 0,
        ...current,
      };
    });
}

async function hydrateDashboardProcesses(config, processes) {
  const firstId = config.processos?.[0]?.id;
  const legacy = await getJSONRetry("data/state.json", {
    attempts: 2,
    delayMs: 700,
    optional: true,
  });

  return Promise.all(processes.map(async process => {
    let state = await getJSONRetry(`data/processos/${process.id}/state.json`, {
      attempts: 2,
      delayMs: 700,
      optional: true,
    });

    // Compatibilidade com a versão de processo único.
    if (!state && process.id === firstId && legacy?.initialized) {
      state = legacy;
    }

    if (!state) return process;

    return {
      ...process,
      ...state,

      // Metadados editoriais continuam vindo de config.json.
      id: process.id,
      numero: process.numero,
      nome: process.nome || process.numero,
      grupo: process.grupo || "Geral",
      url: process.url,

      initialized: Boolean(state.initialized),
      counts: state.counts || process.counts,
    };
  }));
}

function setGlobalPill(status, label) {
  const pill = $("#global-status");
  pill.className = `status-pill ${status}`;
  $("span:last-child", pill).textContent = label;
}

function cardHtml(process, config) {
  const status = statusFor(process, config);
  const protocols = process.initialized
    ? (process.counts?.protocolos ?? "—")
    : "—";
  const movements = process.initialized
    ? (process.counts?.andamentos ?? "—")
    : "—";
  return `
    <article class="process-card state-edge-${status.key}" data-search="${escapeHtml(`${process.numero} ${process.nome} ${process.grupo}`.toLowerCase())}" data-status="${status.key}">
      <div class="process-card-head">
        <div>
          <div class="card-kicker">${escapeHtml(process.grupo || "Geral")}</div>
          <h3>${escapeHtml(process.nome || process.numero)}</h3>
          <div class="process-number-small">${escapeHtml(process.numero)}</div>
        </div>
        ${statusBadge(status)}
      </div>
      <div class="process-stats">
        <div><strong>${protocols}</strong><span>Protocolos</span></div>
        <div><strong>${movements}</strong><span>Andamentos</span></div>
      </div>
      <div class="process-meta-grid">
        <div><span>Última alteração</span><strong>${process.last_change_at ? fmtDate(process.last_change_at) : "Nenhuma registrada"}</strong></div>
        <div><span>Última consulta válida</span><strong>${process.last_success_at ? fmtDate(process.last_success_at) : (process.initialized ? "Linha de base preservada" : "Aguardando")}</strong></div>
      </div>
      ${process.last_error ? `<div class="inline-alert"><strong>Último erro:</strong> ${escapeHtml(process.last_error)}</div>` : ""}
      <div class="card-actions">
        <a class="secondary-button" href="${processHref(process.id)}">Ver detalhes</a>
        <a class="link-button" href="${escapeHtml(process.url || "#")}" target="_blank" rel="noopener noreferrer">Abrir no SEI ↗</a>
      </div>
    </article>`;
}

function renderDashboard(config, summary, processes) {
  const preservedQuery = $("#process-search")?.value ?? dashboardUiState.query;
  const preservedFilter = $(".filter.active")?.dataset.filter ?? dashboardUiState.filter;

  const statuses = processes.map(p => statusFor(p, config));
  const changes = statuses.filter(s => s.key === "change").length;
  const problems = statuses.filter(s => ["error", "delayed"].includes(s.key)).length;
  const operational = statuses.filter(s => s.key === "operational").length;
  const latestCheck = processes.map(p => p.last_attempt_at || p.last_success_at).filter(Boolean).sort().at(-1);

  const app = $("#app");
  app.innerHTML = `
    <section class="dashboard-hero">
      <div>
        <p class="eyebrow">PAINEL DE ACOMPANHAMENTO</p>
        <h1>Processos SEI/IPHAN</h1>
        <p class="hero-copy">Monitoramento independente de múltiplos processos públicos, com comparação semântica de Protocolos e Andamentos e registro das alterações detectadas.</p>
      </div>
      <div class="window-note">
        <span>Janela automática</span>
        <strong>${escapeHtml(config.monitoramento?.execucoes || "08:07–19:37")}</strong>
        <small>a cada ${escapeHtml(config.monitoramento?.intervalo_minutos || 30)} min</small>
      </div>
    </section>

    <section class="overview-metrics">
      <article><span>Processos monitorados</span><strong>${processes.length}</strong></article>
      <article><span>Operacionais</span><strong>${operational}</strong></article>
      <article><span>Alterações recentes</span><strong>${changes}</strong></article>
      <article><span>Atenção necessária</span><strong>${problems}</strong></article>
      <article class="wide-metric"><span>Última informação registrada</span><strong>${latestCheck ? fmtDate(latestCheck) : "Aguardando execução"}</strong></article>
    </section>

    <section class="toolbar-panel">
      <label class="search-box">
        <span aria-hidden="true">⌕</span>
        <input id="process-search" type="search" placeholder="Pesquisar número, nome ou grupo…" autocomplete="off">
      </label>
      <div class="filters" role="group" aria-label="Filtrar processos">
        <button class="filter ${preservedFilter === "all" ? "active" : ""}" data-filter="all">Todos</button>
        <button class="filter ${preservedFilter === "change" ? "active" : ""}" data-filter="change">Alterados</button>
        <button class="filter ${preservedFilter === "operational" ? "active" : ""}" data-filter="operational">Operacionais</button>
        <button class="filter ${preservedFilter === "attention" ? "active" : ""}" data-filter="attention">Com atenção</button>
      </div>
    </section>

    <section class="process-grid" id="process-grid">
      ${processes.map(p => cardHtml(p, config)).join("") || `<div class="empty-state panel">Nenhum processo ativo foi configurado.</div>`}
    </section>
    <div class="no-results" id="no-results" hidden>Nenhum processo corresponde ao filtro atual.</div>
  `;

  const searchInput = $("#process-search");
  if (searchInput) searchInput.value = preservedQuery;

  const applyFilters = () => {
    const query = $("#process-search").value.trim().toLowerCase();
    const selected = $(".filter.active")?.dataset.filter || "all";

    dashboardUiState.query = $("#process-search").value;
    dashboardUiState.filter = selected;
    let visible = 0;
    $$(".process-card").forEach(card => {
      const matchesText = !query || card.dataset.search.includes(query);
      const status = card.dataset.status;
      const matchesStatus = selected === "all" ||
        status === selected ||
        (selected === "attention" && ["error", "delayed"].includes(status));
      const show = matchesText && matchesStatus;
      card.hidden = !show;
      if (show) visible++;
    });
    $("#no-results").hidden = visible > 0;
  };

  $("#process-search").addEventListener("input", applyFilters);
  $$(".filter").forEach(button => button.addEventListener("click", () => {
    $$(".filter").forEach(b => b.classList.remove("active"));
    button.classList.add("active");
    applyFilters();
  }));

  if (problems > 0) setGlobalPill("fail", `${problems} processo(s) exigem atenção`);
  else if (changes > 0) setGlobalPill("change", `${changes} alteração(ões) recente(s)`);
  else if (!isInsideWindow(config)) setGlobalPill("paused", "Fora da janela automática");
  else setGlobalPill("ok", `${operational}/${processes.length} operacionais`);
}

function renderHistory(history) {
  if (!history?.length) {
    return `<div class="empty-state">Ainda não houve alteração após a criação da linha de base. Quando o SEI mudar, o evento aparecerá aqui e uma Issue será aberta no GitHub.</div>`;
  }
  return history.map(event => {
    const s = event.summary || {};
    const chips = [
      [s.protocolos_adicionados, "novo(s) protocolo(s)"],
      [s.protocolos_modificados, "protocolo(s) modificado(s)"],
      [s.protocolos_removidos, "protocolo(s) removido(s)"],
      [s.andamentos_adicionados, "novo(s) andamento(s)"],
      [s.andamentos_removidos, "andamento(s) removido(s)"],
    ].filter(([count]) => Number(count) > 0);
    return `<article class="timeline-item">
      <span class="timeline-marker" aria-hidden="true"></span>
      <div>
        <div class="timeline-date">${fmtDate(event.detected_at)}</div>
        <div class="timeline-title">Alteração processual detectada</div>
        <div class="chips">${chips.map(([count, label]) => `<span class="chip">${count} ${label}</span>`).join("")}</div>
      </div>
    </article>`;
  }).join("");
}

function recordsProtocols(state) {
  const rows = (state.protocolos || []).slice(-6).reverse();
  if (!rows.length) return `<div class="empty-state">Nenhum protocolo carregado.</div>`;
  return rows.map(row => `<article class="record">
    <span class="record-id">${escapeHtml(row.documento)}</span>
    <p class="record-type">${escapeHtml(row.tipo)}</p>
    <div class="record-meta">Inclusão: ${escapeHtml(row.data_inclusao)} · ${escapeHtml(row.unidade)}</div>
  </article>`).join("");
}

function recordsMovements(state) {
  const rows = (state.andamentos || []).slice(0, 6);
  if (!rows.length) return `<div class="empty-state">Nenhum andamento carregado.</div>`;
  return rows.map(row => `<article class="record">
    <span class="record-id">${escapeHtml(row.data_hora)}</span>
    <p class="record-type">${escapeHtml(row.descricao)}</p>
    <div class="record-meta">${escapeHtml(row.unidade)}</div>
  </article>`).join("");
}

async function loadProcessData(id, config) {
  let state = await getJSONRetry(`data/processos/${id}/state.json`, {
    attempts: 3,
    delayMs: 900,
    optional: true,
  });
  let history = await getJSONRetry(`data/processos/${id}/history.json`, {
    attempts: 2,
    delayMs: 700,
    optional: true,
  });
  if (!state && config.processos?.[0]?.id === id) {
    state = await getJSONRetry("data/state.json", {
      attempts: 2,
      delayMs: 700,
      optional: true,
    });
    history = await getJSONRetry("data/history.json", {
      attempts: 2,
      delayMs: 700,
      optional: true,
    });
  }
  return { state, history: history || [] };
}

async function renderDetail(config, process) {
  const { state, history } = await loadProcessData(process.id, config);
  const merged = {
    id: process.id,
    process_id: process.id,
    process_number: process.numero,
    process_name: process.nome || process.numero,
    group: process.grupo || "Geral",
    source_url: process.url,
    counts: { protocolos: null, andamentos: null },
    consecutive_failures: 0,
    ...(state || {}),
  };
  const status = statusFor({
    id: merged.process_id,
    numero: merged.process_number,
    nome: merged.process_name,
    grupo: merged.group,
    url: merged.source_url,
    ...merged,
  }, config);

  $("#app").innerHTML = `
    <a class="back-link" href="./">← Todos os processos</a>
    <section class="detail-hero">
      <div>
        <div class="detail-topline">
          <p class="eyebrow">${escapeHtml(merged.group || "Geral")}</p>
          ${statusBadge(status)}
        </div>
        <h1>${escapeHtml(merged.process_number || process.numero)}</h1>
        <h2 class="process-name-title">${escapeHtml(merged.process_name || process.nome || process.numero)}</h2>
        <p class="hero-copy">Comparação automática das tabelas públicas de Protocolos e Andamentos. Uma nova linha de base é criada apenas para processos ainda não inicializados.</p>
      </div>
      <a class="primary-button" href="${escapeHtml(merged.source_url || process.url)}" target="_blank" rel="noopener noreferrer">Abrir no SEI/IPHAN ↗</a>
    </section>

    ${merged.last_error ? `<div class="detail-alert"><strong>Problema na última tentativa:</strong><span>${escapeHtml(merged.last_error)}</span><small>${merged.last_error_at ? fmtDate(merged.last_error_at) : ""}</small></div>` : ""}

    <section class="metrics" aria-label="Resumo do processo">
      <article class="metric-card"><span class="metric-label">Protocolos</span><strong class="metric-value">${merged.counts?.protocolos ?? "—"}</strong><span class="metric-help">documentos/protocolos visíveis</span></article>
      <article class="metric-card"><span class="metric-label">Andamentos</span><strong class="metric-value">${merged.counts?.andamentos ?? "—"}</strong><span class="metric-help">movimentações processuais</span></article>
      <article class="metric-card"><span class="metric-label">Última alteração</span><strong class="metric-date">${merged.last_change_at ? fmtDate(merged.last_change_at) : "Nenhuma desde a linha de base"}</strong><span class="metric-help">detectada pelo monitor</span></article>
      <article class="metric-card"><span class="metric-label">Última consulta válida</span><strong class="metric-date">${merged.last_success_at ? fmtDate(merged.last_success_at) : (merged.initialized ? "Aguardando novo ciclo" : "Ainda não inicializado")}</strong><span class="metric-help">resposta interpretada com sucesso</span></article>
    </section>

    <section class="content-grid">
      <div class="panel">
        <div class="panel-heading">
          <div><p class="eyebrow">HISTÓRICO</p><h2>Alterações detectadas</h2></div>
          <span class="counter">${history.length} ${history.length === 1 ? "registro" : "registros"}</span>
        </div>
        <div class="timeline">${renderHistory(history)}</div>
      </div>
      <aside class="side-stack">
        <section class="panel compact"><p class="eyebrow">ÚLTIMOS DOCUMENTOS</p><h2>Protocolos recentes</h2><div class="record-list">${recordsProtocols(merged)}</div></section>
        <section class="panel compact"><p class="eyebrow">ÚLTIMAS MOVIMENTAÇÕES</p><h2>Andamentos recentes</h2><div class="record-list">${recordsMovements(merged)}</div></section>
      </aside>
    </section>`;

  setGlobalPill(
    status.key === "operational" ? "ok" : status.key,
    status.label,
  );
}

async function loadWorkflowStatus(config, processes) {
  const host = window.location.hostname;
  if (!host.endsWith(".github.io")) return;
  const owner = host.split(".")[0];
  const repo = window.location.pathname.split("/").filter(Boolean)[0] || `${owner}.github.io`;
  try {
    const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/monitor.yml/runs?per_page=1`, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!response.ok) return;
    const data = await response.json();
    const run = data.workflow_runs?.[0];
    if (!run) return;
    if (["queued", "in_progress", "waiting", "pending"].includes(run.status)) {
      setGlobalPill("neutral", `GitHub Actions: ${run.status === "queued" ? "em fila" : "em execução"}`);
      return;
    }
    const problemCount = processes.filter(p => ["error", "delayed"].includes(statusFor(p, config).key)).length;
    if (run.conclusion === "failure" && problemCount === 0) {
      setGlobalPill("fail", "Último workflow falhou");
    }
  } catch (error) {
    console.warn("Não foi possível consultar a API do GitHub", error);
  }
}

async function loadDashboardSnapshot(config) {
  const summary = await getJSONRetry("data/summary.json", {
    attempts: 3,
    delayMs: 1000,
    optional: true,
  });

  let processes = mergeConfiguredProcesses(config, summary);

  // O resumo é útil, mas cada state.json é a fonte autoritativa para o card.
  // Isso evita que um summary.json temporariamente defasado mostre 0/0.
  processes = await hydrateDashboardProcesses(config, processes);

  return { summary, processes };
}

async function refreshDashboard(config, { checkWorkflow = false } = {}) {
  if (dashboardRefreshInFlight) return;
  if (new URLSearchParams(window.location.search).get("processo")) return;

  dashboardRefreshInFlight = true;

  try {
    const { summary, processes } = await loadDashboardSnapshot(config);
    renderDashboard(config, summary, processes);

    if (checkWorkflow) {
      loadWorkflowStatus(config, processes);
    }
  } catch (error) {
    console.warn("Atualização automática do painel não concluída:", error);
  } finally {
    dashboardRefreshInFlight = false;
  }
}

function startDashboardAutoRefresh(config) {
  if (dashboardRefreshTimer) {
    clearInterval(dashboardRefreshTimer);
  }

  dashboardRefreshTimer = setInterval(() => {
    if (document.visibilityState === "visible") {
      refreshDashboard(config);
    }
  }, DASHBOARD_REFRESH_MS);

  // Ao retornar para a aba/app, sincroniza imediatamente.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshDashboard(config);
    }
  });

  // Importante em celulares e no botão "voltar", quando a página pode vir do BFCache.
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      refreshDashboard(config);
    }
  });
}

async function init() {
  try {
    const config = await getJSONRetry("config.json", {
      attempts: 3,
      delayMs: 800,
    });

    const selectedId = new URLSearchParams(window.location.search).get("processo");
    if (selectedId) {
      const process = config.processos?.find(p => p.id === selectedId);
      if (!process) {
        $("#app").innerHTML = `<section class="panel"><h2>Processo não localizado</h2><p class="empty-state">O identificador informado não existe em config.json.</p><a class="secondary-button" href="./">Voltar ao painel</a></section>`;
        setGlobalPill("fail", "Processo não localizado");
        return;
      }
      await renderDetail(config, process);
    } else {
      const { summary, processes } = await loadDashboardSnapshot(config);
      renderDashboard(config, summary, processes);
      loadWorkflowStatus(config, processes);
      startDashboardAutoRefresh(config);
    }
  } catch (error) {
    console.error(error);
    $("#app").innerHTML = `<section class="panel"><h2>Não foi possível carregar o monitor</h2><p class="empty-state">Verifique config.json e a publicação dos arquivos no GitHub Pages.</p></section>`;
    setGlobalPill("fail", "Falha ao carregar dados");
  }
}

init();
