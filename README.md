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
pip install --user git+https://github.com/flathub-infra/flathub-repro-checker.git@v0.1.5#egg=flathub_repro_checker
```

### Usage

```sh
flathub-repro-checker --appid $FLATPAK_ID
```

```
usage: flathub-repro-checker [-h] --appid APPID [--output-dir OUTPUT_DIR] [--cleanup] [--version]

Flathub reproducibility checker

options:
  -h, --help            show this help message and exit
  --appid APPID         App ID on Flathub
  --output-dir OUTPUT_DIR
                        Output dir for diffoscope report (default: ./diffoscope_result-$FLATPAK_ID)
  --cleanup             Cleanup all state
  --version
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
