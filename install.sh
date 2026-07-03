#!/usr/bin/env bash
# Instalador de claudes / claude-monitor — https://github.com/rotorrest/claudes
#   curl -fsSL https://raw.githubusercontent.com/rotorrest/claudes/main/install.sh | bash
# Variables opcionales:
#   CLAUDES_INSTALL_DIR  destino (default ~/.local/bin)
#   CLAUDES_VERSION      tag a instalar, ej. v0.1.0 (default: último release)
set -euo pipefail

REPO="rotorrest/claudes"
INSTALL_DIR="${CLAUDES_INSTALL_DIR:-$HOME/.local/bin}"

command -v curl >/dev/null || { echo "error: necesito curl" >&2; exit 1; }
command -v python3 >/dev/null || {
  echo "error: necesito python3 (en macOS: xcode-select --install)" >&2; exit 1;
}

TAG="${CLAUDES_VERSION:-}"
if [ -z "$TAG" ]; then
  TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | grep -m1 '"tag_name"' | cut -d'"' -f4)"
fi
[ -n "$TAG" ] || { echo "error: no pude resolver el último release" >&2; exit 1; }

BASE="https://github.com/$REPO/releases/download/$TAG"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ descargando claudes $TAG…"
for f in claudes claude-notify SHA256SUMS; do
  curl -fsSL -o "$TMP/$f" "$BASE/$f"
done

echo "→ verificando SHA256…"
(cd "$TMP" && grep -E ' \*?(claudes|claude-notify)$' SHA256SUMS | shasum -a 256 -c - >/dev/null)

mkdir -p "$INSTALL_DIR"
install -m 0755 "$TMP/claudes" "$INSTALL_DIR/claudes"
install -m 0755 "$TMP/claude-notify" "$INSTALL_DIR/claude-notify"
ln -sf "$INSTALL_DIR/claudes" "$INSTALL_DIR/claude-monitor"

echo "✓ claudes $TAG instalado en $INSTALL_DIR (claudes, claude-monitor, claude-notify)"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "⚠ $INSTALL_DIR no está en tu PATH; agrega a tu shell:"
     echo "    export PATH=\"$INSTALL_DIR:\$PATH\"" ;;
esac

cat <<'EOF'

Siguiente paso (opcional, para notificaciones nativas con clic-para-saltar):
  brew install terminal-notifier
y registra los hooks Stop/Notification en ~/.claude/settings.json — ver README:
  https://github.com/rotorrest/claudes#notificaciones
EOF
