#!/usr/bin/env zsh
# =============================================================================
# setup_env.sh — Installa le dipendenze mancanti nel venv whisperx_env
#
# Uso:
#   chmod +x setup_env.sh
#   ./setup_env.sh
#
# Il venv ~/Desktop/Titoli\ Fabbri/whisperx_env ha già:
#   faster-whisper 1.2.1, whisperx 3.8.7, torch 2.8.0, pyannote-audio 4.0.7
#
# Questo script aggiunge solo:
#   - mlx-whisper  (ASR nativo Apple Silicon)
#   - mlx          (framework ML di Apple, dipendenza di mlx-whisper)
#   - praat-parselmouth  (analisi prosodia)
#   - soundfile    (lettura WAV, probabile già presente)
#
# Non tocca: torch, pyannote, faster-whisper, whisperx
# =============================================================================

set -euo pipefail

VENV_DIR="$HOME/Desktop/Titoli Fabbri/whisperx_env"
PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

# Colori per output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}=== audio-to-text: setup dipendenze ===${NC}"
echo ""

# Verifica che il venv esista
if [[ ! -f "$PIP" ]]; then
    echo -e "${RED}ERRORE: venv non trovato in: $VENV_DIR${NC}"
    echo "Controlla il percorso in setup_env.sh"
    exit 1
fi

PYTHON_VER=$("$PYTHON" --version 2>&1)
echo -e "${GREEN}✓ venv trovato: $PYTHON_VER${NC}"
echo ""

# =============================================================================
# 1. Aggiorna pip silenziosamente
# =============================================================================
echo -e "${YELLOW}→ Aggiorno pip...${NC}"
"$PIP" install --upgrade pip --quiet

# =============================================================================
# 2. soundfile (lettura WAV)
# =============================================================================
echo -e "${YELLOW}→ Verifico soundfile...${NC}"
if "$PYTHON" -c "import soundfile" 2>/dev/null; then
    echo -e "${GREEN}  ✓ soundfile già installato${NC}"
else
    echo "  Installo soundfile..."
    "$PIP" install soundfile==0.12.1 --quiet
    echo -e "${GREEN}  ✓ soundfile installato${NC}"
fi

# =============================================================================
# 3. Parselmouth (Praat in Python — analisi prosodia)
# =============================================================================
echo -e "${YELLOW}→ Verifico praat-parselmouth...${NC}"
if "$PYTHON" -c "import parselmouth" 2>/dev/null; then
    echo -e "${GREEN}  ✓ parselmouth già installato${NC}"
else
    echo "  Installo praat-parselmouth (include binari Praat, ~50MB)..."
    "$PIP" install praat-parselmouth==0.4.5 --quiet
    echo -e "${GREEN}  ✓ praat-parselmouth installato${NC}"
fi

# =============================================================================
# 4. librosa (analisi audio per speech rate)
# =============================================================================
echo -e "${YELLOW}→ Verifico librosa...${NC}"
if "$PYTHON" -c "import librosa" 2>/dev/null; then
    echo -e "${GREEN}  ✓ librosa già installato${NC}"
else
    echo "  Installo librosa..."
    "$PIP" install librosa==0.10.2.post1 --quiet
    echo -e "${GREEN}  ✓ librosa installato${NC}"
fi

# =============================================================================
# 5. MLX + mlx-whisper (ASR nativo Apple Silicon)
# =============================================================================
echo -e "${YELLOW}→ Verifico mlx-whisper...${NC}"
if "$PYTHON" -c "import mlx_whisper" 2>/dev/null; then
    echo -e "${GREEN}  ✓ mlx-whisper già installato${NC}"
else
    echo "  Installo mlx e mlx-whisper (framework Apple Silicon)..."
    # mlx richiede macOS 13.5+ e Apple Silicon
    "$PIP" install mlx==0.22.0 --quiet
    "$PIP" install mlx-whisper==0.4.1 --quiet
    echo -e "${GREEN}  ✓ mlx-whisper installato${NC}"
fi

# =============================================================================
# 6. Verifica MPS disponibile
# =============================================================================
echo ""
echo -e "${YELLOW}→ Verifico MPS (Metal Performance Shaders)...${NC}"
MPS_STATUS=$("$PYTHON" -c "
import torch
if torch.backends.mps.is_available():
    print('disponibile')
else:
    print('non disponibile')
" 2>/dev/null || echo "errore")

if [[ "$MPS_STATUS" == "disponibile" ]]; then
    echo -e "${GREEN}  ✓ MPS disponibile — la GPU M1 Pro sarà usata per diarizzazione${NC}"
else
    echo -e "${YELLOW}  ⚠ MPS non disponibile — diarizzazione su CPU (più lenta)${NC}"
fi

# =============================================================================
# 7. Verifica token Hugging Face
# =============================================================================
echo ""
echo -e "${YELLOW}→ Verifico token Hugging Face...${NC}"

HF_TOKEN_FILE="$HOME/.huggingface/token"
HF_TOKEN_FILE2="$HOME/.cache/huggingface/token"

if [[ -f "$HF_TOKEN_FILE" ]] || [[ -f "$HF_TOKEN_FILE2" ]] || [[ -n "${HF_TOKEN:-}" ]]; then
    echo -e "${GREEN}  ✓ Token HF trovato${NC}"
else
    echo ""
    echo -e "${YELLOW}  ⚠ Token Hugging Face non trovato.${NC}"
    echo ""
    echo "  Il token è necessario per scaricare il modello pyannote"
    echo "  (diarizzazione speaker). È GRATUITO. Segui questi passi:"
    echo ""
    echo -e "  ${BLUE}1.${NC} Vai su: https://huggingface.co/join"
    echo -e "  ${BLUE}2.${NC} Crea un account gratuito"
    echo -e "  ${BLUE}3.${NC} Vai su: https://huggingface.co/settings/tokens"
    echo -e "  ${BLUE}4.${NC} Clicca 'New token' → tipo 'Read' → dai un nome (es: 'audio-to-text')"
    echo -e "  ${BLUE}5.${NC} Copia il token (inizia con 'hf_...')"
    echo -e "  ${BLUE}6.${NC} Accetta i termini del modello pyannote su:"
    echo "       https://huggingface.co/pyannote/speaker-diarization-3.1"
    echo "       https://huggingface.co/pyannote/segmentation-3.0"
    echo ""
    echo -e "  ${BLUE}7.${NC} Poi esegui:"
    echo "       $VENV_DIR/bin/huggingface-cli login"
    echo "     oppure imposta la variabile d'ambiente:"
    echo "       export HF_TOKEN='hf_tuotoken'"
    echo ""
    echo -e "  ${YELLOW}Senza token, la diarizzazione sarà disabilitata.${NC}"
    echo "  Puoi comunque usare: python run.py input/file.mp3 --no-diarization"
    echo ""
fi

# =============================================================================
# 8. Riepilogo finale
# =============================================================================
echo ""
echo -e "${BLUE}=== Riepilogo installazione ===${NC}"

check() {
    local pkg="$1"
    local import_name="${2:-$1}"
    if "$PYTHON" -c "import $import_name" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $pkg"
    else
        echo -e "  ${RED}✗${NC} $pkg  ← NON TROVATO"
    fi
}

check "faster-whisper" "faster_whisper"
check "whisperx"       "whisperx"
check "torch"          "torch"
check "pyannote-audio" "pyannote.audio"
check "mlx-whisper"    "mlx_whisper"
check "parselmouth"    "parselmouth"
check "librosa"        "librosa"
check "soundfile"      "soundfile"
check "numpy"          "numpy"

echo ""
echo -e "${GREEN}Setup completato!${NC}"
echo ""
echo "Per processare un file:"
echo "  $VENV_DIR/bin/python run.py input/tuo_file.mp3"
echo ""
echo "Per testare senza diarizzazione (non serve token HF):"
echo "  $VENV_DIR/bin/python run.py input/tuo_file.mp3 --no-diarization"
echo ""
