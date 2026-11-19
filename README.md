# ffny-linux-updater

Actualizador e instalador de **Def Jam: Fight for NY — FFNY Online** para
**Linux + Wine**.

El launcher oficial se actualiza descargando un `DefJamSetup-*.exe` y ejecutándolo,
pero ese instalador **se niega a correr bajo Wine** (*"FFNY Online supports Windows
only for now"*). Este proyecto mantiene la descarga y la verificación de firma del
launcher, pero en lugar de ejecutar el instalador de Windows:

1. verifica de nuevo la firma ECDSA del manifiesto,
2. extrae las fuentes del `.exe` sin ejecutarlo,
3. aplica los parches mínimos de compatibilidad Wine,
4. compila el juego para Windows con **llvm-mingw** en tu Linux,
5. activa el nuevo build de forma atómica (si algo falla, sigues jugando al anterior).

El fork [`N4ML355/ffny-online`](https://github.com/N4ML355/ffny-online) aporta la
otra mitad: su `DefJamSetup` del release **`wine-latest`** arranca en Wine y, en vez
de compilar con MSVC, entrega su payload ya verificado a este adaptador. Así se
puede **instalar desde cero** y también actualizar desde el propio launcher.

No se distribuye ningún archivo del juego: todo se descarga del servidor oficial
(y de tu propio dump) y se compila en tu máquina.

## Requisitos

- Linux x86-64.
- `python3` (+ `python3-venv`), `curl`, `tar`, `cmake` y `ninja`.
- ~3 GB libres para el toolchain y ~15 GB para compilar.
- Un **dump** propio del juego (imagen `.xiso`/`.iso` o carpeta con `default.xbe`)
  solo si instalas desde cero; para actualizar no hace falta.

## Instalación desde cero

1. Prepara las herramientas (elige como carpeta `<padre>/ffny-tools`, al lado de
   donde irá el juego, para que el launcher las encuentre solo):

   ```bash
   git clone https://github.com/N4ML355/ffny-linux-updater.git
   cd ffny-linux-updater
   ./setup.sh --tools-dir "$HOME/Games/FFNY Online/ffny-tools"
   ```

   `setup.sh` crea el venv, descarga llvm-mingw (~200 MB, una sola vez) y compila
   `wine-update-bridge.exe`; en este modo solo apunta el toolchain en `config.json`
   (la instalación se añade al terminar, con `--install-dir`).

2. Descarga el último `DefJamSetup-*.exe` del release **wine-latest** de
   <https://github.com/N4ML355/ffny-online/releases/tag/wine-latest> y cópialo
   dentro de esa carpeta (el setup busca el puente ahí).

3. Instala con Wine (los paths son **Windows**, dentro del prefix):

   ```bash
   export TOOLS="$HOME/Games/FFNY Online/ffny-tools"
   export WINEPREFIX="$HOME/Games/prefixes/defjam"   # el que uses
   wine "$TOOLS/DefJamSetup-<versión>-windows-x64.exe" --silent \
        --dump "/ruta/a/tu/dump.xiso" \
        --install-dir 'C:\Games\FFNY Online\app' \
        --data-dir 'C:\Games\FFNY Online\data'
   ```

   El puente se localiza, en este orden: `FFNY_BRIDGE`, junto al setup, en
   `<dir del setup>\ffny-tools`, en el directorio actual, en
   `.\ffny-tools` y en `<install-dir>\..\..\ffny-tools`. Si el setup está en otra
   carpeta, exporta `FFNY_BRIDGE='Z:\ruta\al\ffny-tools\wine-update-bridge.exe'`.
   Con doble clic también funciona el asistente gráfico; el progreso del wizard
   refleja la compilación en Linux.

4. Configura el actualizador para esa instalación:

   ```bash
   ./setup.sh --install-dir "$WINEPREFIX/drive_c/Games/FFNY Online/app"
   ```

5. En Lutris, apunta el juego a `.../FFNY Online/app/DefJamLauncher.exe`.

## Actualizar una instalación existente

Si ya tienes el juego instalado y `ffny-tools`:

```bash
./setup.sh --install-dir "/ruta/a/FFNY Online/app" --prefix "/ruta/al/prefix"
```

## Uso

```bash
/path/a/ffny-tools/ffny-update           # actualizar al último build
/path/a/ffny-tools/ffny-update check     # ¿hay una versión nueva?
/path/a/ffny-tools/ffny-update status    # build instalado vs. oficial
/path/a/ffny-tools/ffny-update log       # log en vivo
```

La actualización corre en segundo plano (20-60 min). Al terminar, abre el juego
desde Lutris. Si tu build no coincide con el del servidor, el online no conectará.

**También funciona el botón "Actualizar" del propio launcher**: `setup.sh` deja el
puente exactamente donde el launcher lo busca
(`<carpeta padre>/ffny-tools/wine-update-bridge.exe`), así que el launcher muestra
su progreso normal mientras Linux compila en segundo plano.

## Cómo funciona

```
Launcher / setup del fork (Wine) ──verifica──> payload (manifest.json + sha256)
        │
        └─ wine-update-bridge.exe --payload <carpeta> --install-dir ...
             (o --setup <DefJamSetup.exe> desde el launcher oficial)
                 │
                 ├─ verifica el payload / la firma del manifiesto (ECDSA P-256)
                 ├─ extrae tu dump (o reutiliza <data>/extracted)
                 ├─ pipeline.py analyze/recomp  (traduce el juego)
                 ├─ cmake + ninja + llvm-mingw  (compila .exe para Windows)
                 └─ activa el build (installed.json/ini) solo si todo salió bien
```

- `wine-update-bridge.c`: puente Win32 mínimo que mantiene el contrato de progreso
  y código de salida del launcher; escribe un archivo `.done` por invocación para
  no dar por buena una compilación aún en curso. Usa `WINEPREFIX` en tiempo de
  ejecución (compilado con la ruta de `setup.sh` como reserva).
- `wine-update.py`: el adaptador. Dos entradas: `--setup` (actualización firmada
  del launcher) y `--payload` (payload del setup del fork bajo Wine). Bloquea
  ejecuciones simultáneas (`flock`), reutiliza candidates ya compilados
  (`--resume-build`) y permite `--check`, `--prepare-only` y `--build-only`.
- `ffny-update`: interfaz de un comando (update/check/status/log).
- `ffny-toolchain.cmake`: toolchain file que usa el llvm-mingw incluido.

## Problemas comunes

- **"Another update is already running"**: hay otra actualización en curso;
  espera o revisa `ffny-update status`.
- **"FFNY Online on Linux/Wine builds with ffny-linux-updater"** al ejecutar el
  setup del fork: falta el puente; ejecuta `./setup.sh --tools-dir ...` y coloca
  el setup dentro de esa carpeta (o define `FFNY_BRIDGE`).
- **Falla al compilar por SDL3/ImGui**: el adaptador lee `dependencies.json` del
  propio payload; si FFNY cambia el layout, el setup oficial también cambia.
- **El launcher sigue intentando usar PowerShell**: revisa que exista
  `<padre>/ffny-tools/wine-update-bridge.exe` (vuelve a ejecutar `setup.sh`).
- **El juego no arranca tras actualizar**: el build anterior sigue en
  `app/versions/`; copia `previous-install/installed.json|ini` de vuelta a `app/`.
- **Texturas HD**: el adaptador todavía no las prepara; instala sin esa opción.

## Aviso

Proyecto no oficial, sin relación con los autores de FFNY Online. No contiene ni
distribuye código ni datos del juego. Úsalo solo con una copia legítima.
