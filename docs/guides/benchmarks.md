# Speed & Memory Benchmarks

This fork replaces Official Decky's Python backend loader with a compiled **Rust** service, reducing Decky's own memory footprint, startup time, shutdown time, and idle CPU usage.

> [!NOTE]
> These are historical measurements of `v3.3.0-rust.1-rc.1` (17 October 2026) and a development build from 19 October 2026. Later releases, including the current stable `v3.3.1-rust.1`, include startup and frontend performance changes that these measurements do not reflect. The figures have not been re-measured.

---

## Official Decky vs. This Fork

We compared this fork against Official Decky on the same computer with the same plugins (in each run, one plugin had no backend, and the rest had Python backends — matching almost all plugins currently in stores). **Smaller numbers are better.**

| What we measured (12 plugins, unless noted) | Official Decky | This fork | Difference |
|---|---:|---:|---|
| Memory used by Decky, no plugins (MB) | 38.6 | 8.4 | **30.2 MB less (78% less)** |
| Memory used by Decky and plugins (MB) | 159.4 | 155.6 | 3.8 MB less (2% less) |
| Time to start (seconds) | 1.94 | 0.88 | **1.06 s faster (55% faster)** |
| Time to stop (seconds) | 5.36 | 0.18 | **5.18 s faster (97% faster)** |
| CPU use while idle (% of one CPU core) | 0.096 | 0.067 | **30% less** |
| CPU time for one plugin call (microseconds) | 222 | 127 | **43% less** |

With 4 and 8 plugins, results were almost identical: the fork used about **3 MB less memory** and **40–55% less idle CPU**, and started **1.6 to 1.9 seconds faster**.

The largest memory reduction is in Decky itself. Existing plugins still use roughly the same memory because most plugins are written in Python, and this fork still runs them in Python (now on Python 3.14.8, sharing a single worker process across normal user plugins).

### How we measured

- Official Decky `v3.2.10` compared against `v3.3.0-rust.1-rc.1` on 17 October 2026.
- Each test ran 10 times; the table shows the average.
- Tests ran inside a SteamOS virtual machine (without the Steam user interface), not on physical Steam Deck hardware — real-world numbers on a handheld can vary.
- Memory figures measure Proportional Set Size (**PSS**); `1 MB = 1,048,576 bytes`.

---

## Python Plugins vs. Native Rust Plugins

Plugin authors can also write a plugin's backend in **Rust** instead of Python (see the [Plugin Authors Guide](./plugin-authors.md)). That capability is unique to this fork.

We tested 12 Python plugins vs. 12 native Rust plugins doing equivalent simple work:

| What we measured (12 plugins, unless noted) | Python plugins | Rust plugins | Difference |
|---|---:|---:|---|
| Memory used by Decky and plugins, 1 plugin (MB) | 44.2 | 6.6 | **37.6 MB less (85% less)** |
| Memory used by Decky and plugins (MB) | 119.6 | 18.3 | **101.3 MB less (85% less)** |
| Memory for each extra plugin (MB) | 6.9 | 1.1 | **5.8 MB less (84% less)** |
| Time to start (seconds) | 0.60 | 0.29 | **0.31 s faster (52% faster)** |
| Time to stop (seconds) | 0.034 | 0.013 | **0.021 s faster (62% faster)** |
| CPU time for one plugin call (microseconds) | 113 | 87 | **23% less** |
| Time for one plugin call (microseconds) | 154 | 125 | **19% faster** |

While idle, both setups used roughly `0.02%` of one CPU core.

> [!NOTE]
> The largest RAM saving happens when all active plugins have native backends. If even one Python plugin is enabled, Decky starts the shared Python worker alongside native plugins, reducing the net memory difference. Mixed Python + Rust sets were not benchmarked separately.

### How we measured

- Development build of this fork from 19 October 2026, in the same SteamOS virtual machine without the Steam UI.
- Each plugin answered one simple call; real-world plugins doing heavier work will consume more memory and CPU.
- Rust test plugins were copies of [`native_example.rs`](../../backend-rust/decky-plugin/examples/native_example.rs).
- Ran each test 5 times with 1, 4, 8, and 12 plugins and averaged results; stop times were measured separately using systemd service teardown.

---

## Checking Live Resource Use on Your Deck

You can see real-time CPU and memory usage for Decky and each individual plugin on your own device anytime by running:

```sh
bash ~/Downloads/install-to-deck.sh --status
```

Check the `resources` section of the output for per-plugin metrics.
