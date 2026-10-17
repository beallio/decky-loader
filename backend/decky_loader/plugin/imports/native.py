"""Public access to the loader's native JSON encoder."""

from importlib import import_module
from typing import Any


def dumps(value: object, *, ensure_ascii: bool = True) -> str:
    """Encode a JSON value with the loader's native encoder.

    The extension is loaded only when this function is called so importing the
    SDK remains possible in an unfrozen development environment without it.
    """
    try:
        encoder = import_module("_decky_native_json")
    except (ImportError, OSError) as error:
        raise ImportError(
            "decky.native.dumps requires the compiled _decky_native_json "
            "extension; build or install the native JSON module for this "
            "Python environment"
        ) from error

    dumps_impl: Any = getattr(encoder, "dumps", None)
    if not callable(dumps_impl):
        raise ImportError(
            "the loaded _decky_native_json extension is incompatible: "
            "it does not provide callable dumps"
        )
    return dumps_impl(value, ensure_ascii=ensure_ascii)
