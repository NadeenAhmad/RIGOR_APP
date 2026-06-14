import sys
import time
import requests
from rdflib import Graph, Namespace, URIRef, BNode, Literal
from rdflib.namespace import RDF, RDFS, OWL
import os
import re

# [FIX 1] Import LLM utilities used by the other repair scripts
from api_utils import send_prompt, extract_between_markers

# =========================
# CONFIGURATION
# =========================
# Default input is overridable via OOPS_INPUT_TTL (or a CLI arg in __main__),
# so the script no longer hard-fails on a machine-specific path.
ONTOLOGY_TTL_PATH = os.environ.get(
    "OOPS_INPUT_TTL",
    "/Users/nadeen/neon-gpt/neon-gpt-extended-main/results/mistral-ontologies/sewerNetMistral_syntax_logic_consist_fixed.ttl",
)

OOPS_ENDPOINT = os.environ.get("OOPS_ENDPOINT", "https://oops.linkeddata.es/rest")
OUTPUT_FORMAT = "TURTLE"
PITFALLS = ""

MAX_AFFECTED_TO_SHOW = 25
TRIPLES_PER_ELEMENT_LIMIT = 250

# [FIX 2] Add repair iteration limit (was entirely absent)
MAX_REPAIR_ITERATIONS = 25

# Importance levels OOPS! assigns to pitfalls. By default only "critical"
# pitfalls are repaired (the original behaviour). "important" can be enabled
# by callers; "minor" is intentionally never repaired automatically.
DEFAULT_IMPORTANCE_LEVELS = ("critical",)

# Structural-preservation guard for the safe-apply step: reject any single
# proposed repair that would delete more existing triples than this (a sane LLM
# fix for one pitfall touches a handful of triples; a large delete set signals a
# bad patch that would erode the ontology rather than repair it).
MAX_DELETED_TRIPLES_PER_PATCH = 50

OUTFILE = os.path.splitext(os.path.basename(ONTOLOGY_TTL_PATH))[0] + "_oops_snippets.txt"

# [FIX 3] Define output ontology path (was entirely absent)
REPAIRED_TTL = os.path.splitext(ONTOLOGY_TTL_PATH)[0] + "_oops_fixed.ttl"

OOPS = Namespace("http://oops.linkeddata.es/def#")
URI_RE = re.compile(r"https?://[^\s<>\"]+")

# Vocabularies used by deterministic ontology-metadata repairs.
DCTERMS = Namespace("http://purl.org/dc/terms/")
DC = Namespace("http://purl.org/dc/elements/1.1/")
CC = Namespace("http://creativecommons.org/ns#")
SCHEMA = Namespace("http://schema.org/")

# Default license added for P41 ("No license declared"). Overridable via the
# RIGOR_DEFAULT_LICENSE env var or the license_uri argument on
# fix_pitfalls()/repair_loop(). CC-BY 4.0 is a common, permissive choice for
# published ontologies; change it if your ontology needs a different license.
DEFAULT_LICENSE_URI = os.environ.get(
    "RIGOR_DEFAULT_LICENSE", "https://creativecommons.org/licenses/by/4.0/"
)

# Predicates that, if already present on the ontology node, mean a license is
# declared — so we never add a second/duplicate one.
_LICENSE_PREDICATES = (
    DCTERMS.license, CC.license, SCHEMA.license, DCTERMS.rights, DC.rights,
)

# Ontology-level pitfalls with a single, canonical fix. These are repaired
# deterministically (no LLM) because the LLM path is unreliable for metadata
# declarations and tends to loop without ever satisfying OOPS. Currently:
#   P41 — no license declared.
DETERMINISTIC_METADATA_PITFALLS = {"P41"}


# =========================
# OOPS request helpers
# =========================
def build_request_xml(rdfxml: str) -> str:
    safe = rdfxml.replace("]]>", "]]]]><![CDATA[>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OOPSRequest>
  <OntologyUrl></OntologyUrl>
  <OntologyContent><![CDATA[{safe}]]></OntologyContent>
  <Pitfalls>{PITFALLS}</Pitfalls>
  <OutputFormat>{OUTPUT_FORMAT}</OutputFormat>
</OOPSRequest>
"""


def call_oops(xml_request: str, max_retries: int = 4, backoff: int = 3) -> str:
    headers = {
        "Content-Type": "application/xml",
        "Accept": "text/turtle, application/rdf+xml;q=0.9, text/plain;q=0.8, */*;q=0.1",
    }
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(
                OOPS_ENDPOINT,
                data=xml_request.encode("utf-8"),
                headers=headers,
                timeout=280,
            )
        except requests.RequestException as e:
            # Transport-level failure (connection reset, timeout, DNS) — retry.
            last_error = e
            if attempt < max_retries:
                wait = backoff * attempt
                print(f"⚠️ OOPS connection error (attempt {attempt}/{max_retries}): {e}; retrying in {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"OOPS request failed after {max_retries} attempts: {e}") from e

        if not r.ok:
            # 5xx is often transient (overload) — retry; 4xx is a real error — raise.
            if r.status_code >= 500 and attempt < max_retries:
                wait = backoff * attempt
                print(f"⚠️ OOPS HTTP {r.status_code} (attempt {attempt}/{max_retries}); retrying in {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"OOPS HTTP {r.status_code}: {r.text[:2000]}")
        return r.text

    raise RuntimeError(f"OOPS request failed: {last_error}")


def parse_oops_graph(response_text: str) -> Graph:
    g = Graph()
    try:
        g.parse(data=response_text, format="turtle")
        return g
    except Exception:
        g = Graph()
        g.parse(data=response_text, format="xml")
        return g


def first_literal(g: Graph, s, p) -> str:
    for o in g.objects(s, p):
        return str(o)
    return ""


# =========================
# Affected elements extraction
# =========================
def get_affected_elements(oops_graph: Graph, pitfall_node):
    elems = []

    def add_from_value(v):
        if isinstance(v, URIRef):
            elems.append(v)
            return
        if isinstance(v, Literal):
            txt = str(v)
            for u in URI_RE.findall(txt):
                elems.append(URIRef(u))
            return
        if isinstance(v, BNode):
            for _, _, o2 in oops_graph.triples((v, None, None)):
                add_from_value(o2)
            return

    for o in oops_graph.objects(pitfall_node, OOPS.hasAffectedElement):
        add_from_value(o)

    for o in oops_graph.objects(pitfall_node, OOPS.hasDescription):
        if isinstance(o, Literal):
            txt = str(o)
            for u in URI_RE.findall(txt):
                elems.append(URIRef(u))

    for _, _, o in oops_graph.triples((pitfall_node, None, None)):
        if isinstance(o, Literal):
            txt = str(o)
            for u in URI_RE.findall(txt):
                elems.append(URIRef(u))

    seen = set()
    uniq = []
    for e in elems:
        if e not in seen:
            seen.add(e)
            uniq.append(e)

    return uniq


# =========================
# Validation helpers for P05, P31, P39
# =========================
def validate_inverse_relationship(ontology_graph: Graph, prop: URIRef) -> tuple[bool, str]:
    """
    Check if an inverse relationship is correctly defined.
    Returns (is_problematic, reason).
    """
    inverses = list(ontology_graph.objects(prop, OWL.inverseOf))

    if not inverses:
        return False, ""

    for inverse_prop in inverses:
        if not isinstance(inverse_prop, URIRef):
            return True, "Inverse is not a proper URI"

        # Self-inverse without SymmetricProperty
        if inverse_prop == prop:
            is_symmetric = (prop, RDF.type, OWL.SymmetricProperty) in ontology_graph
            if not is_symmetric:
                return True, "Property is inverse of itself but not declared symmetric"

        # [FIX 4] Removed non-reciprocation check — OWL semantics entail symmetry
        # of owl:inverseOf automatically; flagging it was a false positive.

        # Transitive property with an inverse (logical conflict in OWL2-DL)
        if (prop, RDF.type, OWL.TransitiveProperty) in ontology_graph:
            return True, "Transitive property has an inverse (non-simple property conflict)"

        # Domain/range compatibility
        prop_domains = set(ontology_graph.objects(prop, RDFS.domain))
        prop_ranges = set(ontology_graph.objects(prop, RDFS.range))
        inv_domains = set(ontology_graph.objects(inverse_prop, RDFS.domain))
        inv_ranges = set(ontology_graph.objects(inverse_prop, RDFS.range))

        if prop_domains and inv_ranges and prop_domains != inv_ranges:
            return True, "Domain of property does not match range of its declared inverse"
        if prop_ranges and inv_domains and prop_ranges != inv_domains:
            return True, "Range of property does not match domain of its declared inverse"

    return False, ""


def validate_equivalent_class(ontology_graph: Graph, cls: URIRef) -> tuple[bool, str]:
    equivalents = list(ontology_graph.objects(cls, OWL.equivalentClass))

    if not equivalents:
        return False, ""

    for equiv_cls in equivalents:
        if not isinstance(equiv_cls, URIRef):
            continue

        if equiv_cls == cls:
            return True, "Class declared equivalent to itself (redundant)"

        if (cls, RDFS.subClassOf, equiv_cls) in ontology_graph:
            return True, "Class is both subclass and equivalent to the same class (contradiction)"
        if (equiv_cls, RDFS.subClassOf, cls) in ontology_graph:
            return True, "Class is both superclass and equivalent to the same class (contradiction)"

    return False, ""


def extract_namespace_from_uri(uri: str) -> str:
    if '#' in uri:
        return uri.rsplit('#', 1)[0] + '#'
    elif '/' in uri:
        return uri.rsplit('/', 1)[0] + '/'
    return uri


def detect_ambiguous_namespaces(ontology_graph: Graph) -> list:
    """
    Detect P39: Ambiguous namespace issues.
    Returns list of problematic namespace strings.
    """
    from urllib.parse import urlparse

    # Collect only ontology-local URIs (exclude standard vocabularies)
    STANDARD_PREFIXES = {
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/2001/XMLSchema#",
    }

    all_uris = set()
    for s, p, o in ontology_graph:
        for term in (s, p, o):
            if isinstance(term, URIRef):
                uri_str = str(term)
                ns = extract_namespace_from_uri(uri_str)
                if ns not in STANDARD_PREFIXES:
                    all_uris.add(uri_str)

    domain_groups = {}
    for uri in all_uris:
        try:
            parsed = urlparse(uri)
            domain = parsed.netloc
            if domain:
                if domain not in domain_groups:
                    domain_groups[domain] = []
                domain_groups[domain].append(uri)
        except Exception:
            continue

    affected = []
    for domain, uris in domain_groups.items():
        namespaces = set()
        for uri in uris:
            ns = extract_namespace_from_uri(uri)
            namespaces.add(ns)

        if len(namespaces) > 1:
            patterns = set()
            for ns in namespaces:
                base = ns.rstrip('/#')
                patterns.add(base)
            if len(patterns) == 1 and len(namespaces) > 1:
                affected.extend(namespaces)

    namespace_list = list(set(extract_namespace_from_uri(uri) for uri in all_uris))
    for i, ns1 in enumerate(namespace_list):
        for ns2 in namespace_list[i + 1:]:
            if ns1.rstrip('/#') == ns2.rstrip('/#'):
                if ns1 not in affected:
                    affected.append(ns1)
                if ns2 not in affected:
                    affected.append(ns2)

    return list(set(affected))


def infer_affected_elements_by_pitfall(ontology_graph: Graph, pitfall_code: str):
    affected = []
    reasons_by_elem = {}

    if pitfall_code == "P05":
        all_props_with_inverses = set()
        for s in ontology_graph.subjects(OWL.inverseOf, None):
            if isinstance(s, URIRef):
                all_props_with_inverses.add(s)
        for o in ontology_graph.objects(None, OWL.inverseOf):
            if isinstance(o, URIRef):
                all_props_with_inverses.add(o)

        for prop in all_props_with_inverses:
            is_problematic, reason = validate_inverse_relationship(ontology_graph, prop)
            if is_problematic:
                affected.append(prop)
                reasons_by_elem[prop] = reason

    elif pitfall_code == "P31":
        all_classes_with_equiv = set()
        for s in ontology_graph.subjects(OWL.equivalentClass, None):
            if isinstance(s, URIRef):
                all_classes_with_equiv.add(s)
        for o in ontology_graph.objects(None, OWL.equivalentClass):
            if isinstance(o, URIRef):
                all_classes_with_equiv.add(o)

        for cls in all_classes_with_equiv:
            is_problematic, reason = validate_equivalent_class(ontology_graph, cls)
            if is_problematic:
                affected.append(cls)
                reasons_by_elem[cls] = reason

    elif pitfall_code == "P39":
        # Returns strings, not URIRefs — handled separately in repair
        affected = detect_ambiguous_namespaces(ontology_graph)

    seen = set()
    uniq = []
    for a in affected:
        if a not in seen:
            seen.add(a)
            uniq.append(a)

    return uniq, reasons_by_elem


# =========================
# Snippet extraction
# =========================
def add_resource_neighborhood(src: Graph, out: Graph, node, max_triples: int):
    count = 0

    def is_empty_rdf_list(n) -> bool:
        return isinstance(n, BNode) and \
               (not any(src.triples((n, RDF.first, None)))) and \
               (not any(src.triples((n, RDF.rest, None))))

    def skip_object(p, o) -> bool:
        if isinstance(o, BNode) and not any(src.triples((o, None, None))):
            return True
        if p == OWL.unionOf and is_empty_rdf_list(o):
            return True
        if is_empty_rdf_list(o):
            return True
        return False

    for s, p, o in src.triples((node, None, None)):
        if skip_object(p, o):
            continue
        out.add((s, p, o))
        count += 1
        if count >= max_triples:
            return
        if isinstance(o, BNode):
            bnode_triples = list(src.triples((o, None, None)))
            if not bnode_triples:
                continue
            for s2, p2, o2 in bnode_triples:
                if skip_object(p2, o2):
                    continue
                out.add((s2, p2, o2))
                count += 1
                if count >= max_triples:
                    return

    allowed_incoming = {
        RDF.type, RDFS.subClassOf, RDFS.domain, RDFS.range,
        OWL.inverseOf, OWL.disjointWith, OWL.equivalentClass, OWL.equivalentProperty
    }
    for s, p, o in src.triples((None, None, node)):
        if p not in allowed_incoming:
            continue
        if isinstance(s, BNode):
            s_triples = list(src.triples((s, None, None)))
            if len(s_triples) == 1 and s_triples[0] == (s, p, o):
                continue
        out.add((s, p, o))
        count += 1
        if count >= max_triples:
            return


def build_snippet_for_affected(src: Graph, affected: list) -> Graph:
    """
    Build a subgraph around affected elements.
    Accepts URIRef objects only; plain strings are skipped.
    """
    out = Graph()
    out.namespace_manager = src.namespace_manager
    for node in affected[:MAX_AFFECTED_TO_SHOW]:
        # [FIX 5] Guard: skip plain strings (e.g., from P39 namespace detection)
        if not isinstance(node, URIRef):
            continue
        add_resource_neighborhood(src, out, node, TRIPLES_PER_ELEMENT_LIMIT)
    return out


# =========================
# LLM repair helpers  [FIX 6 — entirely new section]
# =========================
def llm_repair_pitfall(
    pitfall_code: str,
    pitfall_description: str,
    pitfall_suggestion: str,
    context_ttl: str,
) -> tuple[str, str]:
    """
    Ask the LLM to propose a Turtle repair for a detected pitfall.

    [FIX 8] Returns a (add_ttl, delete_ttl) pair:
      - add_ttl    : new/corrected triples to ADD (between the turtle markers).
      - delete_ttl : exact existing triples to REMOVE (between the delete
                     markers), used only for "wrong-axiom" pitfalls such as P05
                     (wrong inverse) or P31 (wrong equivalence). Empty when the
                     fix is purely additive (e.g. P10 missing disjointness, P11
                     missing domain/range), which keeps the original behaviour.
    Either element may be an empty string if the LLM proposes nothing for it.
    """
    prompt = f"""You are an expert OWL ontology engineer.
The OOPS! Pitfall Scanner has detected the following critical pitfall in the ontology:

Pitfall code: {pitfall_code}
Description: {pitfall_description}
Suggestion: {pitfall_suggestion}

Relevant ontology fragment (Turtle):
{context_ttl}

Your task:
- Propose a minimal repair that resolves this pitfall.
- Put NEW or CORRECTED triples (the ones to ADD) between the turtle markers.
- ONLY IF an existing triple is wrong and must be removed for the repair to be
  correct (e.g. a wrong owl:inverseOf or owl:equivalentClass), put those exact
  triples to REMOVE between the delete markers. Leave the delete section empty
  when nothing needs to be removed. Do not delete unrelated triples.
- Do NOT repeat unmodified triples in either section.
- Both sections must be syntactically valid Turtle; include any @prefix lines
  they rely on.
- Place each section strictly between its markers below.

###start_turtle###
###end_turtle###

###start_delete###
###end_delete###
"""
    reply = send_prompt(
        prompt,
        persona="OWL ontology repair expert",
        step_name=f"oops_repair_{pitfall_code}",
    )
    added = extract_between_markers(reply, "###start_turtle###", "###end_turtle###")
    removed = extract_between_markers(reply, "###start_delete###", "###end_delete###")
    add_ttl = added[0].strip() if (added and added[0].strip()) else ""
    del_ttl = removed[0].strip() if (removed and removed[0].strip()) else ""
    return add_ttl, del_ttl


def apply_turtle_patch(ontology_graph: Graph, patch_ttl: str, delete_ttl: str = "") -> Graph:
    """
    Merge an LLM-proposed Turtle repair into the ontology graph.

    [FIX 8] Triples in `patch_ttl` are ADDED. Triples in `delete_ttl` (optional)
    are REMOVED first — and only those *exact* triples, so multi-valued axioms
    (e.g. several rdfs:subClassOf on one class) are never clobbered. When the
    LLM proposes no deletions this is purely additive, matching the original
    behaviour. Both blocks are parsed with the ontology's own prefix bindings
    prepended, so prefixed names resolve to the same IRIs as the graph and a
    prefix the LLM forgot to declare doesn't silently drop the whole repair.
    """
    # Ontology's prefix bindings, prepended to LLM output before parsing.
    # Re-declaring a prefix the block also declares is legal Turtle (last wins).
    prefix_header = "".join(
        f"@prefix {p}: <{n}> .\n" for p, n in ontology_graph.namespaces()
    )

    # --- Optional removals: only the exact triples the LLM enumerated ---
    if delete_ttl and delete_ttl.strip():
        del_graph = Graph()
        try:
            del_graph.parse(data=prefix_header + delete_ttl, format="turtle")
            removed_count = 0
            for triple in del_graph:
                if triple in ontology_graph:
                    ontology_graph.remove(triple)
                    removed_count += 1
            print(f"  ✓ Removed {removed_count} triple(s) per LLM delete block.")
        except Exception as e:
            print(f"  ⚠️  Delete-block Turtle failed to parse, skipping removals: {e}")

    # --- Additions (additive merge) ---
    patch_graph = Graph()
    try:
        patch_graph.parse(data=prefix_header + patch_ttl, format="turtle")
    except Exception as e:
        print(f"  ⚠️  Patch Turtle failed to parse, skipping: {e}")
        return ontology_graph

    for triple in patch_graph:
        ontology_graph.add(triple)

    print(f"  ✓ Applied patch: {len(patch_graph)} triples merged into ontology.")
    return ontology_graph


# =========================
# Safe application of a proposed repair
# =========================
def _clone_graph(src: Graph) -> Graph:
    """A shallow triple-level copy that preserves prefix bindings."""
    g = Graph()
    g.namespace_manager = src.namespace_manager
    for t in src:
        g.add(t)
    return g


def _is_wellformed(graph: Graph) -> tuple[bool, str]:
    """Round-trip the graph through Turtle to confirm it stays parseable
    (guards requirement: a repair must not introduce syntax errors)."""
    try:
        text = graph.serialize(format="turtle")
        probe = Graph()
        probe.parse(data=text, format="turtle")
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _is_consistent(graph: Graph, *, java_exe: str, robot_jar) -> tuple[str, str]:
    """
    Best-effort logical-consistency probe using the same ROBOT/HermiT reasoner
    the consistency stage uses. Returns (status, diagnostics) with status in
    {"coherent", "incoherent", "error", "unavailable"}. "unavailable" means the
    check could not be run (no Java/jar) — the caller treats that as "no
    evidence of inconsistency" and degrades gracefully, matching the rest of the
    app. Only a definitive "incoherent" blocks a repair.
    """
    if not robot_jar:
        return "unavailable", "no ROBOT jar provided"
    try:
        import validate_fix_ontology_consistency as consist_mod
        from pathlib import Path as _Path
    except Exception as e:  # noqa: BLE001
        return "unavailable", f"consistency module unavailable: {e}"

    if not consist_mod._java_available(java_exe):
        return "unavailable", "java not available"
    robot_path = _Path(robot_jar)
    if not robot_path.exists():
        return "unavailable", f"robot jar not found: {robot_jar}"

    tmp = tempfile.NamedTemporaryFile(suffix=".ttl", delete=False)
    tmp_path = _Path(tmp.name)
    tmp.close()
    try:
        graph.serialize(destination=str(tmp_path), format="turtle")
        status, diag = consist_mod.robot_reason_status(
            tmp_path, robot_path, reasoner="HermiT")
        return status, diag
    except Exception as e:  # noqa: BLE001
        return "error", str(e)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def safe_apply_turtle_patch(
    ontology_graph: Graph,
    patch_ttl: str,
    delete_ttl: str = "",
    *,
    label: str = "patch",
    verify_consistency: bool = False,
    java_exe: str = "java",
    robot_jar=None,
) -> Graph:
    """
    Apply an LLM-proposed repair *only if* it is safe, otherwise return the
    original graph unchanged. A repair is committed only when it:

      1. keeps the ontology syntactically well-formed (Turtle round-trip);
      2. does not make the ontology logically inconsistent (checked with
         ROBOT/HermiT when available; skipped gracefully otherwise — a missing
         reasoner never blocks a repair, but a confirmed inconsistency does);
      3. preserves existing structure — it never removes more than
         MAX_DELETED_TRIPLES_PER_PATCH triples, and never removes a triple that
         was not part of the explicit delete block.

    The trial runs on a clone, so a rejected repair leaves the working graph
    untouched. This is the single safe entry point used for both critical and
    important pitfalls, so both get identical guarantees.
    """
    if not (patch_ttl or delete_ttl):
        return ontology_graph

    original_count = len(ontology_graph)
    trial = _clone_graph(ontology_graph)
    trial = apply_turtle_patch(trial, patch_ttl, delete_ttl)

    # (3) Structural preservation: bound deletions and forbid collateral loss.
    removed = original_count - sum(1 for t in trial if t in ontology_graph)
    if removed > MAX_DELETED_TRIPLES_PER_PATCH:
        print(f"  ⛔ {label}: repair would delete {removed} triples "
              f"(> {MAX_DELETED_TRIPLES_PER_PATCH}); rejected to preserve structure.")
        return ontology_graph

    # (1) Syntax: the patched ontology must still parse.
    ok, err = _is_wellformed(trial)
    if not ok:
        print(f"  ⛔ {label}: repair would introduce a syntax error ({err}); rejected.")
        return ontology_graph

    # (2) Logical consistency: block only a confirmed inconsistency.
    if verify_consistency:
        status, diag = _is_consistent(trial, java_exe=java_exe, robot_jar=robot_jar)
        if status == "incoherent":
            print(f"  ⛔ {label}: repair would make the ontology inconsistent; rejected.")
            return ontology_graph
        if status in {"unavailable", "error"}:
            print(f"  ℹ️  {label}: consistency not verified ({diag[:120]}); "
                  "accepting on syntax + structure checks only.")

    print(f"  ✅ {label}: repair verified safe; committing.")
    return trial


# =========================
# Response validation  [FIX 9]
# =========================
def _is_valid_oops_response(oops_graph: Graph) -> bool:
    """
    A genuine OOPS! result always contains a resource typed
    oops:response (http://oops.linkeddata.es/def#response). Error responses use
    a different namespace and a non-OOPS body won't contain it at all. Checking
    for it lets us distinguish "ontology is clean (zero pitfalls)" from "the
    service returned an error / something unparseable" — without it, a server
    error would be silently treated as a clean ontology.
    """
    return (None, RDF.type, OOPS.response) in oops_graph


def get_pitfalls_by_importance(oops_graph: Graph, levels) -> list:
    """
    Return (pitfall_node, level) pairs from an OOPS response graph whose
    importance level is in `levels` (case-insensitive). Generalises
    get_critical_pitfalls so the same repair machinery can also process
    "important" pitfalls when a caller opts in.
    """
    wanted = {str(l).strip().lower() for l in levels}
    selected = []
    for node in oops_graph.subjects(RDF.type, OOPS.pitfall):
        level = first_literal(oops_graph, node, OOPS.hasImportanceLevel).strip().lower()
        if level in wanted:
            selected.append((node, level))
    return selected


def get_critical_pitfalls(oops_graph: Graph) -> list:
    """
    Return all pitfall nodes from an OOPS response graph that are Critical.
    Kept for backward compatibility; thin wrapper over get_pitfalls_by_importance.
    """
    return [node for node, _ in get_pitfalls_by_importance(oops_graph, ("critical",))]


def run_oops(ontology_graph: Graph) -> Graph:
    """
    Serialise ontology, send to OOPS!, return parsed OOPS response graph.
    """
    rdfxml = ontology_graph.serialize(format="xml")
    xml_request = build_request_xml(rdfxml)
    response_text = call_oops(xml_request)
    oops_graph = parse_oops_graph(response_text)
    # [FIX 9] Guard: don't mistake an error / unrecognised body for a clean run.
    if not _is_valid_oops_response(oops_graph):
        raise RuntimeError(
            "OOPS returned a response that is not a recognised OOPS result "
            "(no oops:response resource found). Treating this as a service "
            "error rather than a clean ontology. First 500 chars of response:\n"
            + response_text[:500]
        )
    return oops_graph


# =========================
# Diagnostic output helper
# =========================
def write_block(f, title: str, text: str):
    f.write("\n" + "=" * 120 + "\n")
    f.write(title + "\n")
    f.write("=" * 120 + "\n\n")
    f.write(text)
    if not text.endswith("\n"):
        f.write("\n")


# =========================
# Main repair loop  [FIX 7 — entirely rewritten]
# =========================
def _resolve_ontology_node(graph: Graph) -> tuple[URIRef | None, bool]:
    """
    Return (ontology_iri, minted) — the owl:Ontology node the license should
    attach to. If the graph already declares one, use it (minted=False). If it
    does not, derive a base IRI from the ontology's own namespace and mint one
    (minted=True). Returns (None, False) only when no usable IRI can be found.
    """
    existing = sorted(
        (s for s in graph.subjects(RDF.type, OWL.Ontology) if isinstance(s, URIRef)),
        key=str,
    )
    if existing:
        return existing[0], False

    # No owl:Ontology declaration: derive a base IRI from the most common
    # namespace among declared classes, then fall back to any URIRef subject.
    from collections import Counter
    counts: Counter = Counter()
    for c in graph.subjects(RDF.type, OWL.Class):
        if isinstance(c, URIRef):
            s = str(c)
            ns = s.rsplit("#", 1)[0] + "#" if "#" in s else s.rsplit("/", 1)[0] + "/"
            counts[ns] += 1
    base_ns = counts.most_common(1)[0][0] if counts else None
    if base_ns is None:
        for subj in graph.subjects():
            if isinstance(subj, URIRef) and str(subj).startswith(("http://", "https://")):
                s = str(subj)
                base_ns = s.rsplit("#", 1)[0] + "#" if "#" in s else s.rsplit("/", 1)[0] + "/"
                break
    if base_ns is None:
        return None, False

    ont_iri = URIRef(base_ns[:-1]) if base_ns.endswith(("#", "/")) else URIRef(base_ns)
    return ont_iri, True


def build_metadata_patch(
    graph: Graph, code: str, license_uri: str | None = None
) -> tuple[str, str]:
    """
    Build a deterministic (add_ttl, delete_ttl) repair for an ontology-metadata
    pitfall. Returns ("", "") when nothing needs to change (idempotent), so a
    second pass over an already-fixed ontology is a no-op.

    Currently handles:
      P41 — no license declared: ensure the owl:Ontology node carries a
            dcterms:license pointing at `license_uri` (URI, not a literal),
            minting the owl:Ontology declaration first if the graph lacks one.
    """
    if code != "P41":
        return "", ""

    license_uri = (license_uri or DEFAULT_LICENSE_URI).strip()
    ont, minted = _resolve_ontology_node(graph)
    if ont is None:
        print("  ⚠️  P41: could not determine an ontology IRI to attach a license to.")
        return "", ""

    # Idempotency: if any recognised license predicate is already on the node,
    # leave it alone.
    for pred in _LICENSE_PREDICATES:
        if (ont, pred, None) in graph:
            return "", ""

    patch = Graph()
    patch.bind("dcterms", DCTERMS)
    patch.bind("owl", OWL)
    if minted:
        patch.add((ont, RDF.type, OWL.Ontology))
        print(f"  ℹ️  P41: no owl:Ontology node found; minting <{ont}>.")
    patch.add((ont, DCTERMS.license, URIRef(license_uri)))
    return patch.serialize(format="turtle"), ""


def repair_loop(ontology_graph: Graph, outfile: str = None, source_label: str = None,
                max_iterations: int = None, progress=None,
                importance_levels=DEFAULT_IMPORTANCE_LEVELS,
                verify_consistency: bool = False, java_exe: str = "java",
                robot_jar=None, license_uri: str = None) -> Graph:
    """
    Iteratively detect and repair OOPS! pitfalls up to max_iterations times.
    Returns the repaired graph.

    By default only "critical" pitfalls are repaired (the original behaviour).
    Pass importance_levels=("critical", "important") to also repair important
    pitfalls — they flow through the exact same detect -> LLM-repair -> safe-apply
    path, so they inherit the same safety guarantees (no syntax errors, no
    introduced inconsistency, structure preserved). `verify_consistency` +
    `robot_jar`/`java_exe` enable the reasoner-backed consistency check.
    """
    outfile = outfile or OUTFILE
    source_label = source_label or ONTOLOGY_TTL_PATH
    max_iterations = MAX_REPAIR_ITERATIONS if max_iterations is None else max_iterations
    levels = tuple(str(l).strip().lower() for l in importance_levels) or ("critical",)

    def emit(msg: str) -> None:
        print(msg, flush=True)
        if progress is not None:
            try:
                progress(msg)
            except Exception:
                pass

    def apply_safely(graph: Graph, add_ttl: str, rm_ttl: str, code: str) -> Graph:
        return safe_apply_turtle_patch(
            graph, add_ttl, rm_ttl, label=code,
            verify_consistency=verify_consistency,
            java_exe=java_exe, robot_jar=robot_jar,
        )

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(f"Ontology: {source_label}\n")
        f.write(f"OOPS endpoint: {OOPS_ENDPOINT}\n")
        f.write(f"NOTE: repairing importance level(s): {', '.join(levels)}. "
                "Other levels are ignored.\n\n")

    for iteration in range(1, max_iterations + 1):
        print(f"\n{'='*60}")
        emit(f"OOPS repair iteration {iteration}/{max_iterations}")
        print(f"{'='*60}")

        oops_graph = run_oops(ontology_graph)
        pitfalls = get_pitfalls_by_importance(oops_graph, levels)
        level_by_node = dict(pitfalls)

        if not pitfalls:
            print(f"✅ No {'/'.join(levels)} pitfalls detected. Repair complete.")
            break

        print(f"⚠️  {len(pitfalls)} pitfall(s) detected at level(s) {', '.join(levels)}.")

        for node in sorted(level_by_node, key=str):
            code        = first_literal(oops_graph, node, OOPS.hasCode).strip()
            # OOPS versions differ: older returns "P10"; newer returns
            # "P10 - Missing disjoint-ness". Normalise to the bare code so the
            # equality checks below work against both.
            _m = re.match(r"\s*(P\d+)", code)
            if _m:
                code = _m.group(1)
            name        = first_literal(oops_graph, node, OOPS.hasName).strip()
            desc        = first_literal(oops_graph, node, OOPS.hasDescription).strip()
            sugg        = first_literal(oops_graph, node, OOPS.hasSuggestion).strip()
            affected_n  = first_literal(oops_graph, node, OOPS.hasNumberAffectedElements).strip()
            importance  = level_by_node.get(node, "critical")

            print(f"\n  Pitfall: {code} — {name} [{importance}]")
            if affected_n:
                print(f"  Affected elements: {affected_n}")

            # Log to diagnostic file
            with open(outfile, "a", encoding="utf-8") as f:
                write_block(f, f"ITERATION {iteration} | PITFALL {code}: {name}",
                            f"importance: {importance}\ndescription: {desc}\nsuggestion: {sugg}\n")

            # ── Deterministic metadata repairs (no LLM) ──────────────────
            # Pitfalls like P41 (no license) have one canonical fix. Routing
            # them through the LLM is unreliable and tends to loop without ever
            # satisfying OOPS, so build the patch deterministically and run it
            # through the same safe-apply guard. Idempotent: once the metadata
            # is present, OOPS stops reporting it and the loop converges.
            if code in DETERMINISTIC_METADATA_PITFALLS:
                add_ttl, del_ttl = build_metadata_patch(ontology_graph, code, license_uri)
                if add_ttl or del_ttl:
                    before = len(ontology_graph)
                    ontology_graph = apply_safely(ontology_graph, add_ttl, del_ttl, code)
                    if len(ontology_graph) != before or del_ttl:
                        emit(f"  ✓ {code}: applied deterministic metadata fix.")
                    with open(outfile, "a", encoding="utf-8") as f:
                        write_block(f, f"Iteration {iteration} | {code} DETERMINISTIC PATCH",
                                    add_ttl or "(no additions)")
                else:
                    print(f"  ℹ️  {code}: metadata already present; nothing to add.")
                continue

            # ── Extract affected elements ────────────────────────────────
            affected = get_affected_elements(oops_graph, node)

            PROBLEMATIC_CRITICAL_PITFALLS = ["P05", "P31", "P39"]
            reasons_by_elem = {}

            if not affected and code in PROBLEMATIC_CRITICAL_PITFALLS:
                # These critical codes carry their fix in local structure even
                # when OOPS lists no elements; infer them. If inference also
                # finds nothing, there is genuinely nothing to repair.
                print(f"  ⚠️  No affected elements from OOPS for {code}. Inferring locally...")
                affected, reasons_by_elem = infer_affected_elements_by_pitfall(
                    ontology_graph, code)
                if affected:
                    print(f"  ✓ Inferred {len(affected)} affected element(s).")
                else:
                    print(f"  ✗ Could not infer affected elements for {code}. Skipping.")
                    continue
            elif affected:
                print(f"  ✓ {len(affected)} affected element(s) from OOPS.")

            # ── Build repair context ─────────────────────────────────────
            if not affected:
                # Ontology-level (generic) pitfall: OOPS reports it against the
                # ontology as a whole rather than against specific elements —
                # e.g. P10 (missing disjointness) or P41 (no license declared).
                # Instead of skipping, give the LLM the full ontology so it can
                # add the missing ontology-wide axiom/annotation; safe-apply
                # still guards syntax, consistency, and structure.
                print(f"  Ontology-level pitfall {code} (no affected elements); "
                      "providing full ontology to LLM.")
                context_ttl = ontology_graph.serialize(format="turtle")
            elif code == "P39":
                # [FIX 5] P39: affected elements are namespace strings, not URIRefs.
                # Build context from description only; snippet generation is skipped.
                context_ttl = f"# Ambiguous namespaces detected:\n"
                context_ttl += "\n".join(f"# {ns}" for ns in affected[:MAX_AFFECTED_TO_SHOW])
                context_ttl += "\n\n"
                context_ttl += ontology_graph.serialize(format="turtle")
            else:
                snippet = build_snippet_for_affected(ontology_graph, affected)
                context_ttl = snippet.serialize(format="turtle")

                with open(outfile, "a", encoding="utf-8") as f:
                    write_block(f,
                                f"Iteration {iteration} | {code} SNIPPET",
                                context_ttl)

                if reasons_by_elem:
                    with open(outfile, "a", encoding="utf-8") as f:
                        for elem, reason in reasons_by_elem.items():
                            write_block(f, f"Reason for {elem}", reason + "\n")

            # ── Call LLM for repair ──────────────────────────────────────
            patch_ttl, del_ttl = llm_repair_pitfall(code, desc, sugg, context_ttl)
            if patch_ttl or del_ttl:
                ontology_graph = apply_safely(ontology_graph, patch_ttl, del_ttl, code)
                with open(outfile, "a", encoding="utf-8") as f:
                    logged = (patch_ttl or "")
                    if del_ttl:
                        logged += "\n# --- triples removed ---\n" + del_ttl
                    write_block(f,
                                f"Iteration {iteration} | {code} LLM PATCH",
                                logged)
            else:
                print(f"  ⚠️  LLM returned no patch for {code}.")

        time.sleep(2)   # brief pause between iterations to respect OOPS rate limits

    return ontology_graph


# =========================
# Importable wrapper (used by the app's post-processing orchestrator)
# =========================
def fix_pitfalls(ontology_graph: Graph, model: str = None, max_iterations: int = 25,
                 outfile: str = None, source_label: str = "ontology", progress=None,
                 importance_levels=DEFAULT_IMPORTANCE_LEVELS,
                 verify_consistency: bool = False, java_exe: str = "java",
                 robot_jar=None, license_uri: str = None) -> Graph:
    """
    Run the OOPS! pitfall detect/repair loop on an in-memory graph and return
    the repaired graph. Requires network access to the OOPS! REST API and a
    configured OpenRouter key (via api_utils).

    By default only "critical" pitfalls are repaired. Pass
    importance_levels=("critical", "important") to also repair important
    pitfalls via the same safe machinery. When `verify_consistency` is set and a
    `robot_jar`/`java_exe` are available, each proposed repair is additionally
    rejected if it would make the ontology logically inconsistent.
    """
    if model:
        try:
            import api_utils
            api_utils.set_model(model)
        except Exception:
            pass
    if outfile is None:
        import tempfile
        outfile = os.path.join(tempfile.gettempdir(), "oops_snippets.txt")
    return repair_loop(ontology_graph, outfile=outfile, source_label=source_label,
                       max_iterations=max_iterations, progress=progress,
                       importance_levels=importance_levels,
                       verify_consistency=verify_consistency,
                       java_exe=java_exe, robot_jar=robot_jar,
                       license_uri=license_uri)


# =========================
# Entry point
# =========================
if __name__ == "__main__":
    # [FIX 10] Accept an optional CLI path (falls back to OOPS_INPUT_TTL /
    # the configured default) and fail with a clear message rather than a raw
    # traceback when the file is missing.
    input_path = sys.argv[1] if len(sys.argv) > 1 else ONTOLOGY_TTL_PATH
    if not os.path.exists(input_path):
        print(f"❌ Input ontology not found: {input_path}")
        print("   Usage: python validate_fix_ontology_pitfall_final.py <input.ttl>")
        print("   (or set OOPS_INPUT_TTL). For app use, call fix_pitfalls() instead.")
        sys.exit(1)

    repaired_out = os.path.splitext(input_path)[0] + "_oops_fixed.ttl"

    print("🔍 Loading ontology (Turtle)...")
    ontology_graph = Graph()
    ontology_graph.parse(input_path, format="turtle")
    print(f"✅ Loaded {len(ontology_graph)} triples.")

    # [FIX 7] Run the full repair loop instead of detection-only reporting
    repaired_graph = repair_loop(ontology_graph, source_label=input_path)

    # [FIX 3] Save the repaired ontology
    repaired_graph.serialize(destination=repaired_out, format="turtle")
    print(f"\n✅ Repaired ontology saved to: {repaired_out}")
    print(f"✅ Diagnostic log saved to: {OUTFILE}")
