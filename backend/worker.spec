import os
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

backend_root = os.path.dirname(os.path.abspath(SPEC))
native_json_dir = os.environ.get("DECKY_NATIVE_JSON_DIR")
if not native_json_dir:
    raise RuntimeError("DECKY_NATIVE_JSON_DIR is required to build the frozen worker")
native_json_path = Path(native_json_dir).resolve()
native_json_extensions = sorted(native_json_path.glob("_decky_native_json*"))
if not native_json_path.is_dir() or not any(
    path.is_file() and path.suffix == ".so" for path in native_json_extensions
):
    raise RuntimeError(
        f"DECKY_NATIVE_JSON_DIR does not contain the compiled _decky_native_json module: {native_json_path}"
    )
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
    "_decky_native_json",
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
            and path.relative_to(sdk_root).parts[0] not in ("static", "locales")
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
    pathex=[backend_root, str(native_json_path)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)

# Use the target system's unwind library, not a bundled build-host copy.
a.binaries = [
    entry
    for entry in a.binaries
    if Path(entry[0]).name != "libgcc_s.so.1"
    and Path(entry[1]).name != "libgcc_s.so.1"
]
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
