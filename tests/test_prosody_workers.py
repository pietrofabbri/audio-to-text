"""
Test della prosodia in parallelo: cosa viaggia verso i worker.

    python tests/test_prosody_workers.py

Non si prova qui se la prosodia e' accurata, ma se il percorso parallelo
finisce. Il difetto che questo test copre non dava errori e non lasciava
tracce nel log: la prosodia semplicemente non tornava, perche' l'audio
finiva dentro ogni task e veniva spedito una volta per segmento.

I test precedenti non lo vedevano: la soglia per andare in parallelo e'
20 segmenti, e i test end-to-end girano su file da mezzo minuto con sei
segmenti. Il difetto si mostrava solo sui file veri da un'ora.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.config import ProsodyConfig  # noqa: E402
from pipeline import prosody  # noqa: E402
from pipeline.prosody import ProsodyAnalyzer  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


CHECKS: list[tuple[str, callable]] = []


def check(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


def _fake_wav(path: Path, seconds: float = 40.0) -> Path:
    """WAV 16kHz con un tono modulato: qualcosa su cui il F0 esiste."""
    import numpy as np
    import soundfile as sf

    sr = 16_000
    t = np.arange(int(sr * seconds)) / sr
    f0 = 140.0 + 12.0 * np.sin(2 * np.pi * 1.5 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    noise = 0.01 * np.random.default_rng(7).normal(0, 1, t.size)
    sf.write(str(path), (0.35 * np.sin(phase) + noise).astype("float32"),
             sr, subtype="FLOAT")
    return path


def _segments(n: int = 22, seconds: float = 1.6) -> list[dict]:
    out = []
    for i in range(n):
        start = i * seconds
        out.append({"idx": i, "start": start, "end": start + seconds})
    return out


def _cfg() -> ProsodyConfig:
    cfg = ProsodyConfig(num_workers=2)
    cfg.extract_jitter = False
    cfg.extract_shimmer = False
    return cfg


RECORDER: dict = {}


class _FakeWorkers:
    """Falso gruppo di worker: non crea processi, registra cosa riceve."""

    def __init__(self, processes=None, initializer=None, initargs=()):
        RECORDER["processes"] = processes
        RECORDER["initializer"] = initializer
        RECORDER["initargs"] = initargs
        self._initializer = initializer
        self._initargs = initargs

    def __enter__(self):
        # Un worker vero esegue l'inizializzatore al partire; qui gira
        # nel padre, che e' il posto dove si vede l'effetto.
        if self._initializer:
            self._initializer(*self._initargs)
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, iterable):
        RECORDER["payload"] = list(iterable)
        return [fn(item) for item in RECORDER["payload"]]

    def terminate(self):
        """Niente da terminare: qui i processi non esistono."""

    def join(self):
        """Niente da aspettare: qui i processi non esistono."""


class _FakeCtx:
    def Pool(self, processes=None, initializer=None, initargs=()):
        return _FakeWorkers(processes, initializer, initargs)


def _con_worker_finti(fn):
    RECORDER.clear()
    vecchio = prosody.mp.get_context
    prosody.mp.get_context = lambda name: _FakeCtx()
    try:
        return fn()
    finally:
        prosody.mp.get_context = vecchio


@check
def ai_worker_viaggia_solo_il_segmento() -> None:
    """Il compito di un worker e' un segmento, non un file audio."""
    import numpy as np

    tmp = Path(tempfile.mkdtemp())
    wav = _fake_wav(tmp / "a.wav")
    risultati = _con_worker_finti(
        lambda: ProsodyAnalyzer(_cfg()).analyze(_segments(), wav))

    payload = RECORDER.get("payload", [])
    require(len(payload) == 22,
            f"un elemento per segmento atteso, trovati {len(payload)}")

    for item in payload:
        require(isinstance(item, dict),
                f"un compito deve essere un segmento (dict), non "
                f"{type(item).__name__}")
        for v in item.values():
            require(not isinstance(v, np.ndarray),
                    "un array e' finito dentro un compito spedito al worker")

    require(len(risultati) == 22,
            f"risultati attesi 22, ottenuti {len(risultati)}")


@check
def il_worker_carica_l_audio_all_avvio() -> None:
    """Se l'audio non viaggia nei compiti, deve arrivare in altro modo."""
    tmp = Path(tempfile.mkdtemp())
    wav = _fake_wav(tmp / "b.wav")
    risultati = _con_worker_finti(
        lambda: ProsodyAnalyzer(_cfg()).analyze(_segments(), wav))

    require(RECORDER.get("initializer") is not None,
            "manca l'inizializzatore che carica l'audio nel worker")
    require(len(RECORDER.get("initargs") or ()) == 3,
            "l'inizializzatore vuole percorso, frequenza e configurazione")
    require(len(risultati) == 22, f"risultati attesi 22, ottenuti {len(risultati)}")

    vocali = [r for r in risultati if r.get("f0_mean_hz") is not None]
    require(len(vocali) >= 20,
            f"il segnale sintetico e' vocalico: F0 atteso in quasi tutti i "
            f"segmenti, misurati {len(vocali)} fra {len(risultati)}")

    f0 = [r["f0_mean_hz"] for r in vocali]
    require(min(f0) >= 100 and max(f0) <= 200,
            f"il tono e' a 140 Hz piu' o meno: F0 fuori intervallo "
            f"({min(f0):.0f}-{max(f0):.0f}) significa che il worker ha "
            f"analizzato altro")


@check
def il_percorso_breve_usa_l_audio() -> None:
    """Sotto i 20 segmenti si va in sequenziale, e l'audio c'e'.

    Il caricamento dell'audio si e' spostato dentro il ramo sequenziale:
    se l'avesse dimenticato, il percorso breve lavorerebbe con None e
    restituirebbe segmenti vuoti senza dirlo.
    """
    tmp = Path(tempfile.mkdtemp())
    wav = _fake_wav(tmp / "c.wav", seconds=12.0)
    risultati = ProsodyAnalyzer(_cfg()).analyze(_segments(n=5), wav)
    vocali = [r for r in risultati if r.get("f0_mean_hz") is not None]
    require(len(vocali) == 5,
            f"tutti e 5 i segmenti attesi, misurati {len(vocali)}")


@check
def parallelo_e_sequenziale_concordano() -> None:
    """Due worker o uno non devono cambiare la risposta."""
    tmp = Path(tempfile.mkdtemp())
    wav = _fake_wav(tmp / "d.wav")
    segs = _segments()

    seq = ProsodyAnalyzer(_cfg()).analyze(segs, wav)
    par = _con_worker_finti(
        lambda: ProsodyAnalyzer(_cfg()).analyze(segs, wav))

    require(len(seq) == len(par) == len(segs), "stesso numero di risultati")
    for a, b in zip(seq, par):
        require(a["idx"] == b["idx"], "stesso ordine")
        if a.get("f0_mean_hz") is None or b.get("f0_mean_hz") is None:
            require(a.get("f0_mean_hz") is None and b.get("f0_mean_hz") is None,
                    f"segmento {a['idx']}: F0 presente da una parte e no "
                    f"dall'altra")
            continue
        require(abs(a["f0_mean_hz"] - b["f0_mean_hz"]) < 0.5,
                f"segmento {a['idx']}: F0 diverso fra i due percorsi")


def main() -> int:
    try:
        import soundfile  # noqa: F401
    except ImportError:
        print("soundfile non presente: prova saltata")
        return 0

    passed = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            passed += 1
            print(f"  ok  {name}")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} - {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} - {type(exc).__name__}: {exc}")

    print(f"\n{passed}/{len(CHECKS)} superati")
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
