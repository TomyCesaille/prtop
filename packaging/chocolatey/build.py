"""Assemble the Chocolatey package's folder, ready for `choco pack`.

    python packaging/chocolatey/build.py [OUTPUT]

OUTPUT defaults to `build/chocolatey`. It receives the nuspec, the install
scripts, the legal files, and `tools/python`: the Windows embeddable Python
below with prtop beside python.exe, where its `._pth` file looks for packages.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PYTHON_VERSION = "3.14.9"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "3c01d7cfc666f746a6f064f60fb2156ab0ec464d3d738ab796371a46a9bb24a2"

HERE = Path(__file__).parent
ROOT = HERE.parents[1]


def prtop_version() -> str:
    text = (ROOT / "prtop" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def fetch_python(destination: Path) -> None:
    with urllib.request.urlopen(PYTHON_URL) as response:
        archive = response.read()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != PYTHON_SHA256:
        sys.exit(f"{PYTHON_URL}: SHA256 {digest}, expected {PYTHON_SHA256}")
    zip_path = destination.with_suffix(".zip")
    zip_path.write_bytes(archive)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(destination)
    zip_path.unlink()


def fill(template: Path, target: Path, **values: str) -> None:
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", value)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def main(output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    python = output / "tools" / "python"
    python.mkdir(parents=True)
    fetch_python(python)
    shutil.copytree(ROOT / "prtop", python / "prtop", ignore=shutil.ignore_patterns("__pycache__"))
    # Chocolatey puts every .exe of a package on PATH unless told otherwise.
    for exe in ("python.exe", "pythonw.exe"):
        (python / f"{exe}.ignore").touch()

    values = {
        "version": prtop_version(),
        "python_version": PYTHON_VERSION,
        "python_url": PYTHON_URL,
        "python_sha256": PYTHON_SHA256,
        "python_slug": PYTHON_VERSION.replace(".", ""),
    }
    fill(HERE / "prtop.nuspec", output / "prtop.nuspec", **values)
    fill(HERE / "legal" / "VERIFICATION.txt", output / "legal" / "VERIFICATION.txt", **values)
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    (output / "legal" / "LICENSE.txt").write_text(
        f"{license_text}\nThe Python in tools\\python comes with its own license, the PSF License,\n"
        "in tools\\python\\LICENSE.txt.\n",
        encoding="utf-8",
    )
    for script in (HERE / "tools").glob("*.ps1"):
        shutil.copyfile(script, output / "tools" / script.name)
    print(f"prtop {values['version']} with Python {PYTHON_VERSION} in {output}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "chocolatey")
