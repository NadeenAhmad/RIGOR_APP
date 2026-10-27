"""
visualize.py — a self-contained, WebVOWL-style interactive view of an ontology.

In the spirit of WebVOWL (https://service.tib.eu/webvowl/), but fully local:
the ontology is parsed with rdflib and rendered as an interactive force-directed
graph (classes, subclass links, object/datatype properties). The graph is drawn
client-side with vis-network, returned as an HTML string the Gradio UI embeds in
a sandboxed iframe. A standalone .html copy is also written so the graph can be
opened directly in a browser if inline rendering is unavailable.

No external service is contacted: vis-network is inlined from assets/ (with a
CDN fallback if that file is missing), so the graph also renders offline.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Tuple

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS, OWL, XSD, SKOS
from rdflib.collection import Collection

# vis-network standalone UMD build (bundles DataSet). A copy ships in assets/
# and is inlined so the graph renders offline; the CDN is only a fallback.
_VIS_CDN = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
_VIS_LOCAL = Path(__file__).resolve().parent / "assets" / "vis-network.min.js"


def _vis_script_tag() -> str:
    try:
        js = _VIS_LOCAL.read_text(encoding="utf-8")
    except OSError:
        return f'<script src="{_VIS_CDN}"></script>'
    return "<script>" + js.replace("</script", "<\\/script") + "</script>"

_COLOR_CLASS = "#aacbff"
_COLOR_DATATYPE = "#ffe49b"
_COLOR_OBJ_EDGE = "#2f9e54"
_COLOR_SUBCLASS = "#888888"
_COLOR_EQUIV = "#9b59b6"
_COLOR_RESTRICTION = "#3a7bd5"
_COLOR_DISJOINT = "#d9534f"
_COLOR_EXTERNAL = "#e6399b"  # magenta/pink — distinct from the green property edges and every other palette entry

# Colorblind-safe variant (Okabe-Ito palette), enabled with
# RIGOR_VIZ_PALETTE=cb (or "colorblind"). The legend and graph read these
# module constants, so reassigning them here recolors everything consistently.
if os.environ.get("RIGOR_VIZ_PALETTE", "").strip().lower() in {"cb", "colorblind", "okabe-ito", "okabe_ito"}:
    _COLOR_CLASS = "#56B4E9"        # sky blue
    _COLOR_DATATYPE = "#F0E442"     # yellow
    _COLOR_OBJ_EDGE = "#009E73"     # bluish green
    _COLOR_SUBCLASS = "#999999"     # neutral gray
    _COLOR_EQUIV = "#E69F00"        # orange
    _COLOR_RESTRICTION = "#0072B2"  # blue
    _COLOR_DISJOINT = "#D55E00"     # vermillion
    _COLOR_EXTERNAL = "#CC79A7"     # reddish purple


def _load_graph(path) -> Graph:
    g = Graph()
    suffix = Path(path).suffix.lower()
    prefer = {".ttl": "turtle", ".n3": "n3", ".nt": "nt",
              ".owl": "xml", ".rdf": "xml", ".xml": "xml"}.get(suffix)
    last = None
    tried = []
    for fmt in (prefer, "xml", "turtle", "nt"):
        if not fmt or fmt in tried:
            continue
        tried.append(fmt)
        try:
            g.parse(str(path), format=fmt)
            return g
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Could not parse ontology at {path}: {last}")


def _local(term) -> str:
    s = str(term)
    if "#" in s:
        return s.rsplit("#", 1)[1] or s
    if "/" in s:
        return s.rstrip("/").rsplit("/", 1)[1] or s
    return s


def _humanize(name: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    return re.sub(r"\s+", " ", s.replace("_", " ").replace("-", " ")).strip()


def _is_generated_label(term, label: str) -> bool:
    """True for the mechanical labels Direct Mapping derives from identifiers
    (e.g. 'BM DEP' for ae_tox_BM_DEP), as opposed to a curated label."""
    parts = _local(term).split("_")
    generated = {_humanize("_".join(parts[i:])).lower() for i in range(len(parts))}
    return label.strip().lower() in generated


def _label(g: Graph, term) -> str:
    """Prefer an English, curated label; fall back to any label, then the IRI."""
    labels = [o for o in g.objects(term, RDFS.label) if str(o).strip()]
    labels += [o for o in g.objects(term, SKOS.altLabel) if str(o).strip()]
    if not labels:
        return _local(term)
    english = [o for o in labels if (getattr(o, "language", None) or "en").startswith("en")] or labels
    primary = set(g.objects(term, RDFS.label))
    whole = _humanize(_local(term)).lower()
    return str(min(english, key=lambda o: (
        _is_generated_label(term, str(o)),
        str(o).strip().lower() == whole,
        o not in primary,
        str(o),
    )))


_WELL_KNOWN_PREFIXES = {
    "http://purl.org/goodrelations/v1#": "gr",
    "http://xmlns.com/foaf/0.1/": "foaf",
    "http://schema.org/": "schema",
    "https://schema.org/": "schema",
    "http://purl.org/dc/terms/": "dcterms",
    "http://www.w3.org/ns/prov#": "prov",
}


def _prefix_from_namespace(ns: str) -> str | None:
    """A readable stand-in prefix when the file binds none, taken from the
    namespace path: http://example.org/ext/oncology# -> 'oncology'."""
    segments = [seg for seg in re.split(r"[/#]", ns.split("://", 1)[-1]) if seg]
    for seg in reversed(segments[1:]):  # skip the host name
        if re.fullmatch(r"v?\d+(\.\d+)*|ns|ontology|owl|rdf|schema", seg.lower()):
            continue
        if re.fullmatch(r"[A-Za-z][\w-]{1,30}", seg):
            return seg.lower()
    return None


def _short_iri(g: Graph, iri: str) -> str:
    ns = _ns(iri)
    for prefix, uri in g.namespaces():
        if prefix and str(uri) == ns and not re.fullmatch(r"ns\d+|default\d*", prefix):
            return f"{prefix}:{_local(iri)}"
    if ns in _WELL_KNOWN_PREFIXES:
        return f"{_WELL_KNOWN_PREFIXES[ns]}:{_local(iri)}"
    guessed = _prefix_from_namespace(ns)
    return f"{guessed}:{_local(iri)}" if guessed else _local(iri)


def _external_label(g: Graph, term) -> str:
    """External terms show their vocabulary prefix (e.g. foaf:Person) when the
    ontology binds one, so a link to a standard vocabulary is recognisable."""
    ns = _ns(term)
    for prefix, uri in g.namespaces():
        if prefix and str(uri) == ns and not re.fullmatch(r"ns\d+|default\d*", prefix):
            return f"{prefix}:{_local(term)}"
    if ns in _WELL_KNOWN_PREFIXES:
        return f"{_WELL_KNOWN_PREFIXES[ns]}:{_local(term)}"
    guessed = _prefix_from_namespace(ns)
    return f"{guessed}:{_local(term)}" if guessed else _label(g, term)


def _key_properties(g: Graph) -> set:
    """Property IRIs that appear in any owl:hasKey list (for a '(key)' marker)."""
    keys: set = set()
    for _, lst in g.subject_objects(OWL.hasKey):
        try:
            for item in Collection(g, lst):
                if isinstance(item, URIRef):
                    keys.add(item)
        except Exception:  # noqa: BLE001
            pass
    return keys


def _is_datatype(term) -> bool:
    return isinstance(term, URIRef) and str(term).startswith(str(XSD))


def _ns(term) -> str:
    s = str(term)
    if "#" in s:
        return s.rsplit("#", 1)[0] + "#"
    return s.rsplit("/", 1)[0] + "/"


def _local_namespace(g: Graph) -> str | None:
    """The ontology's own namespace — the most common namespace among its
    declared classes. External alignment targets (mo:, doremus:, …) are not
    declared as classes here, so they don't skew this."""
    from collections import Counter
    counts: Counter = Counter()
    for c in g.subjects(RDF.type, OWL.Class):
        if isinstance(c, URIRef):
            counts[_ns(c)] += 1
    return counts.most_common(1)[0][0] if counts else None


def build_vis_data(g: Graph) -> Tuple[list, list, dict]:
    """Extract nodes/edges for vis-network from an ontology graph."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    n_restr = 0
    n_disjoint = 0
    lit_counter = 0
    dt_counter = 0
    local_ns = _local_namespace(g)

    def add_class(term) -> str:
        nid = str(term)
        if nid not in nodes:
            external = bool(local_ns) and not nid.startswith(local_ns)
            if external:
                nodes[nid] = {"id": nid, "label": _external_label(g, term), "title": nid,
                              "shape": "ellipse", "color": _COLOR_EXTERNAL, "borderWidth": 2,
"shapeProperties": {"borderDashes": [6, 4]}}
            else:
                nodes[nid] = {"id": nid, "label": _label(g, term), "title": nid,
                             "shape": "ellipse", "color": _COLOR_CLASS}
        return nid

    def add_datatype(term) -> str:
        # Fresh node per occurrence (like WebVOWL): a class is one entity, but
        # each literal range gets its own box so datatypes fan out next to their
        # property instead of all collapsing onto one crowded hub.
        nonlocal dt_counter
        dt_counter += 1
        nid = f"dt::{dt_counter}::{term}"
        nodes[nid] = {"id": nid, "label": _local(term), "title": str(term),
                      "shape": "box", "color": _COLOR_DATATYPE}
        return nid

    def add_filler(term) -> str:
        """A restriction filler may be a datatype, a class, or (hasValue) a literal."""
        nonlocal lit_counter
        if _is_datatype(term):
            return add_datatype(term)
        if isinstance(term, URIRef):
            return add_class(term)
        # Literal (hasValue): give it its own small node.
        lit_counter += 1
        nid = f"lit::{lit_counter}"
        nodes[nid] = {"id": nid, "label": str(term), "title": str(term),
                     "shape": "box", "color": _COLOR_DATATYPE}
        return nid

    key_props = _key_properties(g)

    def plabel_of(p) -> str:
        lbl = _label(g, p)
        return f"{lbl} (key)" if p in key_props else lbl

    # Seed classes from declarations and structural usage.
    classes = set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, RDFS.Class))
    for s, o in g.subject_objects(RDFS.subClassOf):
        classes.add(s)
        classes.add(o)
    for c in classes:
        if isinstance(c, URIRef):
            add_class(c)

    # --- External alignment (beyond subClassOf/equivalentClass) -------------
    # Property edges are drawn as labelled lines, not nodes, so a property->
    # property alignment can't be a node; we annotate the property's edge with
    # it. Class-level skos:exactMatch/closeMatch, by contrast, do get rendered
    # as alignment edges to an external (magenta) class node.
    obj_prop_iris = {p for p in g.subjects(RDF.type, OWL.ObjectProperty) if isinstance(p, URIRef)}
    data_prop_iris = {p for p in g.subjects(RDF.type, OWL.DatatypeProperty) if isinstance(p, URIRef)}
    all_prop_iris = obj_prop_iris | data_prop_iris

    _STD_NS = (str(OWL), str(RDFS), str(RDF), str(XSD), str(SKOS))

    def _is_external(term) -> bool:
        if not isinstance(term, URIRef):
            return False
        s = str(term)
        if local_ns and s.startswith(local_ns):
            return False
        return not any(s.startswith(ns) for ns in _STD_NS)

    # Property -> external alignment, keyed by property IRI. Symbols: ≡ owl:equivalent*,
    # ⊑ rdfs:subPropertyOf, ≈ skos mapping. Used to annotate property edge labels.
    _ALIGN_PREDS = ((OWL.equivalentProperty, "\u2261"), (RDFS.subPropertyOf, "\u2291"),
                    (SKOS.exactMatch, "\u2248"), (SKOS.closeMatch, "\u2248"))
    prop_alignments: dict = {}
    for pred, sym in _ALIGN_PREDS:
        for s, o in g.subject_objects(pred):
            if s in all_prop_iris and _is_external(o):
                prop_alignments.setdefault(s, []).append((sym, _local(o), str(o)))

    def _prop_label(p) -> str:
        base = plabel_of(p)
        al = prop_alignments.get(p)
        if al:
            base += "  " + " ".join(f"{sym} {loc}" for sym, loc, _ in al)
        return base

    def _prop_title(p):
        al = prop_alignments.get(p)
        if not al:
            return None
        return f"{p}  ·  aligned: " + "; ".join(f"{sym} {iri}" for sym, _, iri in al)

    # Pairwise class axioms: one edge per pair (symmetric ones in either order).
    seen_pairs: set = set()

    def pair_edge(s, o, label, color, arrows, symmetric) -> bool:
        key = (label, frozenset((str(s), str(o)))) if symmetric else (label, str(s), str(o))
        if key in seen_pairs:
            return False
        seen_pairs.add(key)
        add_class(s)
        add_class(o)
        edges.append({"from": str(s), "to": str(o), "label": label,
                      "dashes": True, "arrows": arrows, "color": {"color": color}})
        return True

    for s, o in g.subject_objects(RDFS.subClassOf):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            pair_edge(s, o, "subClassOf", _COLOR_SUBCLASS, "to", False)
    for s, o in g.subject_objects(OWL.equivalentClass):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            pair_edge(s, o, "equivalentClass", _COLOR_EQUIV, "", True)
    for s, o in g.subject_objects(OWL.disjointWith):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            if pair_edge(s, o, "disjointWith", _COLOR_DISJOINT, "", True):
                n_disjoint += 1

    # Class-level skos:exactMatch / skos:closeMatch -> external alignment edges.
    n_align = 0
    for pred, plbl in ((SKOS.exactMatch, "exactMatch"), (SKOS.closeMatch, "closeMatch")):
        for s, o in g.subject_objects(pred):
            if not (isinstance(s, URIRef) and isinstance(o, URIRef)):
                continue
            if s in all_prop_iris:
                continue  # property alignment -> annotated on the property edge instead
            if pair_edge(s, o, plbl, _COLOR_EQUIV, "", True):
                n_align += 1

    # Property axioms. Domain/range declarations and every restriction on the
    # same (class, property, filler) are merged into ONE edge whose label lists
    # them (e.g. "unit price  ∀ ∃ ≤1"), so a column is never drawn several times.
    prop_edges: dict = {}

    def note(cls, prop, filler, mark: str) -> None:
        entry = prop_edges.setdefault((str(cls), str(prop), str(filler)),
                                      {"cls": cls, "prop": prop, "filler": filler, "marks": []})
        if mark not in entry["marks"]:
            entry["marks"].append(mark)

    _CARDINALITIES = (
        (OWL.qualifiedCardinality, "="), (OWL.cardinality, "="),
        (OWL.minQualifiedCardinality, "\u2265"), (OWL.minCardinality, "\u2265"),
        (OWL.maxQualifiedCardinality, "\u2264"), (OWL.maxCardinality, "\u2264"),
    )
    for s, o in g.subject_objects(RDFS.subClassOf):
        if not isinstance(s, URIRef) or (o, RDF.type, OWL.Restriction) not in g:
            continue
        prop = next(g.objects(o, OWL.onProperty), None)
        if not isinstance(prop, URIRef):
            continue
        noted = False
        for filler in g.objects(o, OWL.allValuesFrom):
            note(s, prop, filler, "\u2200"); noted = True
        for filler in g.objects(o, OWL.someValuesFrom):
            note(s, prop, filler, "\u2203"); noted = True
        for value in g.objects(o, OWL.hasValue):
            note(s, prop, value, "value"); noted = True
        for pred, sym in _CARDINALITIES:
            for n in g.objects(o, pred):
                filler = (next(g.objects(o, OWL.onClass), None)
                          or next(g.objects(o, OWL.onDataRange), None)
                          or next((r for r in g.objects(prop, RDFS.range) if isinstance(r, URIRef)), None))
                if filler is not None:
                    note(s, prop, filler, f"{sym}{n}"); noted = True
        if noted:
            n_restr += 1

    # Declared domain -> range (object and datatype properties).
    for p in obj_prop_iris | data_prop_iris:
        domains = [d for d in g.objects(p, RDFS.domain) if isinstance(d, URIRef)]
        ranges = [r for r in g.objects(p, RDFS.range) if isinstance(r, URIRef)]
        for d in domains:
            for r in ranges:
                note(d, p, r, "domain")
    n_obj = len(obj_prop_iris)
    n_dt = len(data_prop_iris)

    _MARK_ORDER = {"\u2200": 0, "\u2203": 1, "value": 2}
    for entry in prop_edges.values():
        marks = entry["marks"]
        declared = "domain" in marks
        shown = sorted((m for m in marks if m != "domain"),
                       key=lambda m: (_MARK_ORDER.get(m, 3), m))
        label = _prop_label(entry["prop"]) + ("  " + " ".join(shown) if shown else "")
        source = add_class(entry["cls"])
        target = add_filler(entry["filler"])
        e = {"from": source, "to": target, "label": label, "arrows": "to", "prop": str(entry["prop"]),
             "color": {"color": _COLOR_OBJ_EDGE if declared else _COLOR_RESTRICTION}}
        if not declared:
            e["dashes"] = True
        _t = _prop_title(entry["prop"])
        if _t:
            e["title"] = _t
            e["dashes"] = True
        edges.append(e)

    # Two different classes must never look identical: add the IRI's short
    # form (prefix:name, or the full IRI if that is still ambiguous) to any
    # shared label.
    def disambiguate(items, iri_of) -> None:
        groups: dict = {}
        for item in items:
            groups.setdefault(item["label"], []).append(item)
        for same in groups.values():
            if len({iri_of(i) for i in same}) < 2:
                continue
            shorts = {iri_of(i): _short_iri(g, iri_of(i)) for i in same}
            unique = len(set(shorts.values())) == len(shorts)
            for item in same:
                tag = shorts[iri_of(item)] if unique else iri_of(item)
                item["label"] = f"{item['label']}\n({tag})"

    disambiguate([n for n in nodes.values() if n.get("shape") == "ellipse"], lambda n: n["id"])
    by_ends: dict = {}
    for e in edges:
        if "prop" in e:
            by_ends.setdefault((e["from"], e["to"]), []).append(e)
    for same_ends in by_ends.values():
        disambiguate(same_ends, lambda e: e["prop"])
    for e in edges:
        e.pop("prop", None)

    stats = {
        "classes": sum(1 for n in nodes.values() if n.get("shape")=="ellipse" and "shapeProperties" not in n),
        "external": sum(1 for n in nodes.values() if n.get("shape")=="ellipse" and "shapeProperties" in n),
        "datatypes": sum(1 for n in nodes.values() if n.get("shape")=="box"),
        "object_props": n_obj,
        "datatype_props": n_dt,
        "restrictions": n_restr,
        "disjoint": n_disjoint,
        "class_alignments": n_align,
        "prop_alignments": len(prop_alignments),
        "edges": len(edges),
    }
    return list(nodes.values()), edges, stats


def _full_html(nodes: list, edges: list) -> str:
    payload = json.dumps({"nodes": nodes, "edges": edges})
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
{_vis_script_tag()}
<style>
  html,body{{margin:0;height:100%;font-family:system-ui,Arial,sans-serif;}}
  #net{{width:100%;height:100%;}}
  #legend{{position:absolute;top:8px;left:8px;background:rgba(255,255,255,.92);
    border:1px solid #e0e0e0;border-radius:6px;padding:6px 10px;font-size:12px;
    line-height:1.6;}}
  .sw{{display:inline-block;width:12px;height:12px;border-radius:3px;
    margin-right:6px;vertical-align:middle;}}
  #err{{font-family:sans-serif;color:#b00;padding:12px;}}
</style></head>
<body>
<div id="legend">
  <div><span class="sw" style="background:{_COLOR_CLASS}"></span>Class</div>
  <div><span class="sw" style="background:{_COLOR_EXTERNAL}"></span>External / aligned class (dashed)</div>
  <div><span class="sw" style="background:{_COLOR_DATATYPE}"></span>Datatype</div>
  <div><span class="sw" style="background:{_COLOR_OBJ_EDGE}"></span>object / datatype property</div>
  <div><span class="sw" style="background:{_COLOR_SUBCLASS}"></span>subClassOf (dashed)</div>
  <div><span class="sw" style="background:{_COLOR_RESTRICTION}"></span>restriction only (dashed)</div>
  <div style="color:#777">∀ ∃ = ≤ ≥ after a property name: its restrictions on that class</div>
  <div><span class="sw" style="background:{_COLOR_EQUIV}"></span>equivalentClass / skos exact·closeMatch (dashed)</div>
  <div><span class="sw" style="background:{_COLOR_DISJOINT}"></span>disjointWith</div>
  <div style="margin-top:4px;color:#777">Property alignments (≡ ⊑ ≈) appear on the property's edge label.</div>
</div>
<div id="net"></div>
<script>
  (function() {{
    if (typeof vis === "undefined") {{
      document.body.innerHTML =
        '<div id="err">Could not load the vis-network library ' +
        '(no internet access?). Download this view and open it once online, ' +
        'or use the official WebVOWL.</div>';
      return;
    }}
    var data = {payload};
    var container = document.getElementById('net');
    var nodes = new vis.DataSet(data.nodes);
    var edges = new vis.DataSet(data.edges);
    new vis.Network(container, {{nodes: nodes, edges: edges}}, {{
     nodes: {{ font: {{ size: 18 }}, borderWidth: 1, shadow: false }},
      groups: {{ useDefaultGroups: false }},
      edges: {{ font: {{ size: 13, align: 'middle', color: '#333',
                         strokeWidth: 3, strokeColor: '#ffffff' }},
               smooth: {{ type: 'dynamic' }}, color: {{ inherit: false, highlight: '#f39c12', hover: '#f39c12' }} }},
      physics: {{ stabilization: {{ iterations: 250 }},
                 barnesHut: {{ springLength: 150, gravitationalConstant: -3500 }} }},
      interaction: {{ hover: true, tooltipDelay: 120, multiselect: true }}
    }});
  }})();
</script>
</body></html>"""


def _iframe(full_html: str, height: int = 620) -> str:
    srcdoc = html.escape(full_html, quote=True)
    return (
        f'<iframe srcdoc="{srcdoc}" '
        f'title="Interactive ontology graph (classes, properties, and alignments)" '
        f'style="width:100%;height:{height}px;border:1px solid #e5e7eb;'
        f'border-radius:8px;background:#fff;" '
        f'sandbox="allow-scripts allow-same-origin"></iframe>'
    )


def _msg(text: str, color: str = "#666") -> str:
    return f"<p style='font-family:sans-serif;color:{color};margin:8px 0'>{html.escape(text)}</p>"


def visualize_ontology(file_or_path, out_dir: str | None = None) -> Tuple[str, str | None]:
    """
    Build the interactive graph for an ontology.

    Returns (inline_html, standalone_html_path). The inline HTML renders in the
    Gradio panel; the standalone file is offered for download so the graph can
    be opened directly in a browser if inline rendering is blocked.
    """
    if not file_or_path:
        return _msg("Upload an ontology (or run the pipeline) and click Render graph."), None

    path = getattr(file_or_path, "name", None) or str(file_or_path)
    if not path or not os.path.exists(path):
        return _msg("Ontology file not found.", "#b00"), None

    try:
        g = _load_graph(path)
    except Exception as e:  # noqa: BLE001
        return _msg(f"Could not parse ontology: {e}", "#b00"), None

    nodes, edges, stats = build_vis_data(g)
    if not nodes:
        return _msg("No classes or properties were found to display in this ontology."), None

    full = _full_html(nodes, edges)

    out_dir = Path(out_dir) if out_dir else Path(tempfile.gettempdir()) / "rigor_viz"
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / (Path(path).stem + ".html")
    html_path.write_text(full, encoding="utf-8")

    info = (
        "<div style='font-family:sans-serif;font-size:13px;margin:4px 0;color:#555'>"
        f"Showing {stats['classes']} classes, {stats['external']} external/aligned "
        f"classes, {stats['object_props']} object properties, "
        f"{stats['datatype_props']} datatype properties, "
        f"{stats['restrictions']} restrictions (∀ ∃ = ≤ ≥), {stats['disjoint']} disjointness "
        f"axioms, {stats['class_alignments']} class + {stats['prop_alignments']} property "
        f"external alignments · {stats['edges']} edges. Drag nodes, scroll to zoom."
        "</div>"
    )
    return info + _iframe(full), str(html_path)
