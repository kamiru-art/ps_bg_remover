import pytest

from kamiru.core.device import arch_status


class TestArchStatus:
    def test_native_exact(self):
        # 5070 Ti (sm_120) con wheels cu128 que traen sm_120
        assert arch_status((12, 0), ["sm_80", "sm_90", "sm_100", "sm_120"]) == "native"

    def test_native_same_major_lower_minor(self):
        # GPU 8.9 (Ada) corre SASS sm_86? no — pero sm_89 sí; misma familia
        assert arch_status((8, 9), ["sm_80", "sm_89"]) == "native"

    def test_jit_only_ptx(self):
        # wheels cu126 sin sm_120: solo PTX viejo → JIT eterno (el "cuelgue")
        assert arch_status((12, 0), ["sm_80", "sm_90", "compute_90"]) == "jit"

    def test_unsupported(self):
        # ni SASS ni PTX compatibles
        assert arch_status((12, 0), []) == "unsupported"

    def test_old_gpu_native(self):
        assert arch_status((8, 6), ["sm_80", "sm_86", "compute_90"]) == "native"

    def test_malformed_entries_ignored(self):
        assert arch_status((12, 0), ["garbage", "sm_120"]) == "native"


class TestEnsureModelOffline:
    def test_offline_cache_hit_no_network(self, monkeypatch, tmp_path):
        """Con cache completo no debe haber NINGUNA llamada de red."""
        import huggingface_hub

        from kamiru.core import model_fetch

        monkeypatch.setenv("KAMIRU_HOME", str(tmp_path))
        calls = {"local_only": 0}

        def fake_snapshot(repo, local_files_only=False, **kw):
            assert local_files_only, "el primer intento debe ser offline"
            calls["local_only"] += 1
            return "/cache/snapshot"

        def boom(*a, **kw):
            raise AssertionError("no debe tocarse la red con cache completo")

        monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
        monkeypatch.setattr(huggingface_hub, "HfApi", boom)
        msgs = []
        path = model_fetch.ensure_model("fake/repo", progress=msgs.append)
        assert path == "/cache/snapshot"
        assert calls["local_only"] == 1
        assert any("sin descarga ni red" in m for m in msgs)

    def test_incomplete_cache_falls_to_online(self, monkeypatch, tmp_path):
        """Cache incompleto → camino online (con timeout)."""
        import huggingface_hub

        from kamiru.core import model_fetch

        monkeypatch.setenv("KAMIRU_HOME", str(tmp_path))

        def fake_snapshot(repo, local_files_only=False, **kw):
            raise FileNotFoundError("cache incompleto")

        class FakeSibling:
            def __init__(self, name, size):
                self.rfilename, self.size = name, size

        class FakeInfo:
            sha = "abc123"
            siblings = [FakeSibling("config.json", 100),
                        FakeSibling("model.safetensors", 1000)]

        downloaded = []

        class FakeApi:
            def __init__(self, token=None):
                pass

            def model_info(self, repo, files_metadata=False, timeout=None):
                assert timeout is not None, "la consulta a HF debe llevar timeout"
                return FakeInfo()

        def fake_hub_download(repo, name, revision=None, token=None, etag_timeout=None):
            downloaded.append(name)
            return str(tmp_path / "snap" / name)

        monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
        monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_hub_download)
        monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache",
                            lambda repo, name, revision=None: None)
        msgs = []
        path = model_fetch.ensure_model("fake/repo", progress=msgs.append)
        assert path == str(tmp_path / "snap")
        assert "config.json" in downloaded and "model.safetensors" in downloaded
        assert any("2 archivo(s)" in m for m in msgs)


class TestEnsureModelRealCache:
    """Caché real de HuggingFace en disco; solo la red es falsa.

    Formas de fallar que este escenario detecta:
    1. Tras una descarga completa, la carga siguiente sin red no encuentra el
       modelo: las descargas con ``revision=<hash>`` no escriben
       ``refs/main``, y el camino offline lo necesita.
    2. Con una caché parcial (descarga cortada) y un ``refs/main`` de una
       versión anterior, el camino offline devuelve la carpeta incompleta y
       nunca vuelve a descargar lo que falta.
    3. Al reparar la caché parcial se baja de nuevo lo que ya estaba.
    """

    SHA = "0123456789abcdef0123456789abcdef01234567"
    FILES = {"config.json": b'{"a": 1}', "model.safetensors": b"\1" * 2048,
             "sub/extra.py": b"x = 1\n"}

    def test_download_then_offline_then_repair(self, monkeypatch, tmp_path):
        import huggingface_hub
        from huggingface_hub import constants

        from kamiru.core import model_fetch

        monkeypatch.setenv("KAMIRU_HOME", str(tmp_path))
        hub = tmp_path / "models" / "hub"
        monkeypatch.setattr(constants, "HF_HUB_CACHE", str(hub))
        repo_dir = hub / "models--fake--repo"
        snap = repo_dir / "snapshots" / self.SHA

        class Sibling:
            def __init__(self, name, size):
                self.rfilename, self.size = name, size

        class Info:
            sha = self.SHA
            siblings = [Sibling(n, len(b)) for n, b in self.FILES.items()]

        net = {"calls": 0, "downloaded": []}

        class Api:
            def __init__(self, token=None):
                pass

            def model_info(self, repo, files_metadata=False, timeout=None):
                net["calls"] += 1
                return Info()

        def hub_download(repo, name, revision=None, token=None, etag_timeout=None):
            # como la librería real con revision=<hash>: blob en snapshots/
            # y sin escribir refs/main
            net["calls"] += 1
            assert revision == self.SHA
            target = snap / name
            if not target.exists():
                net["downloaded"].append(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.FILES[name])
            return str(target)

        monkeypatch.setattr(huggingface_hub, "HfApi", Api)
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", hub_download)

        # corrida 1: caché vacía → descarga todo
        first = model_fetch.ensure_model("fake/repo")
        assert first == str(snap)
        assert sorted(net["downloaded"]) == sorted(self.FILES)
        assert not (repo_dir / "refs" / "main").exists()

        # corrida 2: sin red → debe cargar de la caché
        def no_network(*_a, **_k):
            raise AssertionError("la carga con caché completa no debe tocar la red")

        monkeypatch.setattr(huggingface_hub, "HfApi", no_network)
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", no_network)
        msgs = []
        assert model_fetch.ensure_model("fake/repo", progress=msgs.append) == str(snap)
        assert any("sin descarga ni red" in m for m in msgs)

        # corrida 3: descarga cortada (falta un archivo) y un refs/main viejo
        (snap / "model.safetensors").unlink()
        (repo_dir / "refs").mkdir(exist_ok=True)
        (repo_dir / "refs" / "main").write_text(self.SHA)
        monkeypatch.setattr(huggingface_hub, "HfApi", Api)
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", hub_download)
        net["downloaded"].clear()
        assert model_fetch.ensure_model("fake/repo") == str(snap)
        assert net["downloaded"] == ["model.safetensors"]
        assert all((snap / n).is_file() for n in self.FILES)
