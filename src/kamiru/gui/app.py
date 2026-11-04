"""GUI de Kamiru: ventana simple de arrastrar y soltar.

Controles principales (los que ve Kamila): entrada (carpeta o fotos sueltas),
carpeta de salida, modo (Conjunto/Individual), formato, sufijo opcional,
Vista previa, Procesar, barra de progreso y resumen. Un desplegable
«▸ Opciones avanzadas» esconde el resto (motor, resolución, área mínima,
separación de piezas que se tocan, croma).

La configuración se recuerda entre sesiones (settings.json junto a la app).
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from .. import APP_NAME, __version__
from ..core.imageio import SUPPORTED_EXTENSIONS, is_supported, list_images
from ..core.pipeline import BatchOptions, run_batch, setup_batch_logging
from ..paths import logs_dir
from ..settings import Settings, load_settings, save_settings

log = logging.getLogger("kamiru.gui")

MODEL_CHOICES = ["rmbg-2.0", "birefnet", "birefnet-hr", "ben2", "croma (por color)"]
FORMAT_LABELS = {
    "PNG (transparente)": "png",
    "TIFF con alfa (8-bit)": "tiff",
    "TIFF con alfa (16-bit)": "tiff16",
    "PSD por capas": "psd",
}
RESOLUTIONS = ["1024", "1536", "2048"]


def _try_dnd_root():
    """Ventana raíz con drag-and-drop si tkinterdnd2 está disponible."""
    try:
        from tkinterdnd2 import TkinterDnD

        return TkinterDnD.Tk(), True
    except Exception:
        return tk.Tk(), False


class KamiruApp:
    def __init__(self) -> None:
        from ..core.matting import resolve_default_model

        self.root, self.has_dnd = _try_dnd_root()
        self.root.title(f"{APP_NAME} — recorte de fondos {__version__}")
        self.root.minsize(600, 500)

        saved = load_settings()
        self.input_dir = tk.StringVar(value=saved.input_dir)
        self.output_dir = tk.StringVar(value=saved.output_dir)
        self.mode = tk.StringVar(value=saved.mode)
        self.format_label = tk.StringVar(
            value=saved.format_label if saved.format_label in FORMAT_LABELS
            else "PNG (transparente)")
        self.suffix = tk.StringVar(value=saved.suffix)
        self.model = tk.StringVar(value=saved.model or resolve_default_model())
        self.resolution = tk.StringVar(
            value=saved.resolution if saved.resolution in RESOLUTIONS else "1024")
        self.min_area = tk.StringVar(value=saved.min_area)
        self.split_touching = tk.BooleanVar(value=saved.split_touching)
        self.review_uncertain = tk.BooleanVar(value=saved.review_uncertain)
        self.verify_second = tk.BooleanVar(value=saved.verify_second)
        self.chroma_auto = tk.BooleanVar(value=True)
        self.chroma_color: tuple[int, int, int] | None = None
        self.dropped_files: list[Path] = []

        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._matter = None
        self._matter_key: tuple | None = None
        self._verifier = None
        self._verifier_key: tuple | None = None
        # generación de trabajo: Cancelar la incrementa y el trabajo viejo
        # queda "huérfano" — la UI se libera al instante sin esperar al hilo
        self._job_gen = 0
        self._busy_since: float | None = None
        # un solo hilo usa los motores (y escribe en la salida) a la vez: el
        # trabajo nuevo espera a que el huérfano termine su carga o su foto
        self._engine_lock = threading.Lock()

        self._build_ui()
        self._logfile = setup_batch_logging(logs_dir())
        self._show_device_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        drop = tk.Label(
            main,
            text=("Arrastra aquí una carpeta o fotos sueltas\n(o usa los botones de abajo)"
                  if self.has_dnd else "Elige la carpeta o las fotos con los botones"),
            relief="groove", height=4, bg="#f2f0ec", fg="#555",
        )
        drop.pack(fill="x", **pad)
        if self.has_dnd:
            from tkinterdnd2 import DND_FILES

            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", self._on_drop)

        row1 = ttk.Frame(main); row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Fotos (entrada):", width=15).pack(side="left")
        ttk.Entry(row1, textvariable=self.input_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="Carpeta…", command=self._pick_input, width=9).pack(
            side="left", padx=(6, 0))
        ttk.Button(row1, text="Fotos…", command=self._pick_files, width=8).pack(
            side="left", padx=(4, 0))

        row2 = ttk.Frame(main); row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Guardar en:", width=15).pack(side="left")
        ttk.Entry(row2, textvariable=self.output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="Elegir…", command=self._pick_output, width=9).pack(
            side="left", padx=(6, 0))

        row3 = ttk.Frame(main); row3.pack(fill="x", **pad)
        ttk.Label(row3, text="Modo:", width=15).pack(side="left")
        ttk.Radiobutton(row3, text="Conjunto (todo en un archivo)",
                        variable=self.mode, value="conjunto").pack(side="left")
        ttk.Radiobutton(row3, text="Individual (un archivo por pieza)",
                        variable=self.mode, value="individual").pack(side="left", padx=(10, 0))

        row4 = ttk.Frame(main); row4.pack(fill="x", **pad)
        ttk.Label(row4, text="Formato:", width=15).pack(side="left")
        ttk.Combobox(row4, textvariable=self.format_label, state="readonly", width=22,
                     values=list(FORMAT_LABELS)).pack(side="left")
        ttk.Label(row4, text="  Sufijo:").pack(side="left")
        suffix_entry = ttk.Entry(row4, textvariable=self.suffix, width=12)
        suffix_entry.pack(side="left")
        ttk.Label(row4, text=" ej: «recorte» → foto_recorte.png",
                  foreground="#888").pack(side="left")

        # ------- desplegable de opciones avanzadas
        self._adv_visible = False
        self._adv_toggle = tk.Label(
            main, text="▸ Opciones avanzadas", fg="#2a5db0", cursor="hand2",
            font=("TkDefaultFont", 9, "underline"),
        )
        self._adv_toggle.pack(anchor="w", padx=12, pady=(2, 0))
        self._adv_toggle.bind("<Button-1>", lambda _e: self._toggle_advanced())
        self.adv = ttk.LabelFrame(main, text="Opciones avanzadas")

        a1 = ttk.Frame(self.adv); a1.pack(fill="x", **pad)
        ttk.Label(a1, text="Motor:", width=15).pack(side="left")
        ttk.Combobox(a1, textvariable=self.model, state="readonly",
                     values=MODEL_CHOICES, width=18).pack(side="left")
        ttk.Label(a1, text="  Resolución:").pack(side="left")
        ttk.Combobox(a1, textvariable=self.resolution, state="readonly",
                     values=RESOLUTIONS, width=6).pack(side="left")

        a2 = ttk.Frame(self.adv); a2.pack(fill="x", **pad)
        ttk.Label(a2, text="Área mínima (px):", width=15).pack(side="left")
        ttk.Entry(a2, textvariable=self.min_area, width=8).pack(side="left")
        ttk.Checkbutton(a2, text="Separar piezas que se tocan (experimental)",
                        variable=self.split_touching).pack(side="left", padx=(14, 0))

        a3 = ttk.Frame(self.adv); a3.pack(fill="x", **pad)
        ttk.Label(a3, text="Croma:", width=15).pack(side="left")
        ttk.Checkbutton(a3, text="Color de fondo automático (esquinas)",
                        variable=self.chroma_auto).pack(side="left")
        ttk.Button(a3, text="Elegir color…", command=self._pick_chroma_color).pack(
            side="left", padx=(10, 0))
        self._chroma_swatch = tk.Label(a3, text="  auto  ", relief="sunken")
        self._chroma_swatch.pack(side="left", padx=(8, 0))

        a4 = ttk.Frame(self.adv); a4.pack(fill="x", **pad)
        ttk.Label(a4, text="Calidad:", width=15).pack(side="left")
        ttk.Checkbutton(a4, text="Mover recortes dudosos a «revisar»",
                        variable=self.review_uncertain).pack(side="left")
        ttk.Checkbutton(a4, text="Contrastar con un 2º modelo",
                        variable=self.verify_second).pack(side="left", padx=(14, 0))

        # ------- acciones, progreso, resumen
        actions = ttk.Frame(main); actions.pack(fill="x", **pad)
        self._actions_frame = actions
        self.run_btn = ttk.Button(actions, text="Procesar", command=self._start)
        self.run_btn.pack(side="left")
        self.preview_btn = ttk.Button(actions, text="Vista previa", command=self._preview)
        self.preview_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn = ttk.Button(actions, text="Cancelar", command=self._cancel_run,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.open_out_btn = ttk.Button(actions, text="Abrir salida",
                                       command=self._open_output, state="disabled")
        self.open_out_btn.pack(side="left", padx=(8, 0))
        self.device_lbl = ttk.Label(actions, text="", foreground="#666")
        self.device_lbl.pack(side="right")
        self.elapsed_lbl = ttk.Label(actions, text="", foreground="#666")
        self.elapsed_lbl.pack(side="right", padx=(0, 10))

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", **pad)
        self.status = ttk.Label(main, text="Lista.")
        self.status.pack(fill="x", padx=10)

        self.summary_box = tk.Text(main, height=7, state="disabled",
                                   bg="#faf9f7", relief="flat", wrap="word")
        self.summary_box.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    def _toggle_advanced(self) -> None:
        self._adv_visible = not self._adv_visible
        if self._adv_visible:
            self._adv_toggle.configure(text="▾ Opciones avanzadas")
            self.adv.pack(fill="x", padx=10, pady=4, before=self._actions_frame)
        else:
            self._adv_toggle.configure(text="▸ Opciones avanzadas")
            self.adv.pack_forget()

    def _show_device_status(self) -> None:
        def work():
            try:
                from ..core.device import detect_device

                info = detect_device()
                self._queue.put(("device", info))
            except Exception as exc:  # torch aún no instalado, etc.
                self._queue.put(("device_error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- eventos
    def _on_drop(self, event) -> None:
        paths = [Path(p) for p in self.root.tk.splitlist(event.data)]
        dirs = [p for p in paths if p.is_dir()]
        files = [p for p in paths if p.is_file() and is_supported(p)]
        if dirs:
            self.input_dir.set(str(dirs[0]))
            self.dropped_files = []
            self._set_status(f"Carpeta: {dirs[0].name} ({len(list_images(dirs[0]))} fotos)")
        elif files:
            self._set_files(files)

    def _set_files(self, files: list[Path]) -> None:
        self.dropped_files = files
        self.input_dir.set(f"{len(files)} foto(s) elegidas")
        self._set_status("Fotos elegidas: " + ", ".join(f.name for f in files[:6])
                         + ("…" if len(files) > 6 else ""))

    def _pick_input(self) -> None:
        d = filedialog.askdirectory(title="Carpeta con las fotos")
        if d:
            self.input_dir.set(d)
            self.dropped_files = []

    def _pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTENSIONS))
        files = filedialog.askopenfilenames(
            title="Elegir fotos sueltas",
            filetypes=[("Imágenes", exts), ("Todos los archivos", "*.*")],
        )
        if files:
            self._set_files([Path(f) for f in files])

    def _pick_output(self) -> None:
        d = filedialog.askdirectory(title="Carpeta donde guardar los recortes")
        if d:
            self.output_dir.set(d)

    def _pick_chroma_color(self) -> None:
        color = colorchooser.askcolor(title="Color del fondo a quitar")
        if color and color[0]:
            self.chroma_color = tuple(int(c) for c in color[0])
            self.chroma_auto.set(False)
            hexcol = "#%02x%02x%02x" % self.chroma_color
            self._chroma_swatch.configure(text="        ", bg=hexcol)

    def _open_output(self) -> None:
        out = self.output_dir.get().strip()
        if not out or not Path(out).is_dir():
            return
        import subprocess
        import sys

        if sys.platform == "win32":
            subprocess.Popen(["explorer", out])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", out])
        else:
            subprocess.Popen(["xdg-open", out])

    def _on_close(self) -> None:
        if self._busy_since is not None and not messagebox.askyesno(
                APP_NAME, "Hay un trabajo en curso. Si cierras ahora, se corta "
                          "y la foto en curso no se guarda.\n\n¿Cerrar igual?"):
            return
        self._save_settings()
        self.root.destroy()

    def _save_settings(self) -> None:
        save_settings(Settings(
            input_dir="" if self.dropped_files else self.input_dir.get(),
            output_dir=self.output_dir.get(),
            mode=self.mode.get(),
            format_label=self.format_label.get(),
            suffix=self.suffix.get(),
            model=self.model.get(),
            resolution=self.resolution.get(),
            min_area=self.min_area.get(),
            split_touching=self.split_touching.get(),
            review_uncertain=self.review_uncertain.get(),
            verify_second=self.verify_second.get(),
        ))

    # ------------------------------------------------------------- proceso
    def _collect_inputs(self):
        if self.dropped_files:
            return self.dropped_files
        raw = self.input_dir.get().strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_dir():
            return None
        return p

    def _matter_spec(self) -> dict:
        """Lee la config del motor EN EL HILO PRINCIPAL (Tk no es thread-safe:
        leer variables Tk desde el worker se cuelga o muere en silencio)."""
        return {
            "choice": self.model.get(),
            "resolution": int(self.resolution.get()),
            "chroma_auto": self.chroma_auto.get(),
            "chroma_color": self.chroma_color,
            "verify": self.verify_second.get(),
        }

    def _build_matter(self, spec: dict, gen: int):
        """Corre en el worker: usa solo valores planos, nada de widgets Tk."""
        choice = spec["choice"]
        if choice.startswith("croma"):
            from ..core.chroma import ChromaMatter

            key = None if spec["chroma_auto"] else spec["chroma_color"]
            return ChromaMatter(key_color=key)
        key = (choice, spec["resolution"])
        if self._matter is not None and self._matter_key == key:
            return self._matter  # ya cargado, no re-descargar ni re-cargar
        from ..core.matting import load_matter

        matter = load_matter(
            choice, process_resolution=spec["resolution"],
            progress=lambda msg: self._queue.put(("status", gen, msg)),
        )
        # aunque este trabajo haya sido cancelado, el modelo queda cacheado
        # para el próximo Procesar (la carga no se repite)
        self._matter, self._matter_key = matter, key
        return matter

    def _build_verifier(self, spec: dict, gen: int):
        """Motor de contraste (2º modelo), con su propio cache entre corridas."""
        from ..core.matting import pick_verifier_model

        choice = spec["choice"]
        if choice.startswith("croma"):
            # croma y un modelo neural difieren por diseño: contrastarlos
            # solo marcaría casi todo como dudoso (falsas alarmas)
            log.info("Motor croma: se omite el modelo de contraste")
            return None
        vchoice = pick_verifier_model(choice)
        key = (vchoice, spec["resolution"])
        if self._verifier is not None and self._verifier_key == key:
            return self._verifier
        from ..core.matting import load_matter

        verifier = load_matter(
            vchoice, process_resolution=spec["resolution"],
            progress=lambda msg: self._queue.put(("status", gen, f"[contraste] {msg}")),
        )
        self._verifier, self._verifier_key = verifier, key
        return verifier

    def _validated_options(self) -> BatchOptions | None:
        try:
            min_area = int(self.min_area.get())
        except ValueError:
            messagebox.showwarning(APP_NAME, "El área mínima debe ser un número entero de píxeles.")
            return None
        return BatchOptions(
            mode=self.mode.get(),
            fmt=FORMAT_LABELS[self.format_label.get()],
            min_area=min_area,
            split_touching=self.split_touching.get(),
            suffix=self.suffix.get(),
            move_uncertain=self.review_uncertain.get(),
        )

    def _begin_job(self) -> int:
        """Marca la UI como ocupada y devuelve la generación de este trabajo."""
        import time

        self._job_gen += 1
        gen = self._job_gen
        self._cancel.clear()
        self._busy_since = time.monotonic()
        self.run_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        return gen

    def _end_job(self) -> None:
        self._busy_since = None
        self.elapsed_lbl.configure(text="")
        self.run_btn.configure(state="normal")
        self.preview_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    def _is_stale(self, gen: int) -> bool:
        return gen != self._job_gen or self._cancel.is_set()

    def _exclusive(self, gen: int, job) -> None:
        """Corre ``job`` en el worker con los motores para él solo.

        Tras Cancelar, el hilo viejo puede seguir cargando el modelo o
        recortando la foto en curso. El trabajo nuevo espera a que termine:
        así no se carga el modelo dos veces ni corren dos recortes a la vez.
        """
        if not self._engine_lock.acquire(blocking=False):
            self._queue.put(("status", gen, "Esperando a que termine el trabajo "
                                            "cancelado..."))
            self._engine_lock.acquire()
        try:
            if not self._is_stale(gen):
                job()
        finally:
            self._engine_lock.release()

    def _start(self) -> None:
        inputs = self._collect_inputs()
        if inputs is None:
            messagebox.showwarning(APP_NAME, "Elige una carpeta o fotos sueltas para procesar.")
            return
        out = self.output_dir.get().strip()
        if not out:
            messagebox.showwarning(APP_NAME, "Elige la carpeta donde guardar los recortes.")
            return
        opts = self._validated_options()
        if opts is None:
            return
        self._save_settings()
        spec = self._matter_spec()
        gen = self._begin_job()
        self.progress.configure(value=0)
        self._set_summary("")

        def work():
            try:
                notes: list[str] = []
                matter = self._build_matter(spec, gen)
                verifier = None
                if spec["verify"]:
                    try:
                        verifier = self._build_verifier(spec, gen)
                    except Exception:
                        # el contraste es un extra: si el 2º modelo no carga
                        # (sin red, sin disco), el lote sigue con el motor
                        # principal y el control de calidad básico
                        log.exception("No se pudo cargar el modelo de contraste")
                        self._queue.put(("status", gen,
                                         "⚠ Sin modelo de contraste (no se pudo "
                                         "cargar); sigo con el control básico."))
                        notes.append("no se pudo cargar el 2º modelo: ninguna "
                                     "foto tuvo contraste, solo el control básico.")
                if self._is_stale(gen):
                    return  # cancelado durante la carga: el modelo queda cacheado
                summary = run_batch(
                    inputs, Path(out), matter, opts,
                    progress=lambda done, total, name: self._queue.put(
                        ("progress", gen, done, total, name)),
                    cancel=lambda: self._is_stale(gen),
                    verifier=verifier,
                )
                summary.notes.extend(notes)
                self._queue.put(("done", gen, summary))
            except Exception as exc:
                log.exception("Fallo del lote")
                self._queue.put(("fatal", gen, str(exc)))

        self._worker = threading.Thread(target=self._exclusive, args=(gen, work),
                                        daemon=True)
        self._worker.start()

    def _preview(self) -> None:
        """Recorta solo la primera foto y la muestra sobre tablero de ajedrez."""
        inputs = self._collect_inputs()
        if inputs is None:
            messagebox.showwarning(APP_NAME, "Elige una carpeta o fotos sueltas primero.")
            return
        files = self.dropped_files or list_images(inputs)
        if not files:
            messagebox.showwarning(APP_NAME, "No hay fotos soportadas en la selección.")
            return
        first = files[0]
        spec = self._matter_spec()
        gen = self._begin_job()
        self._set_status(f"Vista previa de {first.name}…")

        def work():
            try:
                from ..core.imageio import load_image

                matter = self._build_matter(spec, gen)
                if self._is_stale(gen):
                    return
                loaded = load_image(first)
                result = matter.cutout(loaded.rgb)
                from ..core.quality import assess_cutout

                reasons = [f.message for f in assess_cutout(loaded.rgb, result.alpha).flags]
                self._queue.put(("preview", gen, first.name, result.rgba, reasons))
            except Exception as exc:
                log.exception("Fallo de la vista previa")
                self._queue.put(("fatal", gen, str(exc)))

        threading.Thread(target=self._exclusive, args=(gen, work), daemon=True).start()

    def _show_preview(self, name: str, rgba, reasons: list[str]) -> None:
        from PIL import Image, ImageTk

        max_side = 680
        scale = min(max_side / rgba.width, max_side / rgba.height, 1.0)
        im = rgba.resize((int(rgba.width * scale), int(rgba.height * scale)),
                         Image.BILINEAR)
        # tablero de ajedrez de fondo para ver la transparencia
        board = Image.new("RGB", im.size, "white")
        t = 12
        for y in range(0, im.height, t):
            for x in range(0, im.width, t):
                if (x // t + y // t) % 2:
                    board.paste((205, 205, 205), (x, y, min(x + t, im.width),
                                                  min(y + t, im.height)))
        board.paste(im, (0, 0), im)

        win = tk.Toplevel(self.root)
        win.title(f"Vista previa — {name}")
        photo = ImageTk.PhotoImage(board)
        lbl = tk.Label(win, image=photo)
        lbl.image = photo  # evitar que el GC borre la imagen
        lbl.pack()
        if reasons:
            ttk.Label(win, text="⚠ Recorte dudoso: " + "; ".join(reasons),
                      foreground="#a33", wraplength=660).pack(padx=10, pady=(6, 0))
        ttk.Label(win, text="Así saldrá el recorte. Cierra esta ventana y "
                            "aprieta Procesar si se ve bien.").pack(pady=6)

    def _cancel_run(self) -> None:
        # liberar la UI al instante; el hilo viejo queda huérfano y termina
        # solo (si estaba cargando el modelo, la carga se aprovecha después)
        self._cancel.set()
        self._job_gen += 1
        self._end_job()
        self.progress.configure(value=0)
        self._set_status("Cancelado. (Si el modelo estaba cargándose, la carga "
                         "sigue de fondo y se aprovecha en el próximo Procesar.)")

    # ------------------------------------------------------------- cola UI
    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "device":
                    info = msg[1]
                    self.device_lbl.configure(text=info.name)
                    # con un trabajo en curso, el aviso no tapa su estado
                    if info.warning and self._busy_since is None:
                        self._set_status(f"⚠ {info.warning}")
                        self._set_summary(f"⚠ {info.warning}")
                    continue
                if kind == "device_error":
                    self.device_lbl.configure(text="PyTorch no disponible")
                    continue

                # el resto de los mensajes vienen etiquetados con la
                # generación; los de un trabajo cancelado se descartan
                gen = msg[1]
                if gen != self._job_gen:
                    continue
                if kind == "progress":
                    _, _, done, total, name = msg
                    self.progress.configure(maximum=max(total, 1), value=done)
                    if name:
                        self._set_status(f"Procesando {done + 1} de {total}: {name}")
                elif kind == "status":
                    self._set_status(msg[2])
                elif kind == "preview":
                    self._end_job()
                    self._set_status("Vista previa lista.")
                    self._show_preview(msg[2], msg[3], msg[4])
                elif kind == "done":
                    summary = msg[2]
                    self._end_job()
                    self.open_out_btn.configure(state="normal")
                    self.progress.configure(value=self.progress["maximum"])
                    self._set_status("Terminado.")
                    self._set_summary(summary.text())
                    messagebox.showinfo(
                        f"{APP_NAME} — resumen",
                        summary.text() or "No había imágenes para procesar.",
                    )
                elif kind == "fatal":
                    self._end_job()
                    self._set_status("Error.")
                    self._set_summary(f"Error: {msg[2]}\n\nDetalles: {self._logfile}")
                    messagebox.showerror(
                        APP_NAME,
                        f"No se pudo procesar:\n{msg[2]}\n\n"
                        f"Detalles técnicos en:\n{self._logfile}",
                    )
        except queue.Empty:
            pass
        self._tick_elapsed()
        self.root.after(100, self._poll_queue)

    def _tick_elapsed(self) -> None:
        if self._busy_since is None:
            return
        import time

        secs = int(time.monotonic() - self._busy_since)
        self.elapsed_lbl.configure(text=f"⏱ {secs // 60}:{secs % 60:02d}")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _set_summary(self, text: str) -> None:
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        self.summary_box.insert("1.0", text)
        self.summary_box.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    from ..runtime import ensure_std_streams, quiet_library_progress

    ensure_std_streams()      # pythonw / exe sin consola: streams seguros
    quiet_library_progress()  # sin barras tqdm de librerías en la GUI
    logging.basicConfig(level=logging.INFO)
    KamiruApp().run()


if __name__ == "__main__":
    main()
