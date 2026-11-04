"""Descarga de modelos con progreso detallado (para GUI y CLI).

Primero se intenta resolver el modelo 100 % offline desde el cache local:
si está completo NO se toca la red (la app funciona sin internet y no puede
quedarse colgada esperando a HuggingFace). Solo si falta algo se listan los
archivos del repo con sus tamaños y se bajan uno por uno, reportando nombre,
tamaño, avance del total y qué ya estaba en cache.

Tras cada descarga completa la app guarda su propio registro del snapshot
(hash del commit y lista de archivos) en ``models/``. Las descargas van con
``revision=<hash>``, y en ese caso huggingface_hub no escribe ``refs/main``:
sin el registro, el camino offline no encontraría el modelo. El registro
también permite comprobar que no falte ningún archivo antes de usarlo.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from ..paths import configure_hf_cache, hf_token, models_dir

log = logging.getLogger("kamiru.model_fetch")

ProgressCb = Callable[[str], None]

NETWORK_TIMEOUT = 30  # s: si HF no responde, error claro en vez de cuelgue


def _mb(n: int | None) -> str:
    if not n:
        return "?"
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    return f"{n / (1 << 20):.0f} MB"


def _record_path(repo: str) -> Path:
    """Registro de la app con el último snapshot completo de ``repo``."""
    return models_dir() / f"kamiru-snapshot-{repo.replace('/', '--')}.json"


def _snapshot_root(file_path: str, rfilename: str) -> str:
    """Carpeta del snapshot a partir de la ruta local de uno de sus archivos."""
    root = Path(file_path)
    for _ in Path(rfilename).parts:
        root = root.parent
    return str(root)


def _recorded_snapshot(repo: str) -> str | None:
    """Ruta del snapshot registrado, solo si todos sus archivos están en disco."""
    from huggingface_hub import try_to_load_from_cache

    try:
        record = json.loads(_record_path(repo).read_text(encoding="utf-8"))
        sha, names = record["sha"], list(record["files"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not names:
        return None
    hits = [try_to_load_from_cache(repo, n, revision=sha) for n in names]
    if not all(isinstance(h, str) for h in hits):
        return None  # descarga cortada: falta al menos un archivo
    return _snapshot_root(hits[0], names[0])


def _save_record(repo: str, sha: str, names: list[str]) -> None:
    path = _record_path(repo)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"repo": repo, "sha": sha, "files": names}, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def ensure_model(repo: str, progress: ProgressCb | None = None) -> str:
    """Devuelve la ruta local del snapshot del modelo, descargando si falta.

    Offline-primero: con el cache completo no hay ninguna llamada de red.
    """
    configure_hf_cache()

    from huggingface_hub import HfApi, hf_hub_download, snapshot_download

    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    # 1) Camino offline: cache completo → ruta local directa, sin red.
    if _record_path(repo).exists():
        path = _recorded_snapshot(repo)
        if path:
            say(f"Modelo {repo} en cache local, sin descarga ni red.")
            return path
        # registro presente pero faltan archivos → camino online
    else:
        # cache bajado por una versión anterior de la app, sin registro
        try:
            path = snapshot_download(repo, local_files_only=True)
            say(f"Modelo {repo} en cache local, sin descarga ni red.")
            return path
        except Exception:
            pass  # cache incompleto o vacío → camino online

    # 2) Camino online con timeout y progreso archivo por archivo.
    token = hf_token()
    say(f"Consultando archivos de {repo} en HuggingFace...")
    api = HfApi(token=token)
    info = api.model_info(repo, files_metadata=True, timeout=NETWORK_TIMEOUT)
    files = [(s.rfilename, s.size or 0) for s in info.siblings]

    from huggingface_hub import try_to_load_from_cache

    pending = []
    cached_bytes = 0
    for name, size in files:
        hit = try_to_load_from_cache(repo, name, revision=info.sha)
        if isinstance(hit, str):
            cached_bytes += size
        else:
            pending.append((name, size))

    total = sum(s for _, s in files)
    if pending:
        todo = sum(s for _, s in pending)
        say(f"Descargando {repo}: {len(pending)} archivo(s), {_mb(todo)} "
            f"({_mb(cached_bytes)} ya en cache)...")
        done = 0
        for i, (name, size) in enumerate(pending, start=1):
            say(f"[{i}/{len(pending)}] {name} ({_mb(size)}) — "
                f"{done * 100 // max(todo, 1)}% del total")
            hf_hub_download(repo, name, revision=info.sha, token=token,
                            etag_timeout=NETWORK_TIMEOUT)
            done += size
        say(f"Modelo {repo} descargado y cacheado ({_mb(total)}).")

    path = hf_hub_download(repo, files[0][0], revision=info.sha, token=token,
                           etag_timeout=NETWORK_TIMEOUT)
    _save_record(repo, info.sha, [name for name, _ in files])
    return _snapshot_root(path, files[0][0])
