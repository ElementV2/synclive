# SPDX-License-Identifier: GPL-3.0-or-later
"""Journal (erreurs retrouvables depuis l'app) et rapport de niveaux (HTML / CSV pour Excel)."""
import csv
import importlib
import logging
import threading
import time
from pathlib import Path


def test_log_captures_errors_and_thread_crashes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from synclive import applog
    importlib.reload(applog)                       # journal neuf, dans le dossier de test
    applog.log.handlers.clear()
    applog.setup()
    applog.log.info("Analyse : test.mp4")
    try:
        raise RuntimeError("boum")
    except RuntimeError:
        applog.error("Analyse impossible : test.mp4")
    t = threading.Thread(target=lambda: 1 / 0, name="fil-test")
    t.start()
    t.join()
    time.sleep(0.1)
    txt = applog.tail()
    assert applog.log_file().parent == tmp_path / "SyncLive" / "journal"
    for key in ("démarrage : SyncLive", "Analyse : test.mp4", "Analyse impossible", "RuntimeError: boum",
                "Erreur imprévue dans le fil fil-test", "ZeroDivisionError"):
        assert key in txt, key
    for h in list(applog.log.handlers):
        h.close()
        applog.log.removeHandler(h)
    logging.getLogger("synclive").handlers.clear()


def test_level_report_html_and_csv(tmp_path):
    from synclive.loudness_report import Row, write_csv, write_html
    rows = [Row("a.mp4", "C:/x/a.mp4", 65.0, 1, -31.2, -4.1, 6.0, 8.2, 0.0, -23.0, -23.0, -4.0, "a_norm.mp4",
                "✓ Écrit"),
            Row("b.wav", "C:/x/b.wav", 120.0, 2, -18.0, -0.5, 9.0, -5.0, 0.0, -23.0, None, None, "", "Prêt")]
    h = tmp_path / "r.html"
    write_html(rows, h, -23.0, -1.0, "EBU R128")
    doc = h.read_text(encoding="utf-8")
    assert "Rapport de niveaux" in doc and "a.mp4" in doc and "b.wav" in doc and "13,2 LU" in doc
    c = tmp_path / "r.csv"
    write_csv(rows, c, -23.0, -1.0)
    raw = c.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                     # BOM : Excel lit les accents
    lines = list(csv.reader(open(c, encoding="utf-8-sig"), delimiter=";"))
    assert lines[0][0] == "Fichier" and lines[1][3] == "-31,2" and lines[2][11] == ""   # nombres pour Excel
