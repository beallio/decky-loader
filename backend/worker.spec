import os
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


backend_root = os.path.dirname(os.path.abspath(SPEC))
runtime_packages = (
    "aiohttp",
    "aiohttp_cors",
    "aiohttp_jinja2",
    "certifi",
    "decky_loader",
    "jinja2",
    "packaging",
    "setproctitle",
    "watchdog",
)
datas = []
binaries = []
hiddenimports = [
    # The loader graph and SDK are imported dynamically by the standalone worker.
    "decky_loader.main",
    "decky_loader.plugin.imports.decky",
    "decky_loader.plugin.sandboxed_plugin",
    "decky_loader.plugin.worker",
    # Common plugin imports selected dynamically by name at runtime.
    "configparser",
    "http.server",
    "logging.handlers",
    "multiprocessing.spawn",
    "multiprocessing.util",
    "socketserver",
    "sqlite3",
    # Extension discovery for native plugin modules loaded from plugin/system paths.
    "importlib.machinery",
    "importlib.metadata",
    "importlib.resources",
    "sysconfig",
]

for package in runtime_packages:
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    if package == "decky_loader":
        # Match SDK source data to Analysis.pathex, not a stale installed wheel.
        sdk_root = Path(backend_root) / "decky_loader"
        package_datas = [
            (str(path), str(Path("decky_loader") / path.parent.relative_to(sdk_root)))
            for path in sorted(sdk_root.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
            and path.suffix not in (".pyc", ".pyo")
        ]
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# These standard-library packages are commonly imported dynamically by plugins
# and are not all discoverable from the loader's static import graph.
for package in (
    "asyncio",
    "concurrent",
    "email",
    "html",
    "http",
    "importlib",
    "logging",
    "multiprocessing",
    "sqlite3",
    "urllib",
    "xml",
):
    hiddenimports += collect_submodules(package)

datas += copy_metadata("decky_loader")

a = Analysis(
    [os.path.join(backend_root, "worker_main.py")],
    pathex=[backend_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure, a.zipped_data)

onedir = os.environ.get("DECKY_WORKER_ONEDIR") == "1"
if onedir:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="DeckyPluginWorker",
        console=True,
        upx=False,
    )
    bundle = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        name="DeckyPluginWorker",
        upx=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        name="DeckyPluginWorker",
        console=True,
        upx=False,
    )
