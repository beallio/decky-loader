# Plugins That Need Administrator Access (Root Plugins)

Some hardware-control plugins — for example **PowerTools** — need administrator (`root`) permissions to adjust CPU/GPU limits or kernel settings.

Unlike Official Decky, where the entire loader and every plugin run as `root` all the time, this fork runs Decky and normal plugins under your regular `deck` user account by default and keeps root plugins turned **off** until you opt in.

---

## Turning Root Plugins On

If you try to use a plugin that needs administrator access while the feature is disabled, Decky displays a **"This plugin needs root"** notice instead of running it with root privileges.

To turn support on once:

- **With the graphical installer:** Open `Install-Decky-Rust.desktop` and choose **Enable Root Plugins** (only appears if Decky Rust is already installed).
- **From Konsole / Terminal:**
  ```sh
  bash ~/Downloads/install-to-deck.sh --enable-root-plugins --channel preview
  ```

What happens when you enable it:

1. The installer updates/verifies your Decky installation and asks for your login password **once** (on a fresh Steam Deck with no password set, run `passwd` in Konsole first).
2. If you already have root-flagged plugins installed, the CLI lists them and asks you to confirm (`y`) allowing them to run as `root`.
3. Only plugins flagged `root` run inside the separate isolated `decky-root-plugins` background service. Decky itself and all your other plugins keep running as your normal user account.

After setup, you can install, update, and uninstall root plugins directly in Gaming Mode like any other plugin, and whenever Decky updates itself, the root host service updates automatically alongside it.

---

## What Happens Each Time You Install or Update a Root Plugin

Before installing or updating any root-flagged plugin, Decky requires explicit confirmation:

1. Read the warning banner and wait **10 seconds**.
2. Toggle **"I understand"**.
3. Enter your account's login password (`sudo` password).

If you mistype your password, the installation stops and enforces a **5-second wait** before letting you retry.

---

## Security Rules & Limitations

- **No local ZIP installs:** Plugins needing root access can only be installed from an active **plugin store** or an **`https://` URL** in Developer settings — local `.zip` uploads are blocked for root plugins.
- **Password required (`NOPASSWD` blocked):** Because your login password is the confirmation gate for granting root access, root plugin installation is blocked if your account is configured for passwordless `sudo`.
- **Trusted stores only:** Your password confirms your intent — Decky allows installing any root plugin you explicitly confirm with your password from any configured store or `https://` address, so install only plugins you trust.
- **Normal folder location:** Just like Official Decky, root plugins keep their configuration and logs in your normal `~/homebrew` folders.

---

## Memory & CPU Cost

- **When at least one root plugin is active:** One separate `decky-root-plugins` background helper runs, using about **25 MB** of RAM and virtually **0% CPU** while idle.
- **When no root plugins are active:** Nothing extra runs at all (socket-activated on demand — zero extra memory or CPU cost).

---

## Turning Root Plugins Off

You can disable root plugin support anytime:

- **With the graphical installer:** Open `Install-Decky-Rust.desktop` and choose **Disable Root Plugins**.
- **From Konsole / Terminal:**
  ```sh
  bash ~/Downloads/install-to-deck.sh --disable-root-plugins
  ```

This stops and removes the root plugin service and returns ownership of plugin files, data, and logs inside your `homebrew` folder back to your normal user account. Any files a root plugin wrote directly into `/root` remain there unless removed manually.

For implementation details on the privilege-separation boundary, socket activation, and `systemd` unit design, see [Root Plugin Host Design](../root-plugin-host.md).
