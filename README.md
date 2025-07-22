## Flathub reproducibility checker

A tool to rebuild Flatpak apps published on Flathub and compare
reproducibility using [diffoscope](https://diffoscope.org/).

### Dependencies

Debian/Ubuntu:

```sh
sudo apt install flatpak flatpak-builder ostree diffoscope
```

ArchLinux:

```sh
sudo pacman -S --needed flatpak flatpak-builder ostree diffoscope
```

Fedora:

```sh
sudo dnf install flatpak flatpak-builder ostree diffoscope
```

### Install

```sh
pip install --user git+https://github.com/flathub-infra/flathub-repro-checker.git@v0.1.7#egg=flathub_repro_checker
```

### Usage

```sh
flathub-repro-checker --appid $FLATPAK_ID
```

```
Flathub reproducibility checker

options:
  -h, --help         Show this help message and exit
  --version          Show the version and exit
  --appid            App ID on Flathub
  --json             JSON output. Always exits with 0 unless fatal errors
  --ref-build-path   Install the reference build from this OSTree repo path instead of Flathub
  --output-dir       Output dir for diffoscope report (default: ./diffoscope_result-$FLATPAK_ID)
  --cleanup          Cleanup all state

    STATUS CODES:
      0   Success
      42  Unreproducible
      1   Failure

    JSON OUTPUT FORMAT:

    Always exits with 0 unless fatal errors. All values are
    strings. "appid", "message", "log_url" can be empty
    strings.

      {
        "timestamp": "2025-07-22T04:00:17.099066+00:00"  // ISO Format
        "appid": "com.example.baz",                      // App ID
        "status_code": "42",                             // Status Code
        "log_url": "https://example.com",                // Log URL
        "message": "Unreproducible"                      // Message
      }
```

### View the result

A folder named by `diffoscope_result-$FLATPAK_ID` is created
in the current working directory by default if the result is not
reproducible.

To view the HTML report run:

```sh
python3 -m http.server 8080 -d diffoscope_result-$FLATPAK_ID
```

Then open it in browser:

```sh
xdg-open http://localhost:8080
```

### Development

```sh
uv run ruff format
uv run ruff check --fix --exit-non-zero-on-fix
uv run mypy .
```
