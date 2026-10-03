"""
Test della protezione termica: quanto si riposa, e quando si sospetta
che il chip stia rallentando.

    python tests/test_thermal.py

Non carica modelli, non tocca la pipeline e non dorme davvero: qui si
prova la *decisione* di riposo, cioè la regola che tiene la macchina
usabile. E una regola che decide male è peggio di una regola che non
c'è, perche' costa tempo di notte senza togliere calore.

I tempi qui sono finti e in scala 1:1000, cosi' una notte da quattro ore
si verifica in pochi millisecondi e con numeri che si possono leggere.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.config import thermal_policy  # noqa: E402
from core.thermal import (  # noqa: E402
    ThermalGovernor,
    ThermalPolicy,
    format_cooldown,
)


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


CHECKS: list[tuple[str, callable]] = []


def check(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


# --------------------------------------------------------------------------
# La regola di riposo
# --------------------------------------------------------------------------

@check
def pausa_corta_usa_il_minimo() -> None:
    """Un file di trenta secondi non merita una pausa da servizio."""
    p = ThermalPolicy(min_sec=90.0)
    require(p.cooldown_for(30) == 90.0,
            f"con 30s di lavoro la pausa deve essere il minimo, non "
            f"{p.cooldown_for(30)}")


@check
def pausa_lunga_e_proporzionale() -> None:
    """Quindici minuti di lavoro vogliono minuti di pausa, non secondi."""
    p = ThermalPolicy(min_sec=90.0, work_ratio=0.25, max_sec=600.0)
    require(p.cooldown_for(900) == 225.0,
            f"900s di lavoro con rapporto 0.25 danno 225s, non {p.cooldown_for(900)}")


@check
def pausa_ha_un_tetto() -> None:
    """Una notte lunga non deve trasformarsi in una notte di sonno."""
    p = ThermalPolicy(min_sec=90.0, work_ratio=0.25, max_sec=600.0)
    require(p.cooldown_for(100_000) == 600.0,
            f"il tetto di 600s deve valere, non {p.cooldown_for(100_000)}")


@check
def pavimento_vince_sulla_proporzione() -> None:
    """Il minimo esplicito non si può perdere sotto una sessione corta."""
    p = ThermalPolicy(min_sec=300.0, work_ratio=0.25, short_job_sec=120.0)
    require(p.cooldown_for(100) == 300.0,
            "il pavimento deve valere anche sotto la soglia della regola")
    require(p.cooldown_for(600) == 300.0,
            "600s * 0.25 = 150s, sotto il pavimento di 300s: deve valere 300")


# --------------------------------------------------------------------------
# Il rallentamento come sensore
# --------------------------------------------------------------------------

@check
def primo_file_non_e_rallentamento() -> None:
    """Non si può paragonare il primo file con niente."""
    g = ThermalGovernor()
    g.note_work(elapsed_sec=600.0, audio_sec=3600.0)
    require(not g.throttled,
            "il primo file non ha un confronto: non può essere lento")


@check
def rallentamento_allunga_la_pausa() -> None:
    """Se un secondo di audio costa il 50% in più, il chip soffre."""
    g = ThermalGovernor()
    g.note_work(600.0, 3600.0)          # 0.167 s/s: riferimento
    require(not g.throttled, "il riferimento non è un rallentamento")

    g.note_work(900.0, 3600.0)          # 0.250 s/s: +50%
    require(g.throttled,
            "un +50% sul costo per secondo di audio è il segnale che si cerca")
    require(g.last_cooldown_sec > 225.0,
            f"la pausa deve allungarsi, è rimasta a {g.last_cooldown_sec}")


@check
def rallentamento_usa_la_mediana() -> None:
    """Un file difficile non deve far sembrare che la macchina rallenti."""
    g = ThermalGovernor()
    for _ in range(5):
        g.note_work(600.0, 3600.0)      # tutti uguali: 0.167 s/s
    g.note_work(5000.0, 3600.0)         # un file patologico
    require(g.throttled,
            "un file patologico anomalo in coda si vede: è lento")

    # Il file dopo torna normale: con la mediana (non la media) il
    # riferimento resta quello dei cinque file normali.
    g = ThermalGovernor()
    for _ in range(5):
        g.note_work(600.0, 3600.0)
    g.note_work(5000.0, 3600.0)
    g.note_work(620.0, 3600.0)          # 0.172 s/s, normale
    require(not g.throttled,
            "la mediana deve resistere a un file patologico: senza di lei "
            "ogni file difficile farebbe creare la barra")


@check
def soglia_di_rallentamento_e_configurabile() -> None:
    """Il +25% è una scelta, non un fatto: chi la cambia deve poterlo."""
    permissivo = ThermalGovernor(ThermalPolicy(slowdown_threshold=3.0))
    permissivo.note_work(600.0, 3600.0)
    permissivo.note_work(900.0, 3600.0)
    require(not permissivo.throttled,
            "con soglia tripla, un +50% non deve contare come rallentamento")


@check
def senza_durata_audio_niente_rallentamento() -> None:
    """Se non si sa quanto era lunga l'audio, non si inventa un confronto."""
    g = ThermalGovernor()
    g.note_work(600.0, 0.0)
    g.note_work(900.0, 0.0)
    require(not g.throttled,
            "senza durata non si può concludere che il chip rallenti")
    require(g.last_cooldown_sec > 0.0,
            "la pausa proporzionale al lavoro deve funzionare anche senza audio")


# --------------------------------------------------------------------------
# Il riposo vero
# --------------------------------------------------------------------------

@check
def riposo_dura_quanto_deciso() -> None:
    """La pausa dichiarata è la pausa dormita: non meno."""
    slept: list[float] = []
    g = ThermalGovernor()
    g.note_work(600.0, 3600.0)          # 150s
    g.rest(sleeper=slept.append)
    require(abs(sum(slept) - 150.0) < 1.5,
            f"dormiti {sum(slept):.1f}s invece di 150s")
    require(len(slept) > 1,
            "il riposo va a pezzi: se arriva una chiusura non si deve "
            "aspettare minuti prima di accorgersene")


@check
def riposo_si_puo_fare_interrompere() -> None:
    """Un segnale di chiusura durante la pausa deve uscire subito."""
    state = {"calls": 0}

    def sleeper(_s: float) -> None:
        state["calls"] += 1

    g = ThermalGovernor()
    g.note_work(600.0, 3600.0)          # 150s
    got = g.rest(sleeper=sleeper, should_stop=lambda: state["calls"] >= 3)
    require(state["calls"] == 3,
            f"dovrebbe fermarsi al terzo pezzo, si è fermato al {state['calls']}")
    require(got < 5.0,
            f"un riposo interrotto deve essere breve, è durato {got:.1f}s")


@check
def nessuna_pausa_non_dorme() -> None:
    """Se la pausa è zero non si deve neanche chiamare il sonno."""
    slept: list[float] = []
    g = ThermalGovernor()
    g.rest(sleeper=slept.append)
    require(not slept, "non c'è lavoro fatto, non c'è pausa da fare")


# --------------------------------------------------------------------------
# La configurazione del progetto
# --------------------------------------------------------------------------

@check
def configurazione_progetto_e_centralizzata() -> None:
    """Il progetto decide i numeri in config.py, non qui dentro."""
    p = thermal_policy()
    require(p.work_ratio > 0.0 and p.max_sec >= p.min_sec,
            "la politica costruita dal progetto deve avere numeri sensati")
    require(p.min_sec >= 30.0,
            "una pausa minima sotto i 30 secondi non raffredda niente")


@check
def minimo_esplicito_vince() -> None:
    """Chi passa --cooldown-sec a mano ha deciso: si rispetta."""
    p = thermal_policy(300.0)
    require(p.min_sec == 300.0,
            f"il minimo esplicito deve valere, vale {p.min_sec}")


@check
def formato_delle_pause() -> None:
    """I log si leggono a occhio: 4m30s, non 270.0."""
    require(format_cooldown(270) == "4m30s", format_cooldown(270))
    require(format_cooldown(45) == "45s", format_cooldown(45))


def main() -> int:
    passed = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            passed += 1
            print(f"  ok  {name}")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} — {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} — {type(exc).__name__}: {exc}")

    print(f"\n{passed}/{len(CHECKS)} superati")
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
