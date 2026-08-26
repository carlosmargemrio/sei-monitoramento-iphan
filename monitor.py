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


def direct_cells(tr) -> list[str]:
    """Lê somente as células pertencentes diretamente ao <tr>."""
    return [
        clean(cell.get_text(" ", strip=True))
        for cell in tr.find_all(["th", "td"], recursive=False)
    ]


def find_table(soup: BeautifulSoup, required_headers: list[str]):
    """
    Localiza a linha de cabeçalho e devolve a tabela MAIS PRÓXIMA dela.

    Isso evita escolher uma tabela externa que apenas contenha, de forma
    aninhada, as tabelas de Protocolos/Andamentos do SEI.
    """
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
    """Encontra uma coluna pelo nome, priorizando correspondência exata."""
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
    """
    Retorna apenas linhas da tabela encontrada, excluindo <tr> de tabelas
    aninhadas. Funciona com ou sem thead/tbody.
    """
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
    return bool(
        re.search(r"\b\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2})?\b", value)
    )


def protocol_from_row(values: list[str], indexes: dict[str, int]) -> dict[str, str] | None:
    """
    Interpreta uma linha de protocolo. Primeiro usa os índices do cabeçalho;
    se o SEI inserir uma coluna técnica vazia/ícone antes dos dados, procura
    automaticamente o bloco Documento + Tipo + Data + Inclusão + Unidade.
    """
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

    # Fallback para eventuais colunas técnicas extras do SEI.
    for start in range(0, max(0, len(values) - 4)):
        bloco = values[start:start + 5]
        if len(bloco) < 5:
            continue
        documento, tipo, data, data_inclusao, unidade = bloco
        if (
            re.search(r"\d", documento)
            and is_date(data)
            and is_date(data_inclusao)
            and unidade
        ):
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
            return {
                "data_hora": data_hora,
                "unidade": unidade,
                "descricao": descricao,
            }
    except IndexError:
        pass

    # Fallback para eventual coluna técnica anterior à data/hora.
    for start in range(0, max(0, len(values) - 2)):
        bloco = values[start:start + 3]
        if len(bloco) < 3:
            continue
        data_hora, unidade, descricao = bloco
        if is_datetime(data_hora) and unidade and descricao:
            return {
                "data_hora": data_hora,
                "unidade": unidade,
                "descricao": descricao,
            }

    return None


def parse_protocolos(soup: BeautifulSoup) -> list[dict[str, str]]:
    table, header_tr, headers = find_table(
        soup, ["Processo", "Tipo", "Data", "Unidade"]
    )

    indexes = {
        "documento": find_column(headers, ["Processo / Documento", "Processo", "Documento"]),
        "tipo": find_column(headers, ["Tipo"]),
        "data": find_column(headers, ["Data"]),
        "data_inclusao": find_column(headers, ["Data de Inclusão", "Data de Inclusao"]),
        "unidade": find_column(headers, ["Unidade"]),
    }

    data_rows = rows_after_header(table, header_tr)
    result: list[dict[str, str]] = []

    for values in data_rows:
        parsed = protocol_from_row(values, indexes)
        if parsed:
            result.append(parsed)

    if not result:
        amostra = data_rows[:3]
        raise RuntimeError(
            "A tabela de Protocolos foi localizada, mas nenhum protocolo pôde ser "
            f"interpretado. Cabeçalho={headers!r}; amostra_linhas={amostra!r}"
        )

    return result


def parse_andamentos(soup: BeautifulSoup) -> list[dict[str, str]]:
    table, header_tr, headers = find_table(
        soup, ["Data/Hora", "Unidade", "Descrição"]
    )

    indexes = {
        "data_hora": find_column(headers, ["Data/Hora", "Data / Hora"]),
        "unidade": find_column(headers, ["Unidade"]),
        "descricao": find_column(headers, ["Descrição", "Descricao"]),
    }

    data_rows = rows_after_header(table, header_tr)
    result: list[dict[str, str]] = []

    for values in data_rows:
        parsed = andamento_from_row(values, indexes)
        if parsed:
            result.append(parsed)

    if not result:
        amostra = data_rows[:3]
        raise RuntimeError(
            "A tabela de Andamentos foi localizada, mas nenhum andamento pôde ser "
            f"interpretado. Cabeçalho={headers!r}; amostra_linhas={amostra!r}"
        )

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
