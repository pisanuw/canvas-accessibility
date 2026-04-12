#!/usr/bin/env bash
# run-locally.sh — Load .env and start Flask in debug mode.
# Handles values that contain spaces, !, quotes, and other special characters.

set -euo pipefail

ENV_FILE="$(dirname "$0")/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found."
  exit 1
fi

# Read .env line by line, skipping comments and blanks, and export each variable.
# Using 'read' avoids shell interpretation of special characters in values.
while IFS='=' read -r key rest; do
  # Skip comment lines and blank lines
  [[ "$key" =~ ^[[:space:]]*# ]] && continue
  [[ -z "$key" ]] && continue
  # Skip lines with no = sign
  [[ -z "$rest" && "$key" != *=* ]] && continue
  # Strip inline comments from value (anything after  # )
  value="${rest%%  #*}"
  # Trim leading/trailing whitespace from key
  key="${key#"${key%%[![:space:]]*}"}"
  key="${key%"${key##*[![:space:]]}"}"
  export "$key=$value"
done < "$ENV_FILE"

cd "$(dirname "$0")"
echo "Starting Flask (http://localhost:5000) ..."
python3 -m flask --app webapp.app run --debug --port 5000
