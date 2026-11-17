#!/bin/sh
# Optional: only needed for the Python fallback build (native/build_windows.py).
# The Go build (native/build_go.py) needs no Wine at all.
set -e
PREFIX="${WINEPREFIX:-$HOME/.wine-py}"
PYPKG="${PYVER:-3.12.8}"

echo "[+] wine prefix: $PREFIX"
export WINEPREFIX="$PREFIX" WINEDEBUG=-all
wineboot --init >/dev/null 2>&1 || true

# python.org's installer needs wine32, which is fragile; the embeddable zip
# plus get-pip is what actually works headless.
DEST="$PREFIX/drive_c/Py312"
if [ ! -x "$DEST/python.exe" ]; then
    mkdir -p "$DEST"
    tmp=$(mktemp -d)
    curl -sSL -o "$tmp/py.zip" "https://www.python.org/ftp/python/$PYPKG/python-$PYPKG-embed-amd64.zip"
    (cd "$DEST" && unzip -oq "$tmp/py.zip")
    sed -i 's/^#import site/import site/' "$DEST/python312._pth"
    printf 'Lib\\site-packages\n' >> "$DEST/python312._pth"
    curl -sSL -o "$tmp/get-pip.py" https://bootstrap.pypa.io/get-pip.py
    wine 'C:\Py312\python.exe' "Z:$(echo "$tmp" | tr '/' '\\')\\get-pip.py" --no-warn-script-location
    wine 'C:\Py312\python.exe' -m pip install --no-warn-script-location pyinstaller
fi
"$DEST/python.exe" --version 2>/dev/null || wine 'C:\Py312\python.exe' -V
echo "[+] ready: python3 native/build_windows.py"
