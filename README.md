![](https://github.com/JeeZeh/steam-shortcut-generator/blob/master/icon.png)

# Steam Shortcut Generator

Ever needed to recreate all of your Steam shortcuts? Maybe you forgot to back them up when reinstalling your OS, or just never created them in the first place...

If you have a large library of installed games and you don't want to manually create the shortcut for each one, then this is the tool for you!

[Here's a demo!](https://www.youtube.com/watch?v=eH-ouDx1Y68)

> **Steam now supports this through selecting multiple library games > Manage > Create Desktop Shortcuts**.
> It can't, however, add them to the start menu for you automatically. This tool can!

---

## Notes

- This tool is currently Windows-only. But I might take the time to add Linux/macOS support if requested!

- The shortcuts created are .url links, just like the ones Steam creates - this is because I can't tell which .exe is the one that launches the game, plus Steam sometimes likes to give you launch dialogues

---

## Usage

1. Install `uv`: https://docs.astral.sh/uv/
2. Run the script with `uv run --python 3.13 .\steam_shortcuts.py`
3. Follow the prompts to create shortcuts with or without icons.
4. If you already generated icons before, the script can refresh them when prompted.
5. The shortcuts will be created in `./shortcuts`, relative to wherever the script was run from, or the Start Menu if requested.

To force icon regeneration without the refresh prompt, run:

```powershell
uv run --python 3.13 .\steam_shortcuts.py --force-refresh
```

## What does it do?

1. Checks your registry for the Steam install folder
2. Reads `steamapps/libraryfolders.vdf` to find out where all your Steam libraries are located
3. For each library, parse all the `appmanifest_xxx.acf` files for game names and install locations, where xxx is the appid of an installed game
4. For each game, check if a generated icon already exists in the game's installation folder
5. Uses SteamCMD to fetch each missing game's `clienticon` metadata
6. Downloads the matching icon from Steam's CDN and writes a multi-size `.ico` file in the game's installation folder
7. For each game, now create the URL shortcuts to `steam://rungameid/{appid}`, set the icon if it exists, or blank if the user asks for icon-less shortcuts
8. Done! No tidying up is done since the icons are kept in the game folders for use by each shortcut. I guess things might break if you uninstall the game, but they're just shortcuts :)

## Warnings

- Icons are resolved through SteamCMD metadata. The script will use `steamcmd.exe` if it is already available, or download it into `.steamcmd` if needed.
- The tool does not need your Steam profile, username, or public library visibility.
- If SteamCMD cannot return icon metadata for a game, the tool can still create an icon-less shortcut if you want.
- It only supports Windows, and does simple checks for sanity like current folder etc. - if you don't follow the usage instructions (or if you do) and it breaks something, don't blame me (all it does is write icon files and create shortcuts)
- Not all icons created may be usable, like Steamworks Redist, Proton, etc.
