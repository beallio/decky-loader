import sys

from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    # Capture native argv before worker setup replaces environment pointers.
    from setproctitle import getproctitle

    getproctitle()
    if sys.argv[1:2] == ["--launcher"]:
        from decky_loader.plugin.launcher import main

        raise SystemExit(main(sys.argv[2:]))

    from decky_loader.plugin.worker import main

    raise SystemExit(main())
