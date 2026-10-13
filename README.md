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

The experimental Rust compatibility probe is for private test systems only. It keeps Python plugins in a separate packaged Python runtime and uses the existing frontend. It is not a replacement installer and does not provide plugin installation or loader updates. See the [private bundle setup command](experiments/rust-runtime-probe/prepare_bundle.py) and [dependency audit](experiments/rust-runtime-probe/audit.sh).

The current probe uses the `v3.2.10` source baseline. Bundle setup verifies the tracked Python SDK and frontend against that tag and records their source hashes. The packaged worker must report the same version.

Worker builds use a single executable by default. For the shared-directory packaging experiment, set `DECKY_WORKER_ONEDIR=1` when building `backend/worker.spec`. Keep the executable and its `_internal` directory together; bundle setup copies and checks both.

Packaged workers default to the lean SDK startup path. Legacy SDK modules load when a plugin imports them. Use the worker's `--bootstrap full` option for a full-startup comparison; both paths use the same Python runtime and plugin API.

For the shared-launcher experiment, pass `--worker-mode fork` to the Rust probe. One single-threaded Python launcher loads the SDK and forks a separate process for each plugin. Plugin code runs only in the child process, with the existing user permissions and socket API. The default `exec` mode starts each worker separately. Both modes retain process-group cleanup on worker failure and shutdown.

The [SteamOS VM benchmark driver](experiments/rust-runtime-probe/benchmark_vm.py) compares the installed Python backend with both worker formats. It requires root access on a private VM with Decky Metadata, CSS Loader, SDH-Ludusavi, and HLTB for Deck installed. Use `--help` for the required executable and output paths. It runs at least ten trials per format using cloned plugin and settings files, private network namespaces, and complete process-group memory and CPU measurements. It leaves the normal service running and records raw trials, failures, and summary statistics. These are backend measurements, not full-product performance or production-parity results.

The dependency audit must pass its security, dependency-source, and dependency-use checks. License findings are reported for information only and do not block this private experiment.

## 📜 Credits

The original idea for the plugin loader concept is based on the work of [marios8543's Steam Deck UI Inject project](https://github.com/marios8543/steamdeck-ui-inject).
