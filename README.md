# Decky Loader (Rust fork)

Decky Loader adds plugins to the Steam Deck's Gaming Mode. Plugins can [restyle menus](https://github.com/suchmememanyskill/SDH-CssLoader), [change system sounds](https://github.com/EMERALD0874/SDH-AudioLoader), [adjust colors](https://github.com/libvibrant/vibrantDeck), and [do much more](https://plugins.deckbrew.xyz/).

This is a modified copy (a "fork") of Decky Loader. It looks and works like the normal Decky, and your current plugins still work. The difference is the part that runs in the background: this fork replaces it with a smaller, faster program written in the Rust programming language. It also runs with your normal `deck` account, not with the administrator (root) account.

This is not the official Decky Loader. For the official version, go to [SteamDeckHomebrew/decky-loader](https://github.com/SteamDeckHomebrew/decky-loader).

## What this fork adds

These features are not in the official Decky Loader:

- **Faster and lighter.** Decky starts and stops faster and uses less memory and CPU. See [Speed and memory](#speed-and-memory).
- **No administrator access by default.** Decky and your plugins run with your normal `deck` account, not as administrator. Plugins can still read and change your account's files, so install only plugins that you trust.
- **Safer administrator plugins.** Plugins that need administrator access run only if you turn them on. Before each install or update of such a plugin, Decky shows a warning and asks for your password. See [Plugins that need administrator access](#plugins-that-need-administrator-access).
- **More than one plugin store.** The store page shows plugins from several stores at the same time, in the order that you choose. The [Decky Extended](https://decky-extended-plugins.beallio.com/) store is included by default.
- **Plugins from GitHub.** You can add a plugin's GitHub page as a store, and get its new versions as updates.
- **A better store page.** You can sort plugins by update date and filter them by store, tag, installed plugins, administrator access, and source code. Each plugin shows when it was last updated, a warning if it needs administrator access, and a button to its GitHub page.
- **Stores that are slow or offline do not stop the store page.** Decky waits at most 20 seconds for each store. It saves each store's list for 6 hours and shows the saved list when a store does not answer.
- **Update notices for more stores.** Decky finds plugin updates also in custom stores that the official Decky cannot check for updates.
- **An install script that protects your data.** It saves a copy of your plugins and settings before each change. It can undo an update, move you from the official Decky to this fork, and move you back.
- **Plugin CPU and memory use.** The status command shows how much CPU and memory each plugin uses.
- **More options for plugin authors.** Plugins can have a backend written in Rust instead of Python, and Python plugins get a fast JSON encoder. See [For plugin authors](#for-plugin-authors).

## What this fork removes or limits

These parts of the official Decky Loader are removed or work differently in this fork:

- **Steam Deck and Linux PCs only.** This fork works only on 64-bit Intel or AMD Linux computers that use systemd, such as the Steam Deck. It does not work on ARM devices, Windows, or macOS.
- **Plugins that need administrator access do not run by default.** The official Decky runs them without asking. In this fork, you must [turn them on](#plugins-that-need-administrator-access) first.
- **No administrator plugins from a ZIP file.** You can install these plugins only from a store or from an `https://` address.
- **A password for each administrator plugin.** Decky asks for your password each time you install or update one of these plugins. If your account can use `sudo` without a password, you cannot install these plugins.
- **Updates come only from this fork.** Decky's update button installs new versions of this fork, not official versions. To go back to the official Decky, see [Undo](#undo).
- **No warning when you add a store.** The official Decky shows a warning when you use a store that is not official. This fork does not. Add only stores that you trust.
- **The file browser shows only what your account can open.** When a plugin asks you to choose a file, you cannot open folders that only the administrator can read.
- **Plugins cannot start or stop SSH through Decky.** This needs administrator access.

## Speed and memory

We compared this fork with the official Decky on the same computer, with the same plugins. In each test, one plugin had no backend, and all the other plugins had a Python backend. Almost all plugins in the stores are of these two types. Smaller numbers are better.

| What we measured (12 plugins, unless noted) | Official Decky | This fork | Difference |
|---|---|---|---|
| Memory used by Decky, no plugins (MB) | 38.6 | 8.4 | 30.2 MB less (78% less) |
| Memory used by Decky and plugins (MB) | 159.4 | 155.6 | 3.8 MB less (2% less) |
| Time to start (seconds) | 1.94 | 0.88 | 1.06 s faster (55% faster) |
| Time to stop (seconds) | 5.36 | 0.18 | 5.18 s faster (97% faster) |
| CPU use while idle (% of one CPU core) | 0.096 | 0.067 | 30% less |
| CPU time for one plugin call (microseconds) | 222 | 127 | 43% less |

With 4 and 8 plugins, the results were almost the same: the fork used about 3 MB less memory and 40–55% less idle CPU, and started 1.6 to 1.9 seconds faster.

The memory saving is mostly in Decky itself. Each plugin still uses about the same memory as before, because most plugins are written in Python, and this fork still runs them with Python.

How we measured:

- Official Decky `v3.2.10` against this fork's `v3.3.0-rust.1-rc.1`, on 17 October 2026.
- Each test ran 10 times. The table shows the average.
- The tests ran in a SteamOS virtual machine, not on a real Steam Deck, and without the Steam user interface. Results on a real Deck can be different.
- "Memory" is the memory that belongs to Decky and its plugins (PSS). 1 MB here is 1,048,576 bytes.

### Plugins with a Rust backend

A plugin can also have a backend written in Rust instead of Python (see [For plugin authors](#for-plugin-authors)). This only works in this fork. We ran this fork with 12 Python plugins, and then with 12 Rust plugins that did the same simple work.

| What we measured (12 plugins, unless noted) | Python plugins | Rust plugins | Difference |
|---|---|---|---|
| Memory used by Decky and plugins, 1 plugin (MB) | 44.2 | 6.6 | 37.6 MB less (85% less) |
| Memory used by Decky and plugins (MB) | 119.6 | 18.3 | 101.3 MB less (85% less) |
| Memory for each extra plugin (MB) | 6.9 | 1.1 | 5.8 MB less (84% less) |
| Time to start (seconds) | 0.60 | 0.29 | 0.31 s faster (52% faster) |
| Time to stop (seconds) | 0.034 | 0.013 | 0.021 s faster (62% faster) |
| CPU time for one plugin call (microseconds) | 113 | 87 | 23% less |
| Time for one plugin call (microseconds) | 154 | 125 | 19% faster |

While idle, both types used about 0.02% of one CPU core, which is almost nothing.

The memory saving is the largest when all your plugins are Rust plugins. When you have at least one Python plugin, Decky also starts a shared Python program for all Python plugins. So if you mix the two types, the saving is smaller. We did not test mixed sets of plugins.

How we measured:

- A development build of this fork from 19 October 2026, on the same SteamOS virtual machine, without the Steam user interface.
- Each plugin answered one simple request. Real plugins do more work, so they use more memory and CPU.
- The Rust plugins were copies of the example plugin in [`native_example.rs`](backend-rust/decky-plugin/examples/native_example.rs).
- We ran each test 5 times, with 1, 4, 8, and 12 plugins. The table shows the average.
- For the stop time, we ran all the tests again and stopped Decky in the same way as the installed service does.

## Install

1. Press the Steam button, open **Power**, and select **Switch to Desktop**.
2. Download [install-to-deck.sh](scripts/install-to-deck.sh) and save it in your **Downloads** folder.
3. Open **Konsole** and type:

   ```sh
   cd ~/Downloads
   bash install-to-deck.sh --check
   bash install-to-deck.sh
   ```

   The `--check` command only tests the download and your Deck. It changes nothing. The second command installs the latest stable version.
4. Go back to Gaming Mode.

You do not need a GitHub account or developer tools. Before each change, the script saves a copy of your `homebrew` folder (your plugins and settings) in `~/decky-user-backups`. To save it in a different folder, add `--backup-dir FOLDER`.

To install a test version, add `--channel preview`. To install one specific version, add `--version` and the version name, for example `--version v3.2.10-rust.7`.

### If the official Decky is already installed

The official Decky runs with the administrator account. To move to this fork, type:

```sh
bash install-to-deck.sh --migrate-legacy
```

The script asks for your password one time. If you do not have a password, type `passwd` first to make one. The script keeps a copy of the official Decky, so you can go back to it later.

## Update

In Gaming Mode, open Decky's settings and check for updates. Select **Stable** for normal versions or **Pre-Release** for test versions. You can also run the install command again.

## Undo

To go back to the previous version of this fork:

```sh
bash install-to-deck.sh --undo
```

To go back to the official Decky that you had before this fork:

```sh
bash install-to-deck.sh --restore-legacy
```

This asks for your password. Undo does not change your plugin data. If you need your old plugin files, they are in the backup folder.

When this fork works well for a few days, you can delete the copy of the official Decky to get space back:

```sh
bash install-to-deck.sh --forget-legacy
```

After you do this, `--restore-legacy` does not work.

## Plugin stores

The store page shows plugins from more than one store. By default, these are [Decky Extended](https://decky-extended-plugins.beallio.com/) and the official store. To change the stores, open Decky's settings, then **General**.

- **Add a store.** Select **Add store** and type the store's web address.
- **Add a plugin from GitHub.** You can also type the GitHub page of one plugin, for example `https://github.com/owner/repo`. Decky then shows the plugin's name, author, tags, description, and picture, and each release of the plugin as a version. Some releases do not show:
  - Releases with more than one `.zip` file, because Decky cannot know which file is the plugin.
  - Some older releases. Decky installs a file only when GitHub gives a checksum for it, and GitHub gives checksums only for files uploaded since mid-2025.

  GitHub lets one network check about 60 times per hour, so add only a few plugins this way.
- **Change the order.** Press **X** on the list, move a store up or down, and press **X** again to save. When two stores have the same plugin, Decky uses the store that is higher in the list. Only that store sends you update notices for the plugin. If a lower store has other versions of the plugin, they also show in the plugin's version list.
- **Test versions.** Turn on **Include pre-releases** to add the test stores (official testing and Decky Extended testing) and the test releases of the GitHub plugins you added. Decky marks these versions "Pre-release", and it can offer them as updates.

If a store does not answer, the store page shows a message and the plugins from the other stores. Until that store answers again, Decky does not offer updates from the stores below it in the list.

## Plugins that need administrator access

Some plugins, for example PowerTools, need administrator (root) access. By default, these plugins do not run, and Decky shows a message for them. To let them run, type:

```sh
bash install-to-deck.sh --enable-root-plugins
```

This installs the current version of this fork and asks for your password one time. Then it shows the installed plugins that need administrator access. Type `y` to let them run. Only these plugins run as administrator, in a separate background program. Decky and all other plugins still run with your `deck` account.

You do this only one time. After this, you install, update, and remove these plugins in Decky as usual: from a store, or from an `https://` address in the developer settings. When Decky updates itself, the administrator program also updates.

Before each install or update of a plugin that needs administrator access, Decky shows a warning. Wait 10 seconds, turn on "I understand", and type your password. If the password is wrong, the install stops, and you must wait 5 seconds before you try again. If your account does not need a password for `sudo`, Decky cannot install these plugins.

- **Cost:** when at least one of these plugins is on, one more background program runs. It uses about 25 MB of memory and no CPU while idle. When none of these plugins is on, nothing more runs.
- **Risk:** a plugin with administrator access can change anything on your Deck. Install only plugins that you trust. Your password is the only check: Decky installs any plugin that you confirm with your password, from any address. You cannot install these plugins from a ZIP file on your Deck. As in the official Decky, these plugins keep their settings in your normal Decky folders.

To stop these plugins, type `bash install-to-deck.sh --disable-root-plugins`. Their files, settings, data, and logs in your `homebrew` folder then belong to your `deck` account again. Anything that a plugin saved in the administrator's home folder (`/root`) stays there.

## Check status

```sh
bash install-to-deck.sh --status
```

The `resources` part of the output shows how much CPU and memory Decky and each plugin use.

## Problems

- **Decky does not show.** Restart the Deck. If Decky still does not show, run `--status`, then install again.
- **A plugin shows "This plugin needs root".** See [Plugins that need administrator access](#plugins-that-need-administrator-access).
- **A plugin shows "Decky could not start".** The plugin did not start. Update or remove it in Decky's settings.
- **An update in Decky stops or fails, and your version is `v3.2.10-rust.8-rc.1` to `rc.10`.** These versions cannot update themselves to `v3.3.0` or later. Run the install command one time with `--channel preview`. After that, updates in Decky work again.
- **An update in Decky fails with "bundle requires a frozen CPython 3.11 worker".** Versions up to `v3.3.0-rust.1-rc.12` cannot update themselves to a version that uses Python 3.14. Run the install command one time with `--channel preview`. After that, updates in Decky work again.
- **A plugin stopped working after an update to a version with Python 3.14.** This fork now uses Python 3.14.8, the same as the official Decky `v3.3.0`. A few plugins include files made only for Python 3.11. Ask the plugin's author for a version that works with the official Decky `v3.3.0`.
- **Another program uses port 1337 or 8080.** Decky needs these ports. Syncthing often uses port 8080. Change Syncthing to port 8384.
- **The script stops because there is not enough space.** Delete some files, or use `--backup-dir` to choose a different folder, and try again. The script stops before it changes anything.
- **The install stops before it is complete.** The script puts back the previous version. It shows where it saved a log file. When you [report a problem](https://github.com/beallio/decky-loader/issues), attach that file or the ZIP file from [collect-debug-info.sh](scripts/collect-debug-info.sh).

## Install from a different computer

To install over SSH from a Linux computer, add `--remote` and the Deck's name, for example `bash install-to-deck.sh --remote steamdeck`. To see all options, type `bash install-to-deck.sh --help`.

## For plugin authors

Plugins work as they do in the official Decky. Start with the [plugin template](https://github.com/SteamDeckHomebrew/decky-plugin-template).

Plugins that set the `root` flag run only when the user turns on support for administrator plugins. They can read their own folder at the path in `DECKY_PLUGIN_DIR`, but they cannot write to it.

This fork has a fast JSON encoder for Python plugins:

```python
from decky import native

text = native.dumps({"name": "Deck", "values": [1, 2, 3]})
```

For normal JSON data, `native.dumps` gives the same text as Python's `json.dumps` with its default settings. The only option it accepts is `ensure_ascii=False`. It is available only in this fork. If your plugin also supports the official Decky, check for it with `hasattr(decky, "native")`.

A plugin can also have a backend written in Rust (or another compiled language) instead of `main.py`. This plugin does not need Python. It works only in this fork. See [Native plugin backends](docs/development.md#native-plugin-backends).

To build or change this fork, see the [development guide](docs/development.md).

## Credits

Decky Loader is made by [Steam Deck Homebrew](https://github.com/SteamDeckHomebrew) and its contributors. The original idea is from [marios8543's Steam Deck UI Inject project](https://github.com/marios8543/steamdeck-ui-inject).
