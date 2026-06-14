"""
rigor_app.py — a thin Gradio UI around the RIGOR pipeline.

Flow:
  1. Upload a CSV or a schema JSON.
  2. See a summary (rows, columns, types, missing values, preview, warnings).
  3. Configure OpenRouter key (in memory only) + model + cleanup options.
  4. Run: enrichment -> full-ontology cleanup (syntax / HermiT / OOPS) -> export.

The API key is held only in memory for the current session. It is never
written to disk and never logged.

Run:
    pip install -r requirements.txt
    python rigor_app.py
"""

from __future__ import annotations

import base64
import atexit
import os
import re
import queue
import shutil
import tempfile
import threading
import time
import traceback
from pathlib import Path

import gradio as gr

import api_utils
import ingest
from app import run_semantic_enrichment, MODELS
from postprocess import postprocess_ontology
import visualize

_DONE = "__DONE__"
_ERR = "__ERR__"


def _logo_html(height: int = 64) -> str:
    """Header logo as an inline <img> (base64 data URI, so it renders without
    relying on Gradio's static-file serving). Looks for 'rigor_logo.png' next
    to this script; falls back to a plain text title if it isn't there."""
    logo = Path(__file__).resolve().parent / "rigor_logo.png"
    try:
        b64 = base64.b64encode(logo.read_bytes()).decode("ascii")
    except Exception:
        return ("<h1 style='margin:6px 0;font-weight:600'>RIGOR</h1>")
    return (
        "<div style='display:flex;align-items:center;margin:4px 0 0'>"
        f"<img src='data:image/png;base64,{b64}' alt='RIGOR logo' "
        f"style='height:{height}px;width:auto'/></div>"
    )


def _resolve_jar(explicit: str | None, keyword: str) -> str | None:
    """Use the explicit path if it exists; otherwise look for a *.jar whose
    name contains `keyword` (case-insensitive) in the cwd and the app folder."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return str(p)
    search_dirs = [Path.cwd(), Path(__file__).resolve().parent]
    seen = set()
    for d in search_dirs:
        if not d.exists() or d in seen:
            continue
        seen.add(d)
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() == ".jar" and keyword in f.name.lower():
                return str(f)
    return explicit or None

# De-duplicated, order-preserving model list (MODELS values may already include
# some of the extras). Add more options by editing MODELS in app.py or appending
# OpenRouter model ids to the list below — see the README ("Adding models").
MODEL_CHOICES = list(dict.fromkeys(list(MODELS.values()) + ["deepseek/deepseek-chat", "openai/gpt-4o"]))
SESSION_DIR = Path(tempfile.gettempdir()) / "rigor_sessions"

# Single-user app: one in-flight run at a time. The Stop button sets this; the
# run loop checks it to break promptly, and Gradio also cancels the generator.
_cancel_event = threading.Event()


def _cleanup_sessions(keep: Path | None = None) -> None:
    """Remove previous per-run temp dirs under SESSION_DIR. The current run's
    dir (`keep`) is preserved because its final ontology is still being served
    for download / visualization. Best-effort; never raises."""
    try:
        if not SESSION_DIR.exists():
            return
        for d in SESSION_DIR.iterdir():
            if d.is_dir() and (keep is None or d.resolve() != keep.resolve()):
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


# On exit, drop the whole session scratch area so nothing lingers in temp.
atexit.register(lambda: shutil.rmtree(SESSION_DIR, ignore_errors=True))


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:d}:{s:02d}"


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def summarize(file_obj):
    if not file_obj:
        return "Upload a CSV or schema JSON to see a summary.", None
    path = file_obj.name
    try:
        kind = ingest.detect_input_kind(path)
        if kind == "csv":
            s = ingest.summarize_csv(path)
            if s.get("error"):
                return f"**Error:** {s['error']}", None
            lines = [
                f"**Detected input:** CSV  ·  table **`{s['table_name']}`**",
                f"**Rows:** {s['row_count']}  ·  **Columns:** {s['column_count']}  "
                f"·  **Primary key:** `{s['primary_key'] or 'none detected'}`",
                "",
                "| Column | Type | Missing | PK |",
                "|---|---|---|---|",
            ]
            for c in s["columns"]:
                lines.append(f"| `{c['name']}` | {c['type']} | {c['missing']} | {'✓' if c['primary_key'] else ''} |")
            if s["preview"]:
                hdr = list(s["preview"][0].keys())
                lines += ["", "**Preview**", "", "| " + " | ".join(hdr) + " |",
                          "|" + "|".join(["---"] * len(hdr)) + "|"]
                for row in s["preview"]:
                    lines.append("| " + " | ".join(str(row.get(h, "")) for h in hdr) + " |")
        else:
            s = ingest.summarize_schema_json(path)
            lines = [f"**Detected input:** schema JSON  ·  **Tables:** {s['table_count']}", ""]
            for t in s["tables"]:
                pk = next((c["name"] for c in t["columns"] if c["primary_key"]), "none")
                lines.append(f"- **`{t['name']}`** — {len(t['columns'])} columns, "
                             f"{len(t['foreign_keys'])} FK(s), PK: `{pk}`")

        if s.get("warnings"):
            lines += ["", "**Warnings**"]
            lines += [f"- ⚠️ {w}" for w in s["warnings"]]
        else:
            lines += ["", "_No obvious issues detected._"]
        return "\n".join(lines), path
    except Exception as e:  # noqa: BLE001
        return f"**Could not read file:** {e}", None


# --------------------------------------------------------------------------- #
# Run pipeline (streaming generator)
# --------------------------------------------------------------------------- #
def run_pipeline(file_obj, api_key, model, k_attempts,
                 run_consistency, hermit_jar, robot_jar, run_pitfalls,
                 repair_important, core_file, docs_files, onto_files):
    log: list[str] = []
    start_ts = time.time()

    def push(msg):
        log.append(msg)

    def status(frac: float | None = None) -> str:
        el = _fmt_elapsed(time.time() - start_ts)
        if frac is None:
            return f"⏱ {el} elapsed"
        return f"⏱ {el} elapsed · ~{int(min(max(frac, 0.0), 1.0) * 100)}%"

    if not file_obj:
        yield "Please upload a file first.", None, "", ""
        return

    # Fresh run: clear any pending Stop, and remove previous runs' temp dirs.
    _cancel_event.clear()
    _cleanup_sessions()

    api_key = (api_key or "").strip()
    has_key = bool(api_key)

    # Best-effort table count, used only for a coarse progress estimate.
    try:
        if ingest.detect_input_kind(file_obj.name) == "csv":
            tables_total = 1
        else:
            tables_total = max(1, int(ingest.summarize_schema_json(file_obj.name).get("table_count", 1)))
    except Exception:
        tables_total = 1

    # In-memory configuration only — never persisted.
    api_utils.configure(api_key=api_key, model=model)
    if has_key:
        os.environ["OPENROUTER_API_KEY"] = api_key
    os.environ["RIGOR_MODEL"] = model

    session = SESSION_DIR / f"run_{os.getpid()}_{int(threading.get_ident())}"
    session.mkdir(parents=True, exist_ok=True)
    dataset = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(file_obj.name).stem).strip("_") or "dataset"
    enriched = session / f"{dataset}_enriched_ontology.owl"
    final = session / f"{dataset}_final_ontology.owl"

    # Resolve jar locations: explicit path, else auto-detect in cwd / app folder.
    resolved_hermit = _resolve_jar(hermit_jar, "hermit") if run_consistency else None
    # ROBOT is needed by the consistency stage AND by the safety check that
    # vets important-pitfall repairs, so resolve it if either needs it.
    need_robot = run_consistency or (run_pitfalls and repair_important)
    resolved_robot = _resolve_jar(robot_jar, "robot") if need_robot else None
    if run_consistency:
        push(f"HermiT jar: {resolved_hermit or 'not found (deterministic stage will skip)'}")
        push(f"ROBOT jar:  {resolved_robot or 'not found (reasoning stage will skip)'}")

    # Optional enrichment context: seed core ontology + documents folder.
    core_path = core_file.name if core_file else (session / "_no_core.owl")
    if docs_files:
        docs_dir = session / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for df in docs_files:
            try:
                shutil.copy(df.name, docs_dir / Path(df.name).name)
            except Exception:
                pass
        docs_path = docs_dir
    else:
        docs_path = session / "_no_docs"

    if onto_files:
        onto_dir = session / "ontologies"
        onto_dir.mkdir(parents=True, exist_ok=True)
        for of in onto_files:
            try:
                shutil.copy(of.name, onto_dir / Path(of.name).name)
            except Exception:
                pass
        ontology_path = onto_dir
    else:
        ontology_path = session / "_no_onto"

    q: "queue.Queue[str]" = queue.Queue()

    def progress(msg):
        q.put(str(msg))

    def work():
        try:
            schema_path, kind = ingest.prepare_schema_file(file_obj.name, session)
            progress(f"Loading table ({kind})")
            run_semantic_enrichment(
                schema_path=schema_path,
                docs_path=docs_path,
                ontology_path=ontology_path,
                core_owl_path=core_path,
                output_path=enriched,
                direct_mappings_dir=session / "direct_mappings",
                model=model,
                use_llm=has_key,
                max_correction_attempts=int(k_attempts),
                progress_cb=progress,
            )
            postprocess_ontology(
                input_path=enriched,
                output_path=final,
                out_dir=session / "postprocess",
                model=model,
                hermit_jar=resolved_hermit,
                robot_jar=resolved_robot,
                run_syntax=True,
                run_consistency=run_consistency,
                run_pitfalls=run_pitfalls and has_key,
                repair_important_pitfalls=bool(repair_important),
                progress=progress,
            )
            q.put(_DONE)
        except Exception as e:  # noqa: BLE001
            q.put(f"{_ERR}{e}\n{traceback.format_exc()}")

    if not has_key:
        push("No API key provided — running deterministic Direct Mapping only "
             "(LLM enrichment and OOPS repair are skipped).")

    t = threading.Thread(target=work, daemon=True)
    t.start()

    # Coarse progress: enrichment fills 0..0.7 by table count; the postprocess
    # stages step it to 1.0. Detection is best-effort and never affects the run.
    tables_done = 0
    frac = 0.0
    _PP = [("syntax validation", 0.80), ("HermiT", 0.86),
           ("OOPS modeling pitfall", 0.92), ("final validation", 0.97),
           ("Final ontology ready", 1.0)]

    while True:
        try:
            msg = q.get(timeout=1.0)
        except queue.Empty:
            if _cancel_event.is_set():
                push("⏹ Cancelled (UI stopped; any in-flight model call finishes in the background).")
                yield "\n".join(log), None, "", status(frac)
                return
            # Keep the elapsed clock ticking during quiet stretches.
            yield "\n".join(log), None, "", status(frac)
            continue

        if msg == _DONE:
            break
        if msg.startswith(_ERR):
            push("❌ " + msg[len(_ERR):])
            yield "\n".join(log), None, "", status(frac)
            return

        push(msg)
        try:
            if "External hints for '" in msg:
                tables_done += 1
            pp_hit = next((f for key, f in _PP if key in msg), None)
            if pp_hit is not None:
                frac = max(frac, pp_hit)
            else:
                frac = max(frac, 0.70 * min(tables_done / tables_total, 1.0))
        except Exception:
            pass

        if _cancel_event.is_set():
            push("⏹ Cancelled (UI stopped; any in-flight model call finishes in the background).")
            yield "\n".join(log), None, "", status(frac)
            return
        yield "\n".join(log), None, "", status(frac)

    turtle_preview = ""
    out_file = None
    if final.exists():
        out_file = str(final)
        try:
            from rdflib import Graph
            g = Graph(); g.parse(str(final), format="xml")
            turtle_preview = g.serialize(format="turtle")
        except Exception:
            turtle_preview = final.read_text(encoding="utf-8", errors="replace")
    push("✅ Done.")
    yield "\n".join(log), out_file, turtle_preview, status(1.0)


# --------------------------------------------------------------------------- #
# Visualization
# --------------------------------------------------------------------------- #
def _coerce_path(x):
    """Normalise a Gradio File value (path str, file obj, or {'path': ...}) to a path."""
    if not x:
        return None
    if isinstance(x, str):
        return x
    if hasattr(x, "name"):
        return x.name
    if isinstance(x, dict):
        return x.get("path") or x.get("name")
    return str(x)


def visualize_uploaded(file_obj):
    return visualize.visualize_ontology(file_obj)


def visualize_result(result_path):
    path = _coerce_path(result_path)
    if not path:
        return ("<p style='font-family:sans-serif;color:#666'>Run the pipeline "
                "first, then click to visualize its final ontology.</p>", None)
    return visualize.visualize_ontology(path)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def build_ui():
    with gr.Blocks(title="RIGOR — Retrieval-Augmented Generation of Ontologies from Relational Databases") as demo:
        gr.HTML(_logo_html())
        gr.Markdown("### Retrieval-Augmented Generation of Ontologies from Relational Databases\n"
                    "Upload a table (CSV) or a SQL schema (JSON), review it, then run the "
                    "enrichment pipeline followed by syntax, logical-consistency, and "
                    "modeling-pitfall cleanup.")

        with gr.Accordion("Requirements & limitations", open=False):
            gr.Markdown(
                "- **External alignment** requires uploading **external ontologies** "
                "(see *Optional context* below). Without them, no alignments are produced; "
                "and even with them, alignment is **best-effort** — the model may align "
                "only some classes/properties.\n"
                "- **Logical consistency** (HermiT/ROBOT) needs **Java on PATH** plus "
                "`HermiT.jar` and `robot.jar`. If any is missing, the stage is skipped.\n"
                "- **OOPS! pitfall detection** uses the public OOPS! web service "
                "(needs **network access**, no OOPS key). The **repair** is LLM-driven, "
                "so it reuses your OpenRouter key — the stage runs only when a key is set, "
                "and is skipped offline.\n"
                "- Without an **API key**, the pipeline runs deterministic Direct Mapping "
                "only (no LLM enrichment, no OOPS repair)."
            )

        with gr.Accordion("Settings", open=True):
            with gr.Row():
                api_key = gr.Textbox(label="OpenRouter API key", type="password",
                                     placeholder="sk-or-...", scale=2)
                model = gr.Dropdown(label="Model", choices=MODEL_CHOICES,
                                    value=MODEL_CHOICES[0], allow_custom_value=True, scale=2)
                k_attempts = gr.Slider(label="Correction retries (k)", minimum=0,
                                       maximum=4, step=1, value=2, scale=1)
            gr.Markdown("_The key is used locally for this session only. It is not stored "
                        "to disk and is not logged._")
            with gr.Row():
                run_consistency = gr.Checkbox(label="Logical consistency (HermiT/ROBOT)", value=True)
                run_pitfalls = gr.Checkbox(label="OOPS! Critical pitfall repair", value=True)
            with gr.Row():
                repair_important = gr.Checkbox(
                    label="Also repair OOPS! Important pitfalls (in addition to Critical)",
                    value=False,
                    info="Routes important pitfalls through the same safe repair path "
                         "(no syntax errors, no new inconsistencies, structure preserved).")
            with gr.Row():
                hermit_jar = gr.Textbox(label="HermiT.jar path (optional)", placeholder="/path/to/HermiT.jar")
                robot_jar = gr.Textbox(label="robot.jar path (optional)", placeholder="/path/to/robot.jar")

        with gr.Accordion("Optional context (advanced)", open=False):
            gr.Markdown(
                "Add optional context to guide enrichment (all optional):\n"
                "- **Textual descriptions & external documents** — domain overviews, "
                "data dictionaries, glossaries, specs, or papers, used for retrieval "
                "during enrichment. Accepts plain text, Markdown, PDF, or Word: "
                "`.txt`, `.md`, `.pdf`, `.docx`.\n"
                "- **Seed core ontology** — an existing ontology to grow. Accepts "
                "`.owl`, `.rdf`, `.ttl`.\n"
                "- **External ontologies** — vocabularies to align with and retrieve "
                "from. Accepts `.owl`, `.rdf`, `.ttl`, `.nt`, `.n3`."
            )
            with gr.Row():
                core_file = gr.File(label="Seed core ontology — .owl / .rdf / .ttl (optional)",
                                    file_types=[".owl", ".rdf", ".ttl"])
                docs_files = gr.File(label="Documents — text, Markdown, PDF, Word (optional)",
                                     file_count="multiple",
                                     file_types=[".txt", ".md", ".pdf", ".docx"])
            with gr.Row():
                onto_files = gr.File(label="External ontologies — .owl / .rdf / .ttl / .nt / .n3 (optional)",
                                     file_count="multiple",
                                     file_types=[".owl", ".rdf", ".ttl", ".nt", ".n3"])

        with gr.Row():
            file_obj = gr.File(label="Upload CSV or schema JSON",
                               file_types=[".csv", ".tsv", ".json"])
        summarize_btn = gr.Button("Summarize table", variant="secondary")
        summary_md = gr.Markdown()
        stored_path = gr.State()

        with gr.Row():
            run_btn = gr.Button("Run pipeline", variant="primary", scale=3)
            stop_btn = gr.Button("Stop", variant="stop", scale=1)
        status_md = gr.Markdown("")
        with gr.Row():
            log_box = gr.Textbox(label="Progress", lines=18, max_lines=18, autoscroll=True)
        with gr.Row():
            out_file = gr.File(label="Final ontology (download)")
        with gr.Accordion("Final ontology (Turtle preview)", open=False):
            turtle_box = gr.Code(label="Turtle")

        with gr.Accordion("Visualize ontology (WebVOWL-style)", open=False):
            gr.Markdown(
                "Interactive graph of an ontology — classes, subclass links, and "
                "object/datatype properties. Visualize the pipeline's result, or "
                "upload any ontology. Drag nodes, scroll to zoom.\n\n"
                "_For the exact VOWL visual notation you can also open the "
                "downloaded ontology in the official "
                "[WebVOWL](https://service.tib.eu/webvowl/)._"
            )
            with gr.Row():
                viz_result_btn = gr.Button("Visualize last result", variant="secondary")
                viz_upload = gr.File(label="…or upload an ontology to visualize",
                                     file_types=[".owl", ".rdf", ".ttl", ".nt", ".n3", ".xml"])
            viz_html = gr.HTML(label="Ontology graph")
            viz_download = gr.File(label="Graph as standalone HTML (open in a browser)")

        summarize_btn.click(summarize, inputs=file_obj, outputs=[summary_md, stored_path])
        file_obj.change(summarize, inputs=file_obj, outputs=[summary_md, stored_path])
        run_event = run_btn.click(
            run_pipeline,
            inputs=[file_obj, api_key, model, k_attempts,
                    run_consistency, hermit_jar, robot_jar, run_pitfalls,
                    repair_important, core_file, docs_files, onto_files],
            outputs=[log_box, out_file, turtle_box, status_md],
        )
        # Stop sets the cancel flag (run loop breaks at the next checkpoint) and
        # cancels the streaming event so the UI returns control immediately.
        stop_btn.click(fn=lambda: _cancel_event.set(), inputs=None, outputs=None,
                       cancels=[run_event])
        viz_result_btn.click(visualize_result, inputs=out_file, outputs=[viz_html, viz_download])
        viz_upload.change(visualize_uploaded, inputs=viz_upload, outputs=[viz_html, viz_download])
    return demo


if __name__ == "__main__":
    build_ui().queue().launch()
