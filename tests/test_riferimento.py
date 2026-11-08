"""Test del set di riferimento: WER e attribuzione si verificano a mano.

    python tests/test_riferimento.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.riferimento import (Parola, confronta, leggi_testo_con_voci,  # noqa: E402
                              normalizza, scegli_finestra, testo_con_voci)


def test_normalizza():
    assert normalizza("C'è, l'Università!") == ["c", "è", "l", "università"]


def test_wer_a_mano():
    rif = leggi_testo_con_voci("[Pietro] oggi andiamo al mare\n[Carola] va bene")
    ipo = leggi_testo_con_voci("[Pietro] oggi andiamo a mare\n[Pietro] va bene grazie")
    c = confronta(rif, ipo)
    # 6 parole: «al»→«a» sostituita, «grazie» inserita: 2/6.
    assert c["sostituzioni"] == 1 and c["inserimenti"] == 1 and c["cancellazioni"] == 0, c
    assert c["wer"] == round(2 / 6, 4), c
    # Giuste 5: «oggi andiamo mare» di Pietro (giuste), «va bene» date a Pietro ma di Carola.
    assert c["parole_giuste"] == 5 and c["attribuzione"] == round(3 / 5, 4), c


def test_formato_e_commenti():
    testo = "# commento\n[Myriam] ci sono vari tavoli\ne il problema\n[Alia] io volevo"
    v = leggi_testo_con_voci(testo)
    assert v[0] == ("Myriam", "ci") and v[6] == ("Myriam", "problema") and v[-1][0] == "Alia", v


def test_finestra_e_turni():
    pp = [Parola(i, i + 0.5, f"w{i}", "GLOBAL_001" if i < 70 else "GLOBAL_002")
          for i in range(0, 120)]
    t = scegli_finestra(pp, durata=60, passo=15)
    assert t is not None and t <= 70 < t + 60, t        # la finestra con due voci
    txt = testo_con_voci(pp[68:72], {"GLOBAL_001": "Pietro"})
    assert txt == "[Pietro] w68 w69\n[GLOBAL_002] w70 w71\n", txt


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
