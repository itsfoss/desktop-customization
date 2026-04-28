#!/bin/bash

# Starship Theme Installer
# Fetches themes from the itsfoss/public-dot-files repository and installs one.
# Can be run directly via: bash -c "$(curl -sSL <URL>)"

# --- Configuration ---
REPO_OWNER="itsfoss"
REPO_NAME="desktop-customization"
CONTENT_PATH="starship"

CONFIG_FILE="$HOME/.config/starship.toml"
API_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/${CONTENT_PATH}"
RAW_URL_BASE="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/main/${CONTENT_PATH}"

# --- Colors for Output ---
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# --- Dependency Check ---
check_deps() {
    if ! command -v curl &> /dev/null; then
        echo -e "${RED}Error: 'curl' is not installed. Please install it to continue.${NC}"
        exit 1
    fi
}

# --- Main Logic ---
main() {
    check_deps

    # 1. Backup existing starship.toml if it exists
    if [ -f "$CONFIG_FILE" ]; then
        echo -e "${YELLOW}Found existing configuration, creating backup...${NC}"
        cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
        echo "Backup created at ${CONFIG_FILE}.bak"
    fi

    # 2. Fetch the list of .toml files from the GitHub repository
    echo "Fetching available themes from GitHub..."
    local files_json
    files_json=$(curl -s "$API_URL")

    local toml_files=()
    while IFS= read -r line; do
        toml_files+=("$line")
    done < <(echo "$files_json" | grep '"name":' | sed -n 's/.*"name": "\(.*\.toml\)".*/\1/p')

    # Gracefully exit if no .toml files are found
    if [ ${#toml_files[@]} -eq 0 ]; then
        echo -e "${RED}Error: No '.toml' theme files were found in the repository.${NC}"
        echo "Please check the repository URL or your internet connection."
        exit 1
    fi

    # 3. Present the theme selection menu
    echo "Please select a Starship theme to install:"

    local options=()
    for file in "${toml_files[@]}"; do
        # Prettify name: remove extension, replace underscores, capitalize
        local pretty_name
        pretty_name=$(basename "$file" .toml | sed -e 's/_/ /g' -e 's/\b\(.\)/\u\1/g')
        options+=("$pretty_name")
    done

    select choice in "${options[@]}"; do
        if [[ -n $choice ]]; then
            for i in "${!options[@]}"; do
                if [[ "${options[$i]}" = "$choice" ]]; then
                    selected_file="${toml_files[$i]}"
                    break
                fi
            done
            echo "You chose: $choice"
            break
        else
            echo -e "${RED}Invalid selection. Please try again.${NC}"
        fi
    done

    # 4. Download the selected file
    echo "Downloading and installing '$choice' theme..."
    if ! curl -sL "$RAW_URL_BASE/$selected_file" -o "$CONFIG_FILE"; then
        echo -e "${RED}Error: Download failed. Could not install the theme.${NC}"
        exit 1
    fi

    echo -e "${GREEN}Installation complete!${NC}"
    echo "Your new Starship theme is ready. Restart your shell to see the changes."
}

# Run the main function
main
