#!/usr/bin/env bash
# Instalación única: prepara ffny-tools (Python, toolchain, puente Wine) junto a
# la instalación de FFNY Online y deja listo el actualizador.
set -euo pipefail

REPO=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
INSTALL_DIR="" PREFIX="" TOOLCHAIN="" SKIP_TOOLCHAIN=0 TOOLS_DIR=""
MINGW_URL="https://github.com/mstorsjo/llvm-mingw/releases/download/20261104/llvm-mingw-20261104-ucrt-ubuntu-22.04-x86_64.tar.xz"
MINGW_DIR="llvm-mingw-20261104-ucrt-ubuntu-22.04-x86_64"

usage() {
    cat <<EOF
Uso: $0 [opciones]

  --install-dir DIR   carpeta "app" de FFNY Online (contiene installed.json).
                      Si se omite, se detecta desde Lutris.
  --tools-dir DIR     prepara esa carpeta para una instalación DESDE CERO (sin juego
                      instalado todavía); después ejecuta ahí el DefJamSetup del release
                      wine-latest. No escribe config.json.
  --prefix DIR        wineprefix del juego (por defecto, el de Lutris/WINEPREFIX/~/.wine).
  --toolchain DIR     raíz de llvm-mingw ya descargada (si omites esto, se descarga).
  --skip-toolchain    no descargar llvm-mingw (debe existir en PATH o en --toolchain).
  -h, --help          esta ayuda.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir) INSTALL_DIR=${2:?falta la ruta}; shift 2 ;;
        --tools-dir) TOOLS_DIR=${2:?falta la ruta}; shift 2 ;;
        --prefix) PREFIX=${2:?falta la ruta}; shift 2 ;;
        --toolchain) TOOLCHAIN=${2:?falta la ruta}; shift 2 ;;
        --skip-toolchain) SKIP_TOOLCHAIN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -n "$TOOLS_DIR" ]; then
    TOOLS=$(mkdir -p "$TOOLS_DIR" && cd "$TOOLS_DIR" && pwd)
elif [ -z "$INSTALL_DIR" ]; then
    for yml in "${XDG_DATA_HOME:-$HOME/.local/share}"/lutris/games/*.yml; do
        [ -f "$yml" ] || continue
        grep -q 'DefJamLauncher' "$yml" || continue
        exe=$(sed -n 's/^[[:space:]]*exe:[[:space:]]*//p' "$yml" | head -1)
        [ -n "$exe" ] || continue
        INSTALL_DIR=$(dirname "$exe")
        if [ -z "$PREFIX" ]; then
            PREFIX=$(sed -n 's/^[[:space:]]*prefix:[[:space:]]*//p' "$yml" | head -1)
        fi
        break
    done
fi

if [ -z "$TOOLS_DIR" ]; then
    [ -n "$INSTALL_DIR" ] || {
        echo "No encontré FFNY Online. Indica la carpeta: $0 --install-dir \".../FFNY Online/app\"" >&2
        exit 1
    }
    APP=$(cd "$INSTALL_DIR" && pwd)
    [ -f "$APP/installed.json" ] || { echo "No existe $APP/installed.json (¿instalación incorrecta?)" >&2; exit 1; }
    PREFIX=${PREFIX:-${WINEPREFIX:-$HOME/.wine}}
    PREFIX=$(cd "$PREFIX" && pwd)
    [ -d "$PREFIX/dosdevices" ] || { echo "No parece un wineprefix: $PREFIX (falta dosdevices)" >&2; exit 1; }
fi
PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "Falta python3" >&2; exit 1; }

if [ -z "$TOOLS_DIR" ]; then
    DATA=$("$PYTHON" - "$APP/installed.json" "$PREFIX" "$APP" <<'PYEOF' 2>/dev/null || true
import json, os, sys
from pathlib import Path
receipt = json.load(open(sys.argv[1]))
prefix, app = Path(sys.argv[2]), Path(sys.argv[3])
value = receipt.get('data', '')
if len(value) > 2 and value[1] == ':':
    print(Path(os.path.realpath(prefix / 'dosdevices' / (value[0].lower() + ':'))) / value[3:].replace('\\', '/'))
else:
    print(app.parent / 'data')
PYEOF
)
    [ -n "$DATA" ] || DATA="$(dirname "$APP")/data"
    TOOLS=$(dirname "$(dirname "$APP")")/ffny-tools
else
    APP="" DATA=""
fi
mkdir -p "$TOOLS"
for file in wine-update.py wine-update-bridge.c ffny-toolchain.cmake ffny-update; do
    [ "$REPO/$file" -ef "$TOOLS/$file" ] || cp -f "$REPO/$file" "$TOOLS/$file"
done
chmod +x "$TOOLS/ffny-update"

if [ -n "$TOOLS_DIR" ]; then
    echo "Herramientas:$TOOLS (preparadas para instalar desde cero)"
else
    echo "Instalación: $APP"
    echo "Datos:       $DATA"
    echo "Prefix:      $PREFIX"
    echo "Herramientas:$TOOLS (el juego busca aquí su puente de actualización)"
fi
echo
echo "Preparando Python y dependencias..."
if [ ! -x "$TOOLS/venv/bin/python" ]; then
    "$PYTHON" -m venv "$TOOLS/venv" || {
        echo "No se pudo crear el venv; instala python3-venv con tu gestor de paquetes." >&2
        exit 1
    }
fi
"$TOOLS/venv/bin/python" -m pip install --quiet --upgrade pip
"$TOOLS/venv/bin/python" -m pip install --quiet pefile cryptography capstone pyxbe numpy pillow cffi

if [ -n "$TOOLCHAIN" ]; then
    TOOLCHAIN=$(cd "$TOOLCHAIN" && pwd)
elif ls -d "$TOOLS"/toolchain/llvm-mingw-*/x86_64-w64-mingw32 >/dev/null 2>&1; then
    TOOLCHAIN=$(dirname "$(ls -d "$TOOLS"/toolchain/llvm-mingw-*/x86_64-w64-mingw32 | head -1)")
elif [ "$SKIP_TOOLCHAIN" = 1 ]; then
    TOOLCHAIN=""
else
    echo
    echo "Descargando llvm-mingw (una sola vez, ~200 MB)..."
    mkdir -p "$TOOLS/toolchain"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    curl -fL --retry 3 -o "$tmp/toolchain.tar.xz" "$MINGW_URL"
    tar -xJf "$tmp/toolchain.tar.xz" -C "$TOOLS/toolchain"
    TOOLCHAIN="$TOOLS/toolchain/$MINGW_DIR"
fi

if [ -n "$TOOLCHAIN" ] && [ -x "$TOOLCHAIN/bin/x86_64-w64-mingw32-gcc" ]; then
    GCC="$TOOLCHAIN/bin/x86_64-w64-mingw32-gcc"
elif GCC=$(command -v x86_64-w64-mingw32-gcc); then
    TOOLCHAIN=$(dirname "$(dirname "$GCC")")
else
    echo "No hay compilador mingw; usa --toolchain o permite la descarga." >&2
    exit 1
fi

echo "Compilando el puente de actualización..."
escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
"$GCC" -O2 -municode -static -o "$TOOLS/wine-update-bridge.exe" "$TOOLS/wine-update-bridge.c" \
    -DFFNY_TOOLS_DIR="\"$(escape "$TOOLS")\"" \
    -DFFNY_PREFIX="\"$(escape "$PREFIX")\"" -lshell32

if [ -z "$TOOLS_DIR" ]; then
    "$TOOLS/venv/bin/python" - "$TOOLS/config.json" "$APP" "$DATA" "$PREFIX" "$TOOLCHAIN" <<'PYEOF'
import json, sys
path, app, data, prefix, toolchain = sys.argv[1:]
config = {'install_dir': app, 'data_dir': data, 'prefix': prefix}
if toolchain:
    config['toolchain'] = toolchain
open(path, 'w').write(json.dumps(config, indent=2) + '\n')
PYEOF
elif [ -n "$TOOLCHAIN" ]; then
    # Tools-dir mode has no installation yet, but the adapter needs the toolchain
    # the next setup run will build with (install/data/prefix are added later).
    "$TOOLS/venv/bin/python" - "$TOOLS/config.json" "$TOOLCHAIN" <<'PYEOF'
import json, sys
path, toolchain = sys.argv[1:]
open(path, 'w').write(json.dumps({'toolchain': toolchain}, indent=2) + '\n')
PYEOF
fi

for tool in cmake ninja; do
    command -v "$tool" >/dev/null || {
        echo "Aviso: falta '$tool'. Instálalo con tu gestor de paquetes antes de compilar." >&2
    }
done

echo
if [ -n "$TOOLS_DIR" ]; then
    echo "Listo para instalar desde cero. Descarga el último DefJamSetup del release"
    echo "wine-latest (github.com/N4ML355/ffny-online/releases/tag/wine-latest) y:"
    echo
    echo "    1) copia el .exe dentro de $TOOLS"
    echo "    2) ejecútalo con wine (WINEPREFIX el que quieras usar):"
    echo "       wine \"$TOOLS/DefJamSetup-*.exe\" --silent \\"
    echo "            --dump '/ruta/a/tu/dump' \\"
    echo "            --install-dir 'C:\\Games\\FFNY Online\\app' \\"
    echo "            --data-dir 'C:\\Games\\FFNY Online\\data'"
    echo
    echo "El puente se encuentra por estar el .exe en esta carpeta. Si lo dejas"
    echo "en otro sitio, exporta FFNY_BRIDGE con la ruta Windows del .exe del puente."
    echo "Al terminar, ejecuta ./setup.sh --install-dir ... para configurar ffny-update."
else
    echo "Listo. Ya puedes actualizar el juego con:"
    echo "    $TOOLS/ffny-update"
    echo
    echo "El botón \"Actualizar\" del propio launcher también usará este puente."
    echo "Comandos: ffny-update [update|check|status|log]"
fi
