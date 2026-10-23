"""Download / update VagOn inside Pythonista (or a-Shell) from GitHub.

Run once in Pythonista: it puts the `vagon` package and VagOn.py next to this
script. Run again later to update (your backups/recipes in Documents/VagOn are
kept). For a private repository set TOKEN to a GitHub personal access token
with read access, or copy the folder via the Files app instead.
"""

import io
import os
import shutil
import urllib.request
import zipfile

REPO = "AlexanderSobin/VagOn"
BRANCH = "main"
TOKEN = ""   # optional: GitHub token for a private repository

HERE = os.path.dirname(os.path.abspath(__file__))


def download() -> bytes:
    url = f"https://api.github.com/repos/{REPO}/zipball/{BRANCH}"
    req = urllib.request.Request(url, headers={"User-Agent": "VagOn-installer"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    print("Скачиваю", REPO, "…")
    data = download()
    z = zipfile.ZipFile(io.BytesIO(data))
    root = z.namelist()[0].split("/")[0]
    target_pkg = os.path.join(HERE, "vagon")
    tmp = os.path.join(HERE, ".vagon_new")
    shutil.rmtree(tmp, ignore_errors=True)
    for name in z.namelist():
        rel = name[len(root) + 1:]
        if not rel or not (rel.startswith("vagon/") or rel == "scripts/ios/VagOn.py"):
            continue
        dest = os.path.join(tmp, rel)
        if name.endswith("/"):
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with z.open(name) as src, open(dest, "wb") as dst:
            dst.write(src.read())
    shutil.rmtree(target_pkg, ignore_errors=True)
    shutil.move(os.path.join(tmp, "vagon"), target_pkg)
    shutil.copy(os.path.join(tmp, "scripts", "ios", "VagOn.py"), os.path.join(HERE, "VagOn.py"))
    shutil.rmtree(tmp, ignore_errors=True)
    print("Готово. Запустите VagOn.py")


if __name__ == "__main__":
    main()
