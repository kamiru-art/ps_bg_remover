"""CLI de Kamiru — el mismo core que la GUI, para scripts y pruebas.

Ejemplos:
    kamiru fotos/ salida/ --modo individual --formato png --area-minima 400
    kamiru fotos/ salida/ --modelo birefnet-hr --resolucion 2048
    kamiru fotos/ salida/ --motor croma --chroma-color 00ff00
    kamiru fotos/ salida/ --verificar --revisar   # consenso + carpeta revisar/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .core.matting import MODEL_REPOS, resolve_default_model
from .core.pipeline import BatchOptions, run_batch, setup_batch_logging
from .paths import logs_dir


def _parse_color(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) != 6:
        raise argparse.ArgumentTypeError("Color esperado en hex RRGGBB, ej. 00ff00")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kamiru",
        description="Recorte de fondos por lotes con RMBG-2.0 (local, sin nube).",
    )
    p.add_argument("entrada", type=Path, help="Carpeta con las fotos (o un archivo)")
    p.add_argument("salida", type=Path, help="Carpeta donde guardar los recortes")
    p.add_argument("--modo", choices=["conjunto", "individual"], default="conjunto")
    p.add_argument("--formato", choices=["png", "tiff", "tiff16", "psd"], default="png")
    p.add_argument("--area-minima", type=int, default=400,
                   help="Área mínima en px² para exportar un objeto (filtra motas)")
    p.add_argument("--umbral", type=float, default=0.5, help="Umbral de binarizado del alfa")
    p.add_argument("--margen", type=int, default=2, help="Margen del recorte en px")
    p.add_argument("--sufijo", default="",
                   help="Sufijo para los nombres: foto_SUFIJO.png / foto_SUFIJO_01.png")
    p.add_argument("--separar-tocandose", action="store_true",
                   help="Watershed para dividir piezas que se tocan")
    p.add_argument("--motor", choices=["ia", "croma"], default="ia",
                   help="ia = modelo neural; croma = por color de fondo")
    p.add_argument("--modelo", choices=list(MODEL_REPOS), default=resolve_default_model(),
                   help="Modelo neural (para A/B de nitidez de borde)")
    p.add_argument("--resolucion", type=int, default=1024,
                   help="Resolución de proceso del modelo (1024/1536/2048)")
    p.add_argument("--chroma-color", type=_parse_color, default=None,
                   help="Color de fondo hex para croma (default: auto por esquinas)")
    p.add_argument("--psd-archivos-sueltos", action="store_true",
                   help="Individual+PSD: un PSD por pieza en vez de un PSD por capas")
    p.add_argument("--revisar", action="store_true",
                   help="Mover los recortes dudosos a SALIDA/revisar/")
    p.add_argument("--verificar", action="store_true",
                   help="Contrastar cada foto con un segundo modelo (consenso): "
                        "el desacuerdo marca el recorte como dudoso")
    p.add_argument("--modelo-verificacion", choices=list(MODEL_REPOS), default=None,
                   help="Modelo del contraste (default: automático, distinto al principal)")
    p.add_argument("--sin-control-calidad", action="store_true",
                   help="No calcular las señales de recorte dudoso (nivel 1)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.verificar and args.motor == "ia"
            and args.modelo_verificacion == args.modelo):
        parser.error("--modelo-verificacion debe ser distinto de --modelo: "
                     "contrastar un modelo consigo mismo no verifica nada")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logfile = setup_batch_logging(logs_dir())

    device = None
    if args.motor == "croma":
        from .core.chroma import ChromaMatter

        matter = ChromaMatter(key_color=args.chroma_color)
    else:
        from .core.device import detect_device
        from .core.matting import load_matter

        device = detect_device()
        print(f"Dispositivo: {device.name}")
        if device.warning:
            print(f"AVISO: {device.warning}")
        matter = load_matter(args.modelo, process_resolution=args.resolucion,
                             device=device, progress=print)

    verifier = None
    if args.verificar:
        from .core.device import detect_device
        from .core.matting import load_matter, pick_verifier_model

        if args.motor == "croma":
            print("AVISO: contrastar croma con un modelo neural difiere por "
                  "diseño; espera muchas fotos marcadas como dudosas.")
        vkey = args.modelo_verificacion or pick_verifier_model(matter.name)
        print(f"Modelo de contraste: {vkey}")
        try:
            if device is None:
                device = detect_device()
            verifier = load_matter(vkey, process_resolution=args.resolucion,
                                   device=device, progress=print)
        except Exception as exc:
            # el contraste es un extra: si no carga, el lote sigue con el
            # motor principal y el control de calidad básico
            print(f"AVISO: no se pudo cargar el modelo de contraste ({exc}); "
                  "se sigue sin consenso, solo con las señales básicas.")

    opts = BatchOptions(
        mode=args.modo,
        fmt=args.formato,
        min_area=args.area_minima,
        alpha_threshold=args.umbral,
        margin=args.margen,
        split_touching=args.separar_tocandose,
        psd_layered=not args.psd_archivos_sueltos,
        suffix=args.sufijo,
        quality_check=not args.sin_control_calidad,
        move_uncertain=args.revisar,
    )

    entrada = args.entrada
    inputs: Path | list[Path] = entrada if entrada.is_dir() else [entrada]

    def progress(done: int, total: int, name: str) -> None:
        if name:
            print(f"[{done + 1}/{total}] {name}", flush=True)

    summary = run_batch(inputs, args.salida, matter, opts, progress=progress,
                        verifier=verifier)
    if matter.fell_back_to_cpu or (verifier is not None and verifier.fell_back_to_cpu):
        from .core.matting import CPU_FALLBACK_NOTE

        summary.notes.append(CPU_FALLBACK_NOTE)
    print("\n===== Resumen =====")
    print(summary.text())
    print(f"Log: {logfile}")
    return 0 if not summary.failed else 1


if __name__ == "__main__":
    sys.exit(main())
