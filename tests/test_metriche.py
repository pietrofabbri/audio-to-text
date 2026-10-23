"""
Test delle metriche per il pannello e della pagina che le mostra.

    python tests/test_metriche.py

Una giornata sintetica con due registrazioni, un buco, due voci. I numeri
si verificano a mano: e' il modo di accorgersi se la maschera di
copertura, i turni o le quote cambiano significato.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.metriche import SOGGETTO, calcola_giorno, mattr, scrivi  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _seg(i, voce, a, b, testo, f0=150.0):
    return {"idx": i, "speaker": voce, "text": testo, "quality": "ok",
            "day_sec_start": a, "day_sec_end": b,
            "prosody": {"f0_mean_hz": f0, "f0_std_hz": 20.0,
                        "intensity_mean_db": 65.0,
                        "speech_rate_syl_per_sec": 4.0, "pause_ratio": 0.1}}


def _giornata(tmp: Path) -> Path:
    """10:00–11:00 e 11:30–12:00; Pietro e un'altra voce."""
    d = tmp / "giorni" / "2026-10-06"
    d.mkdir(parents=True)
    manifesto = {
        "day": "2026-10-06",
        "totals": {"speech_sec": 1200.0, "first_start": "2026-10-06T10:00:00",
                   "last_end": "2026-10-06T12:00:00",
                   "quality": {"ok": 4, "low": 0, "unreliable": 0}},
        "blocks": [{}, {}],
        "sessions": [
            {"start": "2026-10-06T10:00:00", "end": "2026-10-06T11:00:00"},
            {"start": "2026-10-06T11:30:00", "end": "2026-10-06T12:00:00"},
        ],
        "speakers": {SOGGETTO: {"seconds": 900.0, "words": 20},
                     "GLOBAL_009": {"seconds": 300.0, "words": 8}},
        "speaker_names": {SOGGETTO: "Pietro"},
    }
    (d / "giorno.json").write_text(json.dumps(manifesto), encoding="utf-8")
    h10 = 36000
    segmenti = [
        # Conversazione 1: apre Pietro, chiude l'altra voce.
        _seg(0, SOGGETTO, h10 + 0, h10 + 600, "Io vado. Tu vieni? Cioè non so."),
        _seg(1, "GLOBAL_009", h10 + 610, h10 + 900, "Noi veniamo."),
        # Conversazione 2 (dopo un'ora): Pietro da solo.
        _seg(2, SOGGETTO, h10 + 5400, h10 + 5700, "Allora, quindi, niente."),
    ]
    (d / "segments.jsonl").write_text(
        "\n".join(json.dumps(s) for s in segmenti) + "\n", encoding="utf-8")
    (d / "tokens.jsonl").write_text(
        "\n".join(json.dumps({"asr_prob": p}) for p in (0.9, 0.3, 1.0, 0.8)) + "\n",
        encoding="utf-8")
    return d


def copertura_e_silenzio(tmp: Path) -> None:
    m = calcola_giorno(_giornata(tmp))
    c = m["copertura"]
    require(c["registrato_ore"] == 1.5, f"un'ora e mezza: {c}")
    require(c["intervalli"] == [[36000, 39600], [41400, 43200]], f"intervalli: {c}")
    require(not m["valido"], "sotto le tre ore il giorno non e' valido")
    sil = m["silenzio"]
    # 10:00–10:15 Pietro solo, 10:15–10:25 con altri (minuti con l'altra
    # voce), il resto silenzio; 11:30–11:35 Pietro solo.
    require(abs(sil["solo_pietro_min"] - 15.0) < 0.6, f"solo Pietro: {sil}")
    require(abs(sil["solo_pietro_min"] + sil["con_altri_min"] + sil["silenzio_min"] - 90) < 0.1,
            f"le tre parti fanno il registrato: {sil}")
    ore = {o["ora"]: o for o in m["ore"]}
    require(set(ore) == {10, 11}, f"ore registrate: {sorted(ore)}")
    require(abs(ore[11]["registrato_min"] - 30) < 0.1, f"alle 11 mezz'ora: {ore[11]}")


def relazioni_e_parole(tmp: Path) -> None:
    m = calcola_giorno(_giornata(tmp))
    r = m["relazioni"]
    require(r["quota_parola_pietro"] == 0.75, f"900 su 1200 secondi: {r}")
    require(r["conversazioni"] == 1 and r["apre_pietro_quota"] == 1.0
            and r["chiude_pietro_quota"] == 0.0,
            f"una conversazione a due voci, aperta da Pietro: {r}")
    require("elenco" in r and r["elenco"][0]["voci"] == [SOGGETTO, "GLOBAL_009"],
            f"elenco: {r['elenco']}")
    p = m["pietro"]["parole"]
    require(p["frasi"] == 4 and p["domande_quota"] == 0.25, f"una domanda su 4: {p}")
    require(p["intercalari"] == {"cioè": 1, "allora": 1, "quindi": 1, "niente": 1},
            f"intercalari solo di Pietro: {p['intercalari']}")
    require(p["io_su_io_noi"] == 1.0, f"il «noi» e' dell'altra voce: {p}")
    q = m["qualita"]
    require(q["asr_prob_media"] == 0.75 and q["parole_incerte_quota"] == 0.25, f"qualita': {q}")
    require(m["limiti"]["testo"].startswith("grezzo"), "testo non corretto dichiarato")


def mattr_robusto() -> None:
    require(mattr(["a"] * 10) is None, "testo corto: nessun valore")
    require(mattr(["a"] * 60) == round(1 / 50, 4), "tutto uguale: 1/50")
    require(mattr([str(i) for i in range(60)]) == 1.0, "tutto diverso: 1")


def pagina_con_i_dati_dentro(tmp: Path) -> None:
    """La pagina incorpora i dati e non porta l'elenco delle conversazioni."""
    sys.path.insert(0, str(HERE.parent / "pannello"))
    from costruisci import costruisci
    _giornata(tmp)
    scrivi(tmp / "giorni", tmp / "metriche")
    html = costruisci(tmp / "metriche", tmp / "sito" / "index.html").read_text(encoding="utf-8")
    require("__DATI__" not in html and "2026-10-06" in html, "dati incorporati")
    require('"elenco"' not in html, "l'elenco delle conversazioni resta fuori")
    require("<script src" not in html and "<link" not in html,
            "nessun file esterno: StatiCrypt cifra solo la pagina")
    require(html.count("</script>") == 2, "nessuna chiusura di script nei dati")



def conversazioni_per_giorno(tmp: Path) -> None:
    """Pagine dei giorni: testo, voci, livelli rispetto alla voce abituale."""
    sys.path.insert(0, str(HERE.parent / "pannello"))
    sys.path.insert(0, str(HERE.parent))
    from pannello.conversazioni import basi_per_voce, dati_giorno, livello
    from costruisci import costruisci
    d = _giornata(tmp)
    # Venticinque frasi di Pietro a 120 Hz e una a 200 Hz: la base e' 120,
    # la frase acuta deve risultare «molto più alta» (+2).
    seg = [_seg(10 + i, SOGGETTO, 50000 + i * 10, 50000 + i * 10 + 5, "frase", f0=118 + (i % 5))
           for i in range(25)]
    seg.append(_seg(40, SOGGETTO, 50400, 50405, "acuta", f0=200))
    with (d / "segments.jsonl").open("a", encoding="utf-8") as f:
        for s in seg:
            f.write(json.dumps(s) + "\n")
    basi = basi_per_voce(tmp / "giorni")
    require(SOGGETTO in basi and "tono" in basi[SOGGETTO], f"base di Pietro: {basi}")
    require("GLOBAL_009" not in basi, "poche frasi: nessuna base, stile neutro")
    require(livello(200, basi[SOGGETTO]["tono"]) == 2, "frase acuta: +2")
    require(livello(120, basi[SOGGETTO]["tono"]) == 0, "frase abituale: 0")
    dati = dati_giorno(d, basi)
    require(dati["voci"][0]["nome"] == "Pietro", "Pietro per primo")
    require(len(dati["conversazioni"]) == 4, f"quattro conversazioni (la frase acuta e' staccata): {len(dati['conversazioni'])}")
    acuta = [f for f in dati["frasi"] if f[2] == "acuta"][0]
    require(acuta[3] == 2, f"tono della frase acuta: {acuta}")
    scrivi(tmp / "giorni", tmp / "metriche")
    costruisci(tmp / "metriche", tmp / "sito" / "index.html", tmp / "giorni")
    pagina = (tmp / "sito" / "giorni" / "2026-10-06.html").read_text(encoding="utf-8")
    indice = (tmp / "sito" / "index.html").read_text(encoding="utf-8")
    require("__DATI__" not in pagina and "Noi veniamo." in pagina, "frasi nella pagina")
    require("<script src" not in pagina and "<link" not in pagina, "nessun file esterno")
    require(pagina.count("</script>") == 2, "nessuna chiusura di script nei dati")
    require('"conversazioni":["2026-10-06"]' in indice, "il pannello sa quali giorni hanno pagina")


def iniziali_per_i_nomi(tmp: Path) -> None:
    """Nomi non in elenco -> iniziali; elenco e parole comuni restano."""
    sys.path.insert(0, str(HERE.parent))
    from pannello.iniziali import Iniziali
    an = Iniziali(["Pietro", "Rita Fantinati", "Ferrara"],
                  {"allora", "no", "si", "telefono"},
                  nomi_voci=["Ilyas Zappaterra", "Rita Fantinati"])
    require(an.nome("Ilyas Zappaterra") == "I. Z.", an.nome("Ilyas Zappaterra"))
    require(an.nome("Rita Fantinati") == "Rita Fantinati", "adulto in elenco")
    require(an.nome("GLOBAL_055") == "GLOBAL_055", "pseudonimo invariato")
    t = an.testo("Allora Gabriele, no. Telefono a Rita, poi dell'Anna e Zappaterra a Ferrara.")
    require(t == "Allora G., no. Telefono a Rita, poi dell'A. e Z. a Ferrara.", t)


def pagine_con_iniziali(tmp: Path) -> None:
    """Pannello e pagine dei giorni usano le iniziali; l'elenco le evita."""
    sys.path.insert(0, str(HERE.parent / "pannello"))
    sys.path.insert(0, str(HERE.parent))
    from costruisci import costruisci
    d = _giornata(tmp)
    m = json.loads((d / "giorno.json").read_text(encoding="utf-8"))
    m["speaker_names"]["GLOBAL_009"] = "Yousef Bianchi"
    (d / "giorno.json").write_text(json.dumps(m), encoding="utf-8")
    with (d / "segments.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(_seg(9, SOGGETTO, 60000, 60005, "Yousef c'è, Marta no.")) + "\n")
    scrivi(tmp / "giorni", tmp / "metriche")
    costruisci(tmp / "metriche", tmp / "sito" / "index.html", tmp / "giorni")
    pagina = (tmp / "sito" / "giorni" / "2026-10-06.html").read_text(encoding="utf-8")
    indice = (tmp / "sito" / "index.html").read_text(encoding="utf-8")
    for testo in (pagina, indice):
        require("Yousef" not in testo and "Bianchi" not in testo, "nome del minorenne fuori")
    require("Y. B." in pagina and "Y. B." in indice, "iniziali presenti")
    require("Y. c'è, M. no." in pagina, "iniziali nel testo")
    (tmp / "pannello").mkdir()
    (tmp / "pannello" / "nomi_in_chiaro.txt").write_text("# adulti\nMarta\n", encoding="utf-8")
    costruisci(tmp / "metriche", tmp / "sito" / "index.html", tmp / "giorni")
    pagina = (tmp / "sito" / "giorni" / "2026-10-06.html").read_text(encoding="utf-8")
    require("Y. c'è, Marta no." in pagina, "un nome in elenco resta intero")
    costruisci(tmp / "metriche", tmp / "sito2" / "index.html", tmp / "giorni", conversazioni=False)
    require(not (tmp / "sito2" / "giorni").exists(), "senza conversazioni nessuna pagina dei giorni")
    require("Yousef" not in (tmp / "sito2" / "index.html").read_text(encoding="utf-8"),
            "anche senza conversazioni il pannello usa le iniziali")

CHECKS = [
    ("copertura e silenzio sulla maschera dei minuti", copertura_e_silenzio),
    ("relazioni, marcatori e qualita'", relazioni_e_parole),
    ("MATTR", mattr_robusto),
    ("la pagina ha i dati dentro e niente fuori", pagina_con_i_dati_dentro),
    ("le pagine dei giorni: testo, voci, stile", conversazioni_per_giorno),
    ("iniziali per i nomi non in elenco", iniziali_per_i_nomi),
    ("pannello e pagine con le iniziali", pagine_con_iniziali),
]


def main() -> int:
    falliti = []
    for nome, fn in CHECKS:
        try:
            if fn.__code__.co_argcount:
                with TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"  ok  {nome}")
        except Exception as exc:  # noqa: BLE001
            falliti.append(nome)
            print(f"  KO  {nome} - {type(exc).__name__}: {exc}")
    print(f"\n{len(CHECKS) - len(falliti)}/{len(CHECKS)} superati")
    return 1 if falliti else 0


if __name__ == "__main__":
    raise SystemExit(main())
