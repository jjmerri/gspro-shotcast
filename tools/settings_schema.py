"""Write the settings contract (the config's JSON Schema and who owns each setting) to a JSON file,
for a hosted service to build its settings forms and sync on.

    python tools/settings_schema.py --out settings-schema.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commentator import settings_owner  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("settings-schema.json"))
    args = ap.parse_args()
    args.out.write_text(json.dumps(settings_owner.export(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
