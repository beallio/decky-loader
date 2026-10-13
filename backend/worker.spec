import os

from PyInstaller.building.api import EXE, PYZ
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
