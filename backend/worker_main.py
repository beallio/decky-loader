from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    from decky_loader.plugin.worker import main

    raise SystemExit(main())
