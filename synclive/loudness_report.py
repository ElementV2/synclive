# SPDX-License-Identifier: GPL-3.0-or-later
"""Rapport de niveaux de l'onglet Normalisation, pour l'ingé son : une page HTML (lisible,
imprimable en PDF) ou un tableau CSV (Excel : séparateur « ; », accents corrects)."""
from __future__ import annotations

import csv
import html
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Row:
    name: str
    path: str
    duration: float
    tracks: int
    lufs: float | None            # niveau mesuré
    peak: float | None            # crête vraie
    lra: float | None
    gain: float | None            # gain appliqué / prévu
    limited: float | None         # réduction maximale du limiteur
    expected: float | None        # niveau attendu après
    after_lufs: float | None      # mesuré sur le fichier écrit
    after_peak: float | None
    output: str
    state: str
    note: str = ""


def _n(v: float | None, dec: int = 1) -> str:
    if v is None or not math.isfinite(v):
        return ""
    return f"{v:.{dec}f}".replace(".", ",").replace("-", "−")


def _c(v: float | None, dec: int = 1) -> str:
    """Nombre pour Excel (réglages français) : virgule décimale, signe moins ordinaire."""
    if v is None or not math.isfinite(v):
        return ""
    return f"{v:.{dec}f}".replace(".", ",")


def _dur(sec: float) -> str:
    if not sec or sec <= 0:
        return ""
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def write_csv(rows: list[Row], path: Path, target_lufs: float, target_peak: float) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:      # BOM : Excel lit les accents
        w = csv.writer(f, delimiter=";")
        w.writerow(["Fichier", "Durée", "Pistes son", "Niveau avant (LUFS)", "Crête avant (dBTP)",
                    "Dynamique (LU)", "Cible (LUFS)", "Crête max (dBTP)", "Gain (dB)", "Limiteur (dB)",
                    "Niveau attendu (LUFS)", "Niveau après (LUFS)", "Crête après (dBTP)", "Fichier écrit",
                    "État", "Remarque", "Chemin"])
        for r in rows:
            w.writerow([r.name, _dur(r.duration), r.tracks, _c(r.lufs), _c(r.peak), _c(r.lra),
                        _c(target_lufs), _c(target_peak), _c(r.gain), _c(r.limited), _c(r.expected),
                        _c(r.after_lufs), _c(r.after_peak), r.output, r.state, r.note, r.path])


def write_html(rows: list[Row], path: Path, target_lufs: float, target_peak: float, preset: str) -> None:
    lv = [r.lufs for r in rows if r.lufs is not None and math.isfinite(r.lufs)]
    after = [r.after_lufs if r.after_lufs is not None else r.expected for r in rows
             if (r.after_lufs if r.after_lufs is not None else r.expected) is not None]
    spread = (max(lv) - min(lv)) if len(lv) >= 2 else None
    spread_after = (max(after) - min(after)) if len(after) >= 2 else None
    done = sum(1 for r in rows if r.after_lufs is not None)

    def bar(r: Row) -> str:
        """Écart à la cible : trait de -20 à +20 LU, cible au milieu."""
        if r.lufs is None or not math.isfinite(r.lufs):
            return ""
        def x(v):
            return 50 + max(-20.0, min(20.0, v - target_lufs)) * 2.5
        a = x(r.lufs)
        col = "#2da44e" if abs(r.lufs - target_lufs) <= 1 else ("#bf8700" if abs(r.lufs - target_lufs) <= 3 else "#cf222e")
        out = (f'<div class="bar"><div class="axis"></div><div class="tgt"></div>'
               f'<div class="seg" style="left:{min(a, 50):.1f}%;width:{abs(a - 50):.1f}%;background:{col}"></div>'
               f'<div class="dot before" style="left:{a:.1f}%"></div>')
        v = r.after_lufs if r.after_lufs is not None else None
        if v is not None and math.isfinite(v):
            out += f'<div class="dot after" style="left:{x(v):.1f}%"></div>'
        return out + "</div>"

    trs = []
    for r in rows:
        ok = (r.after_lufs is not None and abs(r.after_lufs - target_lufs) <= 0.5
              and r.after_peak is not None and r.after_peak <= target_peak + 0.05)
        trs.append(
            "<tr>"
            f'<td class="name" title="{html.escape(r.path)}">{html.escape(r.name)}</td>'
            f"<td>{_dur(r.duration)}</td>"
            f"<td>{_n(r.lufs)}</td><td>{_n(r.peak)}</td><td>{_n(r.lra)}</td>"
            f"<td>{bar(r)}</td>"
            f"<td>{_n(r.gain)}{(' · lim. −' + _n(r.limited)) if r.limited and r.limited >= 0.05 else ''}</td>"
            f'<td class="{"ok" if ok else ""}">{_n(r.after_lufs) or _n(r.expected) + (" (prévu)" if r.expected is not None else "")}</td>'
            f"<td>{_n(r.after_peak)}</td>"
            f"<td>{html.escape(r.state)}{('<br><small>' + html.escape(r.note) + '</small>') if r.note else ''}</td>"
            "</tr>")
    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    doc = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Rapport de niveaux — SyncLive</title>
<style>
body {{ font-family: "Segoe UI", Arial, sans-serif; color:#1f2328; margin:28px; font-size:13px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .sub {{ color:#656d76; margin-bottom:18px; }}
.cards {{ display:flex; gap:12px; margin-bottom:18px; flex-wrap:wrap; }}
.card {{ border:1px solid #d0d7de; border-radius:8px; padding:10px 14px; min-width:150px; }}
.card b {{ display:block; font-size:20px; }} .card span {{ color:#656d76; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ border-bottom:1px solid #d8dee4; padding:6px 8px; text-align:right; vertical-align:middle; }}
th {{ background:#f6f8fa; color:#424a53; font-weight:600; }}
td.name, th.name {{ text-align:left; max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
td.ok {{ color:#1a7f37; font-weight:600; }} small {{ color:#656d76; }}
th:last-child, td:last-child {{ text-align:left; max-width:380px; }}
.bar {{ position:relative; width:180px; height:14px; margin-left:auto; }}
.axis {{ position:absolute; left:0; right:0; top:6px; height:2px; background:#d0d7de; }}
.tgt {{ position:absolute; left:50%; top:0; width:2px; height:14px; background:#fb8500; }}
.seg {{ position:absolute; top:5px; height:4px; border-radius:2px; }}
.dot {{ position:absolute; top:2px; width:10px; height:10px; margin-left:-5px; border-radius:50%; }}
.before {{ background:#8c959f; }} .after {{ background:#2da44e; }}
.legend {{ color:#656d76; margin-top:10px; }}
@media print {{ body {{ margin:10mm; }} }}
</style></head><body>
<h1>Rapport de niveaux</h1>
<div class="sub">SyncLive · {stamp} · cible {html.escape(preset)} : {_n(target_lufs)} LUFS, crête max {_n(target_peak)} dBTP
 (mesure EBU R128 / ITU-R BS.1770)</div>
<div class="cards">
<div class="card"><b>{len(rows)}</b><span>fichier{'s' if len(rows) > 1 else ''}</span></div>
<div class="card"><b>{_n(spread) + ' LU' if spread is not None else '—'}</b><span>écart de volume avant</span></div>
<div class="card"><b>{_n(spread_after) + ' LU' if spread_after is not None else '—'}</b><span>écart après{' (prévu)' if done < len(rows) else ''}</span></div>
<div class="card"><b>{done}</b><span>fichier{'s' if done > 1 else ''} écrit{'s' if done > 1 else ''}</span></div>
</div>
<table><thead><tr><th class="name">Fichier</th><th>Durée</th><th>Niveau (LUFS)</th><th>Crête (dBTP)</th>
<th>Dynamique (LU)</th><th>Écart à la cible</th><th>Correction (dB)</th><th>Après (LUFS)</th>
<th>Crête après (dBTP)</th><th>État</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<div class="legend">Écart à la cible : de −20 à +20 LU, trait orange = cible, point gris = niveau du fichier, point vert =
niveau du fichier écrit (mesuré). « Après » : mesuré sur le fichier écrit, ou prévu s'il n'est pas encore écrit.</div>
</body></html>"""
    path.write_text(doc, encoding="utf-8")
