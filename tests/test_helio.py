"""
Test delle misure Helio (export Zepp) e del loro arrivo nel pannello.

    python tests/test_helio.py

L'export sintetico riproduce le trappole trovate nell'export vero del
7/11/2026 (documentate in `core/helio.py`): battito in ora locale, SLEEP
e SPORT in UTC, SLEEP_MINUTE con la data spostata di un giorno in avanti,
minuti «24:xx» nel giorno del cambio d'ora, righe di SLEEP vuote, totale
dei passi diverso dalla somma dei minuti.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["A2T_HELIO_DIR"] = tempfile.mkdtemp(prefix="helio_test_")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "pannello"))

from core import helio as H  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _csv(cartella: Path, tipo: str, intestazione: str, righe: list[str]) -> None:
    d = cartella / tipo
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tipo}_1794054432000.csv").write_text(
        "﻿" + intestazione + "\n" + "\n".join(righe) + "\n", encoding="utf-8")


def export_finto(radice: Path, nome: str = "7086234041_1794054432703",
                 battito_notte: int = 50) -> Path:
    """Due giorni, 4 e 5 novembre 2026 (ora solare, UTC+1).

    Notte vera: 5/11 dalle 01:00 alle 07:00 locali = 00:00-06:00 UTC.
    In SLEEP_MINUTE la stessa notte e' scritta il 6/11 (trappola 2).
    """
    c = radice / nome
    battito = []
    t = datetime(2026, 11, 4, 0, 0)
    while t < datetime(2026, 11, 6, 0, 0):
        notte = datetime(2026, 11, 5, 1) <= t < datetime(2026, 11, 5, 7)
        parla = datetime(2026, 11, 5, 10) <= t < datetime(2026, 11, 5, 10, 30)
        v = battito_notte if notte else (90 if parla else 70)
        battito.append(f"{t:%Y-%m-%d},{t:%H:%M},{v}")
        t += timedelta(minutes=1)
    battito.append("2026-11-04,24:00,199")  # gia' presente come 5/11 00:00
    _csv(c, "HEARTRATE_AUTO", "date,time,heartRate", battito)
    # Passi: fra le 10:40 e le 10:59 si cammina.
    passi = [f"2026-11-05,10:{m:02d},100" for m in range(40, 60)]
    _csv(c, "ACTIVITY_MINUTE", "date,time,steps", passi)
    _csv(c, "ACTIVITY", "date,steps,distance,runDistance,calories",
         ["2026-11-04,3000,2000,0,100", "2026-11-05,2500,1800,0,90"])
    _csv(c, "SLEEP", "date,deepSleepTime,shallowSleepTime,wakeTime,start,stop,REMTime,naps",
         ["2026-11-03,0,0,0,2026-11-02 23:00:00+0000,2026-11-02 23:00:00+0000,0,",
          "2026-11-04,60,240,10,2026-11-05 00:00:00+0000,2026-11-05 06:00:00+0000,50,"])
    fasi = []
    t = datetime(2026, 11, 6, 1, 0)  # +1 giorno, come nell'export vero
    while t < datetime(2026, 11, 6, 7, 0):
        fase = "DEEP" if t.hour < 2 else ("REM" if t.hour == 6 else "LIGHT")
        fasi.append(f"{t:%Y-%m-%d},{t:%H:%M},{fase},999,")
        t += timedelta(minutes=1)
    # Un pisolino il 5/11 dalle 15:00 alle 15:20, scritto il 6/11.
    fasi += [f"2026-11-06,15:{m:02d},LIGHT,,"for m in range(20)]
    _csv(c, "SLEEP_MINUTE", "date,time,stage,hr,respiratory_rate", fasi)
    _csv(c, "SPORT", "type,startTime,sportTime(s),maxPace(/meter),minPace(/meter),"
         "distance(m),avgPace(/meter),calories(kcal)",
         ["223,2026-11-05 09:40:00+0000,1200,0.0,0.0,0.0,0.0,80.0"])
    _csv(c, "USER", "userId,gender,height,weight,nickName,avatar,birthday",
         ["1,1,183.0,81.0,nome,null,1990-01"])
    return c


def t_lettura_e_trappole(tmp: Path) -> None:
    h = H.leggi_export(export_finto(tmp))
    require(h.battito[datetime(2026, 11, 5, 0, 0)] == 70, "24:00 ha sovrascritto 00:00")
    require(len(h.notti) == 1, f"riga vuota di SLEEP non scartata: {h.notti}")
    (a, b), = h.notti
    require((a, b) == (datetime(2026, 11, 5, 1), datetime(2026, 11, 5, 7)),
            f"SLEEP non portato in ora locale: {a} {b}")
    require(h.fasi.get(datetime(2026, 11, 5, 1, 30)) == "DEEP",
            "SLEEP_MINUTE non riportato indietro di un giorno")
    require(any("spostato di -1" in x for x in h.avvisi), "spostamento non dichiarato")
    (inizio, sp), = h.sport.items()
    require(inizio == datetime(2026, 11, 5, 10, 40), f"SPORT non in ora locale: {inizio}")


def t_giorno(tmp: Path) -> None:
    h = H.leggi_export(export_finto(tmp))
    g = H.calcola_giorno(h, "2026-11-05")
    so = g["sonno"]
    require(so and so["inizio"] == "2026-11-05T01:00" and so["fine"] == "2026-11-05T07:00",
            f"notte sbagliata: {so}")
    require(so["dormito_min"] == 350 and so["battito_media"] == 50.0, f"sonno: {so}")
    require(so["fasi"][0] == [60, 120, "DEEP"], f"fasi: {so['fasi'][:2]}")
    require(H.calcola_giorno(h, "2026-11-04")["sonno"] is None,
            "la notte va al giorno della sveglia, non a quello prima")
    require(g["altri_sonni"] == [{"inizio": "15:00", "fine": "15:20", "minuti": 20}],
            f"pisolino: {g['altri_sonni']}")
    require(g["passi"]["totale"] == 2500 and sum(g["passi"]["per_ora"]) == 2000,
            "totale da ACTIVITY, distribuzione dai minuti")
    require(g["battito"]["copertura_quota"] == 1.0, "copertura del battito")
    require(len(g["battito"]["per_5min"]) == 288, "serie da 5 minuti")
    # Da sveglio: tolti notte e pisolino.
    require(70 < g["battito"]["sveglio_media"] < 72, f"sveglio: {g['battito']['sveglio_media']}")
    require(g["allenamenti"][0]["inizio"] == "10:40" and g["allenamenti"][0]["minuti"] == 20,
            "allenamento")
    testo = json.dumps(g)
    require("183" not in testo and "nome" not in testo, "dati anagrafici nel blocco")


def t_battito_e_parlato(tmp: Path) -> None:
    h = H.leggi_export(export_finto(tmp))
    # Registrato 10:00-11:00; Pietro parla 10:00-10:30, poi silenzio.
    reg = {m: 60.0 for m in range(600, 660)}
    parlato = {m: {"GLOBAL_001"} for m in range(600, 630)}
    c = H.battito_e_parlato(h, "2026-11-05", reg, parlato, "GLOBAL_001")
    require(c["tutti"]["parli"] == {"minuti": 30, "media": 90.0}, f"parli: {c['tutti']}")
    require(c["tutti"]["silenzio"]["minuti"] == 30, "silenzio")
    # Fermi: si tolgono 10:38-10:59 (passi dalle 10:40, piu' 2 minuti).
    require(c["fermo"]["silenzio"]["minuti"] == 8, f"fermo: {c['fermo']}")
    require(c["parli_meno_silenzio_fermo"] == 20.0, f"differenza: {c}")
    require(H.battito_e_parlato(None, "2026-11-05", reg, parlato, "GLOBAL_001") is None,
            "senza Helio niente corpo")


def t_piu_export(tmp: Path) -> None:
    archivio = tmp / "archivio"
    H.importa(export_finto(tmp / "a", "1_1000"), archivio)
    H.importa(export_finto(tmp / "b", "1_2000", battito_notte=55), archivio)
    h = H.carica(archivio)
    require(h.esportazioni == ["1_1000", "1_2000"], f"ordine: {h.esportazioni}")
    require(H.calcola_giorno(h, "2026-11-05")["sonno"]["battito_media"] == 55.0,
            "l'export piu' recente non ha vinto")
    require(len(h.notti) == 1, "notte doppia dopo due export")
    require((archivio / "1_1000" / "USER").is_dir(), "l'archivio non e' completo")


def t_pannello(tmp: Path) -> None:
    from costruisci import costruisci
    h = H.leggi_export(export_finto(tmp))
    met = tmp / "corpus" / "metriche"
    met.mkdir(parents=True)
    H.scrivi(h, tmp / "corpus" / "helio")
    html = costruisci(met, tmp / "sito" / "index.html").read_text(encoding="utf-8")
    blob = html.split('<script id="dati" type="application/json">')[1].split("</script>")[0]
    dati = json.loads(blob.replace("<\\/", "</"))
    require([c["giorno"] for c in dati["corpo"]] == ["2026-11-04", "2026-11-05"],
            "il pannello non ha le misure Helio")
    require("helio_assenti" in dati["limiti"], "limiti dell'Helio non dichiarati")
    require("sezioneCorpo" in html, "manca la sezione del corpo")


def t_attivazione(tmp: Path) -> None:
    # Riposo: nella notte del finto export il battito e' 50 fisso.
    h = H.leggi_export(export_finto(tmp))
    require(H.calcola_giorno(h, "2026-11-05")["sonno"]["battito_riposo_30min"] == 50.0,
            "battito a riposo")
    # Quattro giorni svegli 08-20 a 70 bpm; il quarto un'ora a 100.
    h = H.Helio()
    for d in range(1, 5):
        t = datetime(2026, 11, d, 8)
        while t < datetime(2026, 11, d, 20):
            h.battito[t] = 100 if (d == 4 and t.hour == 10) else 70
            t += timedelta(minutes=1)
    h.passi_minuto[datetime(2026, 11, 4, 12, 0)] = 30
    require(H.calcola_giorno(h, "2026-11-02")["attivazione"]["quota"] is None,
            "con due giorni il riferimento non basta")
    a3 = H.calcola_giorno(h, "2026-11-03")["attivazione"]
    require(a3["quota"] == 0.0 and a3["soglia_bpm"] == 70.0, f"giorno tipico: {a3}")
    a4 = H.calcola_giorno(h, "2026-11-04")["attivazione"]
    # 12:00-12:05 tolti dai passi (il minuto e i 5 dopo).
    require(a4["minuti_fermo"] == 720 - 6, f"minuti fermi: {a4['minuti_fermo']}")
    require(a4["minuti"] == 60 and a4["per_ora"][10] == 1.0 and a4["per_ora"][9] == 0.0,
            f"attivazione: {a4}")
    require(a4["per_ora"][3] is None, "ora senza minuti fermi")
    require(a4["sopra_riposo_bpm"] is None, "senza notte niente scarto dal riposo")


def t_legami(tmp: Path) -> None:
    # Quattro giorni svegli 08-20 a 70 bpm; il quarto, 10:00-10:29, Pietro
    # parla e il battito e' 80. Registrato 09:50-11:00.
    h = H.Helio()
    for d in range(1, 5):
        t = datetime(2026, 11, d, 8)
        while t < datetime(2026, 11, d, 20):
            h.battito[t] = 80 if (d == 4 and t.hour == 10 and t.minute < 30) else 70
            t += timedelta(minutes=1)
    h.passi_minuto[datetime(2026, 11, 4, 10, 45)] = 30
    reg = {m: 60.0 for m in range(590, 660)}
    parlato = {m: {"GLOBAL_001"} for m in range(600, 615)}
    parlato.update({m: {"GLOBAL_001", "GLOBAL_009"} for m in range(615, 630)})
    conv = [{"inizio_sec": 600 * 60, "durata_min": 30.0, "voci": ["GLOBAL_001", "GLOBAL_009"]}]
    segm = [{"day_sec_start": 600 * 60 + 5, "day_sec_end": 600 * 60 + 50,
             "prosody": {"f0_mean_hz": 120.0, "intensity_mean_db": 65.0,
                         "speech_rate_syl_per_sec": 4.5}}]
    L = H.legami(h, "2026-11-04", reg, parlato, "GLOBAL_001", conv, segm)
    c = L["classi"]
    require(c["solo_tu"]["n"] == 15 and c["dialogo"]["n"] == 15, f"classi: {c}")
    # 10:45 ha passi: tolti 10:45-10:50 (6 minuti) dai 40 di silenzio.
    require(c["silenzio"]["n"] == 40 - 6, f"silenzio: {c['silenzio']}")
    require(c["silenzio"]["s"] == 0.0 and c["solo_tu"]["s"] == 150.0, f"residui: {c}")
    require(L["persone"]["GLOBAL_009"]["n"] == 15, f"persone: {L['persone']}")
    require(L["eventi"] == 1 and L["inizio"][10] == [1, 10.0] and L["inizio"][0] == [1, 0.0],
            f"curva: {L['inizio'][:12]}")
    require(L["prosodia"] == [[10.0, 120.0, 65.0, 4.5]], f"prosodia: {L['prosodia']}")
    require(H.legami(None, "2026-11-04", reg, parlato, "GLOBAL_001", conv, segm) is None,
            "senza Helio niente legami")
    # Una conversazione senza registrazione prima non entra nelle curve.
    reg2 = {m: 60.0 for m in range(600, 660)}
    require(H.legami(h, "2026-11-04", reg2, parlato, "GLOBAL_001", conv, segm)["eventi"] == 0,
            "inizio della registrazione scambiato per inizio conversazione")


def main() -> int:
    tests = [
        ("lettura dell'export e trappole di Zepp", t_lettura_e_trappole),
        ("il giorno: notte, fasi, pisolino, passi", t_giorno),
        ("battito quando si parla, da fermi", t_battito_e_parlato),
        ("piu' export: vince il piu' recente", t_piu_export),
        ("battito a riposo e attivazione da fermo", t_attivazione),
        ("parlato e battito minuto per minuto", t_legami),
        ("le misure arrivano nel pannello", t_pannello),
    ]
    ok = 0
    for nome, f in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                f(Path(d))
                print(f"  ok  {nome}")
                ok += 1
            except Failure as e:
                print(f"  NO  {nome}: {e}")
    print(f"\n{ok}/{len(tests)} superati")
    return 0 if ok == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
