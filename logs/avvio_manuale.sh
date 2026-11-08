#!/bin/zsh
cd ~/Progetti/audio-to-text
echo "$(date) attendo la passata in corso (PID 30181)"
while kill -0 30181 2>/dev/null; do sleep 30; done
echo "$(date) parto con la passata completa"
caffeinate -i "$HOME/Desktop/Titoli Fabbri/whisperx_env/bin/python" nightly.py --max-seconds 43200 --threads 4
echo "$(date) finito, codice $?"
