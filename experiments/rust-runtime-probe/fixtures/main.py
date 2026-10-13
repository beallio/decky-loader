"""Private compatibility fixture; never install this in the normal plugin tree."""
import asyncio
import json
import math
import os
from pathlib import Path

import aiohttp._http_parser
import multidict._multidict
import decky
import decky_plugin
import helpers
import settings

_STATE = Path(decky.DECKY_PLUGIN_RUNTIME_DIR) / 'state.json'
_LIFECYCLE = Path(decky.DECKY_PLUGIN_RUNTIME_DIR) / 'lifecycle.json'

class Plugin:
    async def _migration(self):
        _LIFECYCLE.write_text(json.dumps(['migration']))

    async def _main(self):
        lifecycle = json.loads(_LIFECYCLE.read_text())
        lifecycle.append('main')
        _LIFECYCLE.write_text(json.dumps(lifecycle))

    async def compute(self, values):
        state = json.loads(_STATE.read_text()) if _STATE.exists() else {'calls': 0}
        state['calls'] += 1
        _STATE.write_text(json.dumps(state))
        result = sum(math.pow(value, 2) for value in values)
        await decky.emit('calculated', result, '日本語')
        return {'total': result, 'calls': state['calls'], 'alias': decky is decky_plugin, 'python': __import__('platform').python_version(), 'native_modules': [aiohttp._http_parser.__name__, multidict._multidict.__name__], 'user': os.geteuid(), 'name': decky.DECKY_PLUGIN_NAME}

    async def slow(self, seconds):
        await decky.emit('slow_started', seconds)
        await asyncio.sleep(seconds)
        return await self.compute([5, 12])

    async def fail(self):
        raise ValueError('expected fixture failure')

    async def _unload(self):
        lifecycle = json.loads(_LIFECYCLE.read_text())
        lifecycle.append('unload')
        _LIFECYCLE.write_text(json.dumps(lifecycle))

    async def _uninstall(self):
        lifecycle = json.loads(_LIFECYCLE.read_text())
        lifecycle.append('uninstall')
        _LIFECYCLE.write_text(json.dumps(lifecycle))
