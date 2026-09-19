#!/usr/bin/env python3
"""
Monitor diario del Boletín Oficial de la Región de Murcia (BORM).

Diseño:
- Primera ejecución: crea una línea base con el último boletín disponible.
- Ejecuciones siguientes: procesa únicamente boletines nuevos.
- Detecta títulos potencialmente relevantes para universidad/UMU.
- NO modifica automáticamente el catálogo jurídico.
- Los candidatos se guardan en data/novedades/borm.json como pendientes de revisión.

A diferencia de la AEBOE, aquí no se presupone una API de consolidación por
artículos. El monitor funciona como detector de novedades, no como consolidador.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "monitor" / "borm_state.json"
NOVEDADES = ROOT / "data" / "novedades" / "borm.json"
CATALOGO = ROOT / "data" / "normas" / "catalogo.json"

USER_AGENT = "Observatorio-Normativo-UMU/0.2 (vigilancia institucional BORM)"
BASE = "https://www.borm.es/services/boletin/ano/{year}/numero/{number}"
MAX_CANDIDATES = 250

UNIVERSITY_TERMS = {
    "universidad": 3,
    "universidades": 3,
    "universitario": 3,
    "universitaria": 3,
    "universitarias": 3,
    "universitarios": 3,
    "universidad de murcia": 6,
    "umu": 5,
    "estudiantes": 2,
    "estudiantado": 2,
    "grado": 1,
    "máster": 1,
    "master": 1,
    "doctorado": 2,
    "consejo interuniversitario": 4,
    "personal docente e investigador": 3,
    "precios públicos": 3,
    "servicios académicos": 2,
    "acceso a la universidad": 3,
    "admisión": 1,
    "ensenanzas universitarias": 3,
    "enseñanzas universitarias": 3,
    "prácticas académicas": 2,
}

NORMATIVE_TERMS = {
    "ley ": 3,
    "decreto": 3,
    "orden": 2,
    "reglamento": 4,
    "estatutos": 4,
    "modifica": 4,
    "modificación": 4,
    "deroga": 4,
    "derogación": 4,
    "corrección de errores": 3,
    "corrección de erratas": 3,
    "criterios de aplicación": 2,
    "instrucción": 2,
    "resolución": 1,
    "acuerdo": 1,
}

EXCLUSION_TERMS = {
    "por la que se nombra": -8,
    "por la que se convoca concurso": -7,
    "concurso de acceso": -5,
    "concurso-oposición": -5,
    "proceso selectivo": -5,
    "lista de espera": -5,
    "profesor titular de universidad": -7,
    "catedrático de universidad": -7,
    "catedrática de universidad": -7,
    "adjudicación de plaza": -6,
    "provisión de plazas": -5,
    "bolsa de trabajo": -5,
    "licitación": -7,
    "formalización de contrato": -7,
}


def today() -> dt.date:
    return dt.date.today()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def request(url: str, method: str = "GET", headers: dict[str, str] | None = None, attempts: int = 3) -> bytes:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, method=method, headers=merged)
            with urllib.request.urlopen(req, timeout=35) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                raise
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"No se pudo consultar {url}: {last}")


def bulletin_exists(year: int, number: int) -> bool:
    url = BASE.format(year=year, number=number) + "/pdf"
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as response:
            return 200 <= response.status < 400
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
    except Exception:
        pass

    # Fallback for servers that do not behave well with HEAD.
    try:
        raw = request(url, headers={"Range": "bytes=0-15"}, attempts=1)
        return raw.startswith(b"%PDF") or b"%PDF" in raw[:16]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        return False
    except Exception:
        return False


def latest_bulletin(year: int) -> int:
    if not bulletin_exists(year, 1):
        return 0

    low, high = 1, 64
    while bulletin_exists(year, high):
        low = high
        high *= 2
        if high > 1024:
            break

    while low + 1 < high:
        mid = (low + high) // 2
        if bulletin_exists(year, mid):
            low = mid
        else:
            high = mid
    return low


def download_bulletin_pdf(year: int, number: int) -> tuple[bytes, str]:
    base = BASE.format(year=year, number=number)

    # Prefer the much smaller summary PDF when the endpoint is available.
    summary_url = base + "/sumario/pdf"
    try:
        raw = request(summary_url, attempts=1)
        if raw.startswith(b"%PDF"):
            return raw, summary_url
    except Exception:
        pass

    full_url = base + "/pdf"
    raw = request(full_url)
    if not raw.startswith(b"%PDF"):
        raise RuntimeError(f"La respuesta del BORM {year}/{number} no parece un PDF.")
    return raw, full_url


def extract_summary_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    collected: list[str] = []

    # Summary PDFs are short. If the full bulletin was downloaded, stop before
    # the first individual announcement page identified by an NPE A- code.
    for idx, page in enumerate(reader.pages[:25]):
        text = page.extract_text() or ""
        upper = text.upper()
        if idx > 0 and re.search(r"NPE:\s*A-\d{6}-\d+", upper):
            break
        collected.append(text)
    return "\n".join(collected)


def normalize_line(line: str) -> str:
    line = line.replace("\u00ad", "")
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def parse_summary_items(text: str) -> list[dict[str, str]]:
    lines = [normalize_line(x) for x in text.splitlines()]
    lines = [x for x in lines if x]

    items: list[dict[str, str]] = []
    current: dict[str, Any] | None = None

    # BORM publication numbers are normally 3-5 digits. In the summary they
    # begin an entry. Titles can wrap across several lines.
    start_re = re.compile(r"^(?P<num>\d{3,5})\s+(?P<rest>.+)$")

    for line in lines:
        m = start_re.match(line)
        if m:
            # Avoid mistaking page headings like "2026 ..." for entries.
            num = int(m.group("num"))
            rest = m.group("rest").strip()
            if 1 <= num <= 99999 and not re.match(r"^(Página|Número)\b", rest, re.I):
                if current:
                    items.append(current)
                current = {"publicacion_numero": str(num), "parts": [rest]}
                continue

        if current:
            # Stop obvious footer/header noise from growing titles forever.
            if line.startswith("NPE:") or line.startswith("D.L. MU-"):
                continue
            if re.match(r"^(Página|Número)\s+\d+", line, re.I):
                continue
            current["parts"].append(line)

    if current:
        items.append(current)

    result = []
    for item in items:
        title = " ".join(item["parts"])
        # Strip a trailing page number from summary entries.
        title = re.sub(r"\s+\d{1,6}\s*$", "", title).strip()
        # Limit accidental over-capture if fallback full PDF parsing is used.
        title = title[:1800]
        if len(title) >= 12:
            result.append(
                {
                    "publicacion_numero": item["publicacion_numero"],
                    "titulo": title,
                }
            )
    return result


def known_regional_references() -> list[str]:
    catalog = load_json(CATALOGO, [])
    refs = set()
    for norma in catalog:
        if norma.get("fuente") != "BORM":
            continue
        title = norma.get("titulo", "")
        for m in re.finditer(r"\b(?:Ley|Decreto)\s+(?:n[.º°]*\s*)?(\d+/\d{4})", title, re.I):
            refs.add(m.group(1))
        siglas = norma.get("siglas")
        if siglas and len(siglas) >= 4:
            refs.add(siglas)
    return sorted(refs)


def score_title(title: str) -> tuple[int, list[str]]:
    low = title.lower()
    score = 0
    reasons: list[str] = []

    for term, points in UNIVERSITY_TERMS.items():
        if term in low:
            score += points
            reasons.append(term)

    for term, points in NORMATIVE_TERMS.items():
        if term in low:
            score += points
            reasons.append(term.strip())

    for term, points in EXCLUSION_TERMS.items():
        if term in low:
            score += points

    for ref in known_regional_references():
        if ref.lower() in low:
            score += 10
            reasons.append(f"referencia {ref}")

    # A university-related item needs at least some normative signal.
    has_university = any(term in low for term in UNIVERSITY_TERMS)
    has_normative = any(term in low for term in NORMATIVE_TERMS)
    if not (has_university and has_normative):
        score -= 5

    return score, list(dict.fromkeys(reasons))[:8]


def classify(title: str) -> str:
    low = title.lower()
    if any(x in low for x in ["modifica", "modificación", "deroga", "derogación", "corrección"]):
        return "Posible modificación normativa"
    if any(x in low for x in ["información pública", "audiencia", "anteproyecto", "proyecto de decreto", "proyecto de ley"]):
        return "Tramitación normativa"
    return "Nueva disposición potencialmente relevante"


def bulletin_date(text: str) -> str | None:
    # Examples: "Sábado, 21 de febrero de 2026"
    months = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
        "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }
    m = re.search(r"\b(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})\b", text.lower())
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = months.get(month_name)
    if not month:
        return None
    return f"{day:02d}/{month:02d}/{year:04d}"


def scan_bulletin(year: int, number: int) -> list[dict[str, Any]]:
    pdf, source_url = download_bulletin_pdf(year, number)
    text = extract_summary_text(pdf)
    date = bulletin_date(text) or f"{year}"
    items = parse_summary_items(text)

    candidates = []
    for item in items:
        score, reasons = score_title(item["titulo"])
        if score < 6:
            continue
        candidates.append(
            {
                "id": f"BORM-{year}-{number}-{item['publicacion_numero']}",
                "detectado_el": today().isoformat(),
                "fecha_boletin": date,
                "ano": year,
                "boletin_numero": str(number),
                "publicacion_numero": item["publicacion_numero"],
                "titulo": item["titulo"],
                "clasificacion": classify(item["titulo"]),
                "puntuacion_relevancia": score,
                "motivos": reasons,
                "url_boletin": BASE.format(year=year, number=number) + "/pdf",
                "url_origen_lectura": source_url,
                "estado_revision": "Pendiente de revisión",
            }
        )
    return candidates


def main() -> int:
    state = load_json(STATE, {"version": 1, "ultimo_boletin": None, "ultima_comprobacion": None})
    novedades = load_json(NOVEDADES, [])

    current_year = today().year
    latest = latest_bulletin(current_year)
    print(f"Último BORM localizado en {current_year}: {latest or 'ninguno'}")

    state["ultima_comprobacion"] = today().isoformat()

    if latest == 0:
        save_json(STATE, state)
        print("No se ha localizado todavía ningún boletín del año actual.")
        return 0

    previous = state.get("ultimo_boletin")
    if not previous:
        state["ultimo_boletin"] = {"ano": current_year, "numero": latest}
        save_json(STATE, state)
        print("Primera ejecución: se crea línea base sin generar novedades históricas.")
        return 0

    prev_year = int(previous.get("ano", current_year))
    prev_num = int(previous.get("numero", 0))

    if prev_year == current_year:
        pending_numbers = list(range(prev_num + 1, latest + 1))
    elif prev_year < current_year:
        pending_numbers = list(range(1, latest + 1))
    else:
        pending_numbers = []

    if not pending_numbers:
        save_json(STATE, state)
        print("Sin nuevos boletines desde la última comprobación.")
        return 0

    existing = {x.get("id") for x in novedades}
    new_count = 0

    for number in pending_numbers:
        print(f"Analizando BORM {current_year}/{number}...")
        if not bulletin_exists(current_year, number):
            print("  No disponible; se omite.")
            continue
        try:
            candidates = scan_bulletin(current_year, number)
        except Exception as exc:
            print(f"  ERROR al analizar el boletín: {exc}", file=sys.stderr)
            # Do not advance state beyond a failed bulletin.
            raise

        for candidate in candidates:
            if candidate["id"] not in existing:
                novedades.append(candidate)
                existing.add(candidate["id"])
                new_count += 1
        print(f"  Candidatos relevantes: {len(candidates)}")

        state["ultimo_boletin"] = {"ano": current_year, "numero": number}
        save_json(STATE, state)

    novedades.sort(
        key=lambda x: (x.get("ano", 0), int(x.get("boletin_numero", 0)), int(x.get("publicacion_numero", 0))),
        reverse=True,
    )
    novedades = novedades[:MAX_CANDIDATES]
    save_json(NOVEDADES, novedades)
    save_json(STATE, state)

    print(f"Nuevas publicaciones enviadas a revisión: {new_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
