## Flathub reproducibility checker

A tool to rebuild Flatpaks published on Flathub and compare
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
pip install --user git+https://github.com/flathub-infra/flathub-repro-checker.git@main#egg=flathub_repro_checker
```

### Usage

```sh
flathub-repro-checker --flatpak-id $FLATPAK_ID
```

### View the result

A folder named by `diffoscope_result-$FLATPAK_ID` is created
in the current working directory by default if the result is not
reproducible. To view the HTML report run (replace
`diffoscope_result-$FLATPAK_ID` with the actual folder):

```sh
bash -c 'trap "kill \$!" EXIT; python3 -m http.server --directory diffoscope_result-$FLATPAK_ID & xdg-open http://localhost:8000 >/dev/null 2>&1; wait'
```

### Development

```sh
uv run ruff format
uv run ruff check --fix --exit-non-zero-on-fix
uv run mypy .
```
