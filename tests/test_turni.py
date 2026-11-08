"""Test della divisione dei segmenti in turni.

    python tests/test_turni.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.turni import dividi  # noqa: E402

P, C = "GLOBAL_001", "GLOBAL_165"


def _parole(spec):
    return [(w, v, float(i)) for i, (w, v) in enumerate(spec)]


def test_un_segmento_di_una_voce_resta_intero():
    t = dividi("Ciao a tutti.", _parole([("Ciao", P), ("a", P), ("tutti.", P)]), P, 0, 3)
    assert t == [{"speaker": P, "text": "Ciao a tutti.", "start": 0, "end": 3}], t


def test_due_voci_due_turni():
    parole = _parole([("Posso", C), ("dire?", C), ("Certo,", P), ("Carola,", P), ("dimmi.", P)])
    t = dividi("Posso dire? Certo, Carola, dimmi.", parole, P, 10, 15)
    assert [x["speaker"] for x in t] == [C, P], t
    assert t[0]["text"] == "Posso dire?" and t[1]["text"] == "Certo, Carola, dimmi.", t
    assert t[1]["start"] == 2.0 and t[0]["end"] == 2.0 and t[1]["end"] == 15, t


def test_testo_corretto_si_allinea():
    parole = _parole([("Posso", C), ("dire", C), ("una", C), ("cosa?", C),
                      ("Sì,", P), ("certo", P), ("Carla.", P)])
    t = dividi("Posso dire una cosa? Sì, certo Carola.", parole, P, 0, 7)
    assert [(x["speaker"], x["text"]) for x in t] == [(C, "Posso dire una cosa?"), (P, "Sì, certo Carola.")], t


def test_una_parola_isolata_si_riassorbe():
    parole = _parole([("allora", P), ("ragazzi", P), ("oggi", C), ("facciamo", P), ("PHP", P)])
    t = dividi("allora ragazzi oggi facciamo PHP", parole, P, 0, 5)
    assert len(t) == 1 and t[0]["speaker"] == P, t


def test_senza_parole_resta_il_segmento():
    t = dividi("testo", [], C, 0, 1)
    assert t[0]["speaker"] == C and t[0]["text"] == "testo"


def main() -> int:
    falliti = 0
    for k, fn in sorted(globals().items()):
        if k.startswith("test_"):
            try:
                fn(); print(f"ok    {k}")
            except AssertionError as e:
                falliti += 1; print(f"FAIL  {k}: {e}")
    return 1 if falliti else 0


if __name__ == "__main__":
    raise SystemExit(main())
