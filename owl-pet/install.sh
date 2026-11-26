#!/usr/bin/env bash
# Installs Hoot the Owl for the current user (no root needed unless
# GTK bindings are missing).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/owl-pet"
APPS="$HOME/.local/share/applications"

# 1. dependencies (usually preinstalled, but check anyway)
if ! python3 -c 'import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk; import cairo; gi.require_foreign("cairo")' 2>/dev/null; then
  echo "Installing GTK bindings for Python (needs your password)..."
  if command -v pacman >/dev/null; then        # Arch, CachyOS, Manjaro
    sudo pacman -S --needed --noconfirm python-gobject python-cairo gtk3
  elif command -v apt-get >/dev/null; then     # Mint, Ubuntu, Debian
    sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0
  elif command -v dnf >/dev/null; then         # Fedora
    sudo dnf install -y python3-gobject python3-cairo gtk3
  elif command -v zypper >/dev/null; then      # openSUSE
    sudo zypper install -y python3-gobject python3-gobject-cairo python3-cairo typelib-1_0-Gtk-3_0
  else
    echo "Please install PyGObject, pycairo and GTK 3 with your package manager." >&2
    exit 1
  fi
fi

# 1b. something that can play sound (PipeWire/PulseAudio client tools)
if ! command -v paplay >/dev/null && ! command -v pw-play >/dev/null; then
  echo "Installing a sound player for the hoot..."
  if command -v pacman >/dev/null; then sudo pacman -S --needed --noconfirm libpulse || true
  elif command -v apt-get >/dev/null; then sudo apt-get install -y pulseaudio-utils || true
  elif command -v dnf >/dev/null; then sudo dnf install -y pulseaudio-utils || true
  fi
fi

# 2. stop any owl that is already running (an old copy would keep the old code)
pkill -f "^[^ ]*python[0-9.]* [^ ]*owl_pet\.py" 2>/dev/null && sleep 0.5 || true

# 3. copy the app and make an icon from the owl itself
mkdir -p "$DEST" "$APPS"
install -m 755 "$SRC_DIR/owl_pet.py" "$DEST/owl_pet.py"
python3 "$DEST/owl_pet.py" --export-icon "$DEST/owl.png" 256

# 4. menu entry
cat > "$APPS/owl-pet.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Hoot the Owl
Comment=A desktop pet owl that watches your mouse pointer
Exec=python3 "$DEST/owl_pet.py"
Icon=$DEST/owl.png
Categories=Amusement;Game;
Terminal=false
StartupNotify=false
EOF
update-desktop-database "$APPS" >/dev/null 2>&1 || true

# 5. start the new owl right away, detached from this terminal
nohup python3 "$DEST/owl_pet.py" >/dev/null 2>&1 &
disown || true

echo "Installed and started. Find 'Hoot the Owl' in your Menu next time."
echo "No sound? Run:  python3 \"$DEST/owl_pet.py\" --test-sound"
echo "Right-click the owl and tick 'Start at login' to have it greet you every day."
