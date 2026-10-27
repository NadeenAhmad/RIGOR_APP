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
import json
import html
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
import validate_fix_ontology_pitfall_final as pitfall_mod

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
                 run_consistency, hermit_jar, robot_jar, repair_critical,
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
        yield "Please upload a file first.", None, "", "", "[]", "", None
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
    run_pitfalls = bool(repair_critical or repair_important)
    resolved_hermit = _resolve_jar(hermit_jar, "hermit") if run_consistency else None
    # ROBOT/HermiT validates any proposed pitfall repair, regardless of severity.
    need_robot = run_consistency or run_pitfalls
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
    work_result: dict = {}

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
            pp_result = postprocess_ontology(
                input_path=enriched,
                output_path=final,
                out_dir=session / "postprocess",
                model=model,
                hermit_jar=resolved_hermit,
                robot_jar=resolved_robot,
                run_syntax=True,
                run_consistency=run_consistency,
                run_pitfalls=run_pitfalls,
                repair_critical_pitfalls=bool(repair_critical),
                repair_important_pitfalls=bool(repair_important),
                progress=progress,
            )
            work_result.update(pp_result)
            q.put(_DONE)
        except Exception as e:  # noqa: BLE001
            q.put(f"{_ERR}{e}\n{traceback.format_exc()}")

    if not has_key:
        push("No API key provided — generation uses deterministic Direct Mapping. "
             "OOPS will still apply deterministic fixes and queue semantic findings for review.")

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
                yield "\n".join(log), None, "", status(frac), "[]", "", None
                return
            # Keep the elapsed clock ticking during quiet stretches.
            yield "\n".join(log), None, "", status(frac), "[]", "", None
            continue

        if msg == _DONE:
            break
        if msg.startswith(_ERR):
            push("❌ " + msg[len(_ERR):])
            yield "\n".join(log), None, "", status(frac), "[]", "", None
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
            yield "\n".join(log), None, "", status(frac), "[]", "", None
            return
        yield "\n".join(log), None, "", status(frac), "[]", "", None

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
    reviews = work_result.get("review_items", [])
    review_state = json.dumps(reviews)
    review_md = _review_overview(reviews)
    report_file = work_result.get("report_markdown")
    push("✅ Done.")
    yield "\n".join(log), out_file, turtle_preview, status(1.0), review_state, review_md, report_file


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
# Focused semantic review
# --------------------------------------------------------------------------- #
MAX_REVIEW_CARDS = 5

# Gradio validates every callback input, including hidden review cards. A Radio
# must never have a non-empty value while its choices are empty. Keep these
# choices stable for all cards and only toggle visibility dynamically.
SUBCLASS_MODE_CHOICES = [
    ("Cancel if a subclass relation exists", "cancel"),
    ("Keep subclass and add equivalence", "keep_subclass"),
    ("Replace subclass axiom with equivalence", "replace_subclass"),
]


def _parse_review_state(state):
    if not state:
        return []
    if isinstance(state, list):
        return state
    try:
        return json.loads(state)
    except Exception:
        return []


def _review_overview(reviews) -> str:
    reviews = _parse_review_state(reviews)
    if not reviews:
        return "✅ **No semantic decisions are required.**"
    lines = [
        f"### Needs your decision ({len(reviews)})",
        "Only choices that could change domain meaning are shown here. "
        "Routine syntax, logic, metadata, and declaration repairs are handled automatically.",
    ]
    for item in reviews[:MAX_REVIEW_CARDS]:
        lines.append(f"- **{item.get('code')}**: {item.get('question') or item.get('title')}")
    if len(reviews) > MAX_REVIEW_CARDS:
        lines.append(
            f"- _{len(reviews) - MAX_REVIEW_CARDS} additional item(s) are listed in the repair report._"
        )
    return "\n".join(lines)


def _review_details_html(item: dict) -> str:
    """Render evidence as an expandable block; this is viewing, not a decision."""
    affected = item.get("affected") or []
    notes = item.get("validation_notes") or []
    add_ttl = (item.get("proposed_add_ttl") or "").strip()
    delete_ttl = (item.get("proposed_delete_ttl") or "").strip()
    context = (item.get("context_excerpt") or "").strip()

    parts = [
        "<details style='margin-top:10px'>",
        "<summary style='cursor:pointer;font-weight:600'>Review details</summary>",
        "<div style='padding:8px 4px'>",
        f"<p><b>Severity:</b> {html.escape(str(item.get('severity') or ''))}</p>",
        f"<p><b>Affected:</b> {html.escape(', '.join(affected) or 'ontology-wide')}</p>",
    ]
    if notes:
        parts.append("<p><b>Validation notes:</b></p><ul>")
        parts.extend(f"<li>{html.escape(str(note))}</li>" for note in notes)
        parts.append("</ul>")
    if add_ttl:
        parts.append("<p><b>Triples proposed for addition:</b></p>")
        parts.append(f"<pre style='white-space:pre-wrap'>{html.escape(add_ttl)}</pre>")
    if delete_ttl:
        parts.append("<p><b>Triples proposed for deletion:</b></p>")
        parts.append(f"<pre style='white-space:pre-wrap'>{html.escape(delete_ttl)}</pre>")
    if not add_ttl and not delete_ttl and item.get("code") == "P24":
        parts.append(
            "<p><b>Executable action:</b> the app will locate the recursive OWL "
            "restriction structurally after you approve removal; blank-node IDs are "
            "not copied into a Turtle patch.</p>"
        )
    if context:
        clipped = context[:3000]
        if len(context) > len(clipped):
            clipped += "\n... [context truncated] ..."
        parts.append("<p><b>Relevant ontology context:</b></p>")
        parts.append(f"<pre style='white-space:pre-wrap;max-height:320px;overflow:auto'>{html.escape(clipped)}</pre>")
    parts.extend(["</div>", "</details>"])
    return "".join(parts)



def _candidate_evidence_markdown(item: dict) -> str:
    """Render source-labelled candidate evidence before expert controls."""
    if item.get("code") not in {"P10", "P30"}:
        return ""
    entries = item.get("candidate_evidence") or []
    lines = ["**Candidate evidence**"]
    if not entries:
        lines.append(
            "- OOPS! did not provide a concrete class pair, and the LLM did not "
            "produce a supported candidate. Use the dropdowns only when domain "
            "knowledge establishes the relationship."
        )
        return "\n\n".join(lines) + "\n\n"
    for entry in entries:
        source = str(entry.get("source") or "Evidence")
        label = str(entry.get("label") or "Candidate")
        summary = str(entry.get("summary") or "")
        lines.append(f"- **{source}:** `{label}`" + (f" — {summary}" if summary else ""))
    return "\n".join(lines) + "\n\n"

def render_review_card(review_state, index: int):
    """Render one semantic-review card using stable code-specific controls.

    P10/P30 always expose expert class-pair dropdowns populated with classes
    that actually occur in the ontology. The LLM may suggest pairs, but it does
    not define the interaction or button labels.
    """
    reviews = _parse_review_state(review_state)
    hidden = (
        gr.update(value="", visible=False),                         # question
        gr.update(choices=[], value=[], visible=False),              # LLM pairs
        gr.update(choices=[], value=None, visible=False),            # decision
        gr.update(value="", visible=False),                         # custom
        gr.update(choices=[], value=None, visible=False),            # left class
        gr.update(value="", visible=False),                         # symbol
        gr.update(choices=[], value=None, visible=False),            # right class
        gr.update(visible=False),                                    # add button
        gr.update(choices=[], value=[], visible=False),              # expert pairs
        gr.update(choices=SUBCLASS_MODE_CHOICES, value="cancel", visible=False),  # subclass handling
        gr.update(value="", visible=False),                         # pair status
    )
    if index >= len(reviews):
        return hidden

    item = reviews[index]
    affected = item.get("affected") or []
    explanation = item.get("explanation") or ""
    question = item.get("question") or item.get("title") or "Review this finding"
    atomic = item.get("atomic_proposals") or []
    md = (
        f"#### {item.get('title', item.get('code', 'Finding'))}\n"
        f"{question}\n\n"
        + _candidate_evidence_markdown(item)
        + f"_{explanation}_\n\n"
        f"Affected: `{', '.join(str(x) for x in affected[:5]) or 'ontology-wide'}`\n\n"
        + _review_details_html(item)
    )

    raw_choices = item.get("choices") or ["Apply proposed repair", "Keep current modeling"]
    choices = [choice for choice in raw_choices if choice != "Review details"]
    if not choices:
        choices = ["Keep current modeling", "Leave unresolved"]
    recommended = item.get("recommended")
    if recommended not in choices:
        recommended = choices[0]

    atomic_choices = [
        (str(proposal.get("label")), str(proposal.get("id") or proposal.get("label")))
        for proposal in atomic if proposal.get("label")
    ]
    input_kind = item.get("custom_input_kind", "none")
    input_label = item.get("custom_input_label") or "Optional value or note"

    pair_supported = item.get("code") in {"P10", "P30"} and len(item.get("entity_options") or []) >= 2
    entity_choices = [
        (str(option.get("label") or option.get("iri")), str(option.get("iri")))
        for option in (item.get("entity_options") or []) if option.get("iri")
    ]
    symbol = "⟂" if item.get("code") == "P10" else "≡"
    expert_label = (
        "Expert-selected disjoint axioms"
        if item.get("code") == "P10"
        else "Expert-selected equivalent-class axioms"
    )

    return (
        gr.update(value=md, visible=True),
        gr.update(
            choices=atomic_choices,
            value=[],
            label="Suggested axioms (approve any subset)",
            visible=bool(atomic_choices),
            show_select_all=bool(atomic_choices),
        ),
        gr.update(choices=choices, value=recommended, visible=True),
        gr.update(
            label=input_label,
            placeholder=("DomainIRI | RangeIRI" if input_kind == "domain_range" else "Optional"),
            value="",
            visible=input_kind != "none",
        ),
        gr.update(choices=entity_choices, value=None, label="First class", visible=pair_supported),
        gr.update(value=f"### {symbol}", visible=pair_supported),
        gr.update(choices=entity_choices, value=None, label="Second class", visible=pair_supported),
        gr.update(value="Add class pair", visible=pair_supported),
        gr.update(choices=[], value=[], label=expert_label, visible=pair_supported),
        gr.update(
            choices=SUBCLASS_MODE_CHOICES,
            value="cancel",
            label="If these classes already have a subclass relation",
            visible=item.get("code") == "P30" and pair_supported,
        ),
        gr.update(value="", visible=False),
    )

def _encoded_pair(left: str, right: str, subclass_mode: str = "") -> str:
    ordered = sorted((str(left), str(right)))
    payload = {"left": ordered[0], "right": ordered[1]}
    if subclass_mode:
        payload["subclass_mode"] = subclass_mode
    return json.dumps(payload, sort_keys=True)

def _pair_identity(encoded) -> tuple[str, str] | None:
    try:
        data = json.loads(encoded) if isinstance(encoded, str) else encoded
        return tuple(sorted((str(data["left"]), str(data["right"]))))
    except Exception:
        return None


def _pair_status(message: str = "", *, visible: bool | None = None):
    """Return a consistent Markdown update for the optional manual-pair status.

    The expert dropdowns are independent from suggested axioms. Their validation
    message is shown only after the user explicitly clicks ``Add class pair``.
    """
    if visible is None:
        visible = bool(message)
    return gr.update(value=message, visible=visible)


def _clear_pair_status():
    """Clear stale manual-pair messages when another decision control is used."""
    return _pair_status("", visible=False)


def add_expert_pair_ui(review_state, index: int, left, right, subclass_mode, current_pairs):
    """Add one user-selected P10/P30 pair with immediate basic validation.

    Logical validity is checked again by ROBOT/HermiT when all decisions are
    applied. This callback catches self-pairs, duplicates, nonexistent classes,
    already-present axioms, and direct disjoint/equivalent conflicts early.
    """
    reviews = _parse_review_state(review_state)
    current_pairs = list(current_pairs or [])
    if index >= len(reviews):
        return gr.update(), _pair_status("❌ Review item is no longer available."), gr.update()
    item = reviews[index]
    code = item.get("code")
    if code not in {"P10", "P30"}:
        return gr.update(), _pair_status("❌ Class-pair input is not supported for this finding."), gr.update()
    allowed = {str(option.get("iri")) for option in (item.get("entity_options") or []) if option.get("iri")}
    if not left or not right:
        return gr.update(), _pair_status("ℹ️ Manual pair not added: select both classes, or use the suggested axioms above."), gr.update()
    if str(left) not in allowed or str(right) not in allowed:
        return gr.update(), _pair_status("❌ Both classes must come from the ontology dropdowns."), gr.update()
    if str(left) == str(right):
        return gr.update(), _pair_status("❌ A class cannot be paired with itself."), gr.update()

    pair_key = _encoded_pair(str(left), str(right))
    if pair_key in set(item.get("existing_pair_keys") or []):
        relation = "disjoint" if code == "P10" else "equivalent"
        return gr.update(), _pair_status(f"ℹ️ These classes are already declared {relation}."), gr.update()
    if pair_key in set(item.get("conflicting_pair_keys") or []):
        conflict = "equivalent" if code == "P10" else "disjoint"
        return gr.update(), _pair_status(
            f"❌ These classes are already declared {conflict}; the requested axiom "
            "would make the ontology inconsistent."
        ), gr.update()
    identity = tuple(sorted((str(left), str(right))))
    if any(_pair_identity(value) == identity for value in current_pairs):
        return gr.update(), _pair_status("ℹ️ This pair is already in your selection."), gr.update()

    status_prefix = "✅"
    relation_note = "Pair added."
    selected_mode = ""
    if code == "P30":
        relations = item.get("subclass_relations") or []
        directed = [rel for rel in relations if {str(rel.get("subclass")), str(rel.get("superclass"))} == set(identity)]
        if directed:
            child = directed[0].get("subclass")
            parent = directed[0].get("superclass")
            labels = {str(option.get("iri")): str(option.get("label") or option.get("iri"))
                      for option in (item.get("entity_options") or []) if option.get("iri")}
            if subclass_mode in {None, "", "cancel"}:
                return gr.update(), _pair_status(
                    f"⚠️ {labels.get(child, child)} is already a subclass of "
                    f"{labels.get(parent, parent)}. Choose whether to keep that subclass "
                    "axiom and add equivalence, replace it with equivalence, or cancel."
                ), gr.update()
            selected_mode = str(subclass_mode)
            status_prefix = "⚠️"
            if selected_mode == "replace_subclass":
                relation_note = (
                    f"The existing subclass axiom will be removed and equivalence proposed. "
                    "The change is still subject to OOPS! and HermiT validation."
                )
            else:
                relation_note = (
                    f"The existing subclass axiom will be kept while equivalence is proposed. "
                    "Exact equivalence is still subject to OOPS! and HermiT validation."
                )
    encoded = _encoded_pair(str(left), str(right), selected_mode)
    current_pairs.append(encoded)
    labels = {str(option.get("iri")): str(option.get("label") or option.get("iri"))
              for option in (item.get("entity_options") or []) if option.get("iri")}
    symbol = "⟂" if code == "P10" else "≡"
    pair_choices = []
    for value in current_pairs:
        try:
            data = json.loads(value)
            display = f"{labels.get(data['left'], data['left'])} {symbol} {labels.get(data['right'], data['right'])}"
            mode = data.get("subclass_mode")
            if mode == "replace_subclass":
                display += " (replace subclass)"
            elif mode == "keep_subclass":
                display += " (keep subclass)"
        except Exception:
            display = value
        pair_choices.append((display, value))
    apply_choice = "Add selected disjoint axioms" if code == "P10" else "Add selected equivalences"
    return (
        gr.update(choices=pair_choices, value=current_pairs),
        _pair_status(
            f"{status_prefix} {relation_note} Uncheck it below to remove it before applying changes."
        ),
        gr.update(value=apply_choice),
    )


def _pair_adder(index: int):
    def add(state, left, right, subclass_mode, current_pairs):
        return add_expert_pair_ui(state, index, left, right, subclass_mode, current_pairs)
    return add


def _card_renderer(index: int):
    def render(state):
        return render_review_card(state, index)
    return render


def render_all_review_cards(review_state):
    """Render every fixed review slot in one callback.

    Using one callback avoids Gradio branch-order issues where only the first
    ``event.then(...)`` review card is refreshed even though the overview lists
    several decisions. The returned values are flattened in the exact order of
    ``review_components``.
    """
    updates = []
    for index in range(MAX_REVIEW_CARDS):
        updates.extend(render_review_card(review_state, index))
    return tuple(updates)


def _coerce_uploaded_path(file_obj):
    return _coerce_path(file_obj)


def apply_review_decisions_ui(
    ontology_file,
    review_state,
    robot_jar,
    *answers,
):
    """Apply semantic-review choices and expose the reviewed artifact clearly.

    Returns both the app's active result file and a dedicated reviewed-file
    download component. A reviewed ontology is written only when at least one
    decision was actually accepted by validation.
    """
    path = _coerce_path(ontology_file)
    reviews = _parse_review_state(review_state)
    if not path:
        return None, None, "No ontology is available.", "", None
    if not reviews:
        return path, None, "No decisions were required.", "", None

    decisions = []
    for index, item in enumerate(reviews[:MAX_REVIEW_CARDS]):
        selected_atomic = answers[index * 5] if index * 5 < len(answers) else []
        choice = answers[index * 5 + 1] if index * 5 + 1 < len(answers) else ""
        custom = answers[index * 5 + 2] if index * 5 + 2 < len(answers) else ""
        manual_pairs = answers[index * 5 + 3] if index * 5 + 3 < len(answers) else []
        subclass_mode = answers[index * 5 + 4] if index * 5 + 4 < len(answers) else "cancel"
        decisions.append({
            "id": item["id"],
            "choice": choice or "",
            "custom": custom or "",
            "selected_atomic": selected_atomic or [],
            "manual_pairs": manual_pairs or [],
            "subclass_mode": subclass_mode or "cancel",
        })

    graph, fmt = pitfall_mod.load_ontology(path)
    resolved_robot = _resolve_jar(robot_jar, "robot")
    updated, outcomes = pitfall_mod.apply_user_decisions(
        graph,
        reviews,
        decisions,
        robot_jar=resolved_robot,
        java_exe="java",
    )

    applied = sum(1 for item in outcomes if item.get("status") == "applied")
    retained = sum(1 for item in outcomes if item.get("status") == "retained")
    rejected = sum(1 for item in outcomes if item.get("status") == "rejected")
    unresolved = sum(1 for item in outcomes if item.get("status") == "unresolved")

    source = Path(path).expanduser().resolve()
    report = source.with_name(source.stem + "_decisions.json")
    report.write_text(json.dumps(outcomes, indent=2), encoding="utf-8")

    if applied == 0:
        rejection_reasons = []
        for outcome in outcomes:
            if outcome.get("status") == "rejected":
                rejection_reasons.extend(outcome.get("reasons") or [])
        reason_text = ""
        if rejection_reasons:
            reason_text = " **Reason:** " + "; ".join(dict.fromkeys(rejection_reasons))
        status = (
            "No ontology change was committed. "
            f"Retained intentionally: {retained} · Rejected by validation: {rejected} "
            f"· Still unresolved: {unresolved}." + reason_text
        )
        return str(source), None, status, graph.serialize(format="turtle"), str(report)

    suffix = source.suffix or ".owl"
    output = source.with_name(source.stem + "_reviewed" + suffix).resolve()
    output_format = pitfall_mod.guess_format(str(output)) or fmt
    pitfall_mod.serialize_ontology(updated, output, output_format)

    # Validate the exact bytes that the user will download.
    probe, _ = pitfall_mod.load_ontology(str(output), output_format)
    pitfall_mod.run_oops_or_local(probe)

    status = (
        f"Updated ontology ready: **{output.name}** ({len(updated):,} triples). "
        f"Applied: {applied} · Retained intentionally: {retained} "
        f"· Rejected by validation: {rejected} · Still unresolved: {unresolved}. "
        f"Download it from **Ontology after decisions** ({output.name})."
    )
    # Return the filepath directly. Keeping the component permanently visible
    # avoids Gradio-version-specific failures when revealing a hidden File
    # component with gr.update().
    return str(output), str(output), status, updated.serialize(format="turtle"), str(report)


def apply_review_decisions_stream(
    ontology_file,
    review_state,
    robot_jar,
    *answers,
):
    """Run slow OOPS!/HermiT decision validation off the Gradio worker.

    The old callback called ``apply_user_decisions`` synchronously. During live
    OOPS and ROBOT/HermiT calls that monopolised the event worker, making the
    review UI look frozen. This generator starts the work in a background
    thread and yields heartbeat updates so the browser remains responsive.
    """
    path = _coerce_path(ontology_file)
    if not path:
        yield None, None, "No ontology is available.", gr.update(), None
        return

    result_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
    started = time.time()

    def worker():
        try:
            result = apply_review_decisions_ui(
                ontology_file, review_state, robot_jar, *answers
            )
            result_queue.put(("done", result))
        except Exception as exc:  # noqa: BLE001
            result_queue.put(("error", (exc, traceback.format_exc())))

    threading.Thread(target=worker, daemon=True).start()

    # Immediate feedback before the first network/reasoner call begins.
    yield (
        gr.update(),
        gr.update(),
        "⏳ Validating the selected decisions with OOPS! and HermiT…",
        gr.update(),
        gr.update(),
    )

    while True:
        try:
            kind, payload = result_queue.get(timeout=0.35)
        except queue.Empty:
            elapsed = _fmt_elapsed(time.time() - started)
            yield (
                gr.update(),
                gr.update(),
                f"⏳ Validating decisions… {elapsed} elapsed. You can still inspect and change controls; do not click Apply again.",
                gr.update(),
                gr.update(),
            )
            continue

        if kind == "done":
            yield payload
            return

        exc, tb = payload
        yield (
            gr.update(),
            gr.update(),
            f"❌ Decision validation failed: {exc}\n\n```text\n{tb[-3000:]}\n```",
            gr.update(),
            gr.update(),
        )
        return


# --------------------------------------------------------------------------- #
# Existing-ontology repair path
# --------------------------------------------------------------------------- #
def run_existing_ontology_repair(
    ontology_file,
    api_key,
    model,
    run_consistency,
    hermit_jar,
    robot_jar,
    repair_critical,
    repair_important,
):
    log = []
    start = time.time()

    def status():
        return f"⏱ {_fmt_elapsed(time.time() - start)} elapsed"

    if not ontology_file:
        yield "Please upload an ontology first.", None, "", status(), "[]", "", None
        return

    api_key = (api_key or "").strip()
    api_utils.configure(api_key=api_key, model=model)
    if api_key:
        os.environ["OPENROUTER_API_KEY"] = api_key
    os.environ["RIGOR_MODEL"] = model

    _cleanup_sessions()
    session = SESSION_DIR / f"repair_{os.getpid()}_{int(threading.get_ident())}"
    session.mkdir(parents=True, exist_ok=True)
    source_path = Path(_coerce_path(ontology_file))
    copied = session / source_path.name
    shutil.copy(source_path, copied)
    suffix = copied.suffix or ".owl"
    final = session / f"{copied.stem}_fixed{suffix}"
    run_pitfalls = bool(repair_critical or repair_important)
    resolved_hermit = _resolve_jar(hermit_jar, "hermit") if run_consistency else None
    resolved_robot = _resolve_jar(robot_jar, "robot") if (run_consistency or run_pitfalls) else None

    q = queue.Queue()
    result_box = {}

    def progress(message):
        q.put(str(message))

    def work():
        try:
            result = postprocess_ontology(
                input_path=copied,
                output_path=final,
                out_dir=session / "postprocess",
                model=model,
                hermit_jar=resolved_hermit,
                robot_jar=resolved_robot,
                run_syntax=True,
                run_consistency=run_consistency,
                run_pitfalls=run_pitfalls,
                repair_critical_pitfalls=bool(repair_critical),
                repair_important_pitfalls=bool(repair_important),
                progress=progress,
            )
            result_box.update(result)
            q.put(_DONE)
        except Exception as exc:
            q.put(f"{_ERR}{exc}\n{traceback.format_exc()}")

    threading.Thread(target=work, daemon=True).start()
    while True:
        try:
            message = q.get(timeout=1)
        except queue.Empty:
            yield "\n".join(log), None, "", status(), "[]", "", None
            continue
        if message == _DONE:
            break
        if message.startswith(_ERR):
            log.append("❌ " + message[len(_ERR):])
            yield "\n".join(log), None, "", status(), "[]", "", None
            return
        log.append(message)
        yield "\n".join(log), None, "", status(), "[]", "", None

    reviews = result_box.get("review_items", [])
    preview = ""
    if final.exists():
        try:
            graph, _ = pitfall_mod.load_ontology(final)
            preview = graph.serialize(format="turtle")
        except Exception:
            preview = final.read_text(encoding="utf-8", errors="replace")
    log.append("✅ Existing ontology repair complete.")
    yield (
        "\n".join(log),
        str(final) if final.exists() else None,
        preview,
        status(),
        json.dumps(reviews),
        _review_overview(reviews),
        result_box.get("report_markdown"),
    )


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def _oops_status_markdown() -> str:
    ok, detail = pitfall_mod.oops_health_check()
    if ok:
        return f"✅ {detail}"
    return (f"⚠️ {detail}  \nRuns will use cached OOPS! results and local pitfall "
            "checks; try another network (e.g. a phone hotspot) for live OOPS!.")


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
                "so it reuses your OpenRouter key — the stage runs only when a key is set. "
                "If OOPS! is unreachable, the stage uses cached OOPS! results and local "
                "checks (P11, P25, P26, P34, P35, P38, P39, P41) instead of waiting.\n"
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
                run_consistency = gr.Checkbox(
                    label="Logical consistency (HermiT/ROBOT)", value=True
                )
                repair_critical = gr.Checkbox(
                    label="Repair Critical pitfalls",
                    value=True,
                    info="Detect and repair OOPS! findings classified as Critical.",
                )
                repair_important = gr.Checkbox(
                    label="Repair Important pitfalls",
                    value=True,
                    info="Detect and repair OOPS! findings classified as Important. Semantic ambiguities are shown for a focused user decision.",
                )
            with gr.Row():
                hermit_jar = gr.Textbox(label="HermiT.jar path (optional)", placeholder="/path/to/HermiT.jar")
                robot_jar = gr.Textbox(label="robot.jar path (optional)", placeholder="/path/to/robot.jar")
            with gr.Row():
                oops_check_btn = gr.Button("Check OOPS! connection", variant="secondary", scale=1)
                oops_status_md = gr.Markdown(
                    "_If OOPS! is unreachable, the run continues with cached OOPS! results "
                    "and local pitfall checks instead of waiting on the service._"
                )
            oops_check_btn.click(_oops_status_markdown, inputs=None, outputs=oops_status_md)

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
            run_btn = gr.Button("Run generation pipeline", variant="primary", scale=3)
            stop_btn = gr.Button("Stop", variant="stop", scale=1)

        with gr.Accordion("Repair an existing ontology", open=False):
            gr.Markdown(
                "Upload an existing ontology to run format-aware syntax repair, "
                "ROBOT/HermiT consistency repair, and Critical/Important OOPS! cleanup."
            )
            existing_ontology = gr.File(
                label="Existing ontology",
                file_types=[".owl", ".rdf", ".xml", ".ttl", ".nt", ".n3", ".jsonld", ".trig", ".trix"],
            )
            repair_existing_btn = gr.Button("Repair uploaded ontology", variant="primary")

        status_md = gr.Markdown("")
        with gr.Row():
            log_box = gr.Textbox(label="Progress", lines=18, max_lines=18, autoscroll=True)
        with gr.Row():
            out_file = gr.File(label="Final ontology (download)")
        with gr.Accordion("Final ontology (Turtle preview)", open=False):
            turtle_box = gr.Code(label="Turtle")

        review_state = gr.State("[]")
        review_summary = gr.Markdown("✅ **No semantic decisions are required.**")
        repair_report = gr.File(label="Repair report")
        review_components = []
        with gr.Accordion("Needs your decision", open=True):
            gr.Markdown(
                "This section appears only for semantic choices that cannot be "
                "settled safely by deterministic rules, the LLM, and automated validators."
            )
            for idx in range(MAX_REVIEW_CARDS):
                with gr.Group():
                    question_md = gr.Markdown(visible=False)
                    atomic_choices = gr.CheckboxGroup(
                        choices=[],
                        label="LLM-proposed axioms (approve any subset)",
                        show_select_all=True,
                        visible=False,
                    )
                    with gr.Row():
                        expert_left = gr.Dropdown(choices=[], label="First class", visible=False)
                        pair_symbol = gr.Markdown("", visible=False)
                        expert_right = gr.Dropdown(choices=[], label="Second class", visible=False)
                        add_pair_btn = gr.Button("Add class pair", visible=False)
                    expert_pairs = gr.CheckboxGroup(
                        choices=[],
                        label="Expert-selected class-pair axioms",
                        visible=False,
                    )
                    subclass_mode = gr.Radio(
                        choices=SUBCLASS_MODE_CHOICES,
                        value="cancel",
                        label="If these classes already have a subclass relation",
                        visible=False,
                    )
                    pair_status = gr.Markdown("", visible=False)
                    choice_radio = gr.Radio(choices=[], label=f"Decision {idx + 1}", visible=False)
                    custom_text = gr.Textbox(label="Optional value", visible=False)
                review_components.append((
                    question_md, atomic_choices, choice_radio, custom_text,
                    expert_left, pair_symbol, expert_right, add_pair_btn,
                    expert_pairs, subclass_mode, pair_status,
                ))
            apply_decisions_btn = gr.Button("Apply selected decisions", variant="primary")
            decision_status = gr.Markdown("")
            reviewed_out_file = gr.File(
                label="Ontology after decisions (download)",
                visible=True,
            )
            decision_report = gr.File(label="Decision validation report")

        with gr.Accordion("Visualize ontology", open=False):
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
                    run_consistency, hermit_jar, robot_jar, repair_critical,
                    repair_important, core_file, docs_files, onto_files],
            outputs=[log_box, out_file, turtle_box, status_md,
                     review_state, review_summary, repair_report],
        )
        review_outputs = [
            component
            for card_components in review_components
            for component in card_components
        ]
        run_event.then(
            render_all_review_cards,
            inputs=review_state,
            outputs=review_outputs,
            queue=False,
        )

        existing_event = repair_existing_btn.click(
            run_existing_ontology_repair,
            inputs=[existing_ontology, api_key, model, run_consistency,
                    hermit_jar, robot_jar, repair_critical, repair_important],
            outputs=[log_box, out_file, turtle_box, status_md,
                     review_state, review_summary, repair_report],
        )
        existing_event.then(
            render_all_review_cards,
            inputs=review_state,
            outputs=review_outputs,
            queue=False,
        )

        # Expert class-pair builders are independent of the pipeline event.
        # Users may add multiple pairs; the CheckboxGroup acts as an editable
        # staging list before the validated Apply action.
        for idx, components in enumerate(review_components):
            (question_md, atomic_choices, choice_radio, custom_text,
             expert_left, pair_symbol, expert_right, add_pair_btn,
             expert_pairs, subclass_mode, pair_status) = components
            add_pair_btn.click(
                _pair_adder(idx),
                inputs=[review_state, expert_left, expert_right, subclass_mode, expert_pairs],
                outputs=[expert_pairs, pair_status, choice_radio],
                queue=False,
            )
            # Do not attach server callbacks to radio/checkbox/dropdown changes.
            # Those controls are intentionally client-side so they remain
            # responsive even while a validation job is running.

        decision_inputs = [out_file, review_state, robot_jar]
        for (question_md, atomic_choices, choice_radio, custom_text,
             expert_left, pair_symbol, expert_right, add_pair_btn,
             expert_pairs, subclass_mode, pair_status) in review_components:
            decision_inputs.extend([atomic_choices, choice_radio, custom_text, expert_pairs, subclass_mode])
        apply_decisions_btn.click(
            apply_review_decisions_stream,
            inputs=decision_inputs,
            outputs=[out_file, reviewed_out_file, decision_status, turtle_box, decision_report],
            show_progress="minimal",
            concurrency_limit=1,
            trigger_mode="once",
        )
        # Stop sets the cancel flag (run loop breaks at the next checkpoint) and
        # cancels the streaming event so the UI returns control immediately.
        stop_btn.click(fn=lambda: _cancel_event.set(), inputs=None, outputs=None,
                       cancels=[run_event, existing_event])
        viz_result_btn.click(visualize_result, inputs=out_file, outputs=[viz_html, viz_download])
        viz_upload.change(visualize_uploaded, inputs=viz_upload, outputs=[viz_html, viz_download])
    return demo


if __name__ == "__main__":
    print(pitfall_mod.oops_health_check()[1], flush=True)
    build_ui().queue().launch()