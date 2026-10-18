# Decky Loader (Rust fork)

Decky Loader adds plugins to the Steam Deck's Gaming Mode. Plugins can [restyle menus](https://github.com/suchmememanyskill/SDH-CssLoader), [change system sounds](https://github.com/EMERALD0874/SDH-AudioLoader), [adjust colors](https://github.com/libvibrant/vibrantDeck), and [more](https://plugins.deckbrew.xyz/).

This fork keeps the normal Decky menus and Python plugins, but replaces the background service with a Rust version. It runs as your normal `deck` account instead of the administrator (root) account. Plugins that need administrator access do not run unless you turn on [root plugin support](#plugins-that-need-administrator-access).

This is not the official Decky Loader. For the official version, see [SteamDeckHomebrew/decky-loader](https://github.com/SteamDeckHomebrew/decky-loader).

## Install

1. Press the Steam button, open **Power**, and select **Switch to Desktop**.
2. Download [install-to-deck.sh](scripts/install-to-deck.sh) and save it in your **Downloads** folder.
3. Open **Konsole** and run:

   ```sh
   cd ~/Downloads
   bash install-to-deck.sh --check
   bash install-to-deck.sh
   ```

   `--check` only tests the download and your Deck. The second command installs the latest stable release.
4. Return to Gaming Mode.

You do not need a GitHub account or developer tools. Before each change, the script saves a copy of your `homebrew` folder (plugins and settings) in `~/decky-user-backups`. Use `--backup-dir DIR` to save it somewhere else.

To install a test version, add `--channel preview`. To install one exact version, add `--version TAG`, for example `--version v3.2.10-rust.7`.

### If you already have Decky installed

The official Decky runs as the administrator account. Move it to this fork with:

```sh
bash install-to-deck.sh --migrate-legacy
```

This step asks for your password once. If you have not set a password, run `passwd` first. The script saves the old Decky so that you can put it back later.

## Update

Open Decky's settings in Gaming Mode and check for updates. Select **Stable** for normal releases or **Pre-Release** for test versions. You can also run the install command again.

## Undo

To go back to the previous version of this fork:

```sh
bash install-to-deck.sh --undo
```

To go back to the official Decky that was installed before you moved to this fork:

```sh
bash install-to-deck.sh --restore-legacy
```

This asks for your password. Undo does not change your plugin data. If you need your old plugin files, they are in the backup folder.

When this fork works well for a few days, you can delete the saved copy of the old Decky to free space:

```sh
bash install-to-deck.sh --forget-legacy
```

After this, `--restore-legacy` cannot work.

## Plugins that need administrator access

Some plugins, for example PowerTools, need administrator (root) access. By default they do not run, and Decky shows a message for them. To let them run:

```sh
bash install-to-deck.sh --enable-root-plugins
```

This installs the current version, asks for your `sudo` password once, and lists the installed plugins that need root. Answer `y` to allow them. Only plugins that need root run as root, in a separate background service. Decky itself and all other plugins still run as `deck`.

After this, you install, update and remove plugins that need root in the Decky store as usual. You do not need to run the command again. When Decky updates itself, the root service updates itself to the same version.

Before each install or update of a plugin that needs root, Decky shows a warning. You must wait 10 seconds and switch on "I understand" before the plugin is installed.

- **Cost:** while at least one root plugin is on, one extra background process uses about 25 MB of memory. With no root plugins, nothing extra runs. It uses no CPU while idle.
- **Risk:** a plugin that runs as root can change anything on your Deck. Install only plugins you trust. Plugins that need root can only come from the Decky store, or from the custom store that is set in Decky's settings, not from a URL or a ZIP file. If you use a custom store, you trust it as much as the official one. As in the official Decky, a root plugin keeps its settings in your normal Decky folders.

To turn root plugin support off, run `bash install-to-deck.sh --disable-root-plugins`. Root plugins then stop and become owned by `deck` again.

## Check status

```sh
bash install-to-deck.sh --status
```

The output also has a `resources` section. It shows CPU and memory for each plugin and for the whole service.

## Problems

- **Decky does not appear.** Restart the Deck. If it is still missing, run `--status`, then install again.
- **A plugin shows "This plugin needs root".** See [Plugins that need administrator access](#plugins-that-need-administrator-access).
- **A plugin shows "Decky could not start".** The plugin failed to start. Update or remove it in Decky's settings.
- **Another program uses port 1337 or 8080.** Decky needs these ports. Syncthing often uses 8080; change Syncthing to 8384.
- **The script stops with a space error.** Free some storage or choose a different `--backup-dir`, then try again. The script stops before it changes anything.
- **The install fails part way.** The script puts back the previous version automatically. It prints the location of a log file. Attach that file, or the ZIP from [collect-debug-info.sh](scripts/collect-debug-info.sh), when you [report a problem](https://github.com/beallio/decky-loader/issues).

## Install from another computer

To install over SSH from a Linux computer, add `--remote HOST`, for example `bash install-to-deck.sh --remote steamdeck`. Run `bash install-to-deck.sh --help` for all options.

## For plugin authors

Plugins work as they do in official Decky. Plugins that set the `root` flag run only when the user turns on root plugin support. They see their own folder read-only at the path in `DECKY_PLUGIN_DIR`, and must not write into it. Start with the [plugin template](https://github.com/SteamDeckHomebrew/decky-plugin-template).

This fork also offers a fast JSON encoder to Python plugins:

```python
from decky import native

text = native.dumps({"name": "Deck", "values": [1, 2, 3]})
```

For normal JSON data, `native.dumps` returns the same text as Python's `json.dumps` with its default settings. It also accepts `ensure_ascii=False`. It has no other `json.dumps` options. It is only available when your plugin runs in this fork. If your plugin also supports official Decky, check for it with `hasattr(decky, "native")`.

To build or change this fork, see the [development guide](docs/development.md).

## Credits

Decky Loader is made by [Steam Deck Homebrew](https://github.com/SteamDeckHomebrew) and its contributors. The original idea is based on [marios8543's Steam Deck UI Inject project](https://github.com/marios8543/steamdeck-ui-inject).
