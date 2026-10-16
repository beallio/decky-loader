<h1 align="center">
  <a name="logo" href="https://deckbrew.xyz/"><img src="https://deckbrew.xyz/static/icon-45ca1f5aea376a9ad37e92db906f283e.png" alt="Deckbrew logo" width="200"></a>
  <br>
  Decky Loader
  <br>
  <a name="download button" href="https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/decky_installer.desktop"><img src="./docs/images/download_button.svg"  alt="Download decky" width="150px" style="padding-top: 15px;"></a>
</h1>

<p align="center">
  <a href="https://github.com/SteamDeckHomebrew/decky-loader/releases"><img src="https://img.shields.io/github/downloads/SteamDeckHomebrew/decky-loader/total" /></a>
  <a href="https://github.com/SteamDeckHomebrew/decky-loader/stargazers"><img src="https://img.shields.io/github/stars/SteamDeckHomebrew/decky-loader" /></a>
  <a href="https://github.com/SteamDeckHomebrew/decky-loader/commits/main"><img src="https://img.shields.io/github/last-commit/SteamDeckHomebrew/decky-loader.svg" /></a>
  <a href="https://weblate.werwolv.net/engage/decky/"><img src="https://weblate.werwolv.net/widgets/decky/-/decky/svg-badge.svg" alt="Translation status" /></a>
  <a href="https://github.com/SteamDeckHomebrew/decky-loader/blob/main/LICENSE"><img src="https://img.shields.io/github/license/SteamDeckHomebrew/decky-loader" /></a>
  <a href="https://deckbrew.xyz/discord"><img src="https://img.shields.io/discord/960281551428522045?color=%235865F2&label=discord" /></a>
  <br>
  <br>
  🌐 <a href="./README.md">English</a> · <a href="./README_zh-Hans.md">简体中文</a>
  <br>
  <br>
<!--  <img src="https://media.discordapp.net/attachments/966017112244125756/1012466063893610506/main.jpg" alt="Decky screenshot" width="80%">-->
</p>

## 🩵 Backers and Sponsors

[Become a backer or sponsor](https://opencollective.com/steamdeckhomebrew) to support our work! Contributing to our collective effort will help Decky Loader developers cover the costs of web servers, acquire new development hardware, and more.

<!-- SPONSORS COMMENTED OUT UNTIL WE GET SOME SPONSORS TO AVOID BLANK SVG SPACE -->
<a href="https://opencollective.com/steamdeckhomebrew"><img alt="Steam Deck Homebrew sponsors on Open Collective" src="https://opencollective.com/steamdeckhomebrew/sponsors.svg?button=true&avatarHeight=46&width=750"></a>

<a href="https://opencollective.com/steamdeckhomebrew"><img alt="Steam Deck Homebrew backers on Open Collective" src="https://opencollective.com/steamdeckhomebrew/backers.svg?button=false&avatarHeight=46&width=750"></a>

## 📖 About

Decky Loader is a homebrew plugin launcher for the Steam Deck. It can be used to [stylize your menus](https://github.com/suchmememanyskill/SDH-CssLoader), [change system sounds](https://github.com/EMERALD0874/SDH-AudioLoader), [adjust your screen saturation](https://github.com/libvibrant/vibrantDeck), [change additional system settings](https://github.com/NGnius/PowerTools), and [more](https://plugins.deckbrew.xyz/).

For more information about Decky Loader as well as documentation and development tools, please visit [our wiki](https://wiki.deckbrew.xyz).

### 🎨 Features

🧹 Clean injecting and loading of multiple plugins.  
🔒 Stays installed between system updates and reboots.  
🔗 Allows two-way communication between plugins and the loader.  
🐍 Supports Python functions run from TypeScript React.  
🌐 Allows plugins to make fetch calls that bypass CORS completely.

### 🤔 Common Issues

- Syncthing may use port 8080 on Steam Deck, which Decky Loader needs to function. If you are using Syncthing as a service, please change its port to something else.
  - 8384 is the recommended port for Syncthing.
- If you are using any software that uses port 1337 or 8080, please change its port to something else or uninstall it.
- Sometimes Decky will disappear on SteamOS updates. This can easily be fixed by just re-running the installer and installing the stable branch again. If this doesn't work, try installing the prerelease instead. If that doesn't work, then [check the existing issues](https://github.com/SteamDeckHomebrew/decky-loader/issues) and if there isn't one then you can [file a new issue](https://github.com/SteamDeckHomebrew/decky-loader/issues/new?assignees=&labels=bug&template=bug_report.yml&title=%5BBUG%5D+%3Ctitle%3E).

## 💾 Installation

- This installation can be done without an admin/sudo password set.

1. Prepare a mouse and keyboard if possible.
   - Keyboards and mice can be connected to the Steam Deck via USB-C or Bluetooth.
   - Many Bluetooth keyboard and mouse apps are available for iOS and Android. (KDE connect is preinstalled on the steam deck)
   - The Steam Link app is available on [Windows](https://media.steampowered.com/steamlink/windows/latest/SteamLink.zip), [macOS](https://apps.apple.com/us/app/steam-link/id1246969117), and [Linux](https://flathub.org/apps/details/com.valvesoftware.SteamLink). It works well as a remote desktop substitute.
   - If you have no other options, use the right trackpad as a mouse and press <img src="./docs/images/light/steam.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/steam.svg#gh-light-mode-only" height=16>+<img src="./docs/images/light/x.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/x.svg#gh-light-mode-only" height=16> to open the on-screen keyboard as needed.
1. Press the <img src="./docs/images/light/steam.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/steam.svg#gh-light-mode-only" height=16> button and open the Power menu.
1. Select "Switch to Desktop".
1. Navigate to this Github page on a browser of your choice.
1. Download the [installer file](https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/decky_installer.desktop). (If using firefox, it will be named `decky_installer.desktop.download`. Rename it to `decky_installer.desktop` before running it)
1. Drag the file onto your desktop and double click it to run it.
1. Either type your admin password or allow Decky to temporarily set your admin password to `Decky!` (this password will be removed after the installer finishes)
1. Choose the version of Decky Loader you want to install.
   - **Latest Release**  
     Intended for most users. This is the latest stable version of Decky Loader.
   - **Latest Pre-Release**  
     Intended for plugin developers. Pre-releases are unlikely to be fully stable but contain the latest changes. For more information on plugin development, please consult [the wiki page](https://wiki.deckbrew.xyz/en/loader-dev/development).
1. Open the Return to Gaming Mode shortcut on your desktop.

- There is also a fast install for those who can use Konsole. Run `curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/install_release.sh | sh` and type your password when prompted.

### 👋 Uninstallation

We are sorry to see you go! If you are considering uninstalling because you are having issues, please consider [opening an issue](https://github.com/SteamDeckHomebrew/decky-loader/issues) or [joining our Discord](https://deckbrew.xyz/discord) so we can help you and other users.

1. Press the <img src="./docs/images/light/steam.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/steam.svg#gh-light-mode-only" height=16> button and open the Power menu.
1. Select "Switch to Desktop".
1. Run the installer file again, and select `uninstall decky loader`.

- There is also a fast uninstall for those who can use Konsole. Run `curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/uninstall.sh | sh` and type your password when prompted.

## 🚀 Getting Started

Now that you have Decky Loader installed, you can start using plugins. Each plugin is maintained by a different developer and has its own uses, but most follow a general structure outlined below.

### 📦 Plugins

1. Press the <img src="./docs/images/light/qam.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/qam.svg#gh-light-mode-only" height=16> button and navigate to the <img src="./docs/images/light/plug.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/plug.svg#gh-light-mode-only" height=16> icon. This is the Decky menu used for interacting with plugins and the loader itself.
1. Select the <img src="./docs/images/light/store.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/store.svg#gh-light-mode-only" height=16> icon to open the Plugins Browser. This is where you can find and install plugins.
   - You can also install from URL in the Settings menu. We do not recommend installing plugins from untrusted sources.
1. To install a plugin, select the "Install" button on the plugin you want. You can also select a version from a dropdown menu, but this is not recommended.
1. To update, uninstall, and reload plugins, navigate to the Decky menu and select the <img src="./docs/images/light/gear.svg#gh-dark-mode-only" height=16><img src="./docs/images/dark/gear.svg#gh-light-mode-only" height=16> icon.
   - Keep in mind that uninstalling a plugin will only remove its plugin files, not any other files it may have created.

### 🛠️ Plugin Development

There is no complete plugin development documentation yet. However a good starting point is the [plugin template repository](https://github.com/SteamDeckHomebrew/decky-plugin-template). Consider [joining our Discord](https://deckbrew.xyz/discord) if you have any questions.

### 🤝 Contributing

Please consult [the wiki page regarding development](https://wiki.deckbrew.xyz/en/loader-dev/development) for more information on installing development versions of Decky Loader. You can also install the Steam Deck UI on a Windows or Linux computer for testing by following [this YouTube guide](https://youtu.be/1IAbZte8e7E?t=112).

For Rust work in OMP, install `rust-analyzer` and the standard-library source for the pinned Rust 1.98.1 toolchain. The project configuration in `.omp/lsp.json` selects `backend-rust` when OMP starts at the repository root. Reload the LSP configuration after installation. Save-triggered Cargo checks are disabled; run the build and checks before submitting changes.

1. Clone the repository using the latest commit to main before starting your PR.
1. In your clone of the repository, run these commands.
   ```bash
   cd frontend
   pnpm i # NOTE: you may need to approve esbuild's build script with pnpm approve-builds
   pnpm run build
   ```
1. If you are modifying the UI, these commands will need to be run before deploying the changes to your Steam Deck.
1. Use the VS Code tasks or `deck.sh` script to deploy your changes to your Steam Deck to test them.
1. You will be testing your changes with the Python script version. You will need to build, deploy, and reload each time.

⚠️ If you are recieving build errors due to an out of date library, you should run this command inside of your repository.

```bash
pnpm update @decky/ui --latest
```

Source control and deploying plugins are left to each respective contributor for the cloned repos in order to keep dependencies up to date.

### Private Rust runtime

The [Rust backend](backend-rust) keeps Python plugins and the existing Decky interface. It is a private candidate, not the default public release. Physical Steam Deck and full Steam interface checks remain deferred. Building it does not change an installed copy of Decky Loader.

The `Runtime Builder` workflow checks dependencies and builds a complete Linux x86-64 bundle. Its artifact contains an installable ZIP, `SHA256SUMS`, and a build record. The ZIP contains the Rust backend, Python launcher and workers, Python plugin support code, interface files, installer, and file checksums. The existing public Python release workflow is unchanged; the runtime workflow does not publish a release.

The separate `Runtime Prerelease` workflow prepares a tagged test build and verifies its source, version, complete file lists, and every payload checksum. It publishes nothing by default. A maintainer must select publication explicitly to create a prerelease with lasting ZIP, checksum, and build-record downloads. Publication must use a separate experimental repository, not the official Python release feed. An existing tag must match the build's exact source commit. The workflow never marks the prerelease as the latest stable release. The Deck installer stays pinned to its selected trial build; preparing or publishing another candidate does not change it.

To build locally, install the GitHub CLI and the Rust, Node.js, and pnpm versions listed in [build-config.json](backend-rust/build-config.json). The build downloads a checksum-verified CPython archive and installs Python dependencies in temporary environments. It uses the committed Cargo, Poetry, and pnpm lockfiles.

```sh
python3 backend-rust/build.py \
  --release-version dev \
  --output "${TMPDIR:-/var/tmp/omp-1000}/decky-runtime"
```

Choose an output directory that does not already exist. The output includes `bundle/`, an installable ZIP, `SHA256SUMS`, and `build-result.json`. The bundle manifest records source hashes, tool versions, package versions, and the separate Rust release identity. These records support repeatable builds; they do not promise byte-for-byte identical binaries on different build hosts.

The Python plugin support code and interface remain based on `v3.2.10`. Bundle assembly verifies that baseline, records the startup changes, and checks the packaged helper code against its source. The Rust release identity is separate; `dev` builds do not offer updates.

The runtime workflow fetches the SDK release tag from the upstream repository, then verifies its pinned commit and source hashes. A fork does not need its own copy of that tag.

Python plugins still run in separate processes. A shared Python parent loads common support code, then creates a child process for each active plugin. This can share memory without putting all plugins in one interpreter.

Plugin startup keeps the selected plugin information and Python imports together if the installed folder is moved or replaced. Normal plugin file paths and settings locations stay the same. This check covers imports managed by the loader, not every file or helper program that a plugin opens itself. Plugins with administrator access must still be trusted.

The selected-file binding stays available until the worker process exits, including shutdown callbacks and failed startup.

The [installer](backend-rust/install.py) supports new installs, replacement of an existing install, and rollback. A failed activation restores the previous backend and service file. User plugins, settings, and data remain in place. Use its `--help` before installing a private candidate.

#### Install the private candidate on the Deck

Save [install-to-deck.sh](scripts/install-to-deck.sh) on the Deck. Switch to Desktop Mode and open Konsole in the folder where you saved it. Local installation is the default. You do not need an IP address, SSH, or another computer.

The script uses Bash, curl, unzip, and the Deck's system Python. It asks for your user's sudo password when needed. If you have not set a password, run `passwd` in Konsole first. Plugins use the included Python runtime. The Deck does not need Rust, Poetry, pnpm, or pip packages.

The script downloads the pinned `v3.2.10-rust.6-rc.1` bundle through [nightly.link](https://nightly.link). No GitHub account, token, or GitHub CLI is required. It checks the runtime ZIP against the pinned SHA256 before it changes the installation. CI artifacts can expire. To use a saved runtime ZIP instead, add `--bundle /path/to/decky-loader-runtime-linux-x86_64.zip`. Use the inner runtime ZIP, not the outer Actions download.

Run these commands as your normal Deck user. The script requests sudo itself:

```sh
bash install-to-deck.sh --check
bash install-to-deck.sh
bash install-to-deck.sh --status
```

`--check` verifies the bundle and basic target requirements without stopping the service or changing the installation. Installation briefly stops the selected service to make a full backup of homebrew and its service configuration, then restores its running state before the maintained installer takes over. Backups stay outside homebrew, in the sibling `decky-runtime-backups` directory by default. Use `--backup-dir` to choose another location with enough space.

Space checks cover temporary files, the installed runtime, and the full backup. The check adds these requirements together when they share a storage volume. It checks separate volumes independently, including a separate volume mounted at `homebrew/services`. If space is too low, the script stops before it changes the service or creates a backup.

To restore the previous backend and service configuration:

```sh
bash install-to-deck.sh --undo
```

Rollback uses small recovery tools retained under `homebrew/services/.decky-deploy`; it does not need GitHub access. It does not restore the separate user-data archive automatically. Keep that recovery directory and your backups.

If an interruption occurs after the backend installer starts, the script runs its recovery before removing temporary files. This includes closing the terminal. An interrupted backup instead attempts to restart the previously running service. Use `--status` to check the service after an interruption. Installer recovery output is kept in `homebrew/services/.decky-deploy/plugin_loader.service/interrupted-recovery.log` for the default service.

Use `--help` for custom paths, service names, and candidate overrides. `DECK_USER` selects the plugin account and defaults to `deck`. `--non-interactive` disables password prompts. The script stays pinned while other runtime builds are investigated.

For optional deployment from another Linux computer, use `--remote HOST`, such as `bash install-to-deck.sh --remote steamdeck --check`. Remote mode requires SSH and SCP on that computer and SSH access to the Deck. `DECK_USER` also selects the SSH account, and `DECK_PORT` overrides its port. Setting `DECK_IP` no longer selects a remote target; use `--remote` explicitly.

After installation, open Decky in Gaming Mode. Open each plugin and use its main function. Check that settings persist after a plugin reload and after a reboot. Check suspend and resume. Keep the pinned candidate and backups until these checks pass on your Deck. If the interface or plugins fail, use `--undo` and check the previous installation again.


The [dependency audit](backend-rust/audit.sh) must pass security, source, and dependency-use checks. The [VM validation tools](backend-rust/benchmark_vm.py) use owned clones rather than replacing the normal installation. Their results do not establish compatibility with every third-party plugin or replace physical Steam Deck testing.

## 📜 Credits

The original idea for the plugin loader concept is based on the work of [marios8543's Steam Deck UI Inject project](https://github.com/marios8543/steamdeck-ui-inject).
