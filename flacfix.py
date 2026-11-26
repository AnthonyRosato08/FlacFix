#!/usr/bin/env python3
"""FlacFix - tidy a folder of FLAC albums.

Run on an artist folder (containing album folders) or on a single album folder.
For every track that has not been processed yet it will:
  * set Artist (+ featured artists), Album Artist and Album (= folder name)
  * clean the title / filename: drop "(Bonus Track)", "(feat. X)" / "(with X)",
    turn "(Live ...)" / "- Live at ..." into "- Live", number duplicates "- V2"
  * embed cover art (1200px) - the image in the folder, else art from a processed
    sibling, else the best match downloaded from the iTunes API
  * give each file a Finder icon that looks like the cover
  * move the used cover image file out of the folder (to the Trash)
Processed tracks are tagged FLACFIX=1 so re-running skips them (use --force to redo).
"""
import argparse
import collections
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from mutagen.flac import FLAC, Picture

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = None

def b(text):
    """Bold when writing to a terminal (still plain black and white)."""
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[1m{text}\033[0m"
    return str(text)


def rule():
    width = LINE_WIDTH
    print("\n" + "_" * width + "\n")


VERSION = "1.1"
LINE_WIDTH = 60
OPTIONS = [
    ("--Help", "", "Show this help message and exit."),
    ("--Version", "", "Show the version number and exit."),
    ("--Artist", "[name]", "Enter the Artist Name as an argument."),
    ("--Convert", "[old] [new]", "Rename one track (file and title) to a new name."),
    ("--Force", "", "Redo tracks that were already processed."),
    ("--NoIcons", "", "Don't set Finder icons."),
    ("--NoDownload", "", "Never fetch covers from the internet."),
    ("--NoWith", "", 'Don\'t treat "(with X)" as a featured artist.'),
]


WHAT_IT_DOES = [
    "Sets Artist, Album Artist and Album (folder name) on each track.",
    "Cleans names: drops (Bonus Track) and (feat. X), shortens Live.",
    "Credits featured artists in the Artist tag, numbers duplicates.",
    "Embeds cover art (folder image or iTunes) at up to 1200px.",
    "Gives each file a Finder icon that looks like its cover.",
    "Trashes the used cover image and lists tracks missing lyrics.",
    "Rename a single track (file and title) with --Convert.",
    "Skips tracks already processed, so it is safe to re-run.",
]


def banner():
    print(f"{b('FlacFix')} Version {VERSION}\nCC Anthony Rosato 2026")


HELP_SHOWN = False


def show_help():
    global HELP_SHOWN
    HELP_SHOWN = True
    size = shutil.get_terminal_size((77, 24))
    out = [f"{b('FlacFix')} Version {VERSION}", "CC Anthony Rosato 2026", "",
           f"{b('Usage:')} FlacFix <folder> [options]", "", b("Options:")]
    col = max(len(f"{o} {m}".strip()) for o, m, _ in OPTIONS) + 2
    for o, m, d in OPTIONS:
        pad = " " * (col - len(f"{o} {m}".strip()))
        out.append(f"  {b(o)}{(' ' + m) if m else ''}{pad}{d}")
    out += ["", b("What it does:")] + [f"  - {t}" for t in WHAT_IT_DOES]
    out.append("")  # always keep a blank line at the bottom
    if sys.stdout.isatty():
        # fill the whole window: clear it, and leave the last row for the prompt
        out += [""] * max(0, size.lines - 1 - len(out))
        sys.stdout.write("\033[2J\033[H")
    print("\n".join(out))


HERE = Path(__file__).resolve().parent
SETICON = HERE / "seticon"
MARKER, MARKER_VER = "FLACFIX", "1"
IMG_EXT = {".jpg", ".jpeg", ".png"}

FEAT_WORDS = r"feat\.?|ft\.?|featuring"
FEAT = re.compile(r"\s*[\(\[]\s*(?:%s|with)\s+([^\)\]]*)[\)\]]" % FEAT_WORDS, re.I)
FEAT_NOWITH = re.compile(r"\s*[\(\[]\s*(?:%s)\s+([^\)\]]*)[\)\]]" % FEAT_WORDS, re.I)


# ----------------------------------------------------------------- title rules
def split_names(s):
    out = []
    for n in re.split(r",\s*|\s+&\s+|\s+and\s+", s):
        n = n.strip()
        if n and n.lower() not in [o.lower() for o in out]:
            out.append(n)
    return out


def clean_title(title, use_with=True):
    """-> (clean title, [featured artists])"""
    feats = []
    t = re.sub(r"\s*\(\s*Bonus Track\s*\)|\s+-\s+Bonus Track\b", "", title, flags=re.I)
    pat = FEAT if use_with else FEAT_NOWITH
    t = pat.sub(lambda m: (feats.append(m.group(1)), "")[1], t)
    if " - " in t:
        base, rest = t.split(" - ", 1)
        if rest.lower().startswith("live"):
            t = base + " - Live"
        elif rest.startswith("+"):
            t = base
    t = re.sub(r"\s*\(\s*Live\b[^)]*\)", " - Live", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    names = []
    for f in feats:
        for n in split_names(f):
            if n.lower() not in [x.lower() for x in names]:
                names.append(n)
    return t, names


def safe_name(s):
    return s.replace("/", "-").replace("\0", "").strip(" .")


# --------------------------------------------------------------------- images
def sips_dims(path):
    r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                       capture_output=True, text=True)
    w = re.search(r"pixelWidth:\s*(\d+)", r.stdout)
    h = re.search(r"pixelHeight:\s*(\d+)", r.stdout)
    return (int(w.group(1)), int(h.group(1))) if w and h else (0, 0)


def prepare_cover(data, size):
    """Re-encode as JPEG (q90), shrinking to `size` px if bigger. -> (bytes, w, h)"""
    with tempfile.TemporaryDirectory() as td:
        ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
        src, out = Path(td) / ("in" + ext), Path(td) / "out.jpg"
        src.write_bytes(data)
        w, h = sips_dims(src)
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90"]
        if max(w, h) > size:
            cmd += ["-Z", str(size)]
        r = subprocess.run(cmd + [str(src), "--out", str(out)], capture_output=True)
        if r.returncode == 0 and out.exists():
            data = out.read_bytes()
            w, h = sips_dims(out)
    return data, w, h


def find_folder_cover(folder):
    imgs = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMG_EXT and not p.name.startswith(".")]
    if not imgs:
        return None
    pref = [p for p in imgs if re.search(r"cover|folder|front|album|art", p.stem, re.I)]
    return max(pref or imgs, key=lambda p: p.stat().st_size)


def trash(p):
    try:
        t = Path.home() / ".Trash"
        dest, i = t / p.name, 1
        while dest.exists():
            dest = t / f"{p.stem} {i}{p.suffix}"
            i += 1
        shutil.move(str(p), str(dest))
        return "trashed"
    except OSError:
        p.unlink()
        return "deleted"


# --------------------------------------------------------------------- iTunes
def itunes(endpoint, **params):
    url = f"https://itunes.apple.com/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "FlacFix/1.0"})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        return json.load(r).get("results", [])


def norm(s):
    s = re.sub(r"[\(\[].*?[\)\]]", "", s.lower())
    s = re.sub(r"-\s*(single|ep)$", "", s.strip())
    return re.sub(r"[^a-z0-9+=÷×\-]+", " ", s).strip()


def download_itunes_cover(artist, album, titles, nfiles, log=print):
    """Pick the most likely iTunes album, return (jpeg bytes, description) or None."""
    a = artist.lower()
    votes, info = collections.Counter(), {}
    try:
        for r in itunes("search", term=f"{artist} {album}", entity="album", limit=25):
            if a in r.get("artistName", "").lower():
                info[r["collectionId"]] = r
                if norm(r["collectionName"]) == norm(album):
                    votes[r["collectionId"]] += 3
        step = max(1, len(titles) // 6)
        for t in titles[::step][:6]:
            seen = set()
            for r in itunes("search", term=f"{artist} {t}", entity="song", limit=15):
                if a in r.get("artistName", "").lower() and "collectionId" in r \
                        and norm(r.get("trackName", "")) == norm(t):
                    info.setdefault(r["collectionId"], r)
                    seen.add(r["collectionId"])
            for c in seen:  # one vote per title per album
                votes[c] += 1
    except Exception as e:  # network trouble
        log(f"    iTunes lookup failed: {e}")
        return None
    if not votes:
        return None
    odd = re.compile(r"\b(live|single|ep|karaoke|remix(es)?|session[s]?|acoustic|disc \d)\b", re.I)

    def rank(c):
        name = info[c].get("collectionName", "")
        penalty = 1 if odd.search(name) and not odd.search(album) else 0
        date = int(re.sub(r"\D", "", info[c].get("releaseDate", ""))[:8] or 99999999)
        return (votes[c], -penalty, -abs(info[c].get("trackCount", 0) - nfiles), -date)

    best = max(votes, key=rank)
    r = info[best]
    base = r["artworkUrl100"]
    for size in ("5000x5000bb", "3000x3000bb", "1400x1400bb", "1000x1000bb"):
        try:
            req = urllib.request.Request(base.replace("100x100bb", size),
                                         headers={"User-Agent": "FlacFix/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                data = resp.read()
            if data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG":
                return data, f'"{r["collectionName"]}" ({r.get("trackCount", "?")} tracks)'
        except Exception:
            continue
    return None


# ------------------------------------------------------------------- planning
def load(p):
    a = FLAC(p)
    if a.tags is None:
        a.add_tags()
    return a


def plan_album(folder, artist, force, use_with):
    flacs = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() == ".flac" and not p.name.startswith("."))
    audio = {p: load(p) for p in flacs}
    done = [p for p in flacs if not force and audio[p].tags.get(MARKER) == [MARKER_VER]]
    todo = [p for p in flacs if p not in done]
    plan = {"folder": folder, "all": flacs, "audio": audio, "done": done, "items": []}
    if not todo:
        return plan
    taken = {p.stem.lower() for p in done}
    groups = collections.OrderedDict()
    for p in todo:
        title = (audio[p].tags.get("title") or [p.stem])[0]
        base, feats = clean_title(title, use_with)
        base = safe_name(base) or p.stem
        groups.setdefault(base.lower(), []).append(
            {"path": p, "base": base, "feats": [f for f in feats if f.lower() != artist.lower()]})
    for items in groups.values():
        items.sort(key=lambda i: (bool(i["feats"]), i["path"].name.lower()))
        n = 1
        for it in items:
            while True:
                if n == 1:
                    cand = it["base"]
                elif it["base"].endswith(" - Live"):
                    cand = f'{it["base"]} V{n}'
                else:
                    cand = f'{it["base"]} - V{n}'
                if cand.lower() not in taken:
                    break
                n += 1
            taken.add(cand.lower())
            it["final"] = cand
            n += 1
            plan["items"].append(it)
    plan["items"].sort(key=lambda i: i["path"].name.lower())
    return plan


def cover_source(plan, allow_download):
    c = find_folder_cover(plan["folder"])
    if c:
        return "folder", c
    for p in plan["done"]:
        if plan["audio"][p].pictures:
            return "sibling", p
    return ("download" if allow_download else "none"), None


# ------------------------------------------------------------------ execution
def process_album(plan, artist, args, cover_choice):
    folder, album = plan["folder"], plan["folder"].name
    kind, ref = cover_choice
    cover = None  # (bytes, w, h)
    note = ""
    if kind == "folder":
        cover, note = prepare_cover(ref.read_bytes(), args.size), f"folder image {ref.name}"
    elif kind == "sibling":
        cover, note = prepare_cover(plan["audio"][ref].pictures[0].data, args.size), "existing album art"
    elif kind == "download":
        titles = [i["final"] for i in plan["items"]]
        got = download_itunes_cover(artist, album, titles, len(plan["all"]))
        if got:
            cover, note = prepare_cover(got[0], args.size), "iTunes " + got[1]
    if cover is None:
        print(b("    ! no cover art found") + " - leaving any existing embedded art alone")
    else:
        print(f"    cover: {note} -> {cover[1]}x{cover[2]}")

    renames = []
    for it in plan["items"]:
        p, a = it["path"], plan["audio"][it["path"]]
        a["title"] = [it["final"]]
        a["artist"] = [", ".join([artist] + it["feats"])]
        a["albumartist"] = [artist]
        a["album"] = [album]
        if cover or a.pictures:
            a[MARKER] = [MARKER_VER]  # only mark done once the track has cover art
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc = 3, "image/jpeg", "Cover"
            pic.width, pic.height, pic.depth, pic.data = cover[1], cover[2], 24, cover[0]
            a.clear_pictures()
            a.add_picture(pic)
        a.save()
        dst = folder / (it["final"] + ".flac")
        if dst != p:
            renames.append((p, dst))
    # two-phase rename so swaps / overlaps can't clobber anything
    tmp = []
    for i, (src, dst) in enumerate(renames):
        t = src.with_name(f".flacfix-tmp-{i}.flac")
        src.rename(t)
        tmp.append((t, dst))
    for t, dst in tmp:
        t.rename(dst)
    final_paths = [folder / (it["final"] + ".flac") for it in plan["items"]]

    if not args.no_icons:
        icon_bytes = cover[0] if cover else None
        if icon_bytes is None:
            a0 = plan["audio"][plan["items"][0]["path"]]
            icon_bytes = a0.pictures[0].data if a0.pictures else None
        if icon_bytes and SETICON.exists():
            with tempfile.NamedTemporaryFile(suffix=".jpg") as tf:
                tf.write(icon_bytes)
                tf.flush()
                r = subprocess.run([str(SETICON), tf.name] + [str(p) for p in final_paths],
                                   capture_output=True, text=True)
            print("    icons:", (r.stdout.strip().splitlines() or ["?"])[-1])
        elif not SETICON.exists():
            print(b("    ! icon helper missing") + f" ({SETICON}); skipped icons")

    if kind == "folder" and cover is not None:
        print(f"    cover image {ref.name}: {trash(ref)}")
    return final_paths


# ------------------------------------------------------------------------ CLI
def find_albums(root):
    def has_flac(d):
        return any(f.suffix.lower() == ".flac" and not f.name.startswith(".") for f in d.iterdir() if f.is_file())
    if has_flac(root):
        return [root], root.parent.name
    subs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".") and has_flac(d))
    return subs, root.name


def convert(albums, old, new):
    """Rename tracks called `old` to `new` - file name and title tag."""
    new = safe_name(re.sub(r"\s+", " ", new)).strip()
    if not new:
        sys.exit("The new track name is empty.")
    matches = []
    for folder in albums:
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() != ".flac" or p.name.startswith("."):
                continue
            a = load(p)
            title = (a.tags.get("title") or [""])[0]
            if old.strip().lower() in (title.lower(), p.stem.lower()):
                matches.append((p, a))
    if not matches:
        sys.exit(f'No track named "{old}" found.')
    folders = [p.parent for p, _ in matches]
    if len(set(folders)) != len(folders):
        sys.exit(f'More than one track named "{old}" in the same album - nothing changed.')
    for p, _ in matches:
        dst = p.with_name(new + ".flac")
        if dst.exists() and not os.path.samefile(dst, p):
            sys.exit(f'"{dst.name}" already exists in {p.parent.name} - nothing changed.')
    rule()
    for p, _ in matches:
        print(f"{b(p.parent.name)}: {p.name}  ->  {b(new + '.flac')}")
    if sys.stdin.isatty():
        rule()
        if input(f"Rename {len(matches)} track{'s' if len(matches) != 1 else ''}? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            sys.exit("Aborted.")
    for p, a in matches:
        a["title"] = [new]
        a.save()
        dst = p.with_name(new + ".flac")
        if dst != p:
            t = p.with_name(".flacfix-tmp-convert.flac")
            p.rename(t)
            t.rename(dst)
    rule()
    print(f"{b('Done:')} {len(matches)} track{'s' if len(matches) != 1 else ''} renamed.")


def main():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            sys.exit(f"FlacFix: {message}\nRun FlacFix --Help for the list of options.")

    ap = Parser(prog="FlacFix", add_help=False, allow_abbrev=False)
    ap.add_argument("folder", nargs="?")
    ap.add_argument("--Help", dest="help", action="store_true")
    ap.add_argument("--Version", dest="version", action="store_true")
    ap.add_argument("--Artist", dest="artist", metavar="name")
    ap.add_argument("--Convert", dest="convert", nargs=2, metavar=("old", "new"))
    ap.add_argument("--Force", dest="force", action="store_true")
    ap.add_argument("--NoIcons", dest="no_icons", action="store_true")
    ap.add_argument("--NoDownload", dest="no_download", action="store_true")
    ap.add_argument("--NoWith", dest="no_with", action="store_true")
    args = ap.parse_args()
    if args.version:
        print(f"FlacFix Version {VERSION}")
        return
    if args.help or not args.folder:
        show_help()
        return
    args.dry_run, args.yes, args.size = False, False, 1200


    root = Path(os.path.expanduser(args.folder))
    root = root.resolve()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    albums, default_artist = find_albums(root)
    if not albums:
        sys.exit(f"No .flac files found in {root} or its subfolders.")
    if args.convert:
        banner()
        convert(albums, *args.convert)
        return

    interactive = sys.stdin.isatty()
    artist = args.artist or default_artist
    if interactive and not args.artist:
        artist = input(f"Artist [{artist}]: ").strip() or artist

    banner()
    print(f"\n{b('Folder:')} {root}\n{b('Artist:')} {artist}")
    plans = [plan_album(a, artist, args.force, not args.no_with) for a in albums]
    rule()
    just_ruled = True
    todo_total = 0
    choices = {}
    for pl in plans:
        n = len(pl["items"])
        todo_total += n
        if n and not just_ruled:
            rule()
        just_ruled = False
        print(f"{b(pl['folder'].name)}: {len(pl['all'])} tracks, {n} to process, {len(pl['done'])} already done")
        if not n:
            continue
        choices[pl["folder"]] = cover_source(pl, not args.no_download)
        kind, ref = choices[pl["folder"]]
        print("    cover:", {"folder": f"folder image ({ref.name})" if ref else "", "sibling": "existing album art",
                              "download": "will search iTunes", "none": "none available"}[kind])
        for it in pl["items"]:
            artist_new = ", ".join([artist] + it["feats"])
            if it["final"] + ".flac" != it["path"].name or it["feats"]:
                extra = f"   [artist: {artist_new}]" if it["feats"] else ""
                print(f"      {it['path'].name}  ->  {b(it['final'] + '.flac')}{extra}")
    if not todo_total:
        rule()
        print(f"{b('Nothing to do')} - everything here is already processed.")
        return
    if args.dry_run:
        rule()
        print(f"{b('Dry run')} - nothing changed.")
        return
    if interactive:
        rule()
        if input(f"Process {todo_total} tracks? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            sys.exit("Aborted.")

    for pl in plans:
        if not pl["items"]:
            continue
        rule()
        print(b(f"== {pl['folder'].name}"))
        process_album(pl, artist, args, choices[pl["folder"]])

    no_lyrics = []
    for pl in plans:
        for it in pl["items"]:
            t = FLAC(pl["folder"] / (it["final"] + ".flac")).tags
            lyr = (t.get("lyrics") or t.get("unsyncedlyrics") or [""])[0]
            if len(lyr.strip()) < 20:
                no_lyrics.append(f"{pl['folder'].name}/{it['final']}")
    rule()
    print(f"{b('Done:')} {todo_total} tracks processed.")
    if no_lyrics:
        print(b(f"{len(no_lyrics)} tracks have no lyrics:"))
        for n in no_lyrics:
            print("  -", n)
    else:
        print("All processed tracks have lyrics.")
    if not args.no_icons:
        print(f"Tip: if Finder icons look stale, run:  {b('killall Finder')}")


if __name__ == "__main__":
    try:
        main()
        if not HELP_SHOWN:
            print()  # blank line at the bottom of every run
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
