"""Entry point used by the .exe build (build_exe.bat). Same as `python -m restclient`."""

import multiprocessing

from restclient.__main__ import main

if __name__ in {"__main__", "__mp_main__"}:
    multiprocessing.freeze_support()
    main()
