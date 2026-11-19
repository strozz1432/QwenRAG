#!/usr/bin/env bash
# Local Docs installer: sets up everything and lets you choose which models to download.
#
#   ./install.sh                         interactive (asks before each install, shows a model menu)
#   ./install.sh --yes                   no questions: install the defaults
#   ./install.sh --models qwen3:8b,qwen3.5:9b --default qwen3.5:9b
#   ./install.sh --dry-run               show what it would do, change nothing
#   ./install.sh --help
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EMBED="qwen3-embedding:0.6b"            # always installed: it builds the search index

# id|size|description   (sizes are the download size)
CHAT_MODELS=(
  "qwen3:4b|2.5 GB|Light and fast. Good for older or low-memory machines"
  "qwen3:8b|5.2 GB|Balanced default. Needs about 8 GB of free memory"
  "qwen3:14b|9.3 GB|Better answers. Needs about 16 GB of memory"
  "qwen3.5:4b|3.3 GB|Newer small model"
  "qwen3.5:9b|6.6 GB|Newer and can look at images (describes pictures and figures)"
  "qwen3.5:27b|17.5 GB|Largest. Needs a high-end machine (32 GB+ memory)"
  "gemma3:4b|3.4 GB|Google's small model, a different style"
  "llama3.2:3b|2.0 GB|Meta's smallest, very light"
)
DEFAULT_SELECTION="qwen3:8b"

YES=0; DRY=0; SELECTED_ARG=""; DEFAULT_ARG=""; SKIP_OCR=0; SKIP_DEPS=0

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --models) SELECTED_ARG="${2:-}"; shift ;;
    --default) DEFAULT_ARG="${2:-}"; shift ;;
    --no-ocr) SKIP_OCR=1 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- helpers
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'; else B=""; G=""; Y=""; R=""; D=""; N=""; fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

run() {   # run a command, or just print it in --dry-run mode
  if [ "$DRY" = 1 ]; then printf '  %s[dry-run]%s %s\n' "$D" "$N" "$*"; return 0; fi
  "$@"
}

ask() {   # ask "question" default(y/n)  -> 0 for yes
  local q="$1" def="${2:-y}" a hint="[Y/n]"
  [ "$def" = n ] && hint="[y/N]"
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then [ "$def" = y ]; return; fi
  read -r -p "  $q $hint " a || a=""
  a="$(printf '%s' "${a:-$def}" | tr '[:upper:]' '[:lower:]')"
  [ "${a:0:1}" = y ]
}

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) fail "This installer supports macOS and Linux. On Windows, use WSL."; exit 1 ;;
esac

pkg_install() {   # pkg_install <brew name> <apt name> : install a system package with whatever manager exists
  if have brew; then run brew install "$1"
  elif have apt-get; then run sudo apt-get install -y "$2"
  elif have dnf; then run sudo dnf install -y "$2"
  elif have pacman; then run sudo pacman -S --noconfirm "$2"
  else fail "No supported package manager found. Please install $1 yourself."; return 1; fi
}

# ---------------------------------------------------------------- 1. tools
step "1/5  Checking tools"

if [ "$SKIP_DEPS" = 0 ]; then
  if have uv; then ok "uv is installed"
  elif ask "uv (Python package manager) is missing. Install it?" y; then
    run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    have uv || [ "$DRY" = 1 ] || { fail "uv still not found. Open a new terminal and run this script again."; exit 1; }
  else fail "uv is required."; exit 1; fi

  if have ollama; then ok "Ollama is installed"
  elif ask "Ollama (runs the AI models on your machine) is missing. Install it?" y; then
    if [ "$OS" = Darwin ] && have brew; then run brew install ollama
    elif [ "$OS" = Linux ]; then run sh -c 'curl -fsSL https://ollama.com/install.sh | sh'
    else warn "Install Ollama from https://ollama.com/download, then run this script again."; [ "$DRY" = 1 ] || exit 1; fi
  else fail "Ollama is required."; exit 1; fi

  if [ "$SKIP_OCR" = 0 ]; then
    if have tesseract; then ok "Tesseract (OCR for scans and images) is installed"
    elif ask "Install Tesseract so scanned PDFs and images can be read? (optional)" y; then
      pkg_install tesseract tesseract-ocr || warn "Skipped OCR. Everything else still works."
    else warn "Skipping OCR: images and scanned pages won't be searchable."; fi
  fi
fi

# ---------------------------------------------------------------- 2. project
step "2/5  Installing Local Docs"
if have uv; then
  (cd "$REPO" && run uv sync) && ok "Python environment ready" || { fail "uv sync failed"; exit 1; }
else
  [ "$DRY" = 1 ] && (cd "$REPO" && run uv sync) || { fail "uv is missing"; exit 1; }
fi

# ---------------------------------------------------------------- 3. Ollama running
step "3/5  Starting Ollama"
ollama_up() { curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; }
if ollama_up; then ok "Ollama is running"
elif [ "$DRY" = 1 ]; then run ollama serve
else
  if [ "$OS" = Darwin ] && have brew && brew services list 2>/dev/null | grep -q '^ollama'; then brew services start ollama >/dev/null 2>&1
  else (nohup ollama serve >/dev/null 2>&1 &); fi
  for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
  ollama_up && ok "Ollama is running" || { fail "Couldn't start Ollama. Run 'ollama serve' in another terminal, then run this script again."; exit 1; }
fi

# ---------------------------------------------------------------- 4. choose models
step "4/5  Choose your models"
INSTALLED=""
ollama_up && INSTALLED="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
is_installed() { printf '%s\n' "$INSTALLED" | grep -qx "$1"; }

ids=(); sizes=(); descs=(); picked=()
for row in "${CHAT_MODELS[@]}"; do
  IFS='|' read -r id size desc <<<"$row"
  ids+=("$id"); sizes+=("$size"); descs+=("$desc"); picked+=(0)
done

pick() { local i; for i in "${!ids[@]}"; do [ "${ids[$i]}" = "$1" ] && picked[$i]=1; done; }

if [ -n "$SELECTED_ARG" ]; then
  IFS=',' read -ra want <<<"$SELECTED_ARG"
  for m in "${want[@]}"; do
    m="$(printf '%s' "$m" | tr -d ' ')"; [ -z "$m" ] && continue
    found=0; for i in "${!ids[@]}"; do [ "${ids[$i]}" = "$m" ] && found=1; done
    [ "$found" = 0 ] && { ids+=("$m"); sizes+=("?"); descs+=("custom model"); picked+=(0); }
    pick "$m"
  done
else
  pick "$DEFAULT_SELECTION"
  if [ "$YES" = 0 ] && [ -t 0 ]; then
    say "  An embedding model ($EMBED, 0.6 GB) is always installed: it builds the search index."
    say "  Pick the chat model(s) you want. Type numbers to toggle (e.g. 2 5), a to add your own, Enter to continue."
    while true; do
      say ""
      for i in "${!ids[@]}"; do
        mark="[ ]"; [ "${picked[$i]}" = 1 ] && mark="[x]"
        tag=""; is_installed "${ids[$i]}" && tag=" ${G}(already installed)${N}"
        printf '   %s %2d) %-14s %8s  %s%s\n' "$mark" "$((i + 1))" "${ids[$i]}" "${sizes[$i]}" "${descs[$i]}" "$tag"
      done
      read -r -p "  Toggle: " line || line=""
      [ -z "$line" ] && break
      if [ "$line" = a ]; then
        read -r -p "  Model name (as in 'ollama pull', e.g. mistral:7b): " custom || custom=""
        custom="$(printf '%s' "$custom" | tr -d ' ')"
        [ -n "$custom" ] && { ids+=("$custom"); sizes+=("?"); descs+=("custom model"); picked+=(1); }
        continue
      fi
      for n in $line; do
        if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#ids[@]}" ]; then
          i=$((n - 1)); [ "${picked[$i]}" = 1 ] && picked[$i]=0 || picked[$i]=1
        fi
      done
    done
  fi
fi

CHOSEN=()
for i in "${!ids[@]}"; do [ "${picked[$i]}" = 1 ] && CHOSEN+=("${ids[$i]}"); done
[ "${#CHOSEN[@]}" = 0 ] && { warn "No chat model selected, using $DEFAULT_SELECTION."; CHOSEN=("$DEFAULT_SELECTION"); }

say ""; say "  Selected: $EMBED (search index) + ${CHOSEN[*]}"

# which one is the default?
DEFAULT_MODEL="${CHOSEN[0]}"
if [ -n "$DEFAULT_ARG" ]; then DEFAULT_MODEL="$DEFAULT_ARG"
elif [ "${#CHOSEN[@]}" -gt 1 ] && [ "$YES" = 0 ] && [ -t 0 ]; then
  say ""; say "  Which one should Local Docs use by default? (you can switch any time with /model)"
  for i in "${!CHOSEN[@]}"; do printf '   %d) %s\n' "$((i + 1))" "${CHOSEN[$i]}"; done
  read -r -p "  Default [1]: " n || n=""
  [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#CHOSEN[@]}" ] && DEFAULT_MODEL="${CHOSEN[$((n - 1))]}"
fi

# ---------------------------------------------------------------- 5. download
step "5/5  Downloading models"
FAILED=()
for m in "$EMBED" "${CHOSEN[@]}"; do
  if is_installed "$m"; then ok "$m already installed"
  else
    say "  Downloading $m ..."
    if run ollama pull "$m"; then ok "$m ready"; else fail "$m failed to download"; FAILED+=("$m"); fi
  fi
done

# remember the default model without touching other saved preferences
if [ "$DRY" = 0 ] && [ -z "$(printf '%s\n' "${FAILED[@]:-}" | grep -x "$DEFAULT_MODEL")" ]; then
  PREFS="$REPO/preferences.json" MODEL="$DEFAULT_MODEL" python3 - <<'PY' && ok "Default model: $DEFAULT_MODEL"
import json, os
p, m = os.environ["PREFS"], os.environ["MODEL"]
try:
    data = json.load(open(p))
except Exception:
    data = {}
data["model"] = m
json.dump(data, open(p, "w"), indent=1)
PY
elif [ "$DRY" = 1 ]; then say "  ${D}[dry-run] default model would be: $DEFAULT_MODEL${N}"; fi

# optional launcher
if ask "Add a 'localdocs' command so you can start it from anywhere?" y; then
  BIN="$HOME/.local/bin"
  run mkdir -p "$BIN"
  if [ "$DRY" = 1 ]; then say "  ${D}[dry-run] would write $BIN/localdocs${N}"
  else
    printf '#!/usr/bin/env bash\nexec uv run --project "%s" "%s/rag.py" "$@"\n' "$REPO" "$REPO" > "$BIN/localdocs"
    chmod +x "$BIN/localdocs"
    ok "Installed $BIN/localdocs"
    case ":$PATH:" in *":$BIN:"*) ;; *) warn "Add $BIN to your PATH to use it (e.g. echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc)." ;; esac
  fi
fi

say ""
if [ "${#FAILED[@]}" -gt 0 ]; then
  warn "Some models failed: ${FAILED[*]}. Re-run this script, or try 'ollama pull <name>' yourself."
fi
printf '%sAll set.%s Start it with:  %scd %s && uv run rag.py%s' "$B" "$N" "$B" "$REPO" "$N"
have localdocs >/dev/null 2>&1 && printf '   (or just: localdocs)'
say ""
say "  First launch scans your Documents, Desktop and Downloads and builds the index."
say "  Optional: connect a cloud model later with /connect (Anthropic, OpenAI, Gemini, Groq, ...)."
