# Kamiru — recorte de fondos por lotes con IA

App local (Windows y macOS) que toma una carpeta de fotos y extrae los
objetos de su fondo con calidad de borde alta, usando **RMBG-2.0** (BRIA,
arquitectura BiRefNet). Pensada para piezas físicas con textura (papel
arrugado, obra dimensional) sobre fondos que cambian entre tandas (madera,
green screen, cartulina). Todo corre en la máquina: **sin nube, sin créditos,
sin terminal** para el uso diario.

> Uso **no comercial** (licencia de RMBG-2.0).

---

## Dos formas de instalar

### A. Ejecutable autocontenido (release)

La carpeta `dist/Kamiru` que produce el build contiene **`Kamiru.exe`**
(Windows) o **`Kamiru`** (macOS) con Python, PyTorch y todas las
dependencias adentro: se copia a cualquier máquina del mismo sistema
operativo y se abre con doble clic, sin instalar nada. Al primer uso la app
descarga el modelo a `models/` junto al ejecutable (con progreso detallado
en la barra de estado) y no vuelve a bajarlo. Sin token de HuggingFace
(ver abajo) la app usa BiRefNet y lo deja como modelo por defecto.

- **Para generarlo:** `packaging/build_release.bat` (Windows — detecta la
  GPU y empaqueta PyTorch CUDA 12.8) o `packaging/build_release.command`
  (macOS — PyTorch con MPS). Produce `dist/Kamiru` y el zip
  `Kamiru-win64.zip` / `Kamiru-macos.zip`. Usa **uv**, así que el build
  tarda pocos minutos.
- **Releases automáticos:** al pushear un tag `v*`, GitHub Actions
  (`.github/workflows/release.yml`) construye y publica
  `Kamiru-win64-cpu.zip` y `Kamiru-macos.zip` en la página de Releases.
  *Nota honesta:* esos assets usan PyTorch CPU/MPS porque el build con CUDA
  pesa varios GB y excede el límite de 2 GB por archivo de GitHub Releases —
  el build GPU para la 5070 Ti se genera local con `build_release.bat` y se
  pasa por USB o red local.

### B. Instalación con scripts (desarrollo o PC propia)

Ambos scripts usan **uv** (se auto-instala si falta), así que la instalación
completa tarda una fracción de lo que tardaría pip.

**Windows (PC con RTX 5070 Ti):**

1. Instala **Python 3.12** desde <https://www.python.org/downloads/>
   marcando **"Add Python to PATH"**. (RMBG-2.0 no soporta 3.13; sirve
   3.10–3.12.)
2. Descarga o clona este repositorio donde quieras que viva la app.
3. Doble clic en **`setup_windows.bat`**. El script:
   - crea el entorno virtual (`venv/`) con uv,
   - instala PyTorch con **CUDA 12.8** (la 5070 Ti es Blackwell; wheels
     `cu128`) o CPU si no hay GPU,
   - instala la app y sus dependencias,
   - verifica `torch.cuda.is_available()`,
   - pide el **token de HuggingFace** (ver abajo) y descarga el modelo
     (~1 GB) a `models/` — **una sola vez**.
4. Crea un acceso directo de **`Kamiru.bat`** en el escritorio de Kamila.

**macOS:**

1. Ten Python 3.10–3.12 (`brew install python@3.12` o python.org).
2. Doble clic en **`setup_macos.command`** (si Gatekeeper se queja:
   clic derecho → Abrir; o `xattr -d com.apple.quarantine *.command`).
   Instala PyTorch con soporte **MPS** (GPU Apple) y cachea el modelo.
3. Para el día a día: doble clic en **`Kamiru.command`**.

### Token de HuggingFace (para RMBG-2.0)

RMBG-2.0 es un repositorio *gated*: gratis, pero con registro.

1. Crear cuenta: <https://huggingface.co/join>
2. Aceptar la licencia: <https://huggingface.co/briaai/RMBG-2.0>
3. Crear un token (Read): <https://huggingface.co/settings/tokens>
4. Pegarlo cuando el setup lo pida (queda en `models/hf_token.txt`).

Si se salta este paso, el setup descarga automáticamente **BiRefNet** (misma
arquitectura, sin registro) y lo deja como modelo por defecto; se puede
volver a RMBG-2.0 después pegando el token y re-corriendo
`scripts/download_model.py`. La GUI sigue ese modelo por defecto mientras
no se elija otro a mano en "Avanzado".

### Verificación rápida

```bash
# corrida de ejemplo sobre fotos sintéticas de prueba (genera samples/)
venv/bin/python scripts/sample_run.py        # macOS/Linux
venv\Scripts\python scripts\sample_run.py    # Windows
```

Los tests unitarios (no requieren GPU ni modelo):

```bash
venv/bin/python -m pip install pytest psd-tools
venv/bin/python -m pytest tests/
```

---

## Uso diario (Kamila)

Ver la guía de una página: **[GUIA_KAMILA.md](GUIA_KAMILA.md)**. En corto:
doble clic en Kamiru → arrastrar la carpeta de fotos → elegir dónde guardar →
Procesar.

---

## Qué hace exactamente

Por cada imagen de la carpeta (JPG, PNG, TIFF, HEIC, WebP, BMP):

1. Carga respetando la **orientación EXIF** y el **perfil ICC**.
2. Infiere el alfa con **RMBG-2.0** a la resolución de proceso (1024 por
   defecto; 1536/2048 para más detalle de borde) y lo reescala al tamaño
   original — el RGBA final es a **resolución completa**.
3. Según el modo:
   - **Conjunto:** todos los objetos de la foto en un archivo, recortado al
     contenido.
   - **Individual:** separa objetos con `connectedComponentsWithStats`,
     descarta motas con el **filtro de área mínima**, y exporta cada pieza
     recortada a su bounding box **conservando el alfa suave** (limitado a la
     pieza para no arrastrar bordes de vecinas).
4. Exporta **sin sobrescribir** (`foto.png`, `foto-1.png`, ...):
   - **PNG-24** transparente (por defecto), con ICC embebido.
   - **TIFF con alfa** 8-bit o **16-bit** (deflate, ICC embebido).
   - **PSD**: en modo individual, **un PSD por foto con cada pieza en su
     propia capa** (en su posición original); en conjunto, PSD de una capa.
     Compatible con Photoshop, Affinity, Krita y GIMP.

Nomenclatura: conjunto → `nombre_origen.ext`; individual →
`nombre_origen_01.ext`, `_02`, ... con relleno de ceros. Con **sufijo**
(campo de la GUI o `--sufijo`): `nombre_origen_recorte.ext` /
`nombre_origen_recorte_01.ext`.

La entrada puede ser una **carpeta o fotos sueltas** (arrastradas o elegidas
con el botón *Fotos…*).

Una imagen que falla **se registra y se salta** sin abortar el lote; al final
hay resumen de exportados y errores, y queda log por corrida en `logs/`.

### Control de calidad y carpeta `revisar/`

Para no tener que calar los recortes uno por uno, cada foto pasa por dos
niveles de control:

1. **Señales del propio alfa** (gratis): borde demasiado difuso (matte
   "lechoso"), objeto que toca el borde del encuadre, color del fondo dentro
   del objeto, o recorte que cubre casi toda la foto.
2. **Consenso entre modelos** (opcional, activado por defecto en la GUI):
   la misma foto se recorta con un **segundo modelo** (automático: `birefnet`
   si el principal es RMBG-2.0) y se comparan las máscaras (IoU). Si los dos
   coinciden, las alarmas suaves del nivel 1 se descartan como falsa alarma;
   si divergen, la foto queda marcada aunque el nivel 1 no haya visto nada.
   El contraste es *best-effort*: si el segundo modelo no puede cargar (sin
   red, sin disco), el lote sigue igual con las señales básicas. Con el motor
   **croma** el contraste se omite (croma y un modelo neural difieren por
   diseño y marcarían casi todo).

Los recortes marcados **se exportan igual**, pero a `salida/revisar/`
(opción "Mover recortes dudosos a «revisar»"), y el resumen lista cada foto
con su motivo. Lo que quedó fuera de `revisar/` es confiable: solo hay que
mirar a mano las dudosas. La **Vista previa** también avisa si el recorte de
la primera foto es dudoso y por qué.

## Opciones avanzadas (GUI → "Avanzado", o CLI)

| Opción | Qué hace |
|---|---|
| Motor / modelo | `rmbg-2.0` (default), `birefnet`, `birefnet-hr`, `ben2` para A/B de nitidez de borde, o **croma por color** |
| Resolución de proceso | 1024 / 1536 / 2048 — subir si un borde fino se pierde |
| Área mínima (px²) | filtro de motas y polvo (default 400) |
| Separar piezas que se tocan | watershed sobre la transformada de distancia (experimental) |
| Croma | color de fondo automático (muestreo de esquinas) o elegido a mano — útil como camino alterno en green screen / cartulina |
| Calidad | mover recortes dudosos a `revisar/` y contrastar con un 2º modelo (consenso) |

Además la GUI trae:

- **Vista previa**: recorta solo la primera foto y la muestra sobre tablero
  de ajedrez antes de lanzar el lote completo.
- **Memoria de configuración**: carpetas, modo, formato, sufijo y opciones
  avanzadas se recuerdan entre sesiones (`settings.json`).
- **Abrir salida**: abre la carpeta de resultados en el explorador al
  terminar.
- **Progreso de descarga del modelo** en la barra de estado: archivos,
  tamaños, porcentaje y qué ya estaba en cache.

### CLI (mismo core que la GUI)

```bash
kamiru fotos/ salida/ --modo individual --formato png --area-minima 400
kamiru fotos/ salida/ --modo individual --formato psd          # PSD por capas
kamiru fotos/ salida/ --sufijo recorte                         # foto_recorte.png
kamiru foto.jpg salida/                                        # una foto suelta
kamiru fotos/ salida/ --modelo birefnet-hr --resolucion 2048   # A/B de borde
kamiru fotos/ salida/ --motor croma --chroma-color 00ff00      # green screen
kamiru fotos/ salida/ --separar-tocandose                      # watershed
kamiru fotos/ salida/ --verificar --revisar                    # consenso 2º modelo + revisar/
```

## Estructura

```
src/kamiru/            paquete (core reutilizable + GUI + CLI)
  core/matting.py        motores RMBG-2.0 / BiRefNet / BEN2 (cutout → RGBA)
  core/separation.py     connected components + watershed + filtro de área
  core/chroma.py         croma HSV (auto por esquinas o color manual)
  core/quality.py        señales de calidad del alfa + consenso entre modelos
  core/export.py         PNG / TIFF 8-16 bit / PSD + nomenclatura
  core/psd_writer.py     escritor PSD por capas (RLE), puro numpy
  core/pipeline.py       lote, progreso, errores, resumen, logging
  gui/app.py             ventana de arrastrar y soltar
  cli.py                 línea de comandos
scripts/               download_model.py, make_samples.py, sample_run.py
packaging/             spec de PyInstaller + build scripts del ejecutable
.github/workflows/     release automático (Windows/macOS) al taggear v*
tests/                 tests sin GPU: separación, export, PSD, pipeline
models/                cache local del modelo (no se re-descarga)
logs/                  un log por corrida
```

## Solución de problemas

- **"Se queda cargando el modelo"**: ya no debería pasar — con el modelo
  cacheado la carga es 100 % offline (cero llamadas de red), reporta cada
  fase ("Leyendo pesos…", "Moviendo a la GPU…", "Probando la GPU…") con un
  cronómetro visible, y **Cancelar libera la app al instante** (la carga
  sigue de fondo y se aprovecha en el próximo Procesar).
- **La app dice "PyTorch sin soporte para tu GPU"**: el PyTorch instalado no
  trae kernels para esa GPU (típico: wheels que no son cu128 con una RTX
  50xx). Sin este chequeo, la primera operación CUDA se queda compilando
  kernels por decenas de minutos — parece un cuelgue. Solución: re-correr
  `setup_windows.bat` (reinstala PyTorch CUDA 12.8). Forzar GPU de todas
  formas: variable de entorno `KAMIRU_FORCE_CUDA=1`.
- **Diagnóstico**: cada corrida deja un log en `logs/`; los errores de la
  GUI muestran la ruta exacta del log en el diálogo. Bajo `pythonw`/exe sin
  consola, la salida de las librerías va a `logs/gui_console.log`.

## Notas y decisiones

- **Piezas que se tocan:** por defecto salen como un solo objeto (regla
  práctica: dejar espacio entre piezas al fotografiar). La opción
  "separar piezas que se tocan" usa watershed clásico; funciona bien con
  piezas convexas unidas por cuellos. El spec mencionaba SAM3 para esto:
  no existe como modelo público, por lo que el watershed cubre el caso hoy
  y el motor es intercambiable si aparece algo mejor.
- **Sombras:** RMBG-2.0 las excluye del matte; además el recorte usa el alfa,
  no el color, así que la sombra no viaja al archivo final.
- **GPU:** se detecta CUDA → MPS → CPU al iniciar, con aviso si no hay GPU.
  En la 5070 Ti el objetivo es < 3 s por foto (el modelo corre en fp16).
- **Cache:** `HF_HOME` apunta a `models/` dentro de la app; el modelo se
  descarga en el setup y no vuelve a bajarse.
