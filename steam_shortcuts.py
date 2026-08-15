# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pillow",
#     "urllib3",
#     "vdf",
# ]
# exclude-newer = "2025-06-14T00:00:00Z"
# ///
import io
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time
import traceback
import zipfile
import winreg
from os import path

import urllib3
import vdf
from PIL import Image, ImageOps

http = urllib3.PoolManager()
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
STEAM_COMMUNITY_ICON_URLS = (
    "https://shared.fastly.steamstatic.com/community_assets/images/apps/{appid}/{icon_hash}.ico",
    "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.ico",
    "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg",
    "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.png",
    "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.ico",
    "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg",
    "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.png",
)
STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
# Redistributables Steam installs alongside games. They sit in the libraries
# looking like games, but nothing launches them and they carry no icon, so
# they would only ever produce a dead shortcut and a failed icon lookup
EXCLUDED_APPIDS = {
    "228980",  # Steamworks Common Redistributables
    "1007",  # Steamworks SDK Redist
}
# How an appinfo.vdf section can be laid out, as (keys live in a string table,
# bytes of section header before the VDF blob). Newer files pool their keys and
# carry a second sha1; older ones do neither. Tried in turn rather than matched
# to a version, so a format we have not seen still reads if it looks like one
# of these
APPINFO_LAYOUTS = ((True, 60), (False, 40))


def main():
    """
    Where the magic happens
    """

    force_refresh = "--force-refresh" in sys.argv or "--force" in sys.argv
    steamcmd_only = "--steamcmd" in sys.argv

    # Get path to Steam and libraries
    steam_path = get_steam_path()
    library_path = get_steam_library_path(steam_path)
    libraries = get_library_folders(steam_path, library_path)

    if not libraries:
        print("No libraries to check")
        sys.exit(0)

    # Show game and folder info to user
    games = get_installed_games(libraries)
    print(
        f"Found {len(games)} game{'s' if len(games) > 1 else ''} in the following libraries:"
    )
    print("\n".join(map(lambda x: f"  {x}", libraries)))

    # Try and find any existing icons for the found games
    check_for_icons(games)
    found_icons = len([True for game in games.values() if game["icon"]])
    print(f"\nFound {found_icons} existing game icon{'s' if found_icons != 1 else ''}")

    if found_icons > 0:
        refresh_icons = force_refresh or (
            input("Refresh existing generated icons? y/[N] ").lower().strip() == "y"
        )
        if refresh_icons:
            removed_icons = clear_existing_icons(games)
            found_icons = len([True for game in games.values() if game["icon"]])
            print(
                f"Removed {removed_icons} generated icon{'s' if removed_icons != 1 else ''}"
            )

    # Ask the user if they'd like to download the missing icons
    # By default will download missing icons and create shortcuts with missing icons
    create_with_missing, try_download, start_menu = True, True, False
    missing = len(games) - found_icons
    if missing > 0:
        print(f"\nMissing icons for {missing} game{'s' if missing != 1 else ''}")
        if not force_refresh:
            try_download = input("Try to download them now? [Y]/n ").lower().strip() != "n"

    if try_download:
        get_icons(
            games,
            steam_path,
            steamcmd_only=steamcmd_only,
            ask=not (force_refresh or steamcmd_only),
        )

    # Check for any icons that are still missing
    failed = [game["name"] for game in games.values() if not game["icon"]]
    if failed and try_download:
        print(
            f"\nFailed to acquire the following {len(failed)} icon{'s' if len(failed) != 1 else ''}"
        )
        print("\n".join(map(lambda x: f"  {x}", failed)))

    # Ask if the user would like to create shortcuts with missing icons
    create_with_missing = (
        input("\nCreate shortcuts for games without icons? [Y]/n ").lower().strip()
        != "n"
    )

    start_menu = (
        input("\nAdd shortcuts to a Start Menu folder (requires Admin)? y/[N] ")
        .lower()
        .strip()
        == "y"
    )

    # Create shortcuts, show some stats, and exit
    try:
        count, folder = create_shortcuts(games, create_with_missing, start_menu)
    except PermissionError:
        print(
            "\n\nTo add to the start menu, please run this tool from an elevated (admin) terminal"
        )
        print("Falling back to ./shortcuts")
        count, folder = create_shortcuts(games, create_with_missing)

    print(f"\nDone! Created {count} shortcut{'s' if count != 1 else ''}")
    print(
        f"You can find them in {f'./{folder}' if not start_menu else f'your Start Menu ({folder})'}"
    )


def get_steam_library_path(steam_path: pathlib.Path) -> pathlib.Path:
    # Try and get the library index file as a sanity check for the right folder
    try:
        return pathlib.Path(
            [x for x in steam_path.glob("steamapps/libraryfolders.vdf")][0]
        )
    except IndexError:
        print("Could not locate local library.")
        sys.exit(-1)

def get_steam_path():
    """
    Tries to get the Steam installation folder, or asks the user.
    This will also get the location to the library listings file,
    which forms part of the check to make sure it's the right folder.

    Returns a tuple of (steam_path, library_index_path)
    """

    # Search Registry
    hkey = None
    try:
        hkey = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Valve\\Steam"
        )
    except OSError:
        print(sys.exc_info())
    steam_path = None
    if hkey:
        try:
            steam_path = winreg.QueryValueEx(hkey, "InstallPath")[0]
        except OSError:
            print(sys.exc_info())
        winreg.CloseKey(hkey)

    # Ask the user if the registry was unhelpful
    if not steam_path:
        steam_path = input(
            "Failed to find Steam installation path, please provide the path e.g. C:\\Program Files (x86)\\Steam, ~/.local/steam, etc\n"
        )

    steam_path = pathlib.Path(steam_path)

    return steam_path


def get_library_folders(steam_path, library_index_path):
    """
    Reads the library index file `libraryfolders.vdf` to get a list of all
    Steam library locations on your system.

    Because of the lazy way in which the RegEx is written, it only supports
    up to 100 Steam libraries... but come on.

    Returns the library locations
    """

    with open(library_index_path) as index_file:
        lib_vdf = vdf.load(index_file)

    paths = set()
    for lib in lib_vdf.get("libraryfolders", {}).values():
        if isinstance(lib, dict) and lib.get("path"):
            paths.add(lib["path"])
    
    return sorted(pathlib.Path(library_path) for library_path in paths)


def get_installed_games(libraries):
    """
    For each library, parse all the appmanifest_xxx.acf files for
    game names and install locations, where xxx is the appid of an installed game.

    Returns a dictionary of appid -> {name, location, icon, icon_hash}
    """

    # Horrible flattening of each manifest file
    manifests = [
        item
        for sublist in [
            [x for x in path.glob("steamapps/appmanifest_*.acf")] for path in libraries
        ]
        for item in sublist
    ]

    # We want to find the game name and install directory
    patterns = [re.compile('"name".+".+"'), re.compile('"installdir".+".+"')]
    games = dict()

    # Parse each manifest and build the games dict
    for m in manifests:
        try:
            appid = m.stem.split("_")[1]
            if appid in EXCLUDED_APPIDS:
                continue

            with open(m.resolve(), encoding="utf-8") as acf:
                lines = acf.readlines()
                name, location = [
                    p.search("\n".join([l.strip() for l in lines])) for p in patterns
                ]

                if name and location:
                    name, location = [
                        field[0].replace('"', "").split("\t\t")[1]
                        for field in [name, location]
                    ]
                    location = m.parent / f"common/{location}"
                    try:
                        location.resolve(strict=True)
                    except FileNotFoundError:
                        continue
                    games[appid] = {
                        "name": name,
                        "location": location,
                        "icon": None,
                        "icon_hash": None,
                    }
                else:
                    print(
                        f"  Couldn't locate name or location for game {m}\n  Name: {name}\n  Location: {location}\n"
                    )

        except KeyboardInterrupt as e:
            raise
        except Exception as e:
            print("Unhandled exception when reading file", m, e)

    return games


def is_icon_hash(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", value or ""))


def read_binary_vdf(data, pos, strings):
    """
    Reads one binary VDF object, returning (object, position after it).

    Keys are indexes into the file's string table from appinfo v28 onwards,
    and inline null terminated strings before that.
    """

    result = {}
    while True:
        node = data[pos]
        pos += 1
        if node == 0x08:
            return result, pos

        if strings is None:
            end = data.index(b"\x00", pos)
            key = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
        else:
            (index,) = struct.unpack_from("<I", data, pos)
            key = strings[index]
            pos += 4

        if node == 0x00:
            result[key], pos = read_binary_vdf(data, pos, strings)
        elif node == 0x01:
            end = data.index(b"\x00", pos)
            result[key] = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
        elif node in (0x02, 0x03, 0x06):
            result[key] = struct.unpack_from("<i", data, pos)[0]
            pos += 4
        elif node in (0x07, 0x0A):
            result[key] = struct.unpack_from("<q", data, pos)[0]
            pos += 8
        else:
            raise ValueError(f"unknown VDF node type {node:#x}")


def read_appinfo_string_table(data, offset):
    """
    Reads the pooled key names an appinfo.vdf section may refer to by index.

    Raises if the offset or count don't fit the file, which is how a layout
    that doesn't apply to this file gets rejected.
    """

    (table_offset,) = struct.unpack_from("<q", data, offset)
    if not 0 < table_offset < len(data) - 4:
        raise ValueError("string table outside file")

    (count,) = struct.unpack_from("<I", data, table_offset)
    if count > len(data) - table_offset:
        raise ValueError("string table longer than file")

    strings = []
    pos = table_offset + 4
    for _ in range(count):
        end = data.index(b"\x00", pos)
        strings.append(data[pos:end].decode("utf-8", "replace"))
        pos = end + 1

    return strings


def read_appinfo(data, wanted, pooled_keys, header):
    """
    Walks appinfo.vdf under one candidate layout.

    The file is a header, then one length prefixed section per app holding a
    binary VDF blob. Only the sections we were asked about get parsed, the
    rest are skipped over using that length.
    """

    strings = read_appinfo_string_table(data, 8) if pooled_keys else None
    offset = 16 if pooled_keys else 8
    hashes = {}

    while offset + 8 <= len(data):
        appid, size = struct.unpack_from("<II", data, offset)
        if appid == 0:
            break

        body = offset + 8
        if size <= header or body + size > len(data):
            raise ValueError("section runs past end of file")
        offset = body + size

        if str(appid) not in wanted:
            continue

        app, _ = read_binary_vdf(data, body + header, strings)
        # Sections wrap their contents in a single "appinfo" node
        common = app.get("appinfo", app).get("common", {})
        icon_hash = common.get("clienticon") if isinstance(common, dict) else None
        if is_icon_hash(icon_hash):
            hashes[str(appid)] = icon_hash

    return hashes


def parse_appinfo_clienticon_hashes(data, appids):
    """
    Pulls clienticon hashes for the given apps out of appinfo.vdf bytes.

    Each known layout is tried until one reads the whole file cleanly, rather
    than deciding from the version in the header. Valve bumps that version for
    changes that don't concern us, and a file we can't read costs nothing
    beyond falling through to SteamCMD.
    """

    wanted = {str(appid) for appid in appids}
    if len(data) < 16 or not wanted:
        return {}

    for pooled_keys, header in APPINFO_LAYOUTS:
        try:
            hashes = read_appinfo(data, wanted, pooled_keys, header)
        except Exception:
            continue
        if hashes:
            return hashes

    return {}


def appinfo_clienticon_hashes(appids, steam_path):
    """
    Reads icon hashes straight out of Steam's own metadata cache.

    Steam keeps the same app info SteamCMD would fetch in appcache/appinfo.vdf,
    so for anything already known to the local client this costs a file read
    rather than a download.
    """

    appids = [str(appid) for appid in appids]
    if not appids:
        return {}

    appinfo_path = steam_path / "appcache" / "appinfo.vdf"
    try:
        data = appinfo_path.read_bytes()
    except OSError:
        return {}

    try:
        return parse_appinfo_clienticon_hashes(data, appids)
    except Exception:
        return {}


def find_steamcmd(steam_path):
    script_steamcmd = pathlib.Path(__file__).resolve().parent / ".steamcmd" / "steamcmd.exe"
    candidates = [
        shutil.which("steamcmd"),
        shutil.which("steamcmd.exe"),
        steam_path / "steamcmd.exe",
        pathlib.Path("steamcmd.exe"),
        script_steamcmd,
    ]

    for candidate in candidates:
        if candidate and pathlib.Path(candidate).exists():
            return str(candidate)

    return None


def download_steamcmd():
    steamcmd_path = pathlib.Path(__file__).resolve().parent / ".steamcmd" / "steamcmd.exe"
    if steamcmd_path.exists():
        return str(steamcmd_path)

    print("    Downloading SteamCMD to fetch Steam icon metadata...")
    try:
        response = http.request(
            "GET",
            STEAMCMD_ZIP_URL,
            timeout=urllib3.Timeout(connect=10, read=60),
        )
    except Exception:
        return None

    if response.status != 200:
        return None

    try:
        steamcmd_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(response.data)) as steamcmd_zip:
            with steamcmd_zip.open("steamcmd.exe") as source, open(
                steamcmd_path, "wb"
            ) as destination:
                shutil.copyfileobj(source, destination)
    except Exception:
        return None

    return str(steamcmd_path) if steamcmd_path.exists() else None


def parse_steamcmd_clienticon_hashes(output, appids):
    """
    Pulls each app's clienticon out of `app_info_print` output.

    SteamCMD writes one block per app, each opening with the appid quoted on
    its own line, so split on that and search within a single block. Scanning
    the whole output for an appid followed by a clienticon runs past the end of
    its block, handing an app that has no icon of its own the next app's hash.
    """

    wanted = {str(appid) for appid in appids}
    hashes = {}

    blocks = re.split(r'^"(\d+)"$', output, flags=re.MULTILINE)
    for appid, block in zip(blocks[1::2], blocks[2::2]):
        if appid not in wanted:
            continue
        match = re.search(r'"clienticon"\s+"([0-9a-fA-F]{40})"', block)
        if match:
            hashes[appid] = match.group(1)

    return hashes


def steamcmd_needs_setup(steamcmd):
    """
    A freshly unzipped steamcmd.exe is only a bootstrapper. It unpacks a
    `package` folder alongside itself the first time it self-updates.
    """

    return not (pathlib.Path(steamcmd).resolve().parent / "package").is_dir()


def prepare_steamcmd(steamcmd):
    """
    Lets SteamCMD self-update before we capture anything from it.

    SteamCMD only reports progress when it has a console to write to, so this
    has to run without redirection, and that means a run of its own. It happens
    once, after which `package` exists and we skip straight to the app info.

    Expect a lot of output. SteamCMD bootstraps in two passes, updating itself
    to the full client and then to the current build, logging a line per
    package each time. That is one setup, not a loop.
    """

    print("    Setting up SteamCMD, this only happens once...", flush=True)
    try:
        subprocess.run([steamcmd, "+quit"], timeout=600)
    except Exception:
        pass

    print("    SteamCMD ready", flush=True)


def run_steamcmd(command, expected):
    """
    Runs SteamCMD, counting off each app's metadata as it arrives.

    SteamCMD prints an `AppID : <id>` header before each block it dumps, which
    is the only progress going on the first run, where every app is fetched
    individually and the whole thing takes half a minute. The blocks after
    those headers are about a megabyte of VDF, so they go to the returned
    output rather than the terminal.

    Returns the combined output.
    """

    lines = []
    read = 0
    deadline = time.monotonic() + 300

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    with process:
        for line in process.stdout:
            lines.append(line)
            if line.startswith("AppID : "):
                read += 1
                print(f"\r    Read metadata for {read} of {expected} app(s)", end="", flush=True)
            if time.monotonic() > deadline:
                process.kill()
                break

    if read:
        print()

    return "".join(lines)


def steamcmd_clienticon_hashes(appids, steam_path):
    appids = [str(appid) for appid in appids]
    if not appids:
        return {}

    steamcmd = find_steamcmd(steam_path) or download_steamcmd()
    if steamcmd is None:
        return {}

    if steamcmd_needs_setup(steamcmd):
        prepare_steamcmd(steamcmd)

    print(f"    Fetching clienticon hashes from SteamCMD for {len(appids)} app(s)...")
    command = [
        steamcmd,
        "+login",
        "anonymous",
        "+app_info_update",
        "1",
    ]
    for appid in appids:
        command.extend(["+app_info_print", appid])
    command.append("+quit")

    try:
        output = run_steamcmd(command, len(appids))
    except Exception:
        return {}

    return parse_steamcmd_clienticon_hashes(output, appids)


def check_for_icons(games):
    """
    For each game, checks to see if an icon exists the  game by looking
    for generated icons in the game's installation directory
    """

    for appid, game in games.items():
        for icon_path in local_generated_icon_paths(game, appid):
            try:
                games[appid]["icon"] = icon_path.resolve(strict=True)
                break
            except Exception:
                continue


def local_generated_icon_paths(game, appid):
    paths = [get_icon_path(game, appid)]
    if game["icon_hash"]:
        paths.append(pathlib.Path(game["location"] / f"{game['icon_hash']}.ico"))
    return paths


def clear_existing_icons(games):
    removed = 0
    for appid, game in games.items():
        for icon_path in local_generated_icon_paths(game, appid):
            try:
                icon_path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                continue

        game["icon"] = None

    return removed


def get_icon_path(game, appid):
    return pathlib.Path(game["location"] / f"steam_shortcut_icon_{appid}.ico")


def image_to_icon(image):
    image = ImageOps.exif_transpose(image).convert("RGBA")
    icon = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    contained = ImageOps.contain(image, (256, 256), Image.Resampling.LANCZOS)
    icon.alpha_composite(
        contained,
        ((icon.width - contained.width) // 2, (icon.height - contained.height) // 2),
    )
    return icon


def open_best_frame(data):
    """
    Opens image bytes, picking the richest frame of a multi-frame .ico.

    Pillow sorts .ico entries by ascending colour depth and hands back the
    first entry matching a size, so an icon shipping a 1-bit monochrome frame
    alongside a full colour frame of the same dimensions (e.g. appid 534380)
    decodes as a flat block of white. Choose the largest, deepest frame here
    instead of taking Pillow's default.
    """

    with Image.open(io.BytesIO(data)) as image:
        if image.format == "ICO":
            entries = image.ico.entry
            best = max(
                range(len(entries)),
                key=lambda i: (entries[i].square, entries[i].bpp),
            )
            image = image.ico.frame(best)

        image.load()
        return image.copy()


def download_steam_icon(appid, icon_hash):
    if not icon_hash:
        return None

    for url_template in STEAM_COMMUNITY_ICON_URLS:
        url = url_template.format(appid=appid, icon_hash=icon_hash)
        try:
            response = http.request("GET", url)
        except Exception:
            continue

        if response.status != 200:
            continue

        try:
            return open_best_frame(response.data)
        except Exception:
            continue

    return None


def get_steam_icon(appid, game):
    icon_hash = game.get("icon_hash")
    image = download_steam_icon(appid, icon_hash)
    if image is not None:
        return image

    raise Exception(f"No Steam icon found for appid {appid}")


def report_unresolved(unresolved):
    print(
        f"\n  Steam's local metadata has no icon for {len(unresolved)} "
        f"game{'s' if len(unresolved) != 1 else ''}:"
    )
    for appid, game in unresolved:
        print(f"    {game['name']} ({appid})")


def confirm_steamcmd_lookup(unresolved):
    report_unresolved(unresolved)
    print("  SteamCMD can look these up. Its first run downloads about 70MB and")
    print("  takes around 45 seconds, later runs a few seconds.")

    return input("  Ask SteamCMD? y/[N] ").lower().strip() == "y"


def get_icons(games, steam_path, steamcmd_only=False, ask=True):
    """
    This will attempt to build missing icons from Steam's app metadata,
    falling back to SteamCMD for anything the local cache doesn't know.
    It might fail, in which case the {appid -> icon} remains None
    """

    missing_games = [(appid, game) for appid, game in games.items() if not game["icon"]]
    missing_hash_appids = [appid for appid, game in missing_games if not game["icon_hash"]]

    if steamcmd_only:
        for appid, icon_hash in steamcmd_clienticon_hashes(
            missing_hash_appids, steam_path
        ).items():
            games[appid]["icon_hash"] = icon_hash
        unresolved = []
    else:
        for appid, icon_hash in appinfo_clienticon_hashes(
            missing_hash_appids, steam_path
        ).items():
            games[appid]["icon_hash"] = icon_hash

        unresolved = [
            (appid, game) for appid, game in missing_games if not game["icon_hash"]
        ]

    if unresolved and not ask:
        # Nobody to ask, so say what was missed and how to go and get it
        report_unresolved(unresolved)
        print("  Run again with --steamcmd to look these up.")
        unresolved = []

    if unresolved and confirm_steamcmd_lookup(unresolved):
        for appid, icon_hash in steamcmd_clienticon_hashes(
            [appid for appid, _ in unresolved], steam_path
        ).items():
            games[appid]["icon_hash"] = icon_hash

    for appid, game in missing_games:
        print(f"  Finding icon for {appid} ({game['name']})")
        try:
            icon_path = get_icon_path(game, appid)
            icon = image_to_icon(get_steam_icon(appid, game))
            icon.save(icon_path, format="ICO", sizes=ICON_SIZES)

            # Set the icon location for the game
            games[appid]["icon"] = icon_path
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"    {e}")
            with open("error_log.txt", "a", encoding="utf-8") as f:
                f.write(f"{appid} ({game['name']}): {e}\n")


def create_shortcuts(games, create_with_missing, start_menu=False):
    """
    For each game, now create the URL shortcuts to steam://rungameid/{appid},
    set the icon if it exists, or blank if the user asks for icon-less shortcuts

    Returns the number of shortcuts created
    """

    if start_menu:
        s = pathlib.Path(
            path.expandvars(
                "%SystemDrive%\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs"
            )
        )
        folder = s / "Steam Games"
    else:
        folder = pathlib.Path("./shortcuts")
    folder.mkdir(parents=True, exist_ok=True)
    count = 0
    for appid, game in games.items():
        # Sanitise the game's name for use as a filename
        filename = re.sub(r'[\\/*?:"<>|]', "", game["name"]) + ".url"

        # Skip game if missing the icon and the user asked to
        # not create shortcuts with missing icons
        if not game["icon"] and not create_with_missing:
            continue

        # Write the shortcut file
        with open(folder / filename, "w+", encoding="utf-8") as shortcut:
            shortcut.write("[InternetShortcut]\n")
            shortcut.write("IconIndex=0\n")
            shortcut.write(f"URL=steam://rungameid/{appid}\n")
            shortcut.write(f"IconFile={game['icon']}\n")
        count += 1

    return (count, folder)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt as ke:
        print(ke)
    except Exception:
        print("Unexpected exception")
        traceback.print_exc()
