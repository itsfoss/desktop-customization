#!/usr/bin/env bash
#
# install-kitty-halloween.sh
#
# Installs the "It's FOSS" Kitty Halloween theme:
#   https://github.com/itsfoss/desktop-customization/tree/main/halloween-customizations/kitty
#
# - Checks for Kitty and the "Iosevka Slab" font
# - Warns (does not auto-install) if either is missing, and asks to continue or quit
# - Downloads kitty.conf, dark-theme.auto.conf, no-preference-theme.auto.conf into ~/.config/kitty/
# - Downloads assets/halloween.png into ~/.config/kitty/assets/
#
# Note: the downloaded config files reference the background image at
#   ~/Pictures/logos/halloween.png
# This script does NOT rewrite that path. If you want the theme to actually pick up
# the image from ~/.config/kitty/assets/halloween.png, update the `background_image`
# line in dark-theme.auto.conf and no-preference-theme.auto.conf yourself.

set -uo pipefail

REPO_RAW_BASE="https://raw.githubusercontent.com/itsfoss/desktop-customization/main/halloween-customizations/kitty"
KITTY_CONFIG_DIR="$HOME/.config/kitty"
ASSETS_DIR="$KITTY_CONFIG_DIR/assets"
FONT_NAME="Iosevka Slab"

CONFIG_FILES=(
  "kitty.conf"
  "dark-theme.auto.conf"
  "no-preference-theme.auto.conf"
)

STARSHIP_RAW_URL="https://raw.githubusercontent.com/itsfoss/desktop-customization/main/starship/halloween-2026-starship.toml"
STARSHIP_CONFIG="$HOME/.config/starship.toml"

# ---------- helpers ----------

info()  { printf '\033[1;34m[INFO]\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$1"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m[ OK ]\033[0m %s\n' "$1"; }

need_downloader() {
  if command -v curl >/dev/null 2>&1; then
    DOWNLOADER="curl"
  elif command -v wget >/dev/null 2>&1; then
    DOWNLOADER="wget"
  else
    error "Neither curl nor wget is installed. Please install one of them and re-run this script."
    exit 1
  fi
}

download() {
  # download <url> <destination>
  local url="$1"
  local dest="$2"
  if [ "$DOWNLOADER" = "curl" ]; then
    curl -fsSL "$url" -o "$dest"
  else
    wget -q "$url" -O "$dest"
  fi
}

# ---------- checks ----------

check_kitty_installed() {
  command -v kitty >/dev/null 2>&1
}

check_font_installed() {
  # Requires fontconfig's `fc-list`. Falls back to "unknown" if not available.
  if command -v fc-list >/dev/null 2>&1; then
    fc-list | grep -qi "iosevka.*slab"
    return $?
  else
    return 2  # unknown: fc-list not available
  fi
}

install_starship_prompt() {
  # Installs the halloween-2026-starship.toml directly (no interactive menu),
  # since the user explicitly wants the Halloween-matching prompt, not a picklist.
  mkdir -p "$HOME/.config" || { error "Failed to create $HOME/.config"; return 1; }

  if [ -f "$STARSHIP_CONFIG" ]; then
    warn "Existing starship.toml found, backing up to starship.toml.bak"
    cp "$STARSHIP_CONFIG" "${STARSHIP_CONFIG}.bak"
  fi

  info "Downloading Halloween Starship prompt..."
  if download "$STARSHIP_RAW_URL" "$STARSHIP_CONFIG"; then
    ok "Installed halloween-2026-starship.toml -> $STARSHIP_CONFIG"
    if ! command -v starship >/dev/null 2>&1; then
      warn "Starship itself does not appear to be installed. See https://starship.rs for install instructions."
    fi
  else
    error "Failed to download the Starship theme from $STARSHIP_RAW_URL"
  fi
}

# ---------- main ----------

main() {
  need_downloader

  info "Checking requirements..."

  local kitty_ok=false
  local font_ok=false
  local font_check_possible=true

  if check_kitty_installed; then
    ok "Kitty is installed."
    kitty_ok=true
  else
    warn "Kitty does not appear to be installed."
  fi

  check_font_installed
  font_status=$?
  if [ "$font_status" -eq 0 ]; then
    ok "Font '$FONT_NAME' is installed."
    font_ok=true
  elif [ "$font_status" -eq 2 ]; then
    warn "Could not check installed fonts ('fc-list' not found). Skipping font check."
    font_check_possible=false
  else
    warn "Font '$FONT_NAME' does not appear to be installed."
  fi

  # Warn and ask to continue if either requirement is missing
  if [ "$kitty_ok" = false ] || { [ "$font_ok" = false ] && [ "$font_check_possible" = true ]; }; then
    echo
    warn "This theme requires both Kitty and the '$FONT_NAME' font to work correctly."
    [ "$kitty_ok" = false ] && warn "  - Kitty:        NOT installed"
    if [ "$font_check_possible" = true ]; then
      [ "$font_ok" = false ] && warn "  - $FONT_NAME:  NOT installed"
    fi
    echo
    read -r -p "Continue with installation anyway? [y/N]: " answer
    case "$answer" in
      [yY][eE][sS]|[yY])
        info "Continuing installation despite missing requirement(s)..."
        ;;
      *)
        info "Installation cancelled by user."
        exit 0
        ;;
    esac
  fi

  # ---------- create directories ----------

  info "Creating directories..."
  mkdir -p "$KITTY_CONFIG_DIR" || { error "Failed to create $KITTY_CONFIG_DIR"; exit 1; }
  mkdir -p "$ASSETS_DIR" || { error "Failed to create $ASSETS_DIR"; exit 1; }
  ok "Directories ready: $KITTY_CONFIG_DIR, $ASSETS_DIR"

  # ---------- download config files ----------

  info "Downloading config files..."
  for file in "${CONFIG_FILES[@]}"; do
    url="$REPO_RAW_BASE/$file"
    dest="$KITTY_CONFIG_DIR/$file"
    if download "$url" "$dest"; then
      ok "Installed $file -> $dest"
    else
      error "Failed to download $file from $url"
      exit 1
    fi
  done

  # ---------- download background image ----------

  info "Downloading Halloween background image..."
  image_url="$REPO_RAW_BASE/assets/halloween.png"
  image_dest="$ASSETS_DIR/halloween.png"
  if download "$image_url" "$image_dest"; then
    ok "Installed halloween.png -> $image_dest"
  else
    error "Failed to download halloween.png from $image_url"
    exit 1
  fi

  # ---------- optional: matching starship prompt ----------

  echo
  read -r -p "Install the matching Halloween Starship prompt too? [y/N]: " starship_answer
  case "$starship_answer" in
    [yY][eE][sS]|[yY])
      install_starship_prompt
      ;;
    *)
      info "Skipping Starship prompt installation."
      ;;
  esac

  # ---------- done ----------

  echo
  ok "Kitty Halloween theme installed successfully!"
  echo
  warn "Note: the config files reference the background image at ~/Pictures/logos/halloween.png,"
  warn "not ~/.config/kitty/assets/halloween.png. Update the 'background_image' line in"
  warn "dark-theme.auto.conf and no-preference-theme.auto.conf if you want it to use the new path."
  echo
  info "Restart Kitty for changes to take effect."
}

main "$@"
