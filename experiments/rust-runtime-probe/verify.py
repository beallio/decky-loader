#!/usr/bin/env python3
"""Exercise the probe's real HTTP, WebSocket, and packaged-plugin boundaries."""
from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import quote

import aiohttp


class Client:
    def __init__(self, session: aiohttp.ClientSession, base: str, token: str):
        self.session, self.base, self.token = session, base, token
        self.socket = None
        self.sequence = 0
        self.events = []

    async def connect(self, messages=None):
        self.socket = await self.session.ws_connect(self.base.replace('http:', 'ws:') + '/ws?auth=' + quote(self.token))
        await self.socket.send_json({'type': 4, 'messages': messages or []})

    async def receive(self, predicate):
        async with asyncio.timeout(15):
            while True:
                frame = await self.socket.receive_json()
                if frame.get('type') == 5:
                    self.events.append(frame)
                if predicate(frame):
                    return frame

    async def send(self, route, args):
        self.sequence += 1
        message = {'type': 0, 'id': self.sequence, 'route': route, 'args': args}
        await self.socket.send_json(message)
        return message

    async def reply(self, message, acknowledge=True):
        frame = await self.receive(lambda frame: frame.get('id') == message['id'])
        if acknowledge:
            await self.socket.send_json({'type': 3, 'id': message['id']})
        return frame

    async def call(self, route, *args):
        frame = await self.reply(await self.send(route, list(args)))
        if frame['type'] != 1:
            raise RuntimeError(f'{route} failed: {frame}')
        return frame['result']

    async def close(self):
        if self.socket is not None:
            await self.socket.close()


async def main(base: str, privileged: bool):
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        async with session.get(base + '/auth/token') as response:
            assert response.status == 200
            token = await response.text()
        async with session.get(base + '/locales/en-US.json') as response:
            assert response.status == 403, response.status
        async with session.get(base + '/locales/en-US.json', headers={'X-Decky-Auth': token, 'Origin': 'https://steamloopback.host'}) as response:
            assert response.status == 200 and response.headers['Access-Control-Allow-Origin'] == 'https://steamloopback.host'
            assert response.headers['Access-Control-Allow-Credentials'] == 'true'
            await response.json()
        async with session.get(base + '/frontend/index.js') as response:
            assert response.status == 200 and 'javascript' in response.headers['Content-Type']
            assert response.headers['Cache-Control'] == 'no-cache'
            await response.read()
        try:
            await session.ws_connect(base.replace('http:', 'ws:') + '/ws?auth=invalid')
        except aiohttp.WSServerHandshakeError as error:
            assert error.status == 403
        else:
            raise AssertionError('unauthenticated websocket was accepted')

        client = Client(session, base, token)
        await client.connect()
        try:
            plugins = await client.call('loader/get_plugins')
            failures = [plugin for plugin in plugins if plugin.get('error')]
            assert not failures, failures
            assert await client.call('utilities/ping') == 'pong'
            user = await client.call('utilities/get_user_info')
            assert user == {'username': 'deck', 'path': '/home/deck'}, user
            version = await client.call('updater/get_version_info')
            assert version['current'] == 'v3.2.10', version

            modern = await client.call('loader/call_plugin_method', 'Rust Probe Modern', 'compute', [3, 4])
            assert modern['total'] == 25 and modern['alias'] and modern['user'] == 1000, modern
            assert modern['python'].startswith('3.11.'), modern
            assert modern['native_modules'] == ['aiohttp._http_parser', 'multidict._multidict'], modern
            legacy = await client.call('loader/call_legacy_plugin_method', 'Rust Probe Legacy', 'compute', {'values': [3, 4]})
            assert legacy['success'] and legacy['result']['total'] == 25 and legacy['result']['user'] == 1000, legacy
            event = next(frame for frame in client.events if frame['event'] == 'loader/plugin_event' and frame['args'][0]['plugin'] == 'Rust Probe Modern')
            assert event['args'][0]['event'] == 'calculated' and event['args'][0]['args'] == [25.0, '日本語'], event

            failure = await client.reply(await client.send('loader/call_plugin_method', ['Rust Probe Modern', 'fail']))
            assert failure['type'] == -1 and failure['error']['message'] == 'expected fixture failure', failure
            private = await client.reply(await client.send('loader/call_plugin_method', ['Rust Probe Modern', '_main']))
            assert private['type'] == -1, private
            missing = await client.reply(await client.send('updater/do_update', []))
            assert missing['type'] == -1 and missing['error']['name'] == 'RouteNotFoundError', missing

            duplicate = await client.send('loader/call_plugin_method', ['Rust Probe Modern', 'compute', [3, 4]])
            original = await client.reply(duplicate, acknowledge=False)
            await client.socket.send_json(duplicate)
            replay = await client.reply(duplicate)
            assert replay == original, (original, replay)
            next_result = await client.call('loader/call_plugin_method', 'Rust Probe Modern', 'compute', [3, 4])
            assert next_result['calls'] == original['result']['calls'] + 1, next_result

            # Persisted side effects establish that FULL_SYNC does not execute an
            # in-flight plugin call twice after the frontend socket is replaced.
            slow = await client.send('loader/call_plugin_method', ['Rust Probe Modern', 'slow', 0.5])
            await client.receive(lambda frame: frame.get('type') == 5 and frame['args'][0].get('event') == 'slow_started')
            await client.close()
            await client.connect([slow])
            synced = await client.reply(slow)
            assert synced['type'] == 1 and synced['result']['total'] == 169, synced
            assert synced['result']['calls'] == next_result['calls'] + 1, synced

            pending = await client.send('loader/call_plugin_method', ['Rust Probe Modern', 'slow', 5])
            await client.receive(lambda frame: frame.get('type') == 5 and frame['args'][0].get('event') == 'slow_started')
            reload = await client.send('loader/reload_plugin', ['Rust Probe Modern'])
            responses = {}
            while len(responses) < 2:
                frame = await client.receive(lambda frame: frame.get('id') in (pending['id'], reload['id']))
                responses[frame['id']] = frame
                await client.socket.send_json({'type': 3, 'id': frame['id']})
            assert responses[pending['id']]['type'] == 2, responses
            assert responses[reload['id']]['type'] == 1, responses
            reloaded = await client.call('loader/call_plugin_method', 'Rust Probe Modern', 'compute', [8, 15])
            assert reloaded['total'] == 289, reloaded

            real = {}
            if privileged:
                root = await client.call('loader/call_plugin_method', 'Rust Probe Root', 'compute', [3, 4])
                assert root['user'] == 0 and root['total'] == 25, root
                real['root_uid'] = root['user']
                metadata = await client.call('loader/call_plugin_method', 'Decky Metadata', 'get_platform_capabilities')
                assert metadata['platform'] == 'linux' and metadata['is_steamos'] is True, metadata
                real['metadata'] = metadata
                ludusavi = await client.call('loader/call_plugin_method', 'SDH-Ludusavi', 'get_settings')
                assert isinstance(ludusavi, dict) and 'auto_sync_enabled' in ludusavi, ludusavi
                real['ludusavi_settings'] = 'read'
                css = await client.call('loader/call_legacy_plugin_method', 'CSS Loader', 'get_backend_version', {})
                assert css['success'] and isinstance(css['result'], int), css
                real['css_backend_version'] = css['result']
                passive = await client.reply(await client.send('loader/call_plugin_method', ['HLTB for Deck', 'anything']))
                assert passive['type'] == -1, passive
                real['passive_plugin'] = 'listed; backend calls rejected'

            print(json.dumps({'source_version': version['current'], 'http_auth_cors_assets': 'passed', 'modern': modern, 'legacy': legacy['result'], 'events_unicode': 'passed', 'errors_private_missing_routes': 'passed', 'duplicate_reply_replay': 'passed', 'inflight_full_sync': 'passed', 'reload_cancellation': 'passed', 'plugins': [{'name': item['name'], 'version': item['version'], 'load_type': item['load_type']} for item in plugins], 'real_plugins': real}, ensure_ascii=False))
        finally:
            await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:1339')
    parser.add_argument('--privileged', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.base, args.privileged))
