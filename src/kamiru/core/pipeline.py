"""Pipeline por lotes: carpeta de entrada → recortes exportados + resumen.

Por cada imagen: cargar (EXIF/HEIC) → alfa con el motor elegido → control de
calidad (señales del alfa y, si hay un motor de contraste, consenso entre
modelos) → RGBA a resolución completa → modo conjunto o individual →
exportar. Los recortes dudosos pueden ir a ``salida/revisar/`` para que solo
haya que mirar esos a mano. Una imagen que falla se registra y se salta, sin
abortar el lote.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .export import FORMATS, export_single, extension_for, numbered_stem, unique_path
from .imageio import list_images, load_image
from .matting import BaseMatter
from .quality import QualityFlag, assess_cutout, compare_alphas, merge_flags
from .separation import Component, content_bbox, crop_component, separate

log = logging.getLogger("kamiru.pipeline")

REVIEW_DIRNAME = "revisar"  # subcarpeta de salida para recortes dudosos


@dataclass
class BatchOptions:
    mode: str = "conjunto"            # "conjunto" | "individual"
    fmt: str = "png"                  # png | tiff | tiff16 | psd
    min_area: int = 400               # px², filtro de motas/polvo
    alpha_threshold: float = 0.5
    margin: int = 2                   # margen extra del recorte, px
    split_touching: bool = False      # watershed para piezas que se tocan
    psd_layered: bool = True          # en individual+PSD: 1 PSD con capas por foto
    suffix: str = ""                  # sufijo opcional: foto_recorte.png, foto_recorte_01.png
    quality_check: bool = True        # nivel 1: señales de recorte dudoso
    move_uncertain: bool = False      # recortes dudosos → salida/revisar/

    def normalized_suffix(self) -> str:
        s = self.suffix.strip()
        if not s:
            return ""
        # limpiar caracteres no aptos para nombre de archivo
        s = "".join(c for c in s if c not in '\\/:*?"<>|').strip()
        if s and not s.startswith(("_", "-", ".")):
            s = "_" + s
        return s

    def validate(self) -> None:
        if self.mode not in ("conjunto", "individual"):
            raise ValueError(f"Modo inválido: {self.mode!r}")
        if self.fmt not in FORMATS:
            raise ValueError(f"Formato inválido: {self.fmt!r}")
        if self.min_area < 0:
            raise ValueError("El área mínima no puede ser negativa")


@dataclass
class ImageReport:
    source: Path
    ok: bool
    outputs: list[Path] = field(default_factory=list)
    objects_found: int = 0
    objects_discarded: int = 0        # bajo el área mínima
    seconds: float = 0.0
    error: str | None = None
    review: bool = False              # recorte dudoso: conviene mirarlo a mano
    review_reasons: list[str] = field(default_factory=list)
    consensus_iou: float | None = None  # coincidencia con el motor de contraste


class BatchCancelled(Exception):
    """Cancelar llegó mientras se recortaba una foto: esa foto no se exporta."""


@dataclass
class BatchSummary:
    reports: list[ImageReport] = field(default_factory=list)
    with_verifier: bool = False         # hubo un motor de contraste
    review_dir: Path | None = None      # destino de las dudosas, si se mueven
    notes: list[str] = field(default_factory=list)  # avisos del lote entero

    @property
    def total(self) -> int:
        return len(self.reports)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.reports if r.ok)

    @property
    def failed(self) -> list[ImageReport]:
        return [r for r in self.reports if not r.ok]

    @property
    def files_exported(self) -> int:
        return sum(len(r.outputs) for r in self.reports)

    @property
    def empty(self) -> list[ImageReport]:
        return [r for r in self.reports if r.ok and not r.outputs]

    @property
    def to_review(self) -> list[ImageReport]:
        return [r for r in self.reports if r.ok and r.review and r.outputs]

    def text(self) -> str:
        lines = [
            f"Imágenes procesadas: {self.succeeded} de {self.total}",
            f"Archivos exportados: {self.files_exported}",
        ]
        if self.to_review:
            moved = self.review_dir is not None and any(
                o.parent == self.review_dir for r in self.to_review for o in r.outputs)
            where = f" (en la carpeta «{REVIEW_DIRNAME}/»)" if moved else ""
            lines.append(f"Recortes dudosos{where}: {len(self.to_review)}")
            for r in self.to_review:
                lines.append(f"  - {r.source.name}: {'; '.join(r.review_reasons)}")
        if self.with_verifier:
            unchecked = [r for r in self.reports if r.ok and r.consensus_iou is None]
            if unchecked:
                lines.append(
                    f"Sin contraste del 2º modelo (solo control básico): "
                    f"{len(unchecked)} ({', '.join(r.source.name for r in unchecked)})"
                )
        if self.empty:
            lines.append(
                f"Sin objetos detectados: {len(self.empty)} "
                f"({', '.join(r.source.name for r in self.empty)})"
            )
        if self.failed:
            lines.append(f"Con error: {len(self.failed)}")
            for r in self.failed:
                saved = ""
                if r.outputs:
                    saved = (f" (antes del error se guardaron {len(r.outputs)} "
                             f"archivo(s): {', '.join(o.name for o in r.outputs)})")
                lines.append(f"  - {r.source.name}: {r.error}{saved}")
        lines.extend(f"Aviso: {n}" for n in self.notes)
        return "\n".join(lines)


# callback(hechas, total, nombre_actual) para la barra de progreso
ProgressFn = Callable[[int, int, str], None]


def _crop_bbox(rgba: np.ndarray, bbox: tuple[int, int, int, int], margin: int) -> np.ndarray:
    h, w = rgba.shape[:2]
    x, y, bw, bh = bbox
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(w, x + bw + margin), min(h, y + bh + margin)
    return rgba[y0:y1, x0:x1]


def process_image(
    path: Path,
    matter: BaseMatter,
    out_dir: Path,
    opts: BatchOptions,
    verifier: BaseMatter | None = None,
    cancel: Callable[[], bool] | None = None,
    report: ImageReport | None = None,
) -> ImageReport:
    """Recorta y exporta una foto.

    Los archivos se anotan en ``report.outputs`` a medida que se escriben.
    Así, si la exportación falla a mitad, quien pasó ``report`` sabe qué
    archivos ya quedaron en disco.
    """
    t0 = time.perf_counter()
    if report is None:
        report = ImageReport(source=path, ok=True)
    loaded = load_image(path)
    result = matter.cutout(loaded.rgb)
    alpha = result.alpha
    rgba = np.asarray(result.rgba, dtype=np.uint8)
    stem = path.stem + opts.normalized_suffix()

    flags: list[QualityFlag] = []
    if opts.quality_check:
        flags = list(assess_cutout(loaded.rgb, alpha, opts.alpha_threshold).flags)
    if verifier is not None:
        try:
            consensus = compare_alphas(alpha, verifier.alpha(loaded.rgb),
                                       opts.alpha_threshold)
            report.consensus_iou = consensus.iou
            flags = merge_flags(flags, consensus)
        except Exception:
            # el contraste es un extra: si falla, la foto sigue su camino
            log.warning("%s: falló el motor de contraste, sigo sin consenso",
                        path.name, exc_info=True)
    report.review = bool(flags)
    report.review_reasons = [f.message for f in flags]
    if report.review:
        log.warning("%s: recorte dudoso — %s", path.name,
                    "; ".join(report.review_reasons))
        if opts.move_uncertain:
            # solo la ruta: la carpeta se crea recién al exportar, para no
            # dejar un revisar/ vacío si la foto no produce archivos
            out_dir = out_dir / REVIEW_DIRNAME

    if cancel and cancel():
        raise BatchCancelled

    if opts.mode == "conjunto":
        bbox = content_bbox(alpha, opts.alpha_threshold)
        if bbox is None:
            log.warning("%s: sin contenido detectado, no se exporta", path.name)
        else:
            cropped = _crop_bbox(rgba, bbox, opts.margin)
            out_dir.mkdir(parents=True, exist_ok=True)
            out = export_single(cropped, out_dir, stem, opts.fmt, loaded.icc_profile)
            report.outputs.append(out)
            report.objects_found = 1
    else:
        comps = separate(
            alpha,
            min_area=opts.min_area,
            threshold=opts.alpha_threshold,
            split_touching_pieces=opts.split_touching,
        )
        from .separation import find_components

        n_all = len(find_components(alpha, min_area=1, threshold=opts.alpha_threshold))
        n_kept = len(find_components(alpha, min_area=opts.min_area, threshold=opts.alpha_threshold))
        report.objects_discarded = max(0, n_all - n_kept)
        report.objects_found = len(comps)
        if comps:
            out_dir.mkdir(parents=True, exist_ok=True)
        if not comps:
            log.warning("%s: sin objetos sobre el área mínima", path.name)
        elif opts.fmt == "psd" and opts.psd_layered:
            # Un PSD por foto con cada objeto en su propia capa, en posición
            layers = []
            for i, c in enumerate(comps, start=1):
                piece = crop_component(rgba, alpha, c, margin=opts.margin)
                x, y = max(0, c.bbox[0] - opts.margin), max(0, c.bbox[1] - opts.margin)
                layers.append((numbered_stem(stem, i, len(comps)), piece, x, y))
            out = unique_path(out_dir, stem, extension_for("psd"))
            from .export import save_psd_layers

            save_psd_layers(layers, out, width=rgba.shape[1], height=rgba.shape[0])
            report.outputs.append(out)
        else:
            for i, c in enumerate(comps, start=1):
                piece = crop_component(rgba, alpha, c, margin=opts.margin)
                out = export_single(
                    piece, out_dir, numbered_stem(stem, i, len(comps)),
                    opts.fmt, loaded.icc_profile,
                )
                report.outputs.append(out)

    report.seconds = time.perf_counter() - t0
    return report


def run_batch(
    inputs: Path | list[Path],
    out_dir: Path,
    matter: BaseMatter,
    opts: BatchOptions | None = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    verifier: BaseMatter | None = None,
) -> BatchSummary:
    """Procesa una carpeta o una lista de archivos. Nunca aborta por una imagen.

    ``verifier`` es un segundo motor opcional: cada foto se recorta también
    con él y el desacuerdo entre ambos marca el recorte como dudoso.
    """
    opts = opts or BatchOptions()
    opts.validate()
    if isinstance(inputs, Path):
        files = list_images(inputs)
    else:
        files = [Path(p) for p in inputs]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = BatchSummary(
        with_verifier=verifier is not None,
        review_dir=out_dir / REVIEW_DIRNAME if opts.move_uncertain else None,
    )
    total = len(files)
    log.info("Lote: %d imágenes → %s (modo=%s, formato=%s)", total, out_dir, opts.mode, opts.fmt)
    for i, path in enumerate(files, start=1):
        if cancel and cancel():
            log.info("Lote cancelado por el usuario en %d/%d", i - 1, total)
            break
        if progress:
            progress(i - 1, total, path.name)
        report = ImageReport(source=path, ok=True)
        try:
            process_image(path, matter, out_dir, opts, verifier=verifier,
                          cancel=cancel, report=report)
            log.info(
                "%s: %d objeto(s), %d archivo(s), %.1fs",
                path.name, report.objects_found, len(report.outputs), report.seconds,
            )
        except BatchCancelled:
            log.info("Lote cancelado por el usuario en %d/%d; %s no se exportó",
                     i - 1, total, path.name)
            break
        except Exception as exc:  # noqa: BLE001 — el lote sigue pase lo que pase
            log.error("%s: ERROR %s\n%s", path.name, exc, traceback.format_exc())
            # report.outputs conserva los archivos que alcanzaron a guardarse
            report.ok = False
            report.error = str(exc)
        summary.reports.append(report)
    if progress:
        progress(len(summary.reports), total, "")
    log.info("Resumen:\n%s", summary.text())
    return summary


def setup_batch_logging(logs_dir: Path) -> Path:
    """Log a archivo por corrida, para diagnosticar sin terminal.

    Cierra el log de la corrida anterior del mismo proceso: cada corrida
    escribe solo en su archivo.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    logfile = logs_dir / f"kamiru_{stamp}.log"
    n = 1
    while logfile.exists():  # dos corridas en el mismo segundo
        logfile = logs_dir / f"kamiru_{stamp}-{n}.log"
        n += 1
    root = logging.getLogger("kamiru")
    for old in [h for h in root.handlers if getattr(h, "_kamiru_batch_log", False)]:
        root.removeHandler(old)
        old.close()
    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler._kamiru_batch_log = True
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return logfile
