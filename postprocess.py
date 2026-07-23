"""Full-ontology cleanup for generated or uploaded ontologies.

Stages:
1. format-aware parsing and syntax repair;
2. ROBOT/HermiT logical-consistency repair;
3. Critical + Important OOPS repair and focused review collection;
4. final parse/serialization validation.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import asdict
from typing import Any, Dict, Optional

from rdflib import Graph

import validate_fix_ontology_syntax as syntax_mod
import validate_fix_ontology_consistency as consist_mod
import validate_fix_ontology_pitfall_final as pitfall_mod


def _load_graph(path: Path, *, allow_syntax_llm: bool = True):
    """Load a common RDF/OWL serialization, with optional LLM syntax repair.

    This is deliberately backward-compatible with older versions of
    ``validate_fix_ontology_syntax.py`` that do not provide
    ``load_or_repair_ontology``. A valid ontology therefore never fails merely
    because the syntax helper module is older than the orchestrator.
    """
    loader = getattr(syntax_mod, "load_or_repair_ontology", None)
    if callable(loader):
        return loader(str(path), allow_llm=allow_syntax_llm)

    # Format-aware parser already maintained by the pitfall module.
    try:
        graph, rdf_format = pitfall_mod.load_ontology(path)
        return graph, rdf_format, "parsed"
    except Exception as parse_error:
        if not allow_syntax_llm:
            raise

    # Compatibility fallback for malformed files when the installed syntax
    # module predates load_or_repair_ontology().
    import api_utils

    source = path.read_text(encoding="utf-8", errors="replace")
    max_chars = 180_000
    if len(source) > max_chars:
        raise RuntimeError(
            "The ontology could not be parsed and is too large for whole-file "
            "LLM syntax repair. Install the updated validate_fix_ontology_syntax.py "
            f"or convert the source with an RDF editor first. Parser error: {parse_error}"
        )

    prompt = f"""You are an RDF/OWL syntax repair expert.
The ontology below could not be parsed in any common RDF serialization.
Convert it to syntactically valid Turtle while preserving every recoverable
assertion and IRI.

Strict rules:
- Repair syntax only; do not redesign semantics.
- Do not invent entities, placeholder IRIs, or example data.
- Preserve labels, comments, provenance, alignments, restrictions, and metadata.
- Return only valid Turtle between the requested markers.

Parser error:
{parse_error}

Source ontology:
{source}

###start_turtle###
###end_turtle###
"""

    raw_sender = getattr(api_utils, "send_prompt_raw", None)
    if callable(raw_sender):
        reply = raw_sender(
            prompt,
            persona="RDF and OWL syntax repair expert",
            step_name="ontology_file_syntax_repair",
        )
    else:
        reply = api_utils.send_prompt(
            prompt,
            persona="RDF and OWL syntax repair expert",
            step_name="ontology_file_syntax_repair",
        )

    blocks = api_utils.extract_between_markers(
        reply or "", "###start_turtle###", "###end_turtle###"
    )
    if not blocks:
        raise RuntimeError("The syntax-repair model did not return a Turtle block.")

    repaired = blocks[0].strip()
    graph = Graph()
    try:
        graph.parse(data=repaired, format="turtle")
    except Exception as exc:
        raise RuntimeError(
            f"The syntax-repair model returned invalid Turtle: {exc}"
        ) from exc
    return graph, "turtle", "llm_repaired"


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
    repair_critical_pitfalls: bool = True,
    repair_important_pitfalls: bool = True,
    auto_download_robot: bool = False,
    output_format: str | None = None,
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
        import api_utils
        api_utils.set_model(model)

    stages: Dict[str, Any] = {}
    emit("Loading full ontology")
    graph, input_format, syntax_note = _load_graph(input_path, allow_syntax_llm=run_syntax)
    emit(f"Loaded full ontology ({len(graph)} triples; {input_format})")
    stages["load"] = {"status": syntax_note, "format": input_format, "triples": len(graph)}

    # Normalize a valid graph to Turtle for the existing consistency pipeline.
    ttl_text = graph.serialize(format="turtle")
    if run_syntax:
        emit("Running syntax normalization")
        try:
            cleaned = syntax_mod.clean_ontology_text(ttl_text)
            probe = Graph(); probe.parse(data=cleaned, format="turtle")
            graph = probe
            ttl_text = cleaned
            stages["syntax"] = {"status": "ok", "triples": len(graph)}
        except Exception as exc:
            emit(f"Syntax normalization skipped ({exc}); keeping parsed graph")
            stages["syntax"] = {"status": "skipped", "error": str(exc)}

    syntax_ttl = out_dir / "after_syntax.ttl"
    syntax_ttl.write_text(ttl_text, encoding="utf-8")
    current_path = syntax_ttl

    if run_consistency:
        emit("Running ROBOT/HermiT logical consistency repair")
        try:
            result = consist_mod.fix_consistency(
                input_ttl=current_path,
                out_dir=out_dir / "consistency",
                java_exe=java_exe,
                hermit_jar=hermit_jar,
                robot_jar=robot_jar,
                model=model,
                auto_download_robot=auto_download_robot,
                progress=progress,
            )
            stages["consistency"] = result
            candidate = result.get("final_ontology")
            if candidate and Path(candidate).exists():
                current_path = Path(candidate)
        except Exception as exc:
            emit(f"Consistency stage issue ({exc}); keeping prior ontology")
            stages["consistency"] = {"status": "skipped", "error": str(exc)}

    graph, _, _ = _load_graph(current_path, allow_syntax_llm=False)
    review_items: list[dict[str, Any]] = []
    report_json = report_markdown = None

    levels = tuple(
        level
        for enabled, level in (
            (repair_critical_pitfalls, "critical"),
            (repair_important_pitfalls, "important"),
        )
        if enabled
    )
    if run_pitfalls and levels:
        emit(f"Running OOPS pitfall repair ({', '.join(levels)})")
        try:
            result = pitfall_mod.repair_pitfalls(
                graph,
                model=model,
                source_label=str(current_path),
                out_dir=out_dir / "pitfalls",
                progress=progress,
                importance_levels=levels,
                java_exe=java_exe,
                robot_jar=str(robot_jar) if robot_jar else None,
            )
            graph = result.graph
            review_items = [asdict(item) for item in result.review_items]
            report_json = result.report_json
            report_markdown = result.report_markdown
            stages["pitfalls"] = {
                "status": "ok",
                "triples": len(graph),
                "applied": result.applied,
                "review_items": review_items,
                "unresolved": result.unresolved,
                "remaining": result.final_summary,
                "report_json": report_json,
                "report_markdown": report_markdown,
            }
            emit(
                f"OOPS cleanup complete: {len(result.applied)} automatic fixes; "
                f"{len(review_items)} user decision(s)"
            )
        except Exception as exc:
            emit(f"OOPS stage issue ({exc}); keeping pre-OOPS ontology")
            stages["pitfalls"] = {"status": "skipped", "error": str(exc)}
    else:
        stages["pitfalls"] = {
            "status": "skipped",
            "reason": "no pitfall severity selected",
        }

    emit("Running final validation")
    turtle = graph.serialize(format="turtle")
    probe = Graph(); probe.parse(data=turtle, format="turtle")
    final_format = output_format or pitfall_mod.guess_format(str(output_path)) or input_format or "xml"
    graph.serialize(destination=str(output_path), format=final_format)
    exact, _ = pitfall_mod.load_ontology(output_path, final_format)
    if len(exact) != len(graph):
        raise RuntimeError(f"Final serialization changed triple count: {len(graph)} -> {len(exact)}")
    stages["final"] = {"status": "ok", "triples": len(exact), "output": str(output_path), "format": final_format}
    emit("Final ontology ready")

    return {
        "output": str(output_path),
        "out_dir": str(out_dir),
        "stages": stages,
        "review_items": review_items,
        "report_json": report_json,
        "report_markdown": report_markdown,
    }
