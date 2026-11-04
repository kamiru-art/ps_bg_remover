"""Detección de dispositivo: CUDA (Windows/RTX), MPS (macOS) o CPU con aviso.

Incluye la verificación de arquitectura CUDA: si el PyTorch instalado no trae
kernels nativos para la GPU (p.ej. wheels que no son cu128 con una RTX 5070
Ti, sm_120), la primera operación dispararía una compilación JIT que tarda
decenas de minutos y parece un cuelgue. En ese caso se usa CPU y se explica
cómo arreglarlo (KAMIRU_FORCE_CUDA=1 fuerza GPU igual).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class DeviceInfo:
    kind: str          # "cuda" | "mps" | "cpu"
    name: str          # nombre legible para mostrar en la GUI
    warning: str | None = None


def arch_status(capability: tuple[int, int], arch_list: list[str]) -> str:
    """Soporte del PyTorch instalado para una GPU: native | jit | unsupported.

    ``capability`` viene de torch.cuda.get_device_capability() (ej. (12, 0));
    ``arch_list`` de torch.cuda.get_arch_list() (ej. ['sm_80', 'compute_90']).
    - native: hay SASS de la misma generación (mayor igual, menor <= al de la GPU).
    - jit: solo hay PTX de una generación anterior → corre, pero la primera vez
      compila kernels por MUCHO tiempo (parece colgado).
    - unsupported: ni siquiera hay PTX compatible.
    """
    num = capability[0] * 10 + capability[1]
    sass, ptx = [], []
    for a in arch_list:
        try:
            kind, v = a.rsplit("_", 1)
            v = int(v)
        except ValueError:
            continue
        (sass if kind == "sm" else ptx).append(v)
    if any(s // 10 == capability[0] and s <= num for s in sass):
        return "native"
    if any(p <= num for p in ptx):
        return "jit"
    return "unsupported"


_CPU_WARN = (
    "No se detectó GPU: el proceso funcionará igual pero será mucho "
    "más lento (decenas de segundos por foto en vez de ~2-3 s)."
)


def detect_device() -> DeviceInfo:
    import torch

    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        status = arch_status(cap, torch.cuda.get_arch_list())
        force = os.environ.get("KAMIRU_FORCE_CUDA", "").strip().lower()
        if status == "native" or force in ("1", "true", "yes", "si", "sí"):
            return DeviceInfo("cuda", f"GPU NVIDIA: {name}")
        return DeviceInfo(
            "cpu",
            f"CPU (PyTorch sin soporte para {name})",
            warning=(
                f"El PyTorch instalado no trae kernels nativos para tu GPU "
                f"{name} (sm_{cap[0]}{cap[1]}; trae {torch.cuda.get_arch_list()}). "
                "Usarla así se colgaría compilando kernels la primera vez, así que "
                "se usará CPU. SOLUCIÓN: reinstalar PyTorch CUDA 12.8 — vuelve a "
                "correr setup_windows.bat (o: uv pip install torch torchvision "
                "--index-url https://download.pytorch.org/whl/cu128). "
                "Para forzar GPU de todas formas: variable KAMIRU_FORCE_CUDA=1."
            ),
        )
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return DeviceInfo("mps", "GPU Apple (Metal/MPS)")
    return DeviceInfo("cpu", "CPU (sin GPU detectada)", warning=_CPU_WARN)
