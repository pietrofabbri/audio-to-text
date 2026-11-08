#!/bin/zsh
cd /Users/pietrofabbri/Progetti/audio-to-text
PY="$HOME/Desktop/Titoli Fabbri/whisperx_env/bin/python"
"$PY" review_speakers.py ridiarizza 2026-10-27_15-32-38 --min 6 --fusione-min-sec 20 && "$PY" review_speakers.py ridiarizza 2026-10-27_16-34-38 --min 6 --fusione-min-sec 20
echo FINE $?
