"""Tests del pipeline con un motor falso (sin GPU ni descarga de modelo)."""

import numpy as np
import pytest
from PIL import Image

from kamiru.core.matting import BaseMatter, MatteResult, compose_rgba
from kamiru.core.pipeline import BatchOptions, run_batch


class FakeMatter(BaseMatter):
    """Alfa = píxeles no-grises (los fondos de prueba son grises)."""

    name = "fake"

    def alpha(self, rgb):
        arr = np.asarray(rgb, dtype=np.float32)
        spread = arr.max(axis=2) - arr.min(axis=2)
        return (spread > 40).astype(np.float32)

    def cutout(self, rgb):
        a = self.alpha(rgb)
        return MatteResult(alpha=a, rgba=compose_rgba(rgb, a))


def scene(pieces, size=(300, 200)):
    """Fondo gris con rectángulos de color. pieces = [(x, y, w, h, color)]"""
    arr = np.full((size[1], size[0], 3), 128, dtype=np.uint8)
    for x, y, w, h, color in pieces:
        arr[y:y + h, x:x + w] = color
    return Image.fromarray(arr)


@pytest.fixture
def folder(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    scene([(40, 40, 60, 50, (200, 30, 40)), (180, 90, 70, 60, (30, 60, 200)),
           (10, 180, 3, 3, (250, 220, 10))]).save(inp / "dos_piezas.png")
    scene([(100, 60, 80, 80, (20, 160, 60))]).save(inp / "una_pieza.jpg")
    (inp / "rota.jpg").write_bytes(b"no soy imagen")
    (inp / "notas.txt").write_text("ignorame")
    return inp


def test_conjunto_mode(folder, tmp_path):
    out = tmp_path / "out"
    s = run_batch(folder, out, FakeMatter(), BatchOptions(mode="conjunto", fmt="png"))
    assert s.total == 3           # txt ignorado
    assert s.succeeded == 2
    assert len(s.failed) == 1 and s.failed[0].source.name == "rota.jpg"
    names = sorted(p.name for p in out.iterdir())
    assert names == ["dos_piezas.png", "una_pieza.png"]


def test_individual_mode_names_and_min_area(folder, tmp_path):
    out = tmp_path / "out"
    s = run_batch(folder, out, FakeMatter(),
                  BatchOptions(mode="individual", fmt="png", min_area=100))
    names = sorted(p.name for p in out.iterdir())
    # la mota de 3x3=9px queda descartada por min_area=100
    assert names == ["dos_piezas_01.png", "dos_piezas_02.png", "una_pieza_01.png"]
    rep = {r.source.name: r for r in s.reports}
    assert rep["dos_piezas.png"].objects_discarded == 1


def test_individual_mode_keeps_speck_when_area_zero(folder, tmp_path):
    out = tmp_path / "out"
    run_batch(folder, out, FakeMatter(),
              BatchOptions(mode="individual", fmt="png", min_area=1))
    assert len(list(out.glob("dos_piezas_*.png"))) == 3


def test_no_overwrite_on_second_run(folder, tmp_path):
    out = tmp_path / "out"
    opts = BatchOptions(mode="conjunto", fmt="png")
    run_batch(folder, out, FakeMatter(), opts)
    run_batch(folder, out, FakeMatter(), opts)
    names = sorted(p.name for p in out.iterdir())
    assert "una_pieza.png" in names and "una_pieza-1.png" in names


def test_summary_text(folder, tmp_path):
    s = run_batch(folder, tmp_path / "out", FakeMatter(), BatchOptions())
    text = s.text()
    assert "Imágenes procesadas: 2 de 3" in text
    assert "rota.jpg" in text


def test_individual_psd_layered(folder, tmp_path):
    from psd_tools import PSDImage

    out = tmp_path / "out"
    run_batch(folder, out, FakeMatter(),
              BatchOptions(mode="individual", fmt="psd", min_area=100))
    psd = PSDImage.open(out / "dos_piezas.psd")
    assert len(psd) == 2
    assert [l.name for l in psd] == ["dos_piezas_01", "dos_piezas_02"]
    assert psd.width == 300 and psd.height == 200
    # las capas quedan en su posición original (± margen)
    assert abs(psd[0].left - 38) <= 4 and abs(psd[0].top - 38) <= 4


def test_conjunto_psd(folder, tmp_path):
    from psd_tools import PSDImage

    out = tmp_path / "out"
    run_batch(folder, out, FakeMatter(), BatchOptions(mode="conjunto", fmt="psd"))
    psd = PSDImage.open(out / "una_pieza.psd")
    assert len(psd) == 1


def test_tiff16_batch(folder, tmp_path):
    import tifffile

    out = tmp_path / "out"
    run_batch(folder, out, FakeMatter(), BatchOptions(mode="conjunto", fmt="tiff16"))
    data = tifffile.imread(out / "una_pieza.tif")
    assert data.dtype == np.uint16


def test_cancel_stops_early(folder, tmp_path):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1  # cancela después de la primera imagen

    s = run_batch(folder, tmp_path / "out", FakeMatter(), BatchOptions(), cancel=cancel)
    assert s.total < 3


def test_suffix_conjunto_and_individual(folder, tmp_path):
    out = tmp_path / "out"
    run_batch(folder, out, FakeMatter(),
              BatchOptions(mode="conjunto", fmt="png", suffix="recorte"))
    assert (out / "una_pieza_recorte.png").exists()
    out2 = tmp_path / "out2"
    run_batch(folder, out2, FakeMatter(),
              BatchOptions(mode="individual", fmt="png", min_area=100, suffix="_rec"))
    names = sorted(p.name for p in out2.iterdir())
    assert names == ["dos_piezas_rec_01.png", "dos_piezas_rec_02.png",
                     "una_pieza_rec_01.png"]


def test_suffix_sanitized():
    opts = BatchOptions(suffix='ma/lo:*?"')
    assert opts.normalized_suffix() == "_malo"
    assert BatchOptions(suffix="  ").normalized_suffix() == ""
    assert BatchOptions(suffix="-v2").normalized_suffix() == "-v2"


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("KAMIRU_HOME", str(tmp_path))
    from kamiru.settings import Settings, load_settings, save_settings

    s = Settings(input_dir="/fotos", mode="individual", suffix="rec",
                 min_area="250", split_touching=True)
    save_settings(s)
    back = load_settings()
    assert back == s
    # archivo corrupto → defaults sin explotar
    (tmp_path / "settings.json").write_text("{rotisimo", encoding="utf-8")
    assert load_settings() == Settings()


def test_exif_orientation(tmp_path):
    """Una imagen con EXIF Orientation=6 debe rotarse antes de procesar."""
    from PIL import Image as PILImage

    inp = tmp_path / "in"
    inp.mkdir()
    im = scene([(10, 10, 40, 30, (200, 30, 40))], size=(100, 60))
    exif = PILImage.Exif()
    exif[274] = 6  # rotar 90° CW
    im.save(inp / "rotada.jpg", exif=exif, quality=95)

    out = tmp_path / "out"
    run_batch(inp, out, FakeMatter(),
              BatchOptions(mode="conjunto", fmt="png", margin=0))
    result = PILImage.open(out / "rotada.png")
    # la pieza era 40x30 apaisada; tras rotar EXIF debe salir ~30x40
    assert result.height > result.width


def test_lote_accidentado(tmp_path, monkeypatch):
    """Un lote que se tuerce de varias formas a la vez.

    Formas de fallar que este escenario detecta:
    1. El disco se llena a mitad de una pieza y queda un archivo truncado con
       un nombre final válido (o un temporal suelto en la carpeta).
    2. Las piezas que sí se guardaron antes del error quedan fuera del
       informe: el resumen no las cuenta ni las nombra.
    3. El 2º modelo falla en una foto y el resumen no dice qué foto quedó
       sin contraste.
    4. Se cancela durante el recorte de una foto y esa foto se exporta igual.
    5. La carpeta de salida se llama «revisar» y el resumen dice que movió
       las dudosas aunque la opción está apagada.
    """
    import errno

    inp = tmp_path / "in"
    inp.mkdir()
    # a: dos piezas, una toca el borde de la foto (dudosa)
    scene([(0, 40, 60, 50, (200, 30, 40)), (180, 90, 70, 60, (30, 60, 200))]
          ).save(inp / "a_dos.png")
    # b: el 2º modelo falla con esta foto (pieza verde)
    scene([(100, 60, 80, 80, (20, 160, 60))]).save(inp / "b_sin_contraste.png")
    # c: tres piezas; el disco se llena al guardar la segunda
    scene([(20, 20, 50, 50, (200, 30, 40)), (120, 20, 50, 50, (30, 60, 200)),
           (220, 20, 50, 50, (200, 200, 20))]).save(inp / "c_tres.png")
    # d: se cancela mientras se recorta
    scene([(100, 60, 80, 80, (160, 40, 160))]).save(inp / "d_ultima.png")

    cancelled = {"flag": False}

    class CancelOnD(FakeMatter):
        def cutout(self, rgb):
            if np.asarray(rgb)[100, 140].tolist() == [160, 40, 160]:
                cancelled["flag"] = True  # la persona aprieta Cancelar ahora
            return super().cutout(rgb)

    class VerifierFailsOnGreen(FakeMatter):
        def alpha(self, rgb):
            if np.asarray(rgb)[100, 140].tolist() == [20, 160, 60]:
                raise RuntimeError("sin memoria en el 2º modelo")
            return super().alpha(rgb)

    real_save = Image.Image.save

    def disk_full_on_c02(self, fp, *args, **kwargs):
        if "c_tres_02" in str(fp):
            with open(fp, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)  # mitad de archivo
            raise OSError(errno.ENOSPC, "No queda espacio en el disco")
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", disk_full_on_c02)

    out = tmp_path / "revisar"  # la carpeta elegida ya se llama así
    s = run_batch(inp, out, CancelOnD(),
                  BatchOptions(mode="individual", fmt="png", min_area=100,
                               move_uncertain=False),
                  cancel=lambda: cancelled["flag"],
                  verifier=VerifierFailsOnGreen())
    monkeypatch.setattr(Image.Image, "save", real_save)

    on_disk = sorted(p.name for p in out.iterdir())
    # 1. sin truncados ni temporales: solo archivos completos
    assert on_disk == ["a_dos_01.png", "a_dos_02.png", "b_sin_contraste_01.png",
                       "c_tres_01.png"]
    for name in on_disk:
        Image.open(out / name).load()  # decodifica entero
    # 4. la foto cancelada no se exportó ni cuenta en el resumen
    assert [r.source.name for r in s.reports] == ["a_dos.png", "b_sin_contraste.png",
                                                  "c_tres.png"]
    # 2. la foto con error conserva lo que alcanzó a guardar
    rep = {r.source.name: r for r in s.reports}
    assert not rep["c_tres.png"].ok
    assert [p.name for p in rep["c_tres.png"].outputs] == ["c_tres_01.png"]
    assert s.files_exported == len(on_disk)
    text = s.text()
    assert "Archivos exportados: 4" in text
    assert "c_tres.png" in text and "1 archivo(s)" in text
    # 3. el resumen nombra la foto que quedó sin contraste
    assert rep["b_sin_contraste.png"].consensus_iou is None
    sin_contraste = [l for l in text.splitlines() if "contraste" in l]
    assert sin_contraste and "b_sin_contraste.png" in sin_contraste[0]
    assert "a_dos.png" not in sin_contraste[0]
    # 5. dudosa reportada, pero sin decir que se movió
    assert "Recortes dudosos: 1" in text
    assert "«revisar/»" not in text
