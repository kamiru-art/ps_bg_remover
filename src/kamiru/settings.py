"""Memoria de configuración entre sesiones (settings.json junto a la app).

Guarda lo último usado en la GUI (carpetas, modo, formato, sufijo, opciones
avanzadas) para que Kamila no tenga que reconfigurar nada cada día.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields

from .paths import app_root

log = logging.getLogger("kamiru.settings")


@dataclass
class Settings:
    input_dir: str = ""
    output_dir: str = ""
    mode: str = "conjunto"
    format_label: str = "PNG (transparente)"
    suffix: str = ""
    model: str = ""
    resolution: str = "1024"
    min_area: str = "400"
    split_touching: bool = False
    # Defaults en True a propósito: el control de calidad debe servir sin
    # configurar nada. Es seguro porque si el 2º modelo no puede cargar, el
    # lote sigue igual (solo sin consenso), y con motor croma se omite.
    review_uncertain: bool = True   # mover recortes dudosos a salida/revisar/
    verify_second: bool = True      # contrastar con un segundo modelo


def settings_path():
    return app_root() / "settings.json"


def load_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        valid = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in data.items() if k in valid})
    except Exception:  # archivo corrupto → defaults, sin romper la app
        log.warning("settings.json ilegible, usando valores por defecto")
        return Settings()


def save_settings(s: Settings) -> None:
    # temporal + renombre: un corte a mitad no deja un JSON roto
    path = settings_path()
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_text(
            json.dumps(asdict(s), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        log.warning("No se pudo guardar settings.json")
