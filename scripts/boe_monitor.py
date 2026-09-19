#!/usr/bin/env python3
"""
Vigilancia automática de legislación consolidada del BOE.

Principios:
- No interpreta jurídicamente cambios tácitos.
- Solo registra automáticamente cambios textuales cuando el índice de bloques
  de la legislación consolidada del BOE muestra una nueva fecha de actualización.
- La primera ejecución crea una línea base sin presentar el contenido actual
  como una novedad recién detectada.
- Conserva el historial manual existente y añade los hallazgos automáticos
  en `cambios_automaticos`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOGO = ROOT / "data" / "normas" / "catalogo.json"
STATE = ROOT / "data" / "monitor" / "boe_state.json"
HEARTBEAT = ROOT / "data" / "monitor" / "heartbeat.json"

API_BASE = "https://www.boe.es/datosabiertos/api/legislacion-consolidada"
USER_AGENT = "Observatorio-Normativo-UMU/0.1 (seguimiento institucional; fuente AEBOE)"
MAX_TEXT_CHARS = 30000
HEARTBEAT_DAYS = 30


class BOEError(RuntimeError):
    pass


def today_iso() -> str:
    return dt.date.today().isoformat()


def compact_date_to_es(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 8:
        return str(value)
    return f"{digits[6:8]}/{digits[4:6]}/{digits[0:4]}"


def normalize_ws(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def fetch(url: str, accept: str, attempts: int = 3) -> bytes:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise BOEError(f"No se pudo consultar {url}: {last_error}")


def fetch_json(url: str) -> Any:
    raw = fetch(url, "application/json")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BOEError(f"Respuesta JSON no válida en {url}: {exc}") from exc


def fetch_xml(url: str) -> ET.Element:
    raw = fetch(url, "application/xml")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise BOEError(f"Respuesta XML no válida en {url}: {exc}") from exc


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def extract_metadata(payload: Any) -> dict[str, Any]:
    candidates = []
    for item in walk_dicts(payload):
        if "fecha_actualizacion" in item:
            candidates.append(item)

    if not candidates:
        return {}

    # Prefer a metadata object containing the usual identifying fields.
    candidates.sort(
        key=lambda d: (
            "identificador" in d,
            "titulo" in d,
            "estado_consolidacion" in d,
        ),
        reverse=True,
    )
    item = candidates[0]
    return {
        "fecha_actualizacion": item.get("fecha_actualizacion"),
        "identificador": item.get("identificador"),
        "titulo": item.get("titulo"),
        "estado_consolidacion": text_value(item.get("estado_consolidacion")),
        "vigencia_agotada": item.get("vigencia_agotada"),
    }


def text_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("texto") or value.get("text") or value.get("valor") or value
    return value


def extract_blocks(payload: Any) -> dict[str, dict[str, str]]:
    blocks: dict[str, dict[str, str]] = {}
    for item in walk_dicts(payload):
        block_id = item.get("id")
        title = item.get("titulo")
        updated = item.get("fecha_actualizacion")
        url = item.get("url")
        if block_id and title is not None and updated:
            block_id = str(block_id)
            blocks[block_id] = {
                "titulo": str(title),
                "fecha_actualizacion": str(updated),
                "url": str(url) if url else "",
            }
    return blocks


def localname(tag: str) -> str:
    return tag.split("}", 1)[-1]


def extract_version_content(version: ET.Element) -> tuple[str, list[str]]:
    body_parts: list[str] = []
    notes: list[str] = []

    for child in list(version):
        text = " ".join(t.strip() for t in child.itertext() if t and t.strip())
        text = normalize_ws(html.unescape(text))
        if not text:
            continue
        if localname(child.tag) == "blockquote":
            notes.append(text)
        else:
            body_parts.append(text)

    return "\n".join(body_parts).strip(), notes


def parse_block_xml(root: ET.Element) -> list[dict[str, Any]]:
    block = None
    for elem in root.iter():
        if localname(elem.tag) == "bloque":
            block = elem
            break
    if block is None:
        return []

    versions = []
    for order, elem in enumerate(list(block)):
        if localname(elem.tag) != "version":
            continue
        text, notes = extract_version_content(elem)
        versions.append(
            {
                "order": order,
                "fecha_publicacion": elem.attrib.get("fecha_publicacion", ""),
                "fecha_vigencia": elem.attrib.get("fecha_vigencia", ""),
                "id_norma": elem.attrib.get("id_norma", ""),
                "texto": text,
                "notas": notes,
            }
        )

    versions.sort(key=lambda v: (v["fecha_publicacion"], v["order"]))
    return versions


def clip_text(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n[Texto truncado por longitud. Consultar BOE.]", True


def make_diff(before: str, after: str, limit_lines: int = 350) -> list[str]:
    if not before or not after:
        return []
    before_lines = [line for line in before.splitlines() if line.strip()]
    after_lines = [line for line in after.splitlines() if line.strip()]
    diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="antes",
            tofile="ahora",
            lineterm="",
            n=2,
        )
    )
    return diff[:limit_lines]


def block_change(norma_id: str, block_id: str, block_info: dict[str, str]) -> dict[str, Any]:
    url = f"{API_BASE}/id/{norma_id}/texto/bloque/{block_id}"
    root = fetch_xml(url)
    versions = parse_block_xml(root)
    if not versions:
        raise BOEError(f"El bloque {block_id} de {norma_id} no contiene versiones.")

    current = versions[-1]
    previous = versions[-2] if len(versions) > 1 else None

    before_raw = previous["texto"] if previous else ""
    after_raw = current["texto"]
    before, before_truncated = clip_text(before_raw)
    after, after_truncated = clip_text(after_raw)

    return {
        "bloque_id": block_id,
        "bloque_titulo": block_info.get("titulo", block_id),
        "fecha_actualizacion_indice": block_info.get("fecha_actualizacion"),
        "fecha_publicacion": current.get("fecha_publicacion"),
        "fecha_vigencia": current.get("fecha_vigencia") or None,
        "norma_modificadora_id": current.get("id_norma") or None,
        "antes": before or None,
        "ahora": after or None,
        "texto_truncado": bool(before_truncated or after_truncated),
        "notas": current.get("notas", []),
        "diff": make_diff(before_raw, after_raw),
    }


def group_changes(norma: dict[str, Any], changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for change in changes:
        key = (
            change.get("fecha_publicacion") or change.get("fecha_actualizacion_indice") or "",
            change.get("norma_modificadora_id") or "",
        )
        groups.setdefault(key, []).append(change)

    result = []
    for (pub_date, modifier), blocks in sorted(groups.items(), reverse=True):
        titles = [b["bloque_titulo"] for b in blocks]
        count = len(titles)
        if count == 1:
            summary = f"El BOE publica una nueva versión de {titles[0]}."
        else:
            visible = ", ".join(titles[:5])
            suffix = "" if count <= 5 else f" y {count - 5} bloques más"
            summary = f"El BOE publica nuevas versiones de {visible}{suffix}."

        change_id = f"{norma['id']}:{pub_date}:{modifier or 'sin-id'}"
        result.append(
            {
                "id": change_id,
                "detectado_el": today_iso(),
                "fecha_publicacion": compact_date_to_es(pub_date),
                "norma_modificadora_id": modifier or None,
                "resumen": summary,
                "numero_bloques": count,
                "bloques": blocks,
                "estado_revision": "Pendiente de revisión humana",
            }
        )
    return result


def update_heartbeat(force: bool = False) -> bool:
    hb = load_json(HEARTBEAT, {})
    raw = hb.get("ultima_actividad_programada")
    try:
        last = dt.date.fromisoformat(raw) if raw else None
    except ValueError:
        last = None

    today = dt.date.today()
    if force or last is None or (today - last).days >= HEARTBEAT_DAYS:
        hb["ultima_actividad_programada"] = today.isoformat()
        save_json(HEARTBEAT, hb)
        return True
    return False


def monitor() -> int:
    catalog = load_json(CATALOGO, [])
    state = load_json(STATE, {"version": 1, "normas": {}})
    state.setdefault("version", 1)
    state.setdefault("normas", {})

    any_state_change = False
    any_legal_change = False

    monitored = [n for n in catalog if n.get("monitorizado_boe") and n.get("id", "").startswith("BOE-")]
    print(f"Normas BOE monitorizadas: {len(monitored)}")

    for norma in monitored:
        norma_id = norma["id"]
        print(f"\nComprobando {norma_id} · {norma.get('siglas', norma_id)}")

        metadata_payload = fetch_json(f"{API_BASE}/id/{norma_id}/metadatos")
        index_payload = fetch_json(f"{API_BASE}/id/{norma_id}/texto/indice")

        metadata = extract_metadata(metadata_payload)
        blocks = extract_blocks(index_payload)
        if not blocks:
            raise BOEError(f"No se pudieron extraer bloques del índice de {norma_id}.")

        old = state["normas"].get(norma_id)
        new_state = {
            "fecha_actualizacion_registro": metadata.get("fecha_actualizacion"),
            "estado_consolidacion": metadata.get("estado_consolidacion"),
            "vigencia_agotada": metadata.get("vigencia_agotada"),
            "bloques": {
                block_id: {
                    "titulo": info["titulo"],
                    "fecha_actualizacion": info["fecha_actualizacion"],
                }
                for block_id, info in sorted(blocks.items())
            },
        }

        if old is None:
            print("  Primera ejecución: se crea línea base sin generar una falsa novedad.")
            state["normas"][norma_id] = new_state
            any_state_change = True
            continue

        changed_block_ids = []
        old_blocks = old.get("bloques", {})
        for block_id, info in blocks.items():
            old_info = old_blocks.get(block_id)
            if old_info is None or old_info.get("fecha_actualizacion") != info.get("fecha_actualizacion"):
                changed_block_ids.append(block_id)

        removed_blocks = sorted(set(old_blocks) - set(blocks))
        if removed_blocks:
            print(f"  Aviso: {len(removed_blocks)} bloques ya no figuran en el índice: {', '.join(removed_blocks)}")

        metadata_changed = (
            old.get("fecha_actualizacion_registro") != new_state.get("fecha_actualizacion_registro")
            or old.get("estado_consolidacion") != new_state.get("estado_consolidacion")
            or old.get("vigencia_agotada") != new_state.get("vigencia_agotada")
        )

        if changed_block_ids:
            print(f"  Cambio textual detectado en {len(changed_block_ids)} bloque(s).")
            details = [block_change(norma_id, block_id, blocks[block_id]) for block_id in changed_block_ids]
            grouped = group_changes(norma, details)

            existing = {item.get("id") for item in norma.setdefault("cambios_automaticos", [])}
            added = 0
            for item in grouped:
                if item["id"] not in existing:
                    norma["cambios_automaticos"].append(item)
                    existing.add(item["id"])
                    added += 1

            norma["cambios_automaticos"].sort(
                key=lambda item: item.get("fecha_publicacion") or "",
                reverse=True,
            )
            norma["seguimiento"] = "Vigilancia automática BOE activa"
            norma["verificacion"] = "Actualización detectada · pendiente de revisión"
            any_legal_change = any_legal_change or added > 0
            print(f"  Registros automáticos nuevos: {added}")
        elif metadata_changed:
            print("  El registro BOE cambió, pero no hay nueva versión textual de los bloques.")
        else:
            print("  Sin cambios.")

        if old != new_state:
            state["normas"][norma_id] = new_state
            any_state_change = True

    # Monthly heartbeat avoids long periods without repository activity in a public repo.
    heartbeat_changed = update_heartbeat()

    if any_legal_change:
        save_json(CATALOGO, catalog)
    if any_state_change:
        save_json(STATE, state)

    if any_legal_change:
        print("\nResultado: se han registrado cambios normativos para revisión humana.")
    elif any_state_change:
        print("\nResultado: estado técnico actualizado; sin nueva modificación textual registrada.")
    elif heartbeat_changed:
        print("\nResultado: sin cambios normativos; actualizado el heartbeat mensual.")
    else:
        print("\nResultado: sin cambios.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force-heartbeat",
        action="store_true",
        help="Actualiza el heartbeat aunque no hayan transcurrido 30 días.",
    )
    args = parser.parse_args()

    if args.force_heartbeat:
        update_heartbeat(force=True)

    try:
        return monitor()
    except BOEError as exc:
        print(f"ERROR BOE: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR inesperado: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
