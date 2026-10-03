#!/usr/bin/env python
"""
Misura il riscaldamento durante una run vera.

    python thermal_probe.py                 # ascolta per 60s
    python thermal_probe.py --seconds 600   # 10 minuti
    python thermal_probe.py --run           # lancia la pipeline e la misura

Perché esiste questo file. Tutto quello che si sa sul calore è stato
dedotto dal tempo: «4 thread sono più veloci di 8, quindi scaldano
meno» è un ragionamento, non una misura. La macchina scalda in un modo
che dipende dalla frequenza, dalla ventilazione e dalla durata, e due
carichi con lo stesso tempo possono scaldare in modo molto diverso.

I numeri che si possono prendere senza strumenti speciali:

| Sorgente | Cosa è |
|---|---|
| `powermetrics` | watt, frequenza, temperatura — **richiede root** |
| `pmset -g therm` | livello di avvertimento termico di macOS |
| `ioreg` | temperatura dei sensori, quando esposti |
| `top` | uso CPU: il carico, che è metà del problema |

Se `powermetrics` non è disponibile, lo dice e va avanti con gli altri:
uno strumento che non c'è è un dato che non si prende, non un motivo
per non misurare niente. Quello che resta è comunque utile, perché il
carico CPU è metà della storia e l'altra metà è quella che non potrebbe
misurare senza permessi.

**La cosa più importante che emerge da una run** è la frequenza. Non il
picco — il *crollo* in coda. Una CPU che parte a 3 GHz e dopo venti
minuti sta a 2 GHz ha fatto i conti con l'esterno, e questo è
esattamente il motivo per cui limitare i thread può rendere *più
veloce*: un processore che non deve più rallentare lavora al suo passo
per tutto il tempo invece di rallentare a metà.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import ROOT_DIR  # noqa: E402

SAMPLE_SEC = 5.0


@dataclass
class Sample:
    t: float
    cpu_percent: float | None = None
    freq_mhz: float | None = None
    temp_c: float | None = None
    watts: float | None = None
    thermal_level: str | None = None


@dataclass
class Report:
    samples: list[Sample] = field(default_factory=list)
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        vals = [s for s in self.samples if s.cpu_percent is not None]
        return {
            "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "samples": len(self.samples),
            "seconds": round(self.samples[-1].t - self.samples[0].t, 1)
                       if len(self.samples) > 1 else 0.0,
            "source": self.source,
            "note": self.note,
            "cpu_percent": _stats([s.cpu_percent for s in vals]),
            "freq_mhz": _stats([s.freq_mhz for s in self.samples
                                if s.freq_mhz is not None]),
            "temp_c": _stats([s.temp_c for s in self.samples
                              if s.temp_c is not None]),
            "watts": _stats([s.watts for s in self.samples
                             if s.watts is not None]),
            "thermal_level": _levels(self.samples),
        }


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    return {
        "min": round(v[0], 1),
        "median": round(v[n // 2], 1),
        "max": round(v[-1], 1),
        "mean": round(sum(v) / n, 1),
        # Il primo e l'ultimo quarto sono la cosa che conta: se la
        # frequenza scende fra il primo e l'ultimo campione, il
        # processore ha rallentato, e quella è l'informazione che
        # spiega perché limitare i thread aiuta.
        "first_quarter_mean": round(sum(v[:max(1, n // 4)]) / max(1, n // 4), 1),
        "last_quarter_mean": round(sum(v[max(0, n - n // 4):]) / max(1, n // 4), 1),
    }


def _levels(samples: list[Sample]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in samples:
        if s.thermal_level:
            out[s.thermal_level] = out.get(s.thermal_level, 0) + 1
    return out


# -------------------------------------------------------------------
# Le sonde
# -------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


_FREQ_RE = re.compile(r"CPU_Scheduler_Limit\s*:\s*([\d.]+)\s*%")
_FREQ2_RE = re.compile(r"Package_Hz\s*:\s*([\d.]+)")
_PKT_RE = re.compile(r"CPU_Package_Power\s*:\s*([\d.]+)")


def has_powermetrics() -> bool:
    return shutil.which("powermetrics") is not None


def read_powermetrics() -> dict:
    """Watt e frequenza reali. Richiede root: senza, dice che non può.

    Il `-n 1` con un timeout breve è il modo più gentile di chiedere un
    campione: `powermetrics` è pensato per girare di continuo e senza
    `--samplers` chiede tutto, che è lento.
    """
    out = _run(["powermetrics", "--samplers", "cpu_power", "-n", "1", "-i", "200"],
               timeout=15)
    if not out.strip():
        return {}
    res: dict[str, float] = {}
    if m := _FREQ_RE.search(out):
        # È una percentuale del massimo, non dei MHz: il limite di
        # scheduling è proprio il numero che dice se il processore è
        # stato rallentato, quindi va riportato così com'è.
        res["sched_limit_pct"] = float(m.group(1))
    if m := _PKT_RE.search(out):
        res["watts"] = float(m.group(1))
    if m := _FREQ2_RE.search(out):
        res["freq_mhz"] = float(m.group(1))
    return res


_TEMP_RE = re.compile(r"temperature\s*=\s*([\d.]+)\s*C", re.IGNORECASE)


def read_temperature() -> float | None:
    """Temperatura da `ioreg`, quando esposta. Su molti Mac non lo è.

    Non si mette nessun valore inventato al suo posto: una temperatura
    non disponibile è un dato assente, e un dato assente travestito da
    zero fa pensare che la macchina sia fredda quando non lo è.
    """
    out = _run(["ioreg", "-c", "IOPlatformExpertDevice", "-r", "-d", "4"], timeout=10)
    if m := _TEMP_RE.search(out):
        return float(m.group(1))
    return None


def read_cpu_percent() -> float | None:
    """Uso CPU di tutto il sistema, da `top` in modalità istantanea.

    `top -l 1` senza `-n` stampa subito e si chiude: è l'unico modo per
    non avere un processo che gira in più.
    """
    out = _run(["top", "-l", "1", "-n", "0", "-s", "0"], timeout=20)
    m = re.search(r"CPU usage:\s*([\d.]+)\s*% user.*?([\d.]+)\s*% sys", out)
    if m:
        return float(m.group(1)) + float(m.group(2))
    return None


_THERM_RE = re.compile(r"(CPU_Speed_Limit|CPU_Available_CPUs|Power_Sources)")


def read_thermal_level() -> str | None:
    """Livello di avvertimento termico di macOS. Quasi sempre `None`.

    Su un portatile senza carico non c'è niente da segnalare, e quel
    `None` è un'informazione utile: vuol dire che il sistema non è in
    allarme. Non viene sostituito con "ok", che sarebbe un'affermazione
    non verificata.
    """
    out = _run(["pmset", "-g", "therm"], timeout=10)
    for line in out.splitlines():
        if "has been recorded" in line or "no warning" in line.lower():
            return "none-recorded"
    return None


# -------------------------------------------------------------------
# Il ciclo
# -------------------------------------------------------------------

def probe(seconds: float, interval: float = SAMPLE_SEC) -> Report:
    rep = Report(source="top+ioreg+pmset")
    if has_powermetrics():
        # Non ci metto powermetrics nel ciclo: chiede root, e senza
        # permessi fallirebbe a ogni campione. Ci va una volta, fuori,
        # e il risultato dice se c'è.
        try:
            if read_powermetrics():
                rep.note = "powermetrics disponibile (servono i permessi root)"
        except Exception:  # noqa: BLE001
            pass

    t0 = time.time()
    n = 0
    while True:
        now = time.time()
        s = Sample(
            t=round(now - t0, 1),
            cpu_percent=read_cpu_percent(),
            temp_c=read_temperature(),
            thermal_level=read_thermal_level(),
        )
        rep.samples.append(s)
        n += 1
        if now - t0 >= seconds:
            break
        # Il passo minimo è di un secondo: sotto, `top` non fa in tempo
        # a produrre un campione e si misurerebbe il costo di top.
        time.sleep(max(1.0, interval))

    if all(s.temp_c is None for s in rep.samples):
        rep.note = (rep.note + " | " if rep.note else "") + \
            "temperatura non esposta da ioreg su questa macchina"
    return rep


def print_report(rep: Report, label: str) -> None:
    d = rep.to_dict()
    print(f"\n=== {label} ===")
    print(f"campioni: {d['samples']} in {d['seconds']:.0f}s")
    for key in ("cpu_percent", "temp_c", "freq_mhz", "watts"):
        st = d[key]
        if st:
            print(f"  {key:14s} min {st['min']:7.1f}  media {st['mean']:7.1f}  "
                  f"max {st['max']:7.1f}")
    if d["thermal_level"]:
        print(f"  livello termico: {d['thermal_level']}")
    if d["note"]:
        print(f"  nota: {d['note']}")
    # Il dato che spiega il resto: se la frequenza cade nel tempo, la
    # CPU ha rallentato per il caldo e il lavoro reale è costato più
    # del preventivato.
    if d["freq_mhz"]:
        calo = d["freq_mhz"]["last_quarter_mean"] - d["freq_mhz"]["first_quarter_mean"]
        if calo < -50:
            print(f"  ATTENZIONE: la frequenza è calata di {-calo:.0f} MHz "
                  "fra inizio e fine: il processore ha rallentato")
    if d["cpu_percent"]:
        print(f"  uso CPU: primo quarto {d['cpu_percent']['first_quarter_mean']:.0f}%, "
              f"ultimo quarto {d['cpu_percent']['last_quarter_mean']:.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=SAMPLE_SEC)
    ap.add_argument("--run", action="store_true",
                    help="lancia la pipeline su input/ e la misura")
    ap.add_argument("--out", help="path del JSON con il risultato")
    args = ap.parse_args()

    if args.run:
        audio = sorted((ROOT_DIR / "input").glob("*"))
        audio = [a for a in audio if a.suffix.lower() in
                 (".mp3", ".wav", ".m4a", ".mp4", ".aac")]
        if not audio:
            print("Nessun file audio in input/", file=sys.stderr)
            return 1
        target = audio[0]
        print(f"Baseline 30s senza carico, poi elaboro {target.name}…")
        base = probe(30.0, args.interval)
        print_report(base, "baseline (macchina ferma)")

        cmd = [sys.executable, str(ROOT / "run.py"), str(target)]
        print(f"\nLancio: {' '.join(cmd)}", flush=True)
        t0 = time.time()

        import threading
        rep_box: list[Report] = []

        def _work() -> None:
            subprocess.run(cmd, cwd=str(ROOT))

        th = threading.Thread(target=_work)
        th.start()
        # Si misura per la durata della pipeline, non per un numero
        # fisso: la finestra deve coprire tutto il lavoro, altrimenti
        # si registra solo il riscaldamento iniziale e si conclude che
        # la macchina non scalda.
        while th.is_alive():
            rep_box.append(probe(args.interval, args.interval))
            time.sleep(0.1)
        th.join()
        durata = time.time() - t0

        campioni = [s for r in rep_box for s in r.samples]
        sotto_carico = Report(samples=campioni, source="top+ioreg+pmset",
                              note=f"durante {durata:.0f}s di pipeline su {target.name}")
        print_report(sotto_carico, "sotto carico")
        out = {"baseline": base.to_dict(), "carico": sotto_carico.to_dict(),
               "seconds": round(durata, 1)}
        path = Path(args.out) if args.out else (ROOT_DIR / "logs" / "thermal.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"\nScritto: {path}")
        return 0

    rep = probe(args.seconds, args.interval)
    print_report(rep, f"{args.seconds:.0f}s di ascolto")
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"Scritto: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())