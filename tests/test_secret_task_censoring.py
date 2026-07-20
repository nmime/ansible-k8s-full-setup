import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_ROOTS = (ROOT / "roles", ROOT / "playbooks" / "tasks")
TASK_START = re.compile(r"^(?P<indent>[ ]*)- name:", re.MULTILINE)


def task_blocks(text: str):
    starts = list(TASK_START.finditer(text))
    for index, start in enumerate(starts):
        indent = start.group("indent")
        end = len(text)
        for candidate in starts[index + 1 :]:
            if candidate.group("indent") == indent:
                end = candidate.start()
                break
        yield text[start.start() : end]


def test_direct_kubernetes_secret_operations_are_always_censored():
    uncensored = []
    for task_root in TASK_ROOTS:
        for path in task_root.rglob("*.yml"):
            for block in task_blocks(path.read_text()):
                direct_secret = re.search(r"^[ ]+kind:[ ]+Secret[ ]*$", block, re.MULTILINE)
                writes_secret_data = re.search(
                    r"^[ ]+stringData:[ ]*(?:$|\{)", block, re.MULTILINE
                )
                if (direct_secret or writes_secret_data) and "no_log: true" not in block:
                    name = block.splitlines()[0].split(":", 1)[1].strip()
                    uncensored.append(f"{path.relative_to(ROOT)}: {name}")

    assert not uncensored, "uncensored Secret tasks:\n" + "\n".join(uncensored)
