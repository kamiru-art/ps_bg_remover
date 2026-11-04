"""Ciclo de vida de los trabajos en la GUI real, con motores falsos.

Se salta si Tk no puede abrir una ventana (CI sin pantalla). Corre en una
máquina con escritorio, o en Linux con ``xvfb-run``.
"""

import threading

import numpy as np
import pytest
from PIL import Image

from kamiru.core.matting import BaseMatter, MatteResult, compose_rgba


def _tk_or_skip():
    tk = pytest.importorskip("tkinter")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        pytest.skip("Tk no puede abrir una ventana aquí")


def test_cancelar_y_procesar_enseguida(tmp_path, monkeypatch):
    """Cancelar durante la carga y durante una foto, y apretar Procesar enseguida.

    Formas de fallar que este escenario detecta:
    1. El trabajo nuevo carga el modelo otra vez mientras sigue la carga del
       trabajo cancelado (dos copias de los pesos).
    2. El hilo cancelado y el trabajo nuevo recortan a la vez con el mismo
       motor.
    3. El hilo cancelado exporta la foto que estaba recortando.
    4. Dos hilos eligen el mismo nombre de salida y uno pisa al otro.
    5. La UI no dice que el trabajo nuevo está esperando.
    """
    _tk_or_skip()
    monkeypatch.setenv("KAMIRU_HOME", str(tmp_path))

    load_started, release_load = threading.Event(), threading.Event()
    cut_started, release_cut = threading.Event(), threading.Event()
    stats = {"loads": [], "active": 0, "max_active": 0, "cuts": 0}
    guard = threading.Lock()

    class GatedFake(BaseMatter):
        def __init__(self, key):
            self.name = key

        def alpha(self, rgb):
            arr = np.asarray(rgb, dtype=np.float32)
            return ((arr.max(axis=2) - arr.min(axis=2)) > 40).astype(np.float32)

        def cutout(self, rgb):
            with guard:
                stats["active"] += 1
                stats["max_active"] = max(stats["max_active"], stats["active"])
                stats["cuts"] += 1
                n = stats["cuts"]
            if n == 2:  # 2ª foto del primer lote que corre: aquí se cancela
                cut_started.set()
                release_cut.wait(10)
            a = self.alpha(rgb)
            with guard:
                stats["active"] -= 1
            return MatteResult(alpha=a, rgba=compose_rgba(rgb, a))

    def fake_load(key, process_resolution=1024, device=None, progress=None):
        stats["loads"].append(key)
        load_started.set()
        release_load.wait(10)
        return GatedFake(key)

    from kamiru.core import matting

    monkeypatch.setattr(matting, "load_matter", fake_load)
    from kamiru.gui import app as gui

    dialogs = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda t, x: dialogs.append(x))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda t, x: dialogs.append("ERROR " + x))

    inp = tmp_path / "fotos"
    inp.mkdir()
    for i in range(3):
        arr = np.full((80, 100, 3), 128, dtype=np.uint8)
        arr[20:60, 20:80] = (200, 30 + i * 40, 40)
        Image.fromarray(arr).save(inp / f"foto{i}.png")
    out = tmp_path / "salida"

    app = gui.KamiruApp()
    app.root.withdraw()
    app.input_dir.set(str(inp))
    app.output_dir.set(str(out))
    app.model.set("birefnet")
    app.verify_second.set(False)
    seen = {"waiting": False, "loads_while_waiting": None, "timeout": False}

    def step(name):
        def run():
            if name == "start":
                app._start()
                return app.root.after(50, step("cancel_load"))
            if name == "cancel_load":
                if not load_started.is_set():
                    return app.root.after(50, run)
                app._cancel_run()
                app._start()
                return app.root.after(50, step("waiting"))
            if name == "waiting":
                waiting = "Esperando" in app.status.cget("text")
                if not waiting and len(stats["loads"]) < 2:
                    return app.root.after(50, run)
                seen["waiting"] = waiting
                seen["loads_while_waiting"] = len(stats["loads"])
                release_load.set()
                return app.root.after(50, step("cancel_cut"))
            if name == "cancel_cut":
                if not cut_started.is_set():
                    return app.root.after(50, run)
                app._cancel_run()
                app._start()
                release_cut.set()
                return app.root.after(50, step("finish"))
            if name == "finish":
                if app._busy_since is not None or app._engine_lock.locked() or not dialogs:
                    return app.root.after(50, run)
                app.root.destroy()
        return run

    def give_up():
        seen["timeout"] = True
        release_load.set()
        release_cut.set()
        app.root.destroy()

    app.root.after(100, step("start"))
    app.root.after(30000, give_up)
    app.run()

    assert not seen["timeout"]
    assert stats["loads"] == ["birefnet"]                   # 1
    assert seen["loads_while_waiting"] == 1                 # 1
    assert stats["max_active"] == 1                         # 2
    assert seen["waiting"]                                  # 5
    # el lote cancelado exportó foto0 y no foto1; el último lote, las tres
    assert sorted(p.name for p in out.iterdir()) == [       # 3, 4
        "foto0-1.png", "foto0.png", "foto1.png", "foto2.png"]
    assert dialogs == ["Imágenes procesadas: 3 de 3\nArchivos exportados: 3"]
