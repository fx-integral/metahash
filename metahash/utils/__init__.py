__version__ = "3.0.0"
__least_acceptable_version__ = "2.1.1"
version_split = __version__.split(".")
version_url = "https://raw.githubusercontent.com/fx-integral/metahash/main/metahash/__init__.py"

__spec_version__ = (
    (1000 * int(version_split[0]))
    + (10 * int(version_split[1]))
    + (1 * int(version_split[2]))
)

import sys
from pathlib import Path

src_path = Path(__file__).resolve().parent / "src"

if src_path.is_dir() and str(src_path) not in sys.path:
    sys.path.append(str(src_path))
