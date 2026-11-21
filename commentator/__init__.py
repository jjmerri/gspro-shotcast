import importlib.util
import os
import sys

# A source checkout installed (pip install -e) before scoring/ existed maps only `commentator`: find the
# shared scoring package beside it until the next reinstall. Installed builds have it already.
if importlib.util.find_spec("shotcast_scoring") is None:
    _src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scoring", "src")
    if os.path.isdir(os.path.join(_src, "shotcast_scoring")):
        sys.path.append(_src)
