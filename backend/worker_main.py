import sys

from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    # Capture native argv before worker setup replaces environment pointers.
    from setproctitle import getproctitle

    getproctitle()
    if sys.argv[1:2] == ["--launcher"]:
        import gc

        # The shared launcher forks plugin workers. Collections before the fork free
        # objects inside shared pages, and children then fill those holes, which
        # copies the pages. The launcher freezes the heap before each fork and
        # enables collection again in each child.
        gc.disable()
        from decky_loader.plugin.launcher import main

        raise SystemExit(main(sys.argv[2:]))

    from decky_loader.plugin.worker import main

    raise SystemExit(main())
