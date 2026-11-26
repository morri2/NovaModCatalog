import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Literal

REPO = "morri2/NovaModCatalog"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = SCRIPT_DIR / ".modsync_config.json"

PATTERN = re.compile(r"^Nova(Map|Mod)(?:-([\w.-]+))?_v\d+\.\d+$")

Kind = Literal["Map", "Mod"]
Release = tuple[str, str, bool, str]
Choice = (
    tuple[Literal["release"], str, str]
    | tuple[Literal["local"], Path]
    | tuple[Literal["skip"]]
)
MenuOption = tuple[str, Choice]


def get_game_dir() -> Path:
    if CONFIG.exists():
        try:
            data: Any = json.loads(CONFIG.read_text(encoding="utf-8"))
            saved = data.get("game_dir") if isinstance(data, dict) else None

            if isinstance(saved, str):
                path = Path(saved).expanduser()
                if path.is_dir():
                    return path.resolve()
        except (OSError, json.JSONDecodeError):
            pass

        print("Saved game location is invalid.")

    while True:
        value = input("Enter Civilization V install folder: ").strip().strip('"')
        path = Path(value).expanduser()

        if path.is_dir():
            path = path.resolve()
            CONFIG.write_text(
                json.dumps({"game_dir": str(path)}, indent=2),
                encoding="utf-8"
            )
            return path

        print("Folder not found. Please try again.")


def read_key() -> str | None:
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getch()
        if key in (b"\x00", b"\xe0"):
            return {b"H": "up", b"P": "down"}.get(msvcrt.getch())

        return {b"\r": "enter", b"\x1b": "esc"}.get(key)

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        key = sys.stdin.read(1)

        if key == "\x1b" and select.select([sys.stdin], [], [], 0.1)[0]:
            key += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return {
        "\x1b[A": "up",
        "\x1b[B": "down",
        "\x1bOA": "up",
        "\x1bOB": "down",
        "\r": "enter",
        "\x1b": "esc"
    }.get(key)


def choose(
    title: str,
    options: list[MenuOption],
    mod_name: str | None = None
) -> Choice | None:
    selected = 0

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"NovaMod Update - {title}\n")
        print("↑/↓ Navigate   Enter Select   Esc Exit")

        if mod_name is not None:
            print(f"(Mod: {mod_name})")

        print()

        for i, (label, _) in enumerate(options):
            print(("> " if i == selected else "  ") + label)

        key = read_key()

        if key == "up":
            selected = (selected - 1) % len(options)
        elif key == "down":
            selected = (selected + 1) % len(options)
        elif key == "enter":
            return options[selected][1]
        elif key == "esc":
            return None


def get_options(
    kind: Kind,
    releases: dict[Kind, list[Release]],
    game_dir: Path
) -> list[MenuOption]:
    destination = game_dir / ("Maps" if kind == "Map" else "Mods")

    installed = {
        p.name for p in destination.iterdir() if p.is_dir()
    } if destination.exists() else set()

    options: list[MenuOption] = [
        (("* " if name in installed else "") + name,
         ("release", name, url))
        for name, url, _, _ in releases[kind]
    ]

    local = SCRIPT_DIR / f"Nova{kind}"

    if local.is_dir():
        options.append((
            ("* " if local.name in installed else "") + "dev",
            ("local", local)
        ))

    options.append(("skip", ("skip",)))
    return options


def download(url: str, file: BinaryIO) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "ModSync"}
    )

    with urllib.request.urlopen(request) as response:
        total = int(response.headers.get("Content-Length", 0) or 0)
        downloaded = 0
        width = 15

        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break

            file.write(chunk)
            downloaded += len(chunk)

            if total:
                percent = min(downloaded / total, 1)
                filled = int(width * percent)
                bar = "#" * filled + "-" * (width - filled)
                size = f"{downloaded / 1048576:.1f}/{total / 1048576:.1f} MB"

                sys.stdout.write(
                    f"\r[{bar}] {percent:6.1%}  {size}"
                )
            else:
                sys.stdout.write(
                    f"\rDownloaded {downloaded / 1048576:.1f} MB"
                )

            sys.stdout.flush()

    print()


def clear_installation(kind: Kind, destination: Path) -> None:
    prefix = f"Nova{kind}".lower()

    for folder in destination.iterdir():
        if folder.is_dir() and (
            folder.name.lower() == kind.lower()
            or folder.name.lower().startswith(prefix)
        ):
            shutil.rmtree(folder)

def install(kind: Kind, choice: Choice, game_dir: Path) -> None:
    match choice:
        case ("skip",):
            return

        case ("local", source):
            destination = game_dir / ("Maps" if kind == "Map" else "Mods")
            destination.mkdir(parents=True, exist_ok=True)
            clear_installation(kind, destination)
            shutil.copytree(source, destination / source.name)
            print(f"Installed {kind}: dev")

        case ("release", name, url):
            destination = game_dir / ("Maps" if kind == "Map" else "Mods")
            destination.mkdir(parents=True, exist_ok=True)

            print(f"\nDownloading {name}...")

            with tempfile.TemporaryFile(mode="w+b") as file:
                download(url, file) #type: ignore
                file.seek(0)

                with zipfile.ZipFile(file) as archive:
                    if archive.testzip():
                        raise ValueError("Downloaded ZIP is corrupted.")

                    base = destination.resolve()

                    for item in archive.infolist():
                        target = (destination / item.filename).resolve()
                        if target != base and base not in target.parents:
                            raise ValueError("Unsafe path in ZIP.")

                    clear_installation(kind, destination)
                    archive.extractall(destination)

            print(f"Installed {name}")


def get_releases() -> dict[Kind, list[Release]]:
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    request = urllib.request.Request(
        url, headers={"User-Agent": "ModSync"}
    )

    with urllib.request.urlopen(request) as response:
        data: Any = json.load(response)

    if not isinstance(data, list):
        raise ValueError("Invalid GitHub API response.")

    items: dict[Kind, list[Release]] = {"Map": [], "Mod": []}

    for release in data:
        if not isinstance(release, dict):
            continue

        name = release.get("name")
        if not isinstance(name, str):
            continue

        match = PATTERN.fullmatch(name)
        if not match:
            continue

        assets = release.get("assets", [])
        if not isinstance(assets, list):
            continue

        asset_url = next(
            (
                asset.get("browser_download_url")
                for asset in assets
                if isinstance(asset, dict)
                and asset.get("name") == name + ".zip"
                and isinstance(asset.get("browser_download_url"), str)
            ),
            None
        )

        if not isinstance(asset_url, str):
            continue

        kind: Kind = "Map" if match.group(1) == "Map" else "Mod"
        published = release.get("published_at")

        items[kind].append((
            name,
            asset_url,
            bool(match.group(2)),
            published if isinstance(published, str) else ""
        ))

    for kind in items:
        items[kind].sort(key=lambda item: item[3], reverse=True)
        items[kind].sort(key=lambda item: item[2])

    return items


def main() -> None:
    game_dir = get_game_dir()
    releases = get_releases()

    mod = choose(
        "Select a mod",
        get_options("Mod", releases, game_dir)
    )

    if mod is None:
        return

    if mod[0] == "release":
        mod_name = mod[1]
    elif mod[0] == "local":
        mod_name = "dev"
    else:
        mod_name = "None"

    map_choice = choose(
        "Select a map",
        get_options("Map", releases, game_dir),
        mod_name
    )

    if map_choice is None:
        return

    install("Mod", mod, game_dir)
    install("Map", map_choice, game_dir)

    print("\nModSync finished.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nError: {error}")

    input("\nPress Enter to close...")