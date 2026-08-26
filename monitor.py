#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
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
STATE_PATH = ROOT / "data" / "state.json"
HISTORY_PATH = ROOT / "data" / "history.json"
RESULT_DIR = ROOT / ".monitor"
RESULT_PATH = RESULT_DIR / "result.json"
REPORT_PATH = RESULT_DIR / "change_report.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(value: str) -> str:
    value = " ".join(value.replace("\xa0", " ").split()).strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.casefold()


def clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).strip()


def table_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            cells = tr.find_all(["th", "td"])
        values = [clean(cell.get_text(" ", strip=True)) for cell in cells]
        if values and any(values):
            rows.append(values)
    return rows


def find_table(soup: BeautifulSoup, required_headers: list[str]) -> list[list[str]]:
    required = [norm(x) for x in required_headers]
    candidates: list[tuple[int, list[list[str]]]] = []

    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue
        for idx, row in enumerate(rows[:5]):
            normalized = [norm(c) for c in row]
            if all(any(req in cell for cell in normalized) for req in required):
                candidates.append((idx, rows))
                break

    if not candidates:
        raise RuntimeError(f"Tabela não localizada. Cabeçalhos esperados: {required_headers}")

    header_index, rows = max(candidates, key=lambda item: len(item[1]))
    return rows[header_index:]


def parse_protocolos(soup: BeautifulSoup) -> list[dict[str, str]]:
    rows = find_table(soup, ["Processo", "Tipo", "Data", "Unidade"])
    result: list[dict[str, str]] = []
    for row in rows[1:]:
        if len(row) < 5:
            continue
        documento, tipo, data, data_inclusao, unidade = row[:5]
        if not re.search(r"\d", documento):
            continue
        result.append({
            "documento": documento,
            "tipo": tipo,
            "data": data,
            "data_inclusao": data_inclusao,
            "unidade": unidade,
        })
    if not result:
        raise RuntimeError("A tabela de Protocolos foi localizada, mas nenhum protocolo pôde ser interpretado.")
    return result


def parse_andamentos(soup: BeautifulSoup) -> list[dict[str, str]]:
    rows = find_table(soup, ["Data/Hora", "Unidade", "Descrição"])
    result: list[dict[str, str]] = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        data_hora, unidade, descricao = row[:3]
        if not re.search(r"\d{1,2}/\d{1,2}/\d{4}", data_hora):
            continue
        result.append({
            "data_hora": data_hora,
            "unidade": unidade,
            "descricao": descricao,
        })
    if not result:
        raise RuntimeError("A tabela de Andamentos foi localizada, mas nenhum andamento pôde ser interpretado.")
    return result


def fetch_html(url: str, timeout: int) -> bytes:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
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
            "User-Agent": "SEI-IPHAN-Monitor/1.0 (+GitHub Actions; consulta pública e de baixa frequência)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    response.raise_for_status()
    return response.content


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
    modified = [
        {"antes": old[k], "depois": new[k]}
        for k in new
        if k in old and old[k] != new[k]
    ]
    return {"added": added, "removed": removed, "modified": modified}


def short_protocol(row: dict[str, str]) -> str:
    return f"{row.get('documento', '—')} — {row.get('tipo', '—')} — inclusão {row.get('data_inclusao', '—')} — {row.get('unidade', '—')}"


def short_andamento(row: dict[str, str]) -> str:
    return f"{row.get('data_hora', '—')} — {row.get('unidade', '—')} — {row.get('descricao', '—')}"


def build_report(config: dict[str, Any], detected_at: str, diffs: dict[str, Any], counts: dict[str, int]) -> str:
    lines = [
        f"## Alteração detectada no processo {config['processo']}",
        "",
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
        f"[Abrir Pesquisa Processual do SEI/IPHAN]({config['url']})",
        "",
        "> Aviso gerado automaticamente por comparação semântica das tabelas públicas de Protocolos e Andamentos.",
    ]
    return "\n".join(lines) + "\n"


def summarize(diffs: dict[str, Any]) -> dict[str, int]:
    return {
        "protocolos_adicionados": len(diffs["protocolos"]["added"]),
        "protocolos_modificados": len(diffs["protocolos"]["modified"]),
        "protocolos_removidos": len(diffs["protocolos"]["removed"]),
        "andamentos_adicionados": len(diffs["andamentos"]["added"]),
        "andamentos_removidos": len(diffs["andamentos"]["removed"]),
    }


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"initialized": False})
    history = load_json(HISTORY_PATH, [])

    detected_at = now_iso()
    html = fetch_html(config["url"], int(config.get("timeout_segundos", 30)))
    soup = BeautifulSoup(html, "html.parser")
    protocolos = parse_protocolos(soup)
    andamentos = parse_andamentos(soup)

    snapshot = {
        "protocolos": protocolos,
        "andamentos": andamentos,
    }
    counts = {"protocolos": len(protocolos), "andamentos": len(andamentos)}

    if not state.get("initialized"):
        new_state = {
            "initialized": True,
            "process_number": config["processo"],
            "source_url": config["url"],
            "baseline_at": detected_at,
            "last_change_at": None,
            "counts": counts,
            **snapshot,
        }
        save_json(STATE_PATH, new_state)
        save_json(HISTORY_PATH, history)
        save_json(RESULT_PATH, {
            "changed": False,
            "initialized": True,
            "message": "Linha de base criada; nenhuma notificação foi emitida.",
            "counts": counts,
            "checked_at": detected_at,
        })
        print(f"Linha de base criada: {counts['protocolos']} protocolos; {counts['andamentos']} andamentos.")
        return 0

    old_protocolos = protocol_map(state.get("protocolos", []))
    new_protocolos = protocol_map(protocolos)
    old_andamentos = andamento_map(state.get("andamentos", []))
    new_andamentos = andamento_map(andamentos)

    diffs = {
        "protocolos": diff_maps(old_protocolos, new_protocolos),
        "andamentos": diff_maps(old_andamentos, new_andamentos),
    }
    summary = summarize(diffs)
    changed = any(summary.values())

    if changed:
        event = {
            "detected_at": detected_at,
            "summary": summary,
            "counts": counts,
            "details": diffs,
        }
        history.insert(0, event)
        history = history[: int(config.get("historico_maximo", 100))]

        new_state = {
            "initialized": True,
            "process_number": config["processo"],
            "source_url": config["url"],
            "baseline_at": state.get("baseline_at", detected_at),
            "last_change_at": detected_at,
            "counts": counts,
            **snapshot,
        }
        save_json(STATE_PATH, new_state)
        save_json(HISTORY_PATH, history)
        REPORT_PATH.write_text(build_report(config, detected_at, diffs, counts), encoding="utf-8")

    save_json(RESULT_PATH, {
        "changed": changed,
        "initialized": True,
        "summary": summary,
        "counts": counts,
        "checked_at": detected_at,
    })

    if changed:
        print("Alteração detectada:", json.dumps(summary, ensure_ascii=False))
    else:
        print(f"Sem alteração: {counts['protocolos']} protocolos; {counts['andamentos']} andamentos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
