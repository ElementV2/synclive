import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    # SyncLive.exe --cli video.mp4 -o dossier  (analyse sans interface)
    if sys.stdout is None:  # exe « fenêtré » : pas de console
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = sys.stdout
    from synclive.cli import main as cli_main

    raise SystemExit(cli_main(sys.argv[2:]))

from synclive.ui import main

raise SystemExit(main())
