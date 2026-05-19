from __future__ import annotations

import ast
from pathlib import Path


def _manifest_tools(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    manifest = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MANIFEST":
                    manifest = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "MANIFEST":
            manifest = node.value

    if not isinstance(manifest, ast.Call):
        return []
    for keyword in manifest.keywords:
        if keyword.arg == "tools":
            value = ast.literal_eval(keyword.value)
            return list(value) if isinstance(value, list) else []
    return []


def test_core_prompt_mentions_every_manifest_tool() -> None:
    prompt = Path("core/prompt.txt").read_text(encoding="utf-8")
    missing: list[str] = []
    for manifest_path in sorted(Path("skills").glob("*/__skill__.py")):
        for tool in _manifest_tools(manifest_path):
            if tool not in prompt:
                missing.append(f"{manifest_path.parent.name}.{tool}")

    assert missing == []
