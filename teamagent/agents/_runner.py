"""Runner — запускает один агент по аргументам.

  python -m teamagent.agents._runner <module> <class> <init_args_json> <name> <category>

Только агент по своему name внутри своей категории. Используется orchestrator-ом
для запуска каждого агента в отдельном процессе.
"""
from __future__ import annotations
import importlib
import json
import sys


def main() -> None:
    if len(sys.argv) < 6:
        print(f"usage: _runner.py module class init_args_json name category", file=sys.stderr)
        sys.exit(2)
    module = sys.argv[1]
    cls = sys.argv[2]
    init_args = json.loads(sys.argv[3]) if sys.argv[3] else {}
    name = sys.argv[4]
    category = sys.argv[5]
    mod = importlib.import_module(module)
    klass = getattr(mod, cls)
    agent = klass(**init_args) if init_args else klass()
    # reaffirm name/category в случае если class default — другие
    agent.name = name
    agent.category = category
    # пересоздаём heartbeat path с новым именем
    from .. import config
    from pathlib import Path
    agent.heartbeat_path = config.STATE_DIR / f"heartbeat_{name}.json"
    agent.state_path = config.STATE_DIR / f"agent_{name}.json"
    agent.run()


if __name__ == "__main__":
    main()
