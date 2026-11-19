# ffny-linux-updater

Actualizador de **Def Jam: Fight for NY — FFNY Online** para **Linux + Wine**.

El launcher oficial se actualiza descargando un `DefJamSetup-*.exe` y ejecutándolo,
pero ese instalador **se niega a correr bajo Wine** (*"FFNY Online supports Windows
only for now"*). Este proyecto mantiene la descarga y la verificación de firma del
launcher, pero en lugar de ejecutar el instalador de Windows:

1. verifica de nuevo la firma ECDSA del manifiesto,
2. extrae las fuentes del `.exe` sin ejecutarlo,
3. aplica los parches mínimos de compatibilidad Wine,
4. compila el juego para Windows con **llvm-mingw** en tu Linux,
5. activa el nuevo build de forma atómica (si algo falla, sigues jugando al anterior).

No se distribuye ningún archivo del juego: todo se descarga del servidor oficial
y se compila en tu máquina.

## Requisitos

- Linux x86-64.
- Una instalación de FFNY Online **ya existente** bajo Wine/Lutris
  (hecha con el setup `wine-latest` del proyecto, que sí arranca en Wine).
- `python3` (+ `python3-venv`), `curl`, `tar`, `git`, `cmake` y `ninja`.
- ~3 GB libres para el toolchain y ~15 GB para compilar.

## Instalación (una sola vez)

```bash
git clone https://github.com/N4ML355/ffny-linux-updater.git
cd ffny-linux-updater
./setup.sh                      # detecta el juego desde Lutris
# o, si no usas Lutris:
./setup.sh --install-dir "$HOME/Games/FFNY Online/app" \
           --prefix "$HOME/.wine"
```

`setup.sh`:

- localiza la instalación (carpeta `app` con `installed.json`) y su wineprefix,
- crea `<carpeta padre>/ffny-tools/` con el Python, el toolchain y el puente,
- descarga llvm-mingw (~200 MB, solo la primera vez),
- compila `wine-update-bridge.exe` con las rutas de **tu** equipo grabadas.

> El puente se compila localmente a propósito: así funciona con cualquier ruta de
> instalación, sin depender de APIs internas de Wine.

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
Launcher (Wine) ──descarga y verifica──> DefJamSetup-89.exe
        │
        └─ src/hooks/updater.c detecta Wine y lanza
           <padre>/ffny-tools/wine-update-bridge.exe --setup ... --install-dir ...
                │
                ├─ verifica la firma del manifiesto (ECDSA P-256)
                ├─ extrae el ZIP del recurso RCDATA 101
                ├─ pipeline.py analyze/recomp  (traduce el juego)
                ├─ cmake + ninja + llvm-mingw  (compila .exe para Windows)
                └─ activa el build (installed.json/ini) solo si todo salió bien
```

- `wine-update-bridge.c`: puente Win32 mínimo que mantiene el contrato de progreso
  y código de salida del launcher; escribe un archivo `.done` por invocación para
  no dar por buena una compilación aún en curso.
- `wine-update.py`: el adaptador. Bloquea ejecuciones simultáneas (`flock`),
  reutiliza candidates ya compilados (`--resume-build`) y permite `--check`,
  `--prepare-only` y `--build-only`.
- `ffny-update`: interfaz de un comando (update/check/status/log).
- `ffny-toolchain.cmake`: toolchain file que usa el llvm-mingw incluido.

## Problemas comunes

- **"This adapter is configured for the existing FFNY installation only"**: ya no
  existe; vuelve a ejecutar `setup.sh` para regenerar `config.json`.
- **`Resource temporarily unavailable` / "Another update is already running"**:
  hay otra actualización en curso; espera o revisa `ffny-update status`.
- **Falla al compilar por SDL3/ImGui**: el adaptador lee `dependencies.json` del
  propio payload; si FFNY cambia el layout, el setup oficial también cambia.
- **El launcher sigue intentando usar PowerShell**: revisa que exista
  `<padre>/ffny-tools/wine-update-bridge.exe` (vuelve a ejecutar `setup.sh`).
- **El juego no arranca tras actualizar**: el build anterior sigue en
  `app/versions/`; copia `previous-install/installed.json|ini` de vuelta a `app/`.

## Aviso

Proyecto no oficial, sin relación con los autores de FFNY Online. No contiene ni
distribuye código ni datos del juego. Úsalo solo con una copia legítima.
