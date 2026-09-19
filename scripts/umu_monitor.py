#!/usr/bin/env python3
"""
Monitor de normativa interna de la Universidad de Murcia.

Objetivo:
- Vigilar URLs oficiales ya incorporadas al catálogo.
- Detectar cambios de contenido mediante SHA-256.
- Explorar páginas oficiales de normativa de centros y delegaciones.
- Guardar cambios/nuevos enlaces como candidatos pendientes de revisión.
- Nunca declarar automáticamente que una norma ha sido modificada, derogada o sustituida.

La primera ejecución crea una línea base sin generar novedades históricas.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data" / "config" / "umu_sources.json"
STATE = ROOT / "data" / "monitor" / "umu_state.json"
NOVEDADES = ROOT / "data" / "novedades" / "umu.json"

USER_AGENT = "Observatorio-Normativo-UMU/0.3 (vigilancia documental institucional)"
MAX_STATE_TEXT = 24000
MAX_NOVEDADES = 350


def today_iso() -> str:
    return dt.date.today().isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str, attempts: int = 2) -> tuple[bytes, str]:
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as response:
                ctype = response.headers.get("Content-Type", "")
                return response.read(), ctype
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5)
    raise RuntimeError(str(last))


def normalize_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor_text: list[str] = []
        self._ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._ignore += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor_text = []

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._ignore:
            self._ignore -= 1
        if tag == "a":
            if self._href:
                self.links.append((self._href, " ".join(self._anchor_text).strip()))
            self._href = None
            self._anchor_text = []

    def handle_data(self, data):
        if self._ignore:
            return
        clean = data.strip()
        if clean:
            self.text.append(clean)
            if self._href is not None:
                self._anchor_text.append(clean)


def content_to_text(raw: bytes, content_type: str, url: str) -> tuple[str, list[tuple[str, str]]]:
    low = url.lower()
    if "pdf" in content_type.lower() or low.endswith(".pdf") or b"%PDF" in raw[:20]:
        reader = PdfReader(io.BytesIO(raw))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return normalize_text("\n".join(pages)), []

    decoded = raw.decode("utf-8", errors="replace")
    parser = TextHTMLParser()
    parser.feed(decoded)
    return normalize_text("\n".join(parser.text)), parser.links


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clip(text: str) -> str:
    if len(text) <= MAX_STATE_TEXT:
        return text
    return text[:MAX_STATE_TEXT] + "\n[texto truncado en el estado técnico]"


def allowed_url(url: str, suffixes: list[str]) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in suffixes)


def regulatory_score(label: str, url: str, keywords: list[str]) -> tuple[int, list[str]]:
    value = f"{label} {url}".lower()
    hits = [k for k in keywords if k.lower() in value]
    score = len(hits)
    if any(url.lower().endswith(ext) for ext in [".pdf", ".doc", ".docx"]):
        score += 2
    if "normativa" in value or "reglamento" in value:
        score += 2
    return score, hits[:8]


def make_diff(before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="antes",
        tofile="ahora",
        lineterm="",
        n=2,
    ))[:260]


def source_snapshot(source: dict[str, Any]) -> dict[str, Any]:
    raw, ctype = fetch(source["url"])
    text, _ = content_to_text(raw, ctype, source["url"])
    return {
        "url": source["url"],
        "titulo": source["titulo"],
        "categoria": source.get("categoria"),
        "centro": source.get("centro"),
        "hash": sha(text),
        "texto": clip(text),
        "comprobado_el": today_iso(),
    }


def discovery_snapshot(page: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    raw, ctype = fetch(page["url"])
    text, links = content_to_text(raw, ctype, page["url"])
    found = {}
    for href, label in links:
        absolute = urllib.parse.urljoin(page["url"], href)
        if not allowed_url(absolute, config["allowed_hosts_suffix"]):
            continue
        score, hits = regulatory_score(label, absolute, config["keywords"])
        if score < 2:
            continue
        found[absolute] = {
            "url": absolute,
            "titulo": label or absolute.rsplit("/", 1)[-1],
            "score": score,
            "motivos": hits,
        }
    return {
        "url": page["url"],
        "titulo": page["titulo"],
        "centro": page.get("centro"),
        "hash_pagina": sha(text),
        "enlaces": found,
        "comprobado_el": today_iso(),
    }


def add_novedad(novedades: list[dict[str, Any]], item: dict[str, Any]) -> bool:
    ids = {x.get("id") for x in novedades}
    if item["id"] in ids:
        return False
    novedades.append(item)
    return True


def main() -> int:
    config = load_json(CONFIG, {})
    state = load_json(STATE, {"version": 1, "sources": {}, "discovery": {}, "ultima_comprobacion": None})
    novedades = load_json(NOVEDADES, [])
    first_run = not state.get("sources") and not state.get("discovery")
    new_count = 0

    state["ultima_comprobacion"] = today_iso()

    print(f"Fuentes UMU directas configuradas: {len(config.get('direct_sources', []))}")
    for source in config.get("direct_sources", []):
        sid = source["id"]
        try:
            snap = source_snapshot(source)
        except Exception as exc:
            print(f"AVISO {sid}: no se pudo consultar {source['url']}: {exc}")
            continue

        old = state.setdefault("sources", {}).get(sid)
        if old and old.get("hash") != snap["hash"]:
            event_id = f"UMU-CAMBIO:{sid}:{snap['hash'][:12]}"
            if add_novedad(novedades, {
                "id": event_id,
                "detectado_el": today_iso(),
                "tipo_evento": "Documento modificado",
                "categoria": source.get("categoria") or "Normativa UMU",
                "centro": source.get("centro"),
                "titulo": source["titulo"],
                "descripcion": "Ha cambiado el contenido técnico de una fuente oficial monitorizada. Requiere comprobar si el cambio es jurídico, formal o meramente editorial.",
                "url": source["url"],
                "estado_revision": "Pendiente de revisión",
                "hash_anterior": old.get("hash"),
                "hash_nuevo": snap["hash"],
                "diff": make_diff(old.get("texto", ""), snap.get("texto", "")),
            }):
                new_count += 1
                print(f"CAMBIO: {source['titulo']}")
        state["sources"][sid] = snap

    print(f"Páginas de descubrimiento configuradas: {len(config.get('discovery_pages', []))}")
    for page in config.get("discovery_pages", []):
        pid = page["id"]
        try:
            snap = discovery_snapshot(page, config)
        except Exception as exc:
            print(f"AVISO descubrimiento {pid}: {exc}")
            continue

        old = state.setdefault("discovery", {}).get(pid)
        if old:
            old_links = old.get("enlaces", {})
            for url, info in snap["enlaces"].items():
                if url in old_links:
                    continue
                event_id = f"UMU-NUEVO-ENLACE:{sha(url)[:16]}"
                if add_novedad(novedades, {
                    "id": event_id,
                    "detectado_el": today_iso(),
                    "tipo_evento": "Nuevo enlace normativo",
                    "categoria": "Descubrimiento automático",
                    "centro": page.get("centro"),
                    "titulo": info["titulo"],
                    "descripcion": "Nuevo enlace con apariencia normativa localizado en una página oficial monitorizada.",
                    "url": url,
                    "pagina_origen": page["url"],
                    "motivos": info.get("motivos", []),
                    "estado_revision": "Pendiente de revisión",
                }):
                    new_count += 1
                    print(f"NUEVO ENLACE: {info['titulo']}")
        state["discovery"][pid] = snap

    novedades.sort(key=lambda x: x.get("detectado_el", ""), reverse=True)
    novedades = novedades[:MAX_NOVEDADES]
    save_json(STATE, state)

    # Crucial: first run is baseline. It never creates historical discoveries.
    if first_run:
        print("Primera ejecución: línea base UMU creada sin generar falsas novedades históricas.")
    else:
        save_json(NOVEDADES, novedades)
        print(f"Nuevas detecciones enviadas a revisión: {new_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
