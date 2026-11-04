"""Usage : python -m synclive.cli video.mp4 [-o dossier]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analysis, export


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mesure du décalage son/image par caméra (claps).")
    ap.add_argument("video")
    ap.add_argument("-o", "--out", help="dossier de sortie (défaut : <video>_synclive)")
    ap.add_argument("--max-offset", type=float, default=600, help="décalage max cherché en ms")
    ap.add_argument("--audio-track", type=int, default=1, help="numéro de piste audio (1 = première)")
    ap.add_argument("--sensitivity", type=float, default=1.0, help="sensibilité audio (>1 = claps plus faibles)")
    ap.add_argument("--group-threshold", type=float, default=0.07)
    ap.add_argument("--cut-threshold", type=float, default=0.05)
    ap.add_argument("--no-hands", action="store_true", help="désactiver la détection des mains")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    st = analysis.Settings(max_offset=a.max_offset / 1000, audio_track=a.audio_track - 1,
                           audio_sensitivity=a.sensitivity, group_threshold=a.group_threshold,
                           cut_threshold=a.cut_threshold, use_hands=not a.no_hands)
    last = [""]

    def prog(stage: str, f: float) -> None:
        if stage != last[0]:
            print(f"[{f * 100:5.1f} %] {stage}", file=sys.stderr)
            last[0] = stage

    an = analysis.run(a.video, st, prog)
    fps = an.info.fps_float
    print(f"\n{Path(a.video).name} : {an.info.width}x{an.info.height} @ {an.info.fps_label} i/s, "
          f"{len(an.shots)} plan(s), {len(an.claps)} clap(s) audio, mains : {an.hands_status}")
    cams = an.cameras()
    for cam in cams:
        head, why = export.status(cam, fps)
        print(f"  {cam.name:16s} {head:38s} ({why})  {cam.n_used}/{cam.n_total}  confiance {cam.confidence}")
    plan = export.correction_plan(cams, fps)
    print("\nPLAN DE CORRECTION")
    print("  " + export.fmt_audio_delay(plan))
    for f in plan.fixes:
        print(f"  {f.cam.name:16s} {export.fmt_fix(f, fps)}")
    out = a.out or str(Path(a.video).with_suffix("")) + "_synclive"
    files = export.export_all(an, out)
    print(f"\nExport : {Path(out).resolve()} ({len(files)} fichiers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
