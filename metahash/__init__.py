__version__ = "1.0.1"
__least_acceptable_version__ = "1.0.1"
version_split = __version__.split(".")
version_url = "https://raw.githubusercontent.com/fx-integral/metahash/main/metahash/__init__.py"

__spec_version__ = (
    (1000 * int(version_split[0]))
    + (10 * int(version_split[1]))
    + (1 * int(version_split[2]))
)
