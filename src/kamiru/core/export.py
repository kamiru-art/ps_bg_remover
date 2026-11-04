"""Exportación: PNG-24, TIFF con alfa (8/16-bit) y PSD por capas.

Reglas de nomenclatura del spec:
- Conjunto:   ``<nombre_origen>.<ext>``
- Individual: ``<nombre_origen>_01.<ext>``, ``_02``, ... con relleno de ceros.
- Sin sobrescritura: si el destino existe se agrega ``-1``, ``-2``, ...

Cada archivo se escribe primero a un temporal oculto junto al destino
(``.foto.png.part``) y se renombra al terminar: un corte a mitad (disco
lleno, ventana cerrada) no deja un archivo truncado con nombre válido.

El perfil ICC de la imagen origen se re-embebe en PNG y TIFF para mantener
color consistente.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from PIL import Image

from .psd_writer import PsdLayer, write_psd

FORMATS = ("png", "tiff", "tiff16", "psd")

_EXTENSIONS = {"png": ".png", "tiff": ".tif", "tiff16": ".tif", "psd": ".psd"}


def extension_for(fmt: str) -> str:
    return _EXTENSIONS[fmt]


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """Ruta que no pisa archivos existentes: agrega -1, -2, ... si hace falta."""
    candidate = folder / f"{stem}{ext}"
    n = 1
    while candidate.exists():
        candidate = folder / f"{stem}-{n}{ext}"
        n += 1
    return candidate


@contextmanager
def _atomic_write(path: Path):
    """Da una ruta temporal; al salir sin error la renombra a ``path``."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.part")
    try:
        yield tmp
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # solo queda si hubo un error


def numbered_stem(stem: str, index: int, total: int) -> str:
    width = max(2, len(str(total)))
    return f"{stem}_{index:0{width}d}"


def save_png(rgba: np.ndarray, path: Path, icc_profile: bytes | None = None) -> None:
    im = Image.fromarray(rgba, mode="RGBA")
    kwargs = {"icc_profile": icc_profile} if icc_profile else {}
    with _atomic_write(path) as tmp:
        im.save(tmp, format="PNG", **kwargs)


def save_tiff(
    rgba: np.ndarray,
    path: Path,
    icc_profile: bytes | None = None,
    bitdepth: int = 8,
) -> None:
    import tifffile

    if bitdepth == 16:
        data = (rgba.astype(np.uint16) * 257)  # 8→16 bits exacto (255→65535)
    else:
        data = rgba
    extratags = []
    if icc_profile:
        extratags.append((34675, "B", len(icc_profile), icc_profile, True))
    with _atomic_write(path) as tmp:
        tifffile.imwrite(
            tmp,
            data,
            photometric="rgb",
            extrasamples=["unassalpha"],
            compression="adobe_deflate",
            extratags=extratags,
        )


def save_psd_layers(
    layers: list[tuple[str, np.ndarray, int, int]],
    path: Path,
    width: int,
    height: int,
) -> None:
    """PSD por capas. ``layers``: lista de (nombre, rgba uint8, left, top)."""
    psd_layers = [PsdLayer(name=n, rgba=a, left=x, top=y) for n, a, x, y in layers]
    with _atomic_write(path) as tmp:
        write_psd(tmp, psd_layers, width=width, height=height)


def export_single(
    rgba: np.ndarray,
    folder: Path,
    stem: str,
    fmt: str,
    icc_profile: bytes | None = None,
    layer_name: str | None = None,
) -> Path:
    """Exporta un RGBA a un archivo en el formato pedido; devuelve la ruta."""
    if fmt not in FORMATS:
        raise ValueError(f"Formato desconocido: {fmt!r}")
    path = unique_path(folder, stem, extension_for(fmt))
    if fmt == "png":
        save_png(rgba, path, icc_profile)
    elif fmt == "tiff":
        save_tiff(rgba, path, icc_profile, bitdepth=8)
    elif fmt == "tiff16":
        save_tiff(rgba, path, icc_profile, bitdepth=16)
    elif fmt == "psd":
        h, w = rgba.shape[:2]
        save_psd_layers([(layer_name or stem, rgba, 0, 0)], path, w, h)
    return path
