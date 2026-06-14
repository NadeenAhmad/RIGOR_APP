"""
postprocess.py — final full-ontology cleanup for the RIGOR pipeline.

Runs, in order, on the COMPLETE merged ontology produced by app.run_semantic_enrichment:
  1. Syntax validation + rdflib-based repair      (validate_fix_ontology_syntax)
  2. Logical consistency check + repair (HermiT)   (validate_fix_ontology_consistency)
  3. Critical modeling-pitfall detection + repair  (validate_fix_ontology_pitfall_final, OOPS!)
  4. Final validation (re-parse to confirm well-formedness)

Design goals:
  - Parse the ontology once and serialise once at the end; pass text/paths/graph
    between stages instead of repeatedly round-tripping.
  - Degrade gracefully: a missing Java/jar (consistency), missing API key, or no
    network (OOPS) skips that stage with a clear message and keeps the best
    artifact so far. The input is never lost.
  - Stream progress via an optional `progress` callback.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from rdflib import Graph

import validate_fix_ontology_syntax as syntax_mod
import validate_fix_ontology_consistency as consist_mod
import validate_fix_ontology_pitfall_final as pitfall_mod


def _load_graph(path: Path) -> Graph:
    g = Graph()
    suffix = path.suffix.lower()
    order = ("turtle", "xml") if suffix in {".ttl", ".n3"} else ("xml", "turtle")
    last = None
    for fmt in order:
        try:
            g.parse(str(path), format=fmt)
            return g
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Could not parse ontology at {path}: {last}")


def postprocess_ontology(
    input_path: str | Path,
    output_path: str | Path,
    *,
    out_dir: Optional[str | Path] = None,
    model: Optional[str] = None,
    hermit_jar: Optional[str | Path] = None,
    robot_jar: Optional[str | Path] = None,
    java_exe: str = "java",
    run_syntax: bool = True,
    run_consistency: bool = True,
    run_pitfalls: bool = True,
    repair_important_pitfalls: bool = False,
    auto_download_robot: bool = False,
    output_format: str = "xml",
    progress=None,
) -> Dict[str, Any]:
    def emit(msg: str) -> None:
        print(msg, flush=True)
        if progress is not None:
            try:
                progress(msg)
            except Exception:
                pass

    input_path = Path(input_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir = Path(out_dir).expanduser().resolve() if out_dir else output_path.parent / "postprocess"
    out_dir.mkdir(parents=True, exist_ok=True)

    if model:
        try:
            import api_utils
            api_utils.set_model(model)
        except Exception:
            pass

    stages: Dict[str, Any] = {}

    emit("Full ontology generated")
    graph = _load_graph(input_path)
    emit(f"Loaded full ontology ({len(graph)} triples)")

    # Single serialisation to Turtle; subsequent stages consume text/file/graph.
    ttl_text = graph.serialize(format="turtle")

    # --- Stage 1: syntax ---
    if run_syntax:
        emit("Running syntax validation on full ontology")
        try:
            emit("Applying rdflib-based syntax fixes")
            cleaned = syntax_mod.clean_ontology_text(ttl_text)
            # Confirm the cleaned text still parses; if not, keep the original.
            test = Graph()
            test.parse(data=cleaned, format="turtle")
            ttl_text = cleaned
            stages["syntax"] = {"status": "ok", "triples": len(test)}
        except Exception as e:  # noqa: BLE001
            emit(f"Syntax stage issue ({e}); keeping pre-syntax ontology")
            stages["syntax"] = {"status": "skipped", "error": str(e)}

    syntax_ttl = out_dir / "after_syntax.ttl"
    syntax_ttl.write_text(ttl_text, encoding="utf-8")

    # --- Stage 2: logical consistency (HermiT/ROBOT) ---
    current_ttl = syntax_ttl
    if run_consistency:
        emit("Running HermiT logical consistency check")
        try:
            res = consist_mod.fix_consistency(
                input_ttl=current_ttl,
                out_dir=out_dir / "consistency",
                java_exe=java_exe,
                hermit_jar=hermit_jar,
                robot_jar=robot_jar,
                model=model,
                auto_download_robot=auto_download_robot,
                progress=progress,
            )
            stages["consistency"] = res
            final_onto = res.get("final_ontology")
            if final_onto and Path(final_onto).exists():
                current_ttl = Path(final_onto)
            if res.get("status") not in {"robot_unavailable", "robot_reason_error", "coherent", "skipped"}:
                emit("Applying logical inconsistency fixes")
        except Exception as e:  # noqa: BLE001
            emit(f"Consistency stage issue ({e}); keeping pre-consistency ontology")
            stages["consistency"] = {"status": "skipped", "error": str(e)}

    # --- Stage 3: OOPS! modeling pitfalls ---
    graph = _load_graph(current_ttl)
    if run_pitfalls:
        emit("Running OOPS modeling pitfall check")
        try:
            graph = pitfall_mod.fix_pitfalls(
                graph,
                model=model,
                outfile=str(out_dir / "oops_snippets.txt"),
                source_label=str(current_ttl),
                importance_levels=(
                    ("critical", "important") if repair_important_pitfalls else ("critical",)
                ),
                progress=progress,
            )
            emit("Applying OOPS-based cleanup")
            stages["pitfalls"] = {"status": "ok", "triples": len(graph)}
        except Exception as e:  # noqa: BLE001
            emit(f"OOPS stage issue ({e}); keeping pre-OOPS ontology")
            stages["pitfalls"] = {"status": "skipped", "error": str(e)}

    # --- Stage 4: final validation + serialise once ---
    emit("Running final validation")
    final_text = graph.serialize(format="turtle")
    check = Graph()
    check.parse(data=final_text, format="turtle")  # raises if malformed
    graph.serialize(destination=str(output_path), format=output_format)
    stages["final"] = {"status": "ok", "triples": len(check), "output": str(output_path)}
    emit("Final ontology ready")

    return {"output": str(output_path), "out_dir": str(out_dir), "stages": stages}
