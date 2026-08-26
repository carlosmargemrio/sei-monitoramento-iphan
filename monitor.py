#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
PROCESSES_DIR = DATA_DIR / "processos"
SUMMARY_PATH = DATA_DIR / "summary.json"
LEGACY_STATE_PATH = DATA_DIR / "state.json"
LEGACY_HISTORY_PATH = DATA_DIR / "history.json"
RESULT_DIR = ROOT / ".monitor"
RESULT_PATH = RESULT_DIR / "result.json"
ISSUES_PATH = RESULT_DIR / "issues.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def norm(value: str) -> str:
    value = " ".join(value.replace("\xa0", " ").split()).strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.casefold()


def clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).strip()


def slug_process_number(value: str) -> str:
    value = clean(value)
    value = re.sub(r"[^0-9A-Za-z]+", "-", value).strip("-")
    return value or hashlib.sha256(clean(value).encode("utf-8")).hexdigest()[:16]


def process_id(process: dict[str, Any]) -> str:
    return clean(str(process.get("id") or "")) or slug_process_number(str(process.get("numero") or ""))


def direct_cells(tr) -> list[str]:
    return [
        clean(cell.get_text(" ", strip=True))
        for cell in tr.find_all(["th", "td"], recursive=False)
    ]


def find_table(soup: BeautifulSoup, required_headers: list[str]):
    required = [norm(x) for x in required_headers]
    for tr in soup.find_all("tr"):
        cells = direct_cells(tr)
        if len(cells) < len(required):
            continue
        normalized = [norm(c) for c in cells]
        if all(any(req in cell for cell in normalized) for req in required):
            table = tr.find_parent("table")
            if table is not None:
                return table, tr, cells
    raise RuntimeError(f"Tabela não localizada. Cabeçalhos esperados: {required_headers}")


def find_column(headers: list[str], names: list[str]) -> int:
    normalized = [norm(h) for h in headers]
    wanted = [norm(n) for n in names]
    for candidate in wanted:
        for idx, header in enumerate(normalized):
            if header == candidate:
                return idx
    for candidate in wanted:
        for idx, header in enumerate(normalized):
            if candidate in header:
                return idx
    raise RuntimeError(
        f"Coluna não localizada. Esperado um de {names}; cabeçalho recebido: {headers}"
    )


def rows_after_header(table, header_tr) -> list[list[str]]:
    rows: list[list[str]] = []
    found_header = False
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        if tr is header_tr:
            found_header = True
            continue
        if not found_header:
            continue
        values = direct_cells(tr)
        if values and any(values):
            rows.append(values)
    return rows


def is_date(value: str) -> bool:
    return bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", value))


def is_datetime(value: str) -> bool:
    return bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2})?\b", value))


def protocol_from_row(values: list[str], indexes: dict[str, int]) -> dict[str, str] | None:
    try:
        documento = values[indexes["documento"]]
        tipo = values[indexes["tipo"]]
        data = values[indexes["data"]]
        data_inclusao = values[indexes["data_inclusao"]]
        unidade = values[indexes["unidade"]]
        if re.search(r"\d", documento) and is_date(data) and is_date(data_inclusao):
            return {
                "documento": documento,
                "tipo": tipo,
                "data": data,
                "data_inclusao": data_inclusao,
                "unidade": unidade,
            }
    except IndexError:
        pass

    for start in range(0, max(0, len(values) - 4)):
        bloco = values[start:start + 5]
        if len(bloco) < 5:
            continue
        documento, tipo, data, data_inclusao, unidade = bloco
        if re.search(r"\d", documento) and is_date(data) and is_date(data_inclusao) and unidade:
            return {
                "documento": documento,
                "tipo": tipo,
                "data": data,
                "data_inclusao": data_inclusao,
                "unidade": unidade,
            }
    return None


def andamento_from_row(values: list[str], indexes: dict[str, int]) -> dict[str, str] | None:
    try:
        data_hora = values[indexes["data_hora"]]
        unidade = values[indexes["unidade"]]
        descricao = values[indexes["descricao"]]
        if is_datetime(data_hora) and unidade and descricao:
            return {"data_hora": data_hora, "unidade": unidade, "descricao": descricao}
    except IndexError:
        pass

    for start in range(0, max(0, len(values) - 2)):
        bloco = values[start:start + 3]
        if len(bloco) < 3:
            continue
        data_hora, unidade, descricao = bloco
        if is_datetime(data_hora) and unidade and descricao:
            return {"data_hora": data_hora, "unidade": unidade, "descricao": descricao}
    return None


def parse_protocolos(soup: BeautifulSoup) -> list[dict[str, str]]:
    table, header_tr, headers = find_table(soup, ["Processo", "Tipo", "Data", "Unidade"])
    indexes = {
        "documento": find_column(headers, ["Processo / Documento", "Processo", "Documento"]),
        "tipo": find_column(headers, ["Tipo"]),
        "data": find_column(headers, ["Data"]),
        "data_inclusao": find_column(headers, ["Data de Inclusão", "Data de Inclusao"]),
        "unidade": find_column(headers, ["Unidade"]),
    }
    data_rows = rows_after_header(table, header_tr)
    result = [parsed for values in data_rows if (parsed := protocol_from_row(values, indexes))]
    if not result:
        raise RuntimeError(
            "A tabela de Protocolos foi localizada, mas nenhum protocolo pôde ser "
            f"interpretado. Cabeçalho={headers!r}; amostra_linhas={data_rows[:3]!r}"
        )
    return result


def parse_andamentos(soup: BeautifulSoup) -> list[dict[str, str]]:
    table, header_tr, headers = find_table(soup, ["Data/Hora", "Unidade", "Descrição"])
    indexes = {
        "data_hora": find_column(headers, ["Data/Hora", "Data / Hora"]),
        "unidade": find_column(headers, ["Unidade"]),
        "descricao": find_column(headers, ["Descrição", "Descricao"]),
    }
    data_rows = rows_after_header(table, header_tr)
    result = [parsed for values in data_rows if (parsed := andamento_from_row(values, indexes))]
    if not result:
        raise RuntimeError(
            "A tabela de Andamentos foi localizada, mas nenhum andamento pôde ser "
            f"interpretado. Cabeçalho={headers!r}; amostra_linhas={data_rows[:3]!r}"
        )
    return result


def fetch_html(url: str, timeout: int) -> bytes:
    # Retry HTTP curto dentro de cada tentativa de alto nível.
    retry = Retry(
        total=1,
        connect=1,
        read=1,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    response = session.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": "SEI-IPHAN-Monitor/2.0 (+GitHub Actions; consulta pública e de baixa frequência)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    response.raise_for_status()
    return response.content


def fetch_snapshot(process: dict[str, Any], global_config: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    timeout = int(process.get("timeout_segundos", global_config.get("timeout_segundos", 30)))
    attempts = int(global_config.get("tentativas_por_processo", 3))
    waits = list(global_config.get("espera_entre_tentativas_segundos", [45, 90]))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"[{process['numero']}] tentativa {attempt}/{attempts}")
            html = fetch_html(process["url"], timeout)
            soup = BeautifulSoup(html, "html.parser")
            return parse_protocolos(soup), parse_andamentos(soup)
        except Exception as exc:  # mantém cada processo independente dos demais
            last_error = exc
            print(f"[{process['numero']}] falha na tentativa {attempt}: {type(exc).__name__}: {exc}")
            if attempt < attempts:
                wait = int(waits[min(attempt - 1, len(waits) - 1)]) if waits else 0
                if wait > 0:
                    print(f"[{process['numero']}] aguardando {wait}s antes da próxima tentativa")
                    time.sleep(wait)

    assert last_error is not None
    raise last_error


def row_key(row: dict[str, str]) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def protocol_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {}
    for row in rows:
        base = clean(row.get("documento", "")) or row_key(row)
        key = base
        counter = 2
        while key in mapped and mapped[key] != row:
            key = f"{base}#{counter}"
            counter += 1
        mapped[key] = row
    return mapped


def andamento_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row_key(row): row for row in rows}


def diff_maps(old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]) -> dict[str, list[Any]]:
    old_keys, new_keys = set(old), set(new)
    added = [new[k] for k in new if k in (new_keys - old_keys)]
    removed = [old[k] for k in old if k in (old_keys - new_keys)]
    modified = [{"antes": old[k], "depois": new[k]} for k in new if k in old and old[k] != new[k]]
    return {"added": added, "removed": removed, "modified": modified}


def short_protocol(row: dict[str, str]) -> str:
    return (
        f"{row.get('documento', '—')} — {row.get('tipo', '—')} — "
        f"inclusão {row.get('data_inclusao', '—')} — {row.get('unidade', '—')}"
    )


def short_andamento(row: dict[str, str]) -> str:
    return f"{row.get('data_hora', '—')} — {row.get('unidade', '—')} — {row.get('descricao', '—')}"


def summarize(diffs: dict[str, Any]) -> dict[str, int]:
    return {
        "protocolos_adicionados": len(diffs["protocolos"]["added"]),
        "protocolos_modificados": len(diffs["protocolos"]["modified"]),
        "protocolos_removidos": len(diffs["protocolos"]["removed"]),
        "andamentos_adicionados": len(diffs["andamentos"]["added"]),
        "andamentos_removidos": len(diffs["andamentos"]["removed"]),
    }


def build_report(process: dict[str, Any], detected_at: str, diffs: dict[str, Any], counts: dict[str, int]) -> str:
    name = process.get("nome") or process["numero"]
    lines = [
        f"## Alteração detectada — {name}",
        "",
        f"**Processo:** {process['numero']}",
        f"**Detectada em:** {detected_at}",
        f"**Estado atual:** {counts['protocolos']} protocolos e {counts['andamentos']} andamentos.",
        "",
    ]
    p = diffs["protocolos"]
    a = diffs["andamentos"]
    if p["added"]:
        lines += ["### Novos protocolos/documentos", ""]
        lines += [f"- {short_protocol(row)}" for row in p["added"]]
        lines.append("")
    if p["modified"]:
        lines += ["### Protocolos/documentos modificados", ""]
        for item in p["modified"]:
            lines.append(f"- Antes: {short_protocol(item['antes'])}")
            lines.append(f"  - Depois: {short_protocol(item['depois'])}")
        lines.append("")
    if p["removed"]:
        lines += ["### Protocolos/documentos removidos da listagem", ""]
        lines += [f"- {short_protocol(row)}" for row in p["removed"]]
        lines.append("")
    if a["added"]:
        lines += ["### Novos andamentos", ""]
        lines += [f"- {short_andamento(row)}" for row in a["added"]]
        lines.append("")
    if a["removed"]:
        lines += ["### Andamentos que deixaram de constar da listagem", ""]
        lines += [f"- {short_andamento(row)}" for row in a["removed"]]
        lines.append("")
    lines += [
        "---",
        f"[Abrir Pesquisa Processual do SEI/IPHAN]({process['url']})",
        "",
        "> Aviso gerado automaticamente por comparação semântica das tabelas públicas de Protocolos e Andamentos.",
    ]
    return "\n".join(lines) + "\n"


def process_paths(pid: str) -> tuple[Path, Path]:
    base = PROCESSES_DIR / pid
    return base / "state.json", base / "history.json"


def migrate_legacy_if_needed(process: dict[str, Any], state_path: Path, history_path: Path) -> None:
    if state_path.exists() or not LEGACY_STATE_PATH.exists():
        return
    legacy = load_json(LEGACY_STATE_PATH, {})
    if not legacy.get("initialized"):
        return
    if clean(str(legacy.get("process_number", ""))) != clean(str(process.get("numero", ""))):
        return

    migrated = dict(legacy)
    migrated.update({
        "process_id": process_id(process),
        "process_number": process["numero"],
        "process_name": process.get("nome") or process["numero"],
        "group": process.get("grupo") or "Geral",
        "source_url": process["url"],
        "last_attempt_at": migrated.get("last_attempt_at"),
        "last_success_at": migrated.get("last_success_at"),
        "last_error_at": None,
        "last_error": None,
        "consecutive_failures": 0,
    })
    save_json(state_path, migrated)
    if LEGACY_HISTORY_PATH.exists():
        history_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LEGACY_HISTORY_PATH, history_path)
    else:
        save_json(history_path, [])
    print(f"[{process['numero']}] linha de base legada migrada para {state_path.relative_to(ROOT)}")


def compact_error(exc: Exception, max_len: int = 300) -> str:
    text = clean(f"{type(exc).__name__}: {exc}")
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def process_one(process: dict[str, Any], global_config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pid = process_id(process)
    state_path, history_path = process_paths(pid)
    migrate_legacy_if_needed(process, state_path, history_path)

    state = load_json(state_path, {"initialized": False})
    history = load_json(history_path, [])
    checked_at = now_iso()

    base_meta = {
        "process_id": pid,
        "process_number": process["numero"],
        "process_name": process.get("nome") or process["numero"],
        "group": process.get("grupo") or "Geral",
        "source_url": process["url"],
        "active": bool(process.get("ativo", True)),
    }

    try:
        protocolos, andamentos = fetch_snapshot(process, global_config)
    except Exception as exc:
        failed_state = {
            **state,
            **base_meta,
            "initialized": bool(state.get("initialized")),
            "last_attempt_at": checked_at,
            "last_error_at": checked_at,
            "last_error": compact_error(exc),
            "consecutive_failures": int(state.get("consecutive_failures", 0)) + 1,
        }
        save_json(state_path, failed_state)
        save_json(history_path, history)
        print(f"[{process['numero']}] ERRO após todas as tentativas: {failed_state['last_error']}")
        return failed_state, None

    counts = {"protocolos": len(protocolos), "andamentos": len(andamentos)}
    common_success = {
        **base_meta,
        "last_attempt_at": checked_at,
        "last_success_at": checked_at,
        "last_error_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "counts": counts,
        "protocolos": protocolos,
        "andamentos": andamentos,
    }

    if not state.get("initialized"):
        new_state = {
            **common_success,
            "initialized": True,
            "baseline_at": checked_at,
            "last_change_at": None,
        }
        save_json(state_path, new_state)
        save_json(history_path, history)
        print(f"[{process['numero']}] linha de base criada: {counts['protocolos']} protocolos; {counts['andamentos']} andamentos")
        return new_state, None

    diffs = {
        "protocolos": diff_maps(protocol_map(state.get("protocolos", [])), protocol_map(protocolos)),
        "andamentos": diff_maps(andamento_map(state.get("andamentos", [])), andamento_map(andamentos)),
    }
    summary = summarize(diffs)
    changed = any(summary.values())

    if changed:
        event = {
            "detected_at": checked_at,
            "summary": summary,
            "counts": counts,
            "details": diffs,
        }
        history.insert(0, event)
        history = history[: int(global_config.get("historico_maximo", 100))]
        last_change_at = checked_at
    else:
        last_change_at = state.get("last_change_at")

    new_state = {
        **common_success,
        "initialized": True,
        "baseline_at": state.get("baseline_at", checked_at),
        "last_change_at": last_change_at,
    }
    save_json(state_path, new_state)
    save_json(history_path, history)

    if changed:
        report_path = RESULT_DIR / f"change_{pid}.md"
        report_path.write_text(build_report(process, checked_at, diffs, counts), encoding="utf-8")
        issue = {
            "process_id": pid,
            "process_number": process["numero"],
            "title": f"SEI/IPHAN: alteração — {process['numero']} — {process.get('nome') or process['numero']}",
            "report_path": str(report_path.relative_to(ROOT)),
            "summary": summary,
        }
        print(f"[{process['numero']}] ALTERAÇÃO: {json.dumps(summary, ensure_ascii=False)}")
        return new_state, issue

    print(f"[{process['numero']}] sem alteração: {counts['protocolos']} protocolos; {counts['andamentos']} andamentos")
    return new_state, None


def summary_item(state: dict[str, Any], history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": state.get("process_id"),
        "numero": state.get("process_number"),
        "nome": state.get("process_name"),
        "grupo": state.get("group", "Geral"),
        "url": state.get("source_url"),
        "ativo": state.get("active", True),
        "initialized": state.get("initialized", False),
        "baseline_at": state.get("baseline_at"),
        "last_change_at": state.get("last_change_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_success_at": state.get("last_success_at"),
        "last_error_at": state.get("last_error_at"),
        "last_error": state.get("last_error"),
        "consecutive_failures": state.get("consecutive_failures", 0),
        "counts": state.get("counts", {"protocolos": 0, "andamentos": 0}),
        "history_count": len(history or []),
    }


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSES_DIR.mkdir(parents=True, exist_ok=True)
    config = load_json(CONFIG_PATH, {})
    processes = [p for p in config.get("processos", []) if p.get("ativo", True)]
    if not processes:
        raise SystemExit("Nenhum processo ativo foi definido em config.json.")

    issues: list[dict[str, Any]] = []
    summary_processes: list[dict[str, Any]] = []
    failures = 0
    interval = int(config.get("intervalo_entre_processos_segundos", 3))

    for index, process in enumerate(processes):
        if not process.get("numero") or not process.get("url"):
            print(f"Processo ignorado por configuração incompleta: {process}")
            continue
        process["id"] = process_id(process)
        state, issue = process_one(process, config)
        state_path, history_path = process_paths(process["id"])
        history = load_json(history_path, [])
        summary_processes.append(summary_item(state, history))
        if issue:
            issues.append(issue)
        if state.get("consecutive_failures", 0) > 0 and state.get("last_error_at") == state.get("last_attempt_at"):
            failures += 1
        if index < len(processes) - 1 and interval > 0:
            time.sleep(interval)

    generated_at = now_iso()
    monitoring = config.get("monitoramento", {})
    dashboard = config.get("dashboard", {})
    save_json(SUMMARY_PATH, {
        "generated_at": generated_at,
        "timezone": config.get("fuso_horario", "America/Bahia"),
        "monitoramento": monitoring,
        "dashboard": dashboard,
        "process_count": len(summary_processes),
        "failure_count": failures,
        "change_count": len(issues),
        "processes": summary_processes,
    })
    save_json(ISSUES_PATH, issues)
    save_json(RESULT_PATH, {
        "generated_at": generated_at,
        "process_count": len(summary_processes),
        "failures": failures,
        "changes": len(issues),
        "issues": issues,
    })

    print(f"Resumo: {len(summary_processes)} processo(s), {len(issues)} alteração(ões), {failures} falha(s).")
    # Sempre retorna 0 para permitir commit dos estados. O workflow decide ao final
    # se deve ficar vermelho quando houver falhas.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
