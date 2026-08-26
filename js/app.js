const $ = (selector) => document.querySelector(selector);

const fmtDate = (value, fallback = "—") => {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "America/Recife",
  }).format(date);
};

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function getJSON(path) {
  const response = await fetch(`${path}?v=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Falha HTTP ${response.status}`);
  return response.json();
}

function renderState(state) {
  if (!state?.initialized) {
    $("#timeline").innerHTML = `<div class="empty-state">${escapeHtml(state?.message || "Aguardando a primeira execução do monitor.")}</div>`;
    return;
  }

  $("#process-number").textContent = state.process_number || "—";
  $("#sei-link").href = state.source_url || "#";
  $("#protocol-count").textContent = state.counts?.protocolos ?? state.protocolos?.length ?? "—";
  $("#movement-count").textContent = state.counts?.andamentos ?? state.andamentos?.length ?? "—";
  $("#last-change").textContent = state.last_change_at ? fmtDate(state.last_change_at) : "Nenhuma desde a linha de base";

  const protocols = (state.protocolos || []).slice(-6).reverse();
  $("#latest-protocols").innerHTML = protocols.length ? protocols.map(row => `
    <article class="record">
      <span class="record-id">${escapeHtml(row.documento)}</span>
      <p class="record-type">${escapeHtml(row.tipo)}</p>
      <div class="record-meta">Inclusão: ${escapeHtml(row.data_inclusao)} · ${escapeHtml(row.unidade)}</div>
    </article>
  `).join("") : `<div class="empty-state">Nenhum protocolo carregado.</div>`;

  const movements = (state.andamentos || []).slice(0, 6);
  $("#latest-movements").innerHTML = movements.length ? movements.map(row => `
    <article class="record">
      <span class="record-id">${escapeHtml(row.data_hora)}</span>
      <p class="record-type">${escapeHtml(row.descricao)}</p>
      <div class="record-meta">${escapeHtml(row.unidade)}</div>
    </article>
  `).join("") : `<div class="empty-state">Nenhum andamento carregado.</div>`;
}

function renderHistory(history) {
  $("#history-count").textContent = `${history.length} ${history.length === 1 ? "registro" : "registros"}`;
  if (!history.length) {
    $("#timeline").innerHTML = `<div class="empty-state">Ainda não houve alteração após a criação da linha de base. Quando o SEI mudar, o evento aparecerá aqui e uma Issue será aberta no GitHub.</div>`;
    return;
  }

  $("#timeline").innerHTML = history.map(event => {
    const s = event.summary || {};
    const chips = [
      [s.protocolos_adicionados, "novo(s) protocolo(s)"],
      [s.protocolos_modificados, "protocolo(s) modificado(s)"],
      [s.protocolos_removidos, "protocolo(s) removido(s)"],
      [s.andamentos_adicionados, "novo(s) andamento(s)"],
      [s.andamentos_removidos, "andamento(s) removido(s)"],
    ].filter(([count]) => Number(count) > 0);
    return `
      <article class="timeline-item">
        <span class="timeline-marker" aria-hidden="true"></span>
        <div>
          <div class="timeline-date">${fmtDate(event.detected_at)}</div>
          <div class="timeline-title">Alteração processual detectada</div>
          <div class="chips">${chips.map(([count, label]) => `<span class="chip">${count} ${label}</span>`).join("")}</div>
        </div>
      </article>`;
  }).join("");
}

function githubRepoFromPages() {
  const host = window.location.hostname;
  if (!host.endsWith(".github.io")) return null;
  const owner = host.split(".")[0];
  const firstPath = window.location.pathname.split("/").filter(Boolean)[0];
  const repo = firstPath || `${owner}.github.io`;
  return { owner, repo };
}

async function loadWorkflowStatus() {
  const pill = $("#workflow-status");
  const repo = githubRepoFromPages();
  if (!repo) {
    pill.querySelector("span:last-child").textContent = "Status disponível no GitHub Pages";
    return;
  }
  try {
    const url = `https://api.github.com/repos/${repo.owner}/${repo.repo}/actions/workflows/monitor.yml/runs?per_page=1`;
    const response = await fetch(url, { headers: { "Accept": "application/vnd.github+json" } });
    if (!response.ok) throw new Error(`GitHub API ${response.status}`);
    const data = await response.json();
    const run = data.workflow_runs?.[0];
    if (!run) throw new Error("Nenhuma execução localizada");
    $("#last-check").textContent = fmtDate(run.updated_at || run.run_started_at || run.created_at);
    const ok = run.conclusion === "success";
    pill.classList.add(ok ? "ok" : "fail");
    pill.querySelector("span:last-child").textContent = ok ? "Monitor operacional" : `Última execução: ${run.conclusion || run.status}`;
  } catch (error) {
    pill.querySelector("span:last-child").textContent = "Status indisponível";
    $("#last-check").textContent = "Consulte Actions";
    console.warn(error);
  }
}

async function init() {
  try {
    const [state, history] = await Promise.all([
      getJSON("data/state.json"),
      getJSON("data/history.json"),
    ]);
    renderState(state);
    renderHistory(history);
  } catch (error) {
    $("#timeline").innerHTML = `<div class="empty-state">Não foi possível carregar os dados do monitor. Verifique a publicação do GitHub Pages.</div>`;
    console.error(error);
  }
  loadWorkflowStatus();
}

init();
