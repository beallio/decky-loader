import sys

from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    # Capture native argv before worker setup replaces environment pointers.
    from setproctitle import getproctitle

    getproctitle()
    if sys.argv[1:2] in (["--launcher"], ["--root-host"]):
        import gc

        # Both modes fork plugin workers. Collections before the fork free
        # objects inside shared pages, and children then fill those holes, which
        # copies the pages. The parent freezes the heap before each fork and
        # enables collection again in each child.
        gc.disable()
        if sys.argv[1] == "--launcher":
            from decky_loader.plugin.launcher import main

            raise SystemExit(main(sys.argv[2:]))
        from decky_loader.plugin.root_host import main as root_host_main

        raise SystemExit(root_host_main(sys.argv[2:]))
    if sys.argv[1:2] == ["--root-host-admin"]:
        from decky_loader.plugin.root_host import admin_main

        raise SystemExit(admin_main(sys.argv[2:]))

    from decky_loader.plugin.worker import main

    raise SystemExit(main())
