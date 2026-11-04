"""Motores de matting: RMBG-2.0 (principal), BiRefNet / BiRefNet-HR y BEN2 (A/B).

Todos exponen la misma interfaz: ``cutout(image) -> RGBA`` y
``alpha(image) -> np.ndarray float32 HxW en [0,1]`` a resolución completa.
La inferencia corre a ``process_resolution`` (1024 por defecto, subible para
más detalle de borde) y la máscara se reescala al tamaño original.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image

from ..paths import configure_hf_cache, default_model_file
from .device import DeviceInfo, detect_device

log = logging.getLogger("kamiru.matting")

# Registro de modelos disponibles. Clave = nombre visible en GUI/CLI.
MODEL_REPOS: dict[str, str] = {
    "rmbg-2.0": "briaai/RMBG-2.0",
    "birefnet": "ZhengPeng7/BiRefNet",
    "birefnet-hr": "ZhengPeng7/BiRefNet_HR",
    "ben2": "PramaLLC/BEN2",
}
DEFAULT_MODEL = "rmbg-2.0"
# Modelo sin registro que reemplaza a RMBG-2.0 cuando no hay token con acceso
GATED_FALLBACK_MODEL = "birefnet"

GATED_HELP = (
    "RMBG-2.0 es un repositorio 'gated' en HuggingFace. Para usarlo:\n"
    "  1. Crea una cuenta gratis en https://huggingface.co/join\n"
    "  2. Acepta la licencia en https://huggingface.co/briaai/RMBG-2.0\n"
    "  3. Crea un token (Read) en https://huggingface.co/settings/tokens\n"
    "  4. Pégalo en el archivo models/hf_token.txt de la app\n"
    "Mientras tanto la app puede usar el modelo alternativo 'birefnet' "
    "(misma arquitectura, sin registro)."
)


class GatedModelError(RuntimeError):
    """El repositorio pide registro en HuggingFace y no hay token con acceso."""


# Preferencia del modelo de contraste (nivel 2): primero los que no piden
# token de HuggingFace, para que la verificación funcione sin registro.
VERIFIER_PRIORITY = ("birefnet", "ben2", "birefnet-hr", "rmbg-2.0")


def pick_verifier_model(primary: str) -> str:
    """Modelo para contrastar resultados: el primero distinto al principal."""
    for key in VERIFIER_PRIORITY:
        if key != primary:
            return key
    return DEFAULT_MODEL


def resolve_default_model() -> str:
    """Modelo por defecto: el fijado por el setup, o RMBG-2.0."""
    f = default_model_file()
    if f.exists():
        key = f.read_text(encoding="utf-8").strip()
        if key in MODEL_REPOS:
            return key
    return DEFAULT_MODEL

_IMAGENET_MEAN = [0.485, 0.456, 0.406]
_IMAGENET_STD = [0.229, 0.224, 0.225]


def compose_rgba(rgb: Image.Image, alpha01: np.ndarray) -> Image.Image:
    """Aplica un alfa float [0,1] (HxW, tamaño de la imagen) al RGB original."""
    if alpha01.shape[:2] != (rgb.height, rgb.width):
        raise ValueError(
            f"alfa {alpha01.shape[:2]} no coincide con imagen {(rgb.height, rgb.width)}"
        )
    a8 = np.clip(np.rint(alpha01 * 255.0), 0, 255).astype(np.uint8)
    rgba = rgb.convert("RGBA")
    rgba.putalpha(Image.fromarray(a8, mode="L"))
    return rgba


@dataclass
class MatteResult:
    alpha: np.ndarray        # float32 HxW [0,1], tamaño original
    rgba: Image.Image        # RGBA a resolución completa


# Aviso para el resumen cuando un motor cayó de la GPU a la CPU.
CPU_FALLBACK_NOTE = "la GPU falló y el resto del lote siguió en la CPU (más lento)."


class BaseMatter:
    """Interfaz común de los motores de recorte."""

    name = "base"
    # True cuando una inferencia falló en la GPU y el motor pasó a la CPU
    fell_back_to_cpu = False

    def alpha(self, rgb: Image.Image) -> np.ndarray:
        raise NotImplementedError

    def cutout(self, rgb: Image.Image) -> MatteResult:
        a = self.alpha(rgb)
        return MatteResult(alpha=a, rgba=compose_rgba(rgb, a))


class NeuralMatter(BaseMatter):
    """Matting con RMBG-2.0 / BiRefNet / BEN2 vía transformers."""

    def __init__(
        self,
        model_key: str = DEFAULT_MODEL,
        process_resolution: int = 1024,
        device: DeviceInfo | None = None,
        progress=None,
    ) -> None:
        if model_key not in MODEL_REPOS:
            raise ValueError(f"Modelo desconocido: {model_key!r}. Opciones: {list(MODEL_REPOS)}")
        configure_hf_cache()

        import torch
        from transformers import AutoModelForImageSegmentation

        self.name = model_key
        self.repo = MODEL_REPOS[model_key]
        self.resolution = int(process_resolution)
        self.device_info = device or detect_device()
        self._torch = torch

        import time

        def say(msg: str) -> None:
            log.info(msg)
            if progress:
                progress(msg)

        log.info("Cargando modelo %s (%s) en %s...", model_key, self.repo, self.device_info.kind)
        try:
            # 1) resolver/descargar a cache local (offline si ya está completo)
            from .model_fetch import ensure_model

            local_path = ensure_model(self.repo, progress)

            # 2) leer pesos DESDE LA RUTA LOCAL: cero red en este paso
            t0 = time.perf_counter()
            say(f"Leyendo pesos de {model_key} desde el disco...")
            self.model = AutoModelForImageSegmentation.from_pretrained(
                local_path, trust_remote_code=True
            )
            say(f"Pesos leídos ({time.perf_counter() - t0:.0f} s).")
        except Exception as exc:
            msg = str(exc).lower()
            if "gated" in msg or "401" in msg:
                raise GatedModelError(
                    f"No hay acceso al modelo {self.repo} (repo gated).\n\n{GATED_HELP}"
                ) from exc
            raise
        self.model.eval()
        torch.set_float32_matmul_precision("high")

        self._device = torch.device(self.device_info.kind)
        self._half = self.device_info.kind == "cuda"
        if self._half:
            self.model.half()
        else:
            # algunos checkpoints (BiRefNet) vienen en fp16; CPU/MPS van en fp32
            self.model.float()
        if self.device_info.kind != "cpu":
            say(f"Moviendo el modelo a la GPU ({self.device_info.kind})...")
        t0 = time.perf_counter()
        self.model.to(self._device)

        # 3) warmup solo en GPU: la primera inferencia inicializa/compila
        # kernels. Hacerla acá, con aviso, para que no parezca un cuelgue
        # en la primera foto.
        if self.device_info.kind != "cpu":
            say("Probando la GPU (primera inferencia; puede tardar un rato "
                "la primera vez)...")
            try:
                dummy = torch.zeros(
                    1, 3, 256, 256, device=self._device,
                    dtype=torch.float16 if self._half else torch.float32)
                with torch.no_grad():
                    self._forward(dummy)
            except Exception:
                log.warning("Warmup falló (se seguirá igual)", exc_info=True)
        say(f"Modelo {model_key} listo en {self.device_info.kind} "
            f"({time.perf_counter() - t0:.0f} s).")

    def _preprocess(self, rgb: Image.Image):
        import torchvision.transforms.functional as TF

        size = self.resolution
        im = rgb.resize((size, size), Image.BILINEAR)
        t = TF.to_tensor(im)
        t = TF.normalize(t, _IMAGENET_MEAN, _IMAGENET_STD)
        t = t.unsqueeze(0).to(self._device)
        if self._half:
            t = t.half()
        return t

    def alpha(self, rgb: Image.Image) -> np.ndarray:
        torch = self._torch
        tensor = self._preprocess(rgb)
        with torch.no_grad():
            try:
                preds = self._forward(tensor)
            except RuntimeError:
                if self._device.type != "cpu":
                    # Fallback: cualquier RuntimeError en GPU (op no soportada
                    # en MPS, memoria agotada en CUDA...) repite la foto en
                    # CPU. El motor queda en CPU; fell_back_to_cpu lo avisa.
                    log.warning("Fallo en %s; reintentando en CPU", self._device.type,
                                exc_info=True)
                    self.model.float().to("cpu")
                    self._device = torch.device("cpu")
                    self._half = False
                    self.fell_back_to_cpu = True
                    preds = self._forward(self._preprocess(rgb))
                else:
                    raise
        mask = preds.squeeze().float().cpu().numpy().astype(np.float32)
        mask = np.clip(mask, 0.0, 1.0)
        # Reescalar la máscara al tamaño original con interpolación suave
        mask_im = Image.fromarray((mask * 65535.0 + 0.5).astype(np.uint16), mode="I;16")
        mask_im = mask_im.resize((rgb.width, rgb.height), Image.BILINEAR)
        return (np.asarray(mask_im, dtype=np.float32) / 65535.0).astype(np.float32)

    def _forward(self, tensor):
        out = self.model(tensor)
        # RMBG-2.0 y BiRefNet devuelven una lista de mapas; el último es el final.
        # BEN2 u otros pueden devolver el tensor directo o un dict.
        if isinstance(out, (list, tuple)):
            out = out[-1]
        if hasattr(out, "logits"):
            out = out.logits
        if isinstance(out, (list, tuple)):
            out = out[-1]
        return out.sigmoid()


def load_matter(
    model_key: str = DEFAULT_MODEL,
    process_resolution: int = 1024,
    device: DeviceInfo | None = None,
    progress=None,
) -> NeuralMatter:
    return NeuralMatter(model_key, process_resolution, device, progress=progress)
