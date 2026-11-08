#!/usr/bin/env python3
"""Ricalcola i centroidi di data/speakers_db.json dagli embedding salvati nei checkpoint (media pesata per secondi). Senza --apply mostra solo il confronto; con --apply salva e fa un backup."""
import json,sys,shutil,datetime
import numpy as np
from pathlib import Path
sys.path.insert(0,'.')
from core.speakers_merge import cosine
APPLY='--apply' in sys.argv
p=Path('data/speakers_db.json'); data=json.load(open(p)); sp=data['speakers']
cache={}
def emb(stem,local):
    if stem not in cache:
        f=Path('output')/stem/f'{stem}.checkpoint.json'
        cache[stem]=json.load(open(f)).get('speaker_embeddings',{}) if f.exists() else {}
    return cache[stem].get(local)
old={g:list(r['centroid']) for g,r in sp.items() if r.get('centroid')}
new={};miss=0;used=0
for g,r in sp.items():
    acc=None;W=0
    for k,s in r.get('sessions',{}).items():
        v=emb(s['stem'],s['local_speaker'])
        if v is None or (r.get('centroid') and len(v)!=len(r['centroid'])): miss+=1; continue
        w=max(s['seconds'],1.0); v=np.asarray(v,dtype=np.float64)
        acc=v*w if acc is None else acc+v*w; W+=w; used+=1
    if acc is not None: new[g]=list(map(float,acc/W))
print(f"contributi usati {used}, mancanti {miss}; voci ricalcolate {len(new)}/{len(sp)}")
nm=lambda g: sp[g].get('name') or g
for g in sorted(new, key=lambda g:-sp[g].get('total_seconds',0))[:12]:
    print(f"  {nm(g):22s} {sp[g]['total_seconds']/60:6.0f} min  coseno vecchio-nuovo {cosine(old[g],new[g]):.2f}")
# quanto ogni contributo e' simile al proprio centroide, prima e dopo
def fit(C):
    xs=[cosine(emb(s['stem'],s['local_speaker']),C[g]) for g,r in sp.items() if g in C for s in r.get('sessions',{}).values() if emb(s['stem'],s['local_speaker']) is not None]
    return np.mean(xs), np.percentile(xs,10)
C2={**old,**new}
print("somiglianza media sessione->propria voce: prima %.3f (10%%: %.3f)  dopo %.3f (10%%: %.3f)"%(*fit(old),*fit(C2)))
if APPLY:
    b=p.with_name(f"speakers_db.backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"); shutil.copy2(p,b)
    for g,c in new.items(): sp[g]['centroid']=c
    json.dump(data,open(p,'w'),ensure_ascii=False,indent=2); print("salvato; backup:",b)
