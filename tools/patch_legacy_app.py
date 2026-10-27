#!/usr/bin/env python3
"""Bring three small improvements into an older RIGOR app checkout.

Run from inside the OLD app folder:
    python /path/to/RIGOR_APP_demo/tools/patch_legacy_app.py

Each change is applied only if the exact original code is found; anything
else is skipped and reported. Every edited file is backed up as
<name>.before_patch, and if an edited file no longer compiles, all edits are
rolled back. Changes:
  1. app.py        keep the LLM's label as rdfs:label instead of "BM DEP"
  2. rigor_app.py  list every column in the schema summary
  3. rigor_app.py  add Gemini Flash models to the model dropdown
It also copies examples/oncology_cancer/ and the new graph (visualize.py +
assets/) from this checkout.
"""
from __future__ import annotations

import py_compile
import shutil
import sys
from pathlib import Path

NEW = Path(__file__).resolve().parent.parent
OLD = Path.cwd()

EDITS = {
    "app.py": [(
        "label fix",
        """        for labs in by_lang.values():
            if len(labs) > 1:
                keep = sorted(labs, key=str)[0]""",
        """        local = str(subj).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        parts = local.split("_")
        generated = {humanize_identifier("_".join(parts[i:])).lower() for i in range(len(parts))}
        for labs in by_lang.values():
            if len(labs) > 1:
                keep = min(labs, key=lambda lab: (
                    str(lab).strip().lower() in generated,
                    str(lab).strip().lower() == humanize_identifier(local).lower(),
                    str(lab),
                ))""",
    )],
    "rigor_app.py": [(
        "column list in summary",
        """            for t in s["tables"]:
                pk = next((c["name"] for c in t["columns"] if c["primary_key"]), "none")
                lines.append(f"- **`{t['name']}`** — {len(t['columns'])} columns, "
                             f"{len(t['foreign_keys'])} FK(s), PK: `{pk}`")""",
        """            for t in s["tables"]:
                pk = ", ".join(c["name"] for c in t["columns"] if c["primary_key"]) or "none"
                fk_by_col = {
                    fk.get("column"): f"{fk.get('references_table')}.{fk.get('references_column')}"
                    for fk in t["foreign_keys"] if isinstance(fk, dict)
                }
                lines += [
                    f"**`{t['name']}`** — {len(t['columns'])} columns, "
                    f"{len(t['foreign_keys'])} FK(s), PK: `{pk}`",
                    "",
                    "| Column | Type | PK | References |",
                    "|---|---|---|---|",
                ]
                for c in t["columns"]:
                    ref = fk_by_col.get(c["name"])
                    lines.append(f"| `{c['name']}` | {c['type']} | {'✓' if c['primary_key'] else ''} "
                                 f"| {f'`{ref}`' if ref else ''} |")
                lines.append("")""",
    ), (
        "Gemini Flash models",
        """MODEL_CHOICES = list(dict.fromkeys(list(MODELS.values()) + ["deepseek/deepseek-chat", "openai/gpt-4o"]))""",
        """MODEL_CHOICES = list(dict.fromkeys(list(MODELS.values()) + [
    "deepseek/deepseek-chat", "openai/gpt-4o",
    "google/gemini-3.8-flash", "google/gemini-3.7-flash",
]))""",
    )],
}


def main() -> int:
    if not (OLD / "rigor_app.py").exists() or OLD.resolve() == NEW:
        print("Run this from inside the OLD app folder (the one with rigor_app.py).")
        return 1
    edited: list[Path] = []
    for name, changes in EDITS.items():
        path = OLD / name
        text = path.read_text(encoding="utf-8")
        new_text = text
        for label, before, after in changes:
            if after in new_text:
                print(f"  already applied: {label}")
            elif new_text.count(before) == 1:
                new_text = new_text.replace(before, after)
                print(f"  applied: {label}")
            else:
                print(f"  SKIPPED (original code not found): {label}")
        if new_text != text:
            backup = path.with_name(path.name + ".before_patch")
            if not backup.exists():
                shutil.copy2(path, backup)
            path.write_text(new_text, encoding="utf-8")
            edited.append(path)
    try:
        for path in edited:
            py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        for path in edited:
            shutil.copy2(path.with_name(path.name + ".before_patch"), path)
        print(f"Compile check failed, all edits rolled back: {exc}")
        return 1

    viz = OLD / "visualize.py"
    if viz.exists() and not (OLD / "visualize.py.before_patch").exists():
        shutil.copy2(viz, OLD / "visualize.py.before_patch")
    shutil.copy2(NEW / "visualize.py", viz)
    (OLD / "assets").mkdir(exist_ok=True)
    shutil.copy2(NEW / "assets" / "vis-network.min.js", OLD / "assets" / "vis-network.min.js")
    print("  copied: new graph (visualize.py + assets/)")
    shutil.copytree(NEW / "examples" / "oncology_cancer", OLD / "examples" / "oncology_cancer",
                    dirs_exist_ok=True)
    print("  copied: examples/oncology_cancer/")
    print("Done. Undo any file with: cp <file>.before_patch <file>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
