"""
Test della revisione delle voci: estratti da ascoltare e voci nuove.

    python tests/test_voice_review.py

Niente modelli e niente rete. L'audio e' un tono sintetico generato con
ffmpeg: serve solo a verificare che il taglio esca e abbia la durata
giusta. Se ffmpeg manca, i test sul taglio si dichiarano saltati.

Cosa si protegge, e perche'.

  - Le parole portano l'etichetta del parlante **prima** della fusione
    dei frammenti. Se lo strumento non segue `speaker_merge.json`, gli
    estratti di una voce perdono proprio i pezzi che la fusione le ha
    restituito — o peggio, includono parole di qualcun altro.
  - Un estratto gia' tagliato deve sopravvivere alla cancellazione
    dell'audio originale: e' la ragione per cui la notte li taglia
    subito.
  - L'elenco delle voci nuove non deve riproporre una voce gia' vista,
    nominata o troppo breve: un elenco che non si svuota mai smette di
    essere guardato.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.speaker_db import SpeakerDB  # noqa: E402
from core.voice_review import (  # noqa: E402
    Estratto, _risolvi, candidati_per_voce, formatta_promemoria,
    ordina_per_varieta, prepara_ascolto, prepara_revisione_notturna,
    trova_audio, voci_da_rivedere,
)


class Failure(Exception):
    pass


class Skip(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _parole(testo: str, inizio: float, speaker: str, passo: float = 0.5,
            prob: float = 0.95) -> list[dict]:
    out = []
    t = inizio
    for w in testo.split():
        out.append({"word": " " + w, "start": round(t, 2),
                    "end": round(t + passo - 0.05, 2), "prob": prob,
                    "speaker": speaker})
        t += passo
    return out


def _sessione(root: Path, nome: str, segmenti: list[dict],
              fusioni: dict | None = None) -> None:
    d = root / nome
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.json").write_text(json.dumps(
        {"meta": {"stem": nome}, "segments": segmenti}), encoding="utf-8")
    if fusioni is not None:
        (d / "speaker_merge.json").write_text(json.dumps(
            {"merged": fusioni}), encoding="utf-8")


def _seg(speaker: str, locale: str, parole: list[dict],
         quality: str = "ok") -> dict:
    return {"speaker": speaker, "speaker_local": locale, "quality": quality,
            "start": parole[0]["start"], "end": parole[-1]["end"],
            "text": "".join(w["word"] for w in parole).strip(),
            "words": parole}


FRASE = "uno due tre quattro cinque sei sette otto nove dieci"   # 5 s


# ----------------------------------------------------------------------

def segue_la_catena_delle_fusioni() -> None:
    """SPEAKER_04 -> 05 -> 03: le parole di 04 sono di chi e' 03."""
    f = {"SPEAKER_04": "SPEAKER_05", "SPEAKER_05": "SPEAKER_03"}
    require(_risolvi("SPEAKER_04", f) == "SPEAKER_03", "catena non seguita")
    require(_risolvi("SPEAKER_01", f) == "SPEAKER_01", "un'etichetta libera resta")
    require(_risolvi("A", {"A": "B", "B": "A"}) in ("A", "B"),
            "un ciclo non deve bloccare")


def estratti_dalla_voce_giusta() -> None:
    """Le parole di un frammento fuso contano per la voce che l'ha assorbito.

    Il segmento e' di GLOBAL_046 (locale SPEAKER_03). Le sue parole
    portano SPEAKER_04, assorbito in 03: devono diventare un estratto.
    Le parole di SPEAKER_01 nello stesso segmento spezzano la corsa e
    non devono finire nell'estratto.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        parole = (_parole(FRASE, 0.0, "SPEAKER_04")
                  + _parole("ehm si", 5.0, "SPEAKER_01")
                  + _parole(FRASE, 6.0, "SPEAKER_03"))
        _sessione(out, "2026-10-05_14-47-58",
                  [_seg("GLOBAL_046", "SPEAKER_03", parole)],
                  {"SPEAKER_04": "SPEAKER_03"})
        c = candidati_per_voce("GLOBAL_046", out)
        require(len(c) == 2, f"due corse separate dall'interruzione, risultano {len(c)}")
        require(all("ehm" not in e.testo for e in c),
                "le parole di un'altra voce non devono entrare nell'estratto")
        require(c[0].testo.startswith("uno") and c[0].inizio == 0.0,
                f"la prima corsa parte da 0: {c[0]}")
        require(candidati_per_voce("GLOBAL_001", out) == [],
                "una voce assente non ha estratti")


def scarta_corti_lunghi_e_cattiva_qualita() -> None:
    """Sotto 3 s si scarta; sopra 12 s si accorcia; `unreliable` no."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        lunga = " ".join(f"p{i}" for i in range(40))          # 20 s
        _sessione(out, "2026-10-04_12-07-22", [
            _seg("GLOBAL_018", "SPEAKER_00", _parole("breve frase", 0.0, "SPEAKER_00")),
            _seg("GLOBAL_018", "SPEAKER_00", _parole(lunga, 10.0, "SPEAKER_00")),
            _seg("GLOBAL_018", "SPEAKER_00", _parole(FRASE, 40.0, "SPEAKER_00"),
                 quality="unreliable"),
        ])
        c = candidati_per_voce("GLOBAL_018", out)
        require(len(c) == 1, f"resta solo la corsa lunga, risultano {len(c)}")
        require(c[0].durata <= 12.0, f"accorciata a 12 s, dura {c[0].durata:.2f}")
        require(c[0].durata > 11.0, f"non accorciata troppo: {c[0].durata:.2f}")


def un_buco_lungo_spezza_la_corsa() -> None:
    """Una pausa oltre 0,8 s fra due parole puo' nascondere un'altra voce."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "output"
        parole = _parole(FRASE, 0.0, "SPEAKER_00") + _parole(FRASE, 7.0, "SPEAKER_00")
        _sessione(out, "2026-10-02_19-42-33", [_seg("GLOBAL_004", "SPEAKER_00", parole)])
        c = candidati_per_voce("GLOBAL_004", out)
        require(len(c) == 2, f"il buco di 2 s deve spezzare: {len(c)} corse")


def varieta_fra_sessioni() -> None:
    """I primi estratti vengono da sessioni diverse."""
    def e(sess, punti):
        return Estratto("G", sess, 0.0, punti, "x", 1.0)
    c = [e("A", 12), e("A", 11), e("A", 10), e("B", 5), e("C", 4)]
    o = ordina_per_varieta(c)
    require([x.sessione for x in o[:3]] == ["A", "B", "C"],
            f"un estratto per sessione prima di ripetere: {[x.sessione for x in o]}")
    require(len(o) == 5, "nessun candidato perso")


def trova_audio_in_sottocartelle() -> None:
    """L'originale puo' stare in archive/, input/ o input/today/."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "input" / "today").mkdir(parents=True)
        (base / "archive").mkdir()
        f = base / "input" / "today" / "2026-10-05_14-47-58.MP3"
        f.write_bytes(b"x")
        (base / "archive" / "2026-10-05_14-47-58.txt").write_text("non audio")
        trovato = trova_audio("2026-10-05_14-47-58", [base / "archive", base / "input"])
        require(trovato == f, f"atteso {f}, trovato {trovato}")
        require(trova_audio("2026-10-04_18-07-13", [base / "archive", base / "input"]) is None,
                "una sessione senza audio deve dare None")


def _tono(dest: Path, secondi: int = 30) -> None:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise Skip("ffmpeg non disponibile")
    subprocess.run([ff, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={secondi}",
                    "-b:a", "64k", str(dest)], check=True)


def _durata(f: Path) -> float:
    fp = shutil.which("ffprobe")
    if not fp:
        raise Skip("ffprobe non disponibile")
    r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    return float(r.stdout.strip())


def taglia_e_riusa_gli_estratti() -> None:
    """L'estratto esce della durata giusta, e sopravvive all'originale."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        out, arch, clips = base / "output", base / "archive", base / "clips"
        arch.mkdir()
        sess = "2026-10-05_13-46-02"
        _tono(arch / f"{sess}.MP3")
        _sessione(out, sess, [_seg("GLOBAL_035", "SPEAKER_00",
                                   _parole(FRASE, 10.0, "SPEAKER_00"))])
        pronti, mancanti = prepara_ascolto("GLOBAL_035", n=4, output_dir=out,
                                           clip_dir=clips, cartelle_audio=[arch])
        require(len(pronti) == 1 and mancanti == 0,
                f"un estratto, nessun audio mancante: {len(pronti)}, {mancanti}")
        f = pronti[0].file
        require(f.exists() and f.parent == clips, f"estratto non scritto: {f}")
        d = _durata(f)
        attesa = pronti[0].estratto.durata + 0.4
        require(abs(d - attesa) < 0.25, f"durata {d:.2f}, attesa ~{attesa:.2f}")

        # L'archivio cancella l'originale: l'estratto c'e' ancora.
        (arch / f"{sess}.MP3").unlink()
        pronti2, mancanti2 = prepara_ascolto("GLOBAL_035", n=4, output_dir=out,
                                             clip_dir=clips, cartelle_audio=[arch])
        require(len(pronti2) == 1 and pronti2[0].file == f and mancanti2 == 0,
                "l'estratto gia' tagliato deve restare utilizzabile")

        # Con --rifai e senza originale non si puo' rifare: si dichiara.
        pronti3, mancanti3 = prepara_ascolto("GLOBAL_035", n=4, output_dir=out,
                                             clip_dir=clips, cartelle_audio=[arch],
                                             rifai=True)
        require(pronti3 == [] and mancanti3 == 1,
                f"senza originale non si ritaglia: {len(pronti3)}, {mancanti3}")


def _db(tmp: Path) -> SpeakerDB:
    db = SpeakerDB(tmp / "speakers_db.json", threshold=0.78)
    voci = {
        "GLOBAL_001": ([1.0, 0.0, 0.0], 3600),   # tu, nominato
        "GLOBAL_035": ([0.0, 1.0, 0.0], 1750),   # nuova, lunga
        "GLOBAL_036": ([0.0, 0.98, 0.2], 300),   # quasi uguale alla 035
        "GLOBAL_051": ([0.0, 0.0, 1.0], 36),     # troppo breve
        "GLOBAL_050": ([0.6, 0.0, 0.8], 400),    # gia' vista, senza nome
    }
    for i, (gid, (v, sec)) in enumerate(voci.items()):
        db._register(gid, f"2026-10-05_1{i}-00-00", "SPEAKER_00", v, sec)
    db.save()
    db.set_name("GLOBAL_001", "Pietro")
    db.mark_reviewed("GLOBAL_050")
    return db


def elenco_delle_voci_nuove() -> None:
    """Solo voci senza nome, mai viste, con almeno un minuto."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(Path(tmp))
        voci = voci_da_rivedere(db)
        gids = [v["gid"] for v in voci]
        require(gids == ["GLOBAL_035", "GLOBAL_036"],
                f"attese 035 e 036 in ordine di parlato, risultano {gids}")
        v36 = voci[1]
        require(v36["vicina"] == "GLOBAL_035" and v36["somiglianza"] > 0.9,
                f"la voce piu' simile alla 036 e' la 035: {v36}")

        # La revisione sopravvive alla rilettura del DB.
        db2 = SpeakerDB(Path(tmp) / "speakers_db.json")
        require(db2.is_reviewed("GLOBAL_050") and db2.is_reviewed("GLOBAL_001"),
                "vista e nominata devono restare viste dopo il salvataggio")
        db2.mark_reviewed("GLOBAL_050", reviewed=False)
        require(not db2.is_reviewed("GLOBAL_050"), "--annulla deve funzionare")

        md = formatta_promemoria(voci, db.threshold)
        require("GLOBAL_035" in md and "GLOBAL_051" not in md,
                "il promemoria deve elencare le voci da rivedere e solo quelle")
        require("vicina alla soglia" in md,
                "la 036 e' quasi uguale alla 035: il promemoria deve dirlo")
        require("## GLOBAL_001" not in md and "## GLOBAL_050" not in md,
                "una voce nominata o gia' vista non va proposta")


def promemoria_notturno_non_solleva() -> None:
    """Senza output e senza audio la notte scrive comunque il promemoria."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        db = _db(base)
        dest = base / "output" / "voci_da_rivedere.md"
        r = prepara_revisione_notturna(db, dest, output_dir=base / "vuota",
                                       clip_dir=base / "clips")
        require(dest.exists(), "il promemoria deve essere scritto")
        require(r["voci"] == 2 and r["estratti"] == 0,
                f"due voci, nessun estratto senza trascrizioni: {r}")


def centroids_non_escono_dal_db() -> None:
    """`centroids()` restituisce copie: modificarle non tocca il DB."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(Path(tmp))
        c = db.centroids()
        c["GLOBAL_001"][0] = 99.0
        require(db.centroids()["GLOBAL_001"][0] != 99.0,
                "centroids() deve restituire copie")


CHECKS = [
    ("le fusioni dei frammenti si seguono", segue_la_catena_delle_fusioni),
    ("gli estratti sono della voce giusta", estratti_dalla_voce_giusta),
    ("corti, lunghi e inaffidabili", scarta_corti_lunghi_e_cattiva_qualita),
    ("un buco lungo spezza la corsa", un_buco_lungo_spezza_la_corsa),
    ("gli estratti vengono da sessioni diverse", varieta_fra_sessioni),
    ("l'audio si trova anche in input/today", trova_audio_in_sottocartelle),
    ("gli estratti si tagliano e sopravvivono all'originale",
     taglia_e_riusa_gli_estratti),
    ("l'elenco delle voci nuove", elenco_delle_voci_nuove),
    ("il promemoria notturno non solleva", promemoria_notturno_non_solleva),
    ("i centroidi non escono dal DB", centroids_non_escono_dal_db),
]


def main() -> int:
    passed = skipped = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            passed += 1
            print(f"  ok  {name}")
        except Skip as exc:
            skipped += 1
            print(f"  --  {name} (saltato: {exc})")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} - {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} - {type(exc).__name__}: {exc}")

    print(f"\n{passed}/{len(CHECKS)} superati" + (f", {skipped} saltati" if skipped else ""))
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
