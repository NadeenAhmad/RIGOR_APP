#!/usr/bin/env python3
"""Safe OOPS!-guided repair for Critical and Important ontology pitfalls.

The module combines:
- deterministic repairs where one semantics-preserving fix is mechanically known;
- code-specific LLM assessment/repair with local, summary, or full context;
- strict pre-merge validation (RDF syntax, vocabulary, structural preservation,
  ROBOT/HermiT consistency, and fresh OOPS verification);
- focused review questions for decisions that can change domain meaning;
- compact JSON/Markdown reports for unresolved findings.

The public ``fix_pitfalls`` function remains backward compatible and returns a
Graph. New app code should call ``repair_pitfalls`` to also receive review items
and reports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from itertools import combinations
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal as TypingLiteral

import requests
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.util import guess_format

import api_utils
from api_utils import extract_between_markers

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OOPS_ENDPOINT = os.environ.get("OOPS_ENDPOINT", "https://oops.linkeddata.es/rest")
DEFAULT_IMPORTANCE_LEVELS = ("critical", "important")
DEFAULT_LICENSE_URI = os.environ.get(
    "RIGOR_DEFAULT_LICENSE", "https://creativecommons.org/licenses/by/4.0/"
)
MAX_REPAIR_ITERATIONS = 25
MAX_LLM_ATTEMPTS_PER_FINDING = 2
MAX_AFFECTED = 25
MAX_NEIGHBORHOOD_TRIPLES = 300
MAX_FULL_CONTEXT_CHARS = 120_000
MAX_SUMMARY_ITEMS = 80

OOPS = Namespace("http://oops.linkeddata.es/def#")
OOPS_OLD = Namespace("http://www.oeg-upm.net/oops#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
DC = Namespace("http://purl.org/dc/elements/1.1/")
CC = Namespace("http://creativecommons.org/ns#")
SCHEMA = Namespace("http://schema.org/")
PROV = Namespace("http://www.w3.org/ns/prov#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

_URI_RE = re.compile(r"(?:https?|urn|file):[^\s<>\"']+")

PROPERTY_TYPES = {
    RDF.Property,
    OWL.ObjectProperty,
    OWL.DatatypeProperty,
    OWL.AnnotationProperty,
}
LICENSE_PREDICATES = (
    DCTERMS.license,
    CC.license,
    SCHEMA.license,
    DCTERMS.rights,
    DC.rights,
)
STANDARD_NAMESPACES = (
    str(RDF), str(RDFS), str(OWL), str(XSD), str(PROV), str(SKOS),
    str(DCTERMS), str(DC), str(CC), str(SCHEMA),
)
ALLOWED_OWL_PREDICATES = {
    OWL.allValuesFrom, OWL.annotatedProperty, OWL.annotatedSource,
    OWL.annotatedTarget, OWL.assertionProperty, OWL.backwardCompatibleWith,
    OWL.cardinality, OWL.complementOf, OWL.datatypeComplementOf,
    OWL.deprecated, OWL.differentFrom, OWL.disjointUnionOf, OWL.disjointWith,
    OWL.distinctMembers, OWL.equivalentClass, OWL.equivalentProperty,
    OWL.hasKey, OWL.hasSelf, OWL.hasValue, OWL.imports, OWL.incompatibleWith,
    OWL.intersectionOf, OWL.inverseOf, OWL.maxCardinality,
    OWL.maxQualifiedCardinality, OWL.members, OWL.minCardinality,
    OWL.minQualifiedCardinality, OWL.onClass, OWL.onDataRange,
    OWL.onDatatype, OWL.oneOf, OWL.onProperties, OWL.onProperty,
    OWL.priorVersion, OWL.propertyChainAxiom, OWL.propertyDisjointWith,
    OWL.qualifiedCardinality, OWL.sameAs, OWL.someValuesFrom,
    OWL.sourceIndividual, OWL.targetIndividual, OWL.targetValue, OWL.unionOf,
    OWL.versionIRI, OWL.versionInfo, OWL.withRestrictions,
}

# ---------------------------------------------------------------------------
# Complete policy for Critical and Important catalogue entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PitfallPolicy:
    severity: str
    mode: TypingLiteral[
        "deterministic", "conditional", "llm", "semantic", "human_only"
    ]
    context: TypingLiteral["local", "summary_local", "full_or_summary"]
    auto_apply_llm: bool = False
    description: str = ""


PITFALL_POLICIES: dict[str, PitfallPolicy] = {
    # Critical
    "P01": PitfallPolicy("critical", "semantic", "summary_local", description="Polysemous element"),
    "P03": PitfallPolicy("critical", "semantic", "summary_local", description="Relationship 'is'"),
    "P05": PitfallPolicy("critical", "semantic", "summary_local", description="Wrong inverse relationship"),
    "P06": PitfallPolicy("critical", "conditional", "summary_local", description="Class hierarchy cycle"),
    "P14": PitfallPolicy("critical", "semantic", "summary_local", description="Misused allValuesFrom"),
    "P15": PitfallPolicy("critical", "semantic", "summary_local", description="some-not vs not-some"),
    "P16": PitfallPolicy("critical", "semantic", "summary_local", description="Primitive rather than defined class"),
    "P19": PitfallPolicy("critical", "conditional", "summary_local", auto_apply_llm=True, description="Multiple domains/ranges"),
    "P27": PitfallPolicy("critical", "semantic", "summary_local", description="Wrong equivalent properties"),
    "P28": PitfallPolicy("critical", "semantic", "summary_local", description="Wrong symmetric relationship"),
    "P29": PitfallPolicy("critical", "semantic", "summary_local", description="Wrong transitive relationship"),
    "P31": PitfallPolicy("critical", "semantic", "summary_local", description="Wrong equivalent classes"),
    "P37": PitfallPolicy("critical", "human_only", "full_or_summary", description="Ontology not published on Web"),
    "P39": PitfallPolicy("critical", "conditional", "full_or_summary", description="Ambiguous namespace"),
    "P40": PitfallPolicy("critical", "semantic", "full_or_summary", description="Namespace hijacking"),
    # Important
    "P10": PitfallPolicy("important", "semantic", "summary_local", description="Missing disjointness"),
    "P11": PitfallPolicy("important", "conditional", "summary_local", auto_apply_llm=True, description="Missing domain/range"),
    "P12": PitfallPolicy("important", "semantic", "summary_local", description="Missing equivalent properties"),
    "P17": PitfallPolicy("important", "semantic", "summary_local", description="Overspecialized hierarchy"),
    "P18": PitfallPolicy("important", "semantic", "summary_local", description="Overspecialized domain/range"),
    "P23": PitfallPolicy("important", "semantic", "summary_local", description="Datatype duplicated as class"),
    "P24": PitfallPolicy("important", "conditional", "summary_local", description="Recursive definition"),
    "P25": PitfallPolicy("important", "deterministic", "local", description="Relationship inverse to itself"),
    "P26": PitfallPolicy("important", "semantic", "summary_local", description="Inverse for symmetric property"),
    "P30": PitfallPolicy("important", "semantic", "summary_local", description="Missing equivalent classes"),
    "P34": PitfallPolicy("important", "deterministic", "local", description="Untyped class"),
    "P35": PitfallPolicy("important", "deterministic", "local", description="Untyped property"),
    "P38": PitfallPolicy("important", "conditional", "full_or_summary", description="No owl:Ontology declaration"),
    "P41": PitfallPolicy("important", "deterministic", "full_or_summary", description="No license"),
}


@dataclass
class Finding:
    code: str
    name: str
    severity: str
    description: str = ""
    suggestion: str = ""
    affected: list[str] = field(default_factory=list)
    # OOPS may return grouped candidate resources for completeness pitfalls
    # such as P30 (classes that might be equivalent). Preserve the grouping
    # instead of flattening all affected IRIs into one ontology-wide bucket.
    candidate_groups: list[list[str]] = field(default_factory=list)
    count: int = 1
    source: str = "oops"


@dataclass
class ReviewItem:
    id: str
    code: str
    severity: str
    title: str
    question: str
    explanation: str
    affected: list[str]
    choices: list[str]
    recommended: str
    proposed_add_ttl: str = ""
    proposed_delete_ttl: str = ""
    atomic_proposals: list[dict[str, str]] = field(default_factory=list)
    # Evidence shown before expert controls. Sources are explicitly labelled
    # as OOPS-returned or LLM-suggested so the UI never attributes an inferred
    # pair to OOPS! when the service only returned an ontology-wide finding.
    candidate_evidence: list[dict[str, str]] = field(default_factory=list)
    # Stable UI templates may let an expert add class-pair axioms even when
    # the LLM did not propose an executable pair. Values are existing IRIs only.
    entity_options: list[dict[str, str]] = field(default_factory=list)
    pair_relation: str = ""
    existing_pair_keys: list[str] = field(default_factory=list)
    conflicting_pair_keys: list[str] = field(default_factory=list)
    # Directed named subclass links that already exist in the ontology. P30
    # uses this to warn before strengthening a subclass alignment to exact
    # equivalence and to support an explicit replace-vs-keep decision.
    subclass_relations: list[dict[str, str]] = field(default_factory=list)
    custom_input_kind: str = "none"
    custom_input_label: str = ""
    validation_notes: list[str] = field(default_factory=list)
    context_excerpt: str = ""


@dataclass
class RepairResult:
    graph: Graph
    applied: list[dict[str, Any]]
    review_items: list[ReviewItem]
    unresolved: list[dict[str, Any]]
    final_summary: dict[str, int]
    report_json: str | None = None
    report_markdown: str | None = None


@dataclass
class PatchValidation:
    valid: bool
    auto_safe: bool
    graph: Graph | None = None
    reasons: list[str] = field(default_factory=list)
    before_summary: dict[str, int] = field(default_factory=dict)
    after_summary: dict[str, int] = field(default_factory=dict)
    metrics_before: dict[str, int] = field(default_factory=dict)
    metrics_after: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RDF I/O
# ---------------------------------------------------------------------------
def _sniff_formats(path: str | Path) -> list[str]:
    path = str(path)
    out: list[str] = []
    guessed = guess_format(path)
    if guessed:
        out.append(guessed)
    try:
        head = Path(path).read_bytes()[:4096].lstrip().lower()
        if head.startswith(b"<?xml") or b"<rdf:rdf" in head:
            out.insert(0, "xml")
        elif head.startswith((b"@prefix", b"prefix", b"base")):
            out.insert(0, "turtle")
        elif head.startswith((b"{", b"[")):
            out.insert(0, "json-ld")
    except OSError:
        pass
    for fmt in ("xml", "turtle", "json-ld", "nt", "n3", "trig", "trix"):
        if fmt not in out:
            out.append(fmt)
    return out


def load_ontology(path: str | Path, rdf_format: str | None = None) -> tuple[Graph, str]:
    path = str(path)
    errors: list[str] = []
    for fmt in ([rdf_format] if rdf_format else _sniff_formats(path)):
        g = Graph()
        try:
            g.parse(path, format=fmt)
            return g, fmt
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fmt}: {exc}")
    raise RuntimeError("Could not parse ontology. " + " | ".join(errors))


def serialize_ontology(graph: Graph, path: str | Path, rdf_format: str | None = None) -> str:
    fmt = rdf_format or guess_format(str(path)) or "turtle"
    graph.serialize(destination=str(path), format=fmt)
    return fmt


def clone_graph(graph: Graph) -> Graph:
    out = Graph()
    out.namespace_manager = graph.namespace_manager
    for triple in graph:
        out.add(triple)
    return out


def all_uris(graph: Graph) -> set[URIRef]:
    return {t for triple in graph for t in triple if isinstance(t, URIRef)}


def uri_occurs(graph: Graph, uri: URIRef) -> bool:
    return (
        any(graph.triples((uri, None, None)))
        or any(graph.triples((None, uri, None)))
        or any(graph.triples((None, None, uri)))
    )


def local_name(term: Any) -> str:
    value = str(term)
    if "#" in value:
        return value.rsplit("#", 1)[1] or value
    return value.rstrip("/").rsplit("/", 1)[-1] or value


def local_namespace(graph: Graph) -> str | None:
    counts: Counter[str] = Counter()
    for entity_type in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty):
        for entity in graph.subjects(RDF.type, entity_type):
            if isinstance(entity, URIRef):
                value = str(entity)
                ns = value.rsplit("#", 1)[0] + "#" if "#" in value else value.rsplit("/", 1)[0] + "/"
                if not ns.startswith(STANDARD_NAMESPACES):
                    counts[ns] += 1
    return counts.most_common(1)[0][0] if counts else None


# ---------------------------------------------------------------------------
# OOPS API
# ---------------------------------------------------------------------------
def build_request_xml(rdfxml: str) -> str:
    safe = rdfxml.replace("]]>", "]]]]><![CDATA[>")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<OOPSRequest>
  <OntologyUrl></OntologyUrl>
  <OntologyContent><![CDATA[{safe}]]></OntologyContent>
  <Pitfalls></Pitfalls>
  <OutputFormat>RDF/XML</OutputFormat>
</OOPSRequest>'''


def call_oops(request_xml: str, retries: int = 4) -> str:
    headers = {
        "Content-Type": "application/xml",
        "Accept": "application/rdf+xml, text/turtle;q=0.8, */*;q=0.1",
    }
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                OOPS_ENDPOINT,
                data=request_xml.encode("utf-8"),
                headers=headers,
                timeout=280,
            )
        except requests.RequestException as exc:
            if attempt == retries:
                raise RuntimeError(f"OOPS request failed: {exc}") from exc
            time.sleep(3 * attempt)
            continue
        if response.ok:
            return response.text
        if response.status_code >= 500 and attempt < retries:
            time.sleep(3 * attempt)
            continue
        raise RuntimeError(f"OOPS HTTP {response.status_code}: {response.text[:1000]}")
    raise RuntimeError("OOPS request failed")


def parse_oops_graph(text: str) -> Graph:
    stripped = (text or "").lstrip("\ufeff \t\r\n")
    formats = ("xml", "turtle") if stripped.startswith(("<?xml", "<rdf:RDF")) else ("turtle", "xml")
    errors: list[str] = []
    for fmt in formats:
        g = Graph()
        try:
            g.parse(data=text, format=fmt)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fmt}: {exc}")
            continue
        if any(
            isinstance(term, URIRef)
            and (str(term).startswith(str(OOPS)) or str(term).startswith(str(OOPS_OLD)))
            for triple in g for term in triple
        ):
            return g
        errors.append(f"{fmt}: no OOPS vocabulary")
    raise RuntimeError("Invalid OOPS response: " + " | ".join(errors))


def run_oops(graph: Graph) -> Graph:
    return parse_oops_graph(call_oops(build_request_xml(graph.serialize(format="xml"))))


def _objects_by_local(graph: Graph, subject: Any, name: str):
    for predicate, obj in graph.predicate_objects(subject):
        if local_name(predicate).lower() == name.lower():
            yield obj


def _first_by_local(graph: Graph, subject: Any, name: str) -> str:
    for obj in _objects_by_local(graph, subject, name):
        return str(obj)
    return ""


def _normalise_code(raw: str) -> str:
    match = re.search(r"P\d+", raw or "", re.I)
    return match.group(0).upper() if match else ""


def _affected_from_node(graph: Graph, node: Any) -> list[str]:
    values: list[str] = []

    def add(value: Any):
        if isinstance(value, URIRef):
            values.append(str(value))
        elif isinstance(value, Literal):
            text = str(value).strip()
            if re.match(r"^(?:https?|urn|file):", text):
                values.append(text)
            values.extend(_URI_RE.findall(text))
        elif isinstance(value, BNode):
            for _, _, nested in graph.triples((value, None, None)):
                add(nested)

    for predicate, obj in graph.predicate_objects(node):
        if local_name(predicate).lower() in {
            "hasaffectedelement", "affectedelement", "mightbeequivalentproperty",
            "mightbeequivalentattribute", "mightnotbeinversedof",
        }:
            add(obj)
    return list(dict.fromkeys(values))


def _uri_values_from_oops_resource(graph: Graph, value: Any) -> list[str]:
    """Collect ontology IRIs from an OOPS nested result resource.

    OOPS represents candidate groups (for example possible equivalent classes)
    as a resource whose outgoing properties contain the affected IRIs. The group
    resource itself is not an ontology entity and must not be returned.
    """
    out: list[str] = []
    seen: set[Any] = set()

    def walk(term: Any) -> None:
        if term in seen:
            return
        seen.add(term)
        if isinstance(term, Literal):
            text = str(term).strip()
            if re.match(r"^(?:https?|urn|file):", text):
                out.append(text)
            out.extend(_URI_RE.findall(text))
            return
        if isinstance(term, (BNode, URIRef)):
            outgoing = list(graph.predicate_objects(term))
            # A URI with no outgoing statements is normally the actual affected
            # ontology entity. A URI/BNode with outgoing statements is an OOPS
            # grouping resource; recurse into its values instead.
            if isinstance(term, URIRef) and not outgoing:
                out.append(str(term))
                return
            for pred, obj in outgoing:
                lname = local_name(pred).lower()
                if lname in {"type", "hascode", "hasname", "hasdescription",
                             "hasimportancelevel", "hasnumberaffectedelements"}:
                    continue
                walk(obj)

    walk(value)
    return list(dict.fromkeys(
        iri for iri in out
        if not iri.startswith((str(OOPS), "http://www.oeg-upm.net/oops"))
    ))


def _candidate_groups_from_node(graph: Graph, node: Any, code: str) -> list[list[str]]:
    """Preserve OOPS candidate groupings for P12/P30-like findings.

    The public OOPS response documentation names mightBeEquivalentProperty and
    mightBeEquivalentAttribute. Deployments also expose equivalent-class groups
    using similarly named predicates. We therefore match the semantic predicate
    name rather than one hard-coded namespace/version.
    """
    groups: list[list[str]] = []
    for predicate, obj in graph.predicate_objects(node):
        lname = local_name(predicate).lower().replace("_", "")
        is_equivalence_group = (
            "mightbeequivalent" in lname
            or "possibleequivalent" in lname
            or (code == "P30" and "equivalentclass" in lname)
        )
        if not is_equivalence_group:
            continue
        values = _uri_values_from_oops_resource(graph, obj)
        if len(values) >= 2:
            groups.append(values)

    # Some OOPS serializations put several affected elements directly under a
    # group node that is itself linked from the pitfall. Recover such nested
    # groups without treating every flat affected list as one equivalence set.
    for _, obj in graph.predicate_objects(node):
        if not isinstance(obj, (BNode, URIRef)):
            continue
        outgoing_names = {local_name(p).lower() for p, _ in graph.predicate_objects(obj)}
        if any("equivalent" in name for name in outgoing_names):
            values = _uri_values_from_oops_resource(graph, obj)
            if len(values) >= 2:
                groups.append(values)

    deduped: list[list[str]] = []
    seen_keys: set[tuple[str, ...]] = set()
    for group in groups:
        clean = list(dict.fromkeys(group))
        key = tuple(sorted(clean))
        if len(clean) >= 2 and key not in seen_keys:
            seen_keys.add(key)
            deduped.append(clean)
    return deduped


def findings_from_oops(oops_graph: Graph, levels: Iterable[str]) -> list[Finding]:
    wanted = {x.lower() for x in levels}
    candidates: set[Any] = set()
    for subject in set(oops_graph.subjects()):
        if _normalise_code(_first_by_local(oops_graph, subject, "hasCode") or _first_by_local(oops_graph, subject, "Code")):
            candidates.add(subject)
    findings: list[Finding] = []
    for node in candidates:
        code = _normalise_code(
            _first_by_local(oops_graph, node, "hasCode")
            or _first_by_local(oops_graph, node, "Code")
        )
        policy = PITFALL_POLICIES.get(code)
        severity = (
            _first_by_local(oops_graph, node, "hasImportanceLevel").strip().lower()
            or (policy.severity if policy else "")
        )
        if severity not in wanted or code not in PITFALL_POLICIES:
            continue
        affected = _affected_from_node(oops_graph, node)
        candidate_groups = _candidate_groups_from_node(oops_graph, node, code)
        # Candidate-group IRIs are affected elements too, but retain their
        # grouping separately so P30 can display the exact OOPS suggestions.
        affected = list(dict.fromkeys(affected + [iri for group in candidate_groups for iri in group]))
        raw_count = _first_by_local(oops_graph, node, "hasNumberAffectedElements")
        try:
            count = max(1, int(raw_count))
        except Exception:
            count = max(1, len(affected))
        findings.append(Finding(
            code=code,
            name=_first_by_local(oops_graph, node, "hasName") or policy.description,
            severity=severity,
            description=_first_by_local(oops_graph, node, "hasDescription"),
            suggestion=_first_by_local(oops_graph, node, "hasSuggestion"),
            affected=affected,
            candidate_groups=candidate_groups,
            count=count,
        ))
    return findings


def summary_from_findings(findings: Iterable[Finding]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for finding in findings:
        out[finding.code] += max(1, finding.count)
    return dict(out)


# ---------------------------------------------------------------------------
# Local detectors for deterministic/global catalogue entries
# ---------------------------------------------------------------------------
def _is_property_declared(graph: Graph, prop: URIRef) -> bool:
    return any((prop, RDF.type, t) in graph for t in PROPERTY_TYPES)


def find_untyped_properties(graph: Graph) -> list[URIRef]:
    candidates = {p for p in graph.predicates() if isinstance(p, URIRef)}
    for axiom in (RDFS.subPropertyOf, OWL.equivalentProperty, OWL.inverseOf, OWL.propertyDisjointWith):
        candidates.update(s for s in graph.subjects(axiom, None) if isinstance(s, URIRef))
        candidates.update(o for o in graph.objects(None, axiom) if isinstance(o, URIRef))
    candidates.update(o for o in graph.objects(None, OWL.onProperty) if isinstance(o, URIRef))
    return sorted([
        p for p in candidates
        if not str(p).startswith((str(RDF), str(RDFS), str(OWL), str(XSD)))
        and not _is_property_declared(graph, p)
    ], key=str)


def find_untyped_classes(graph: Graph) -> list[URIRef]:
    candidates: set[URIRef] = set()
    for predicate in (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith):
        candidates.update(s for s in graph.subjects(predicate, None) if isinstance(s, URIRef))
        candidates.update(o for o in graph.objects(None, predicate) if isinstance(o, URIRef))
    candidates.update(o for o in graph.objects(None, RDFS.domain) if isinstance(o, URIRef))
    candidates.update(o for o in graph.objects(None, RDFS.range) if isinstance(o, URIRef) and not str(o).startswith(str(XSD)))
    candidates.update(o for o in graph.objects(None, OWL.someValuesFrom) if isinstance(o, URIRef))
    candidates.update(o for o in graph.objects(None, OWL.allValuesFrom) if isinstance(o, URIRef))
    candidates.update(o for o in graph.objects(None, OWL.onClass) if isinstance(o, URIRef))
    return sorted([
        c for c in candidates
        if c not in {OWL.Thing, OWL.Nothing, RDFS.Resource, RDFS.Literal}
        and not str(c).startswith((str(RDF), str(RDFS), str(OWL), str(XSD)))
        and (c, RDF.type, OWL.Class) not in graph
        and (c, RDF.type, RDFS.Class) not in graph
    ], key=str)


def find_missing_domain_range_properties(graph: Graph) -> list[URIRef]:
    """Return declared object/datatype properties missing a domain or range."""
    properties = {
        prop for prop in graph.subjects(RDF.type, OWL.ObjectProperty)
        if isinstance(prop, URIRef)
    } | {
        prop for prop in graph.subjects(RDF.type, OWL.DatatypeProperty)
        if isinstance(prop, URIRef)
    }
    return sorted(
        [prop for prop in properties
         if not any(graph.objects(prop, RDFS.domain))
         or not any(graph.objects(prop, RDFS.range))],
        key=str,
    )


def find_p26_properties(graph: Graph) -> list[URIRef]:
    """Symmetric properties that also participate in a distinct inverse axiom."""
    out: set[URIRef] = set()
    for prop in graph.subjects(RDF.type, OWL.SymmetricProperty):
        if not isinstance(prop, URIRef):
            continue
        if any(isinstance(inv, URIRef) and inv != prop for inv in graph.objects(prop, OWL.inverseOf)):
            out.add(prop)
        if any(isinstance(inv, URIRef) and inv != prop for inv in graph.subjects(OWL.inverseOf, prop)):
            out.add(prop)
    return sorted(out, key=str)


def _declared_class_types(graph: Graph, resource: URIRef) -> set[URIRef]:
    return {
        cls for cls in graph.objects(resource, RDF.type)
        if isinstance(cls, URIRef)
        and ((cls, RDF.type, OWL.Class) in graph or (cls, RDF.type, RDFS.Class) in graph)
    }


def _single_candidate(scored: dict[URIRef, int]) -> URIRef | None:
    if not scored:
        return None
    best = max(scored.values())
    winners = [value for value, score in scored.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def infer_p11_domain_range(graph: Graph, prop: URIRef) -> tuple[URIRef | None, URIRef | None, list[str]]:
    """Infer only strongly supported domain/range values for P11.

    The function prefers logical evidence already present in the ontology.
    It never invents a class. The caller may use neutral top types for any
    side that remains ambiguous.
    """
    domain_scores: dict[URIRef, int] = defaultdict(int)
    range_scores: dict[URIRef, int] = defaultdict(int)
    notes: list[str] = []

    existing_domains = [x for x in graph.objects(prop, RDFS.domain) if isinstance(x, URIRef)]
    existing_ranges = [x for x in graph.objects(prop, RDFS.range) if isinstance(x, URIRef)]

    # Inverse-property semantics: domain(p)=range(inv), range(p)=domain(inv).
    inverses = {x for x in graph.objects(prop, OWL.inverseOf) if isinstance(x, URIRef)}
    inverses |= {x for x in graph.subjects(OWL.inverseOf, prop) if isinstance(x, URIRef)}
    for inv in inverses:
        for value in graph.objects(inv, RDFS.range):
            if isinstance(value, URIRef):
                domain_scores[value] += 10
        for value in graph.objects(inv, RDFS.domain):
            if isinstance(value, URIRef):
                range_scores[value] += 10

    # Equivalent/super-properties already impose compatible domain/range semantics.
    related = {x for x in graph.objects(prop, OWL.equivalentProperty) if isinstance(x, URIRef)}
    related |= {x for x in graph.subjects(OWL.equivalentProperty, prop) if isinstance(x, URIRef)}
    related |= {x for x in graph.objects(prop, RDFS.subPropertyOf) if isinstance(x, URIRef)}
    for other in related:
        for value in graph.objects(other, RDFS.domain):
            if isinstance(value, URIRef):
                domain_scores[value] += 8
        for value in graph.objects(other, RDFS.range):
            if isinstance(value, URIRef):
                range_scores[value] += 8

    # Named restriction owners/fillers are useful only when every occurrence
    # agrees. This avoids turning a local restriction into an unjustified
    # global domain/range axiom.
    owners: set[URIRef] = set()
    fillers: set[URIRef] = set()
    for restriction in graph.subjects(OWL.onProperty, prop):
        for owner, link in graph.subject_predicates(restriction):
            if link in {RDFS.subClassOf, OWL.equivalentClass} and isinstance(owner, URIRef):
                owners.add(owner)
        for pred in (OWL.onClass, OWL.someValuesFrom, OWL.allValuesFrom):
            for filler in graph.objects(restriction, pred):
                if isinstance(filler, URIRef):
                    fillers.add(filler)
    if len(owners) == 1:
        domain_scores[next(iter(owners))] += 6
    if len(fillers) == 1:
        range_scores[next(iter(fillers))] += 6

    # Existing assertions can supply a unique declared type/datatype.
    subject_types: set[URIRef] = set()
    object_types: set[URIRef] = set()
    literal_types: set[URIRef] = set()
    for subject, obj in graph.subject_objects(prop):
        if isinstance(subject, URIRef):
            subject_types |= _declared_class_types(graph, subject)
        if isinstance(obj, URIRef):
            object_types |= _declared_class_types(graph, obj)
        elif isinstance(obj, Literal) and obj.datatype:
            literal_types.add(URIRef(obj.datatype))
    if len(subject_types) == 1:
        domain_scores[next(iter(subject_types))] += 7
    if len(object_types) == 1:
        range_scores[next(iter(object_types))] += 7
    if len(literal_types) == 1:
        range_scores[next(iter(literal_types))] += 9

    # A symmetric property with one side already declared entails the same
    # class on the other side; making that explicit is semantics-preserving.
    if (prop, RDF.type, OWL.SymmetricProperty) in graph:
        if len(existing_domains) == 1 and not existing_ranges:
            range_scores[existing_domains[0]] += 10
        if len(existing_ranges) == 1 and not existing_domains:
            domain_scores[existing_ranges[0]] += 10

    domain = existing_domains[0] if len(existing_domains) == 1 else _single_candidate(domain_scores)
    range_ = existing_ranges[0] if len(existing_ranges) == 1 else _single_candidate(range_scores)
    if domain and not existing_domains:
        notes.append(f"domain inferred as <{domain}>")
    if range_ and not existing_ranges:
        notes.append(f"range inferred as <{range_}>")
    return domain, range_, notes


def local_findings(graph: Graph, levels: Iterable[str]) -> list[Finding]:
    wanted = {x.lower() for x in levels}
    out: list[Finding] = []
    if "important" in wanted:
        p34 = find_untyped_classes(graph)
        if p34:
            out.append(Finding("P34", "Untyped class", "important", affected=[str(x) for x in p34], count=len(p34), source="local"))
        p35 = find_untyped_properties(graph)
        if p35:
            out.append(Finding("P35", "Untyped property", "important", affected=[str(x) for x in p35], count=len(p35), source="local"))
        p11 = find_missing_domain_range_properties(graph)
        if p11:
            out.append(Finding("P11", "Missing domain or range in properties", "important", affected=[str(x) for x in p11], count=len(p11), source="local"))
        p26 = find_p26_properties(graph)
        if p26:
            out.append(Finding("P26", "Inverse for symmetric property", "important", affected=[str(x) for x in p26], count=len(p26), source="local"))
        self_inverse = sorted({s for s, o in graph.subject_objects(OWL.inverseOf) if s == o and isinstance(s, URIRef)}, key=str)
        if self_inverse:
            out.append(Finding("P25", "Relationship inverse to itself", "important", affected=[str(x) for x in self_inverse], count=len(self_inverse), source="local"))
        if not any(graph.subjects(RDF.type, OWL.Ontology)):
            out.append(Finding("P38", "No OWL ontology declaration", "important", count=1, source="local"))
        if not _has_license(graph):
            out.append(Finding("P41", "No license declared", "important", count=1, source="local"))
    if "critical" in wanted:
        # P39 can be safely identified locally only when there is no ontology IRI
        # and no stable dominant namespace can be inferred.
        if not any(graph.subjects(RDF.type, OWL.Ontology)) and local_namespace(graph) is None:
            out.append(Finding("P39", "Ambiguous namespace", "critical", count=1, source="local"))
    return out


def merge_findings(oops_findings: list[Finding], local: list[Finding]) -> list[Finding]:
    merged: dict[str, Finding] = {}
    for f in oops_findings + local:
        if f.code not in merged:
            merged[f.code] = f
        else:
            current = merged[f.code]
            current.affected = list(dict.fromkeys(current.affected + f.affected))
            existing_groups = {tuple(sorted(group)) for group in current.candidate_groups}
            for group in f.candidate_groups:
                key = tuple(sorted(group))
                if key not in existing_groups:
                    current.candidate_groups.append(group)
                    existing_groups.add(key)
            current.count = max(current.count, f.count, len(current.affected) or 1)
            current.source = "oops+local"
            if not current.description:
                current.description = f.description
    return list(merged.values())


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------
def add_neighborhood(source: Graph, output: Graph, node: URIRef, limit: int = MAX_NEIGHBORHOOD_TRIPLES) -> None:
    count = 0
    queue: list[Any] = [node]
    seen: set[Any] = set()
    incoming = {
        RDF.type, RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain, RDFS.range,
        OWL.onProperty, OWL.someValuesFrom, OWL.allValuesFrom, OWL.inverseOf,
        OWL.disjointWith, OWL.equivalentClass, OWL.equivalentProperty,
    }
    while queue and count < limit:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for triple in source.triples((current, None, None)):
            output.add(triple); count += 1
            if isinstance(triple[2], BNode):
                queue.append(triple[2])
            if count >= limit:
                return
        for triple in source.triples((None, None, current)):
            if triple[1] not in incoming:
                continue
            output.add(triple); count += 1
            if isinstance(triple[0], BNode):
                queue.append(triple[0])
            if count >= limit:
                return
        for triple in source.triples((None, current, None)):
            output.add(triple); count += 1
            if count >= limit:
                return


def neighborhood_ttl(graph: Graph, affected: list[str]) -> str:
    out = Graph(); out.namespace_manager = graph.namespace_manager
    for value in affected[:MAX_AFFECTED]:
        uri = URIRef(value)
        if uri_occurs(graph, uri):
            add_neighborhood(graph, out, uri)
    return out.serialize(format="turtle") if len(out) else ""


def graph_metrics(graph: Graph) -> dict[str, int]:
    local_ns = local_namespace(graph)
    classes = set(graph.subjects(RDF.type, OWL.Class)) | set(graph.subjects(RDF.type, RDFS.Class))
    obj = set(graph.subjects(RDF.type, OWL.ObjectProperty))
    data = set(graph.subjects(RDF.type, OWL.DatatypeProperty))
    prov = sum(1 for _ in graph.triples((None, PROV.wasDerivedFrom, None)))
    alignments = 0
    if local_ns:
        for predicate in (RDFS.subClassOf, OWL.equivalentClass, RDFS.subPropertyOf, OWL.equivalentProperty):
            for subject, target in graph.subject_objects(predicate):
                if isinstance(subject, URIRef) and isinstance(target, URIRef):
                    if str(subject).startswith(local_ns) and not str(target).startswith(local_ns):
                        alignments += 1
    source_targets = len(set(graph.objects(None, PROV.wasDerivedFrom)))
    return {
        "triples": len(graph), "classes": len(classes), "object_properties": len(obj),
        "data_properties": len(data), "provenance": prov,
        "external_alignments": alignments, "source_targets": source_targets,
    }


def ontology_summary(graph: Graph) -> str:
    metrics = graph_metrics(graph)
    lines = ["ONTOLOGY SUMMARY", json.dumps(metrics, indent=2), "", "NAMESPACES"]
    for prefix, ns in list(graph.namespaces())[:30]:
        lines.append(f"{prefix or '(default)'}: {ns}")
    lines += ["", "CLASSES AND HIERARCHY"]
    classes = sorted(set(graph.subjects(RDF.type, OWL.Class)), key=str)[:MAX_SUMMARY_ITEMS]
    for cls in classes:
        supers = [local_name(x) for x in graph.objects(cls, RDFS.subClassOf) if isinstance(x, URIRef)]
        label = next((str(x) for x in graph.objects(cls, RDFS.label)), local_name(cls))
        lines.append(f"- {label} <{cls}> subclasses={supers}")
    lines += ["", "PROPERTIES"]
    properties = sorted(
        set(graph.subjects(RDF.type, OWL.ObjectProperty))
        | set(graph.subjects(RDF.type, OWL.DatatypeProperty))
        | set(graph.subjects(RDF.type, RDF.Property)), key=str
    )[:MAX_SUMMARY_ITEMS]
    for prop in properties:
        types = [local_name(x) for x in graph.objects(prop, RDF.type)]
        domains = [str(x) for x in graph.objects(prop, RDFS.domain)]
        ranges = [str(x) for x in graph.objects(prop, RDFS.range)]
        supers = [str(x) for x in graph.objects(prop, RDFS.subPropertyOf)]
        inverses = [str(x) for x in graph.objects(prop, OWL.inverseOf)]
        equivalents = [str(x) for x in graph.objects(prop, OWL.equivalentProperty)]
        lines.append(f"- <{prop}> types={types} domain={domains} range={ranges} subPropertyOf={supers} inverseOf={inverses} equivalent={equivalents}")
    lines += ["", "RESTRICTIONS"]
    n = 0
    for cls, restriction in graph.subject_objects(RDFS.subClassOf):
        if not isinstance(restriction, BNode) or (restriction, RDF.type, OWL.Restriction) not in graph:
            continue
        prop = next(graph.objects(restriction, OWL.onProperty), None)
        fillers = []
        for pred in (OWL.someValuesFrom, OWL.allValuesFrom, OWL.hasValue, OWL.onClass):
            fillers.extend(f"{local_name(pred)}={obj}" for obj in graph.objects(restriction, pred))
        lines.append(f"- {cls} onProperty={prop} {' '.join(fillers)}")
        n += 1
        if n >= MAX_SUMMARY_ITEMS:
            break
    return "\n".join(lines)


def context_for_finding(graph: Graph, finding: Finding, policy: PitfallPolicy) -> str:
    local = neighborhood_ttl(graph, finding.affected)
    summary = ontology_summary(graph)
    if policy.context == "local":
        return local or summary
    if policy.context == "summary_local":
        return summary + "\n\nAFFECTED NEIGHBORHOOD (TURTLE)\n" + (local or "(none returned)")
    full = graph.serialize(format="turtle")
    if len(full) <= MAX_FULL_CONTEXT_CHARS:
        return summary + "\n\nFULL ONTOLOGY (TURTLE)\n" + full
    return summary + "\n\nAFFECTED NEIGHBORHOOD (TURTLE)\n" + (local or "(none returned)")


# ---------------------------------------------------------------------------
# Deterministic repairs
# ---------------------------------------------------------------------------
def _resolve_ontology_node(graph: Graph) -> tuple[URIRef | None, bool]:
    existing = sorted((x for x in graph.subjects(RDF.type, OWL.Ontology) if isinstance(x, URIRef)), key=str)
    if existing:
        return existing[0], False
    ns = local_namespace(graph)
    if not ns:
        return None, False
    return URIRef(ns[:-1] if ns.endswith(("#", "/")) else ns), True


def _has_license(graph: Graph) -> bool:
    nodes = list(graph.subjects(RDF.type, OWL.Ontology))
    return any((node, predicate, None) in graph for node in nodes for predicate in LICENSE_PREDICATES)


def deterministic_patch(graph: Graph, finding: Finding, license_uri: str) -> tuple[str, str, str]:
    """Return (add_ttl, delete_ttl, note). Empty blocks mean no safe fix."""
    code = finding.code
    add = Graph(); add.namespace_manager = graph.namespace_manager
    delete = Graph(); delete.namespace_manager = graph.namespace_manager
    affected = [URIRef(x) for x in finding.affected if re.match(r"^(?:https?|urn):", x)]

    if code == "P41":
        node, minted = _resolve_ontology_node(graph)
        if node is None:
            return "", "", "No stable ontology IRI could be inferred."
        if _has_license(graph):
            return "", "", "License already present."
        if minted:
            add.add((node, RDF.type, OWL.Ontology))
        add.add((DCTERMS.license, RDF.type, RDF.Property))
        add.add((DCTERMS.license, RDF.type, OWL.AnnotationProperty))
        add.add((node, DCTERMS.license, URIRef(license_uri)))
        return add.serialize(format="turtle"), "", "Added CC BY 4.0 license metadata."

    if code in {"P38", "P39"}:
        if any(graph.subjects(RDF.type, OWL.Ontology)):
            return "", "", "Ontology declaration already present."
        node, minted = _resolve_ontology_node(graph)
        if node is None or not minted:
            return "", "", "No unambiguous dominant namespace was available."
        add.add((node, RDF.type, OWL.Ontology))
        return add.serialize(format="turtle"), "", "Added owl:Ontology declaration using the dominant local namespace."

    if code == "P34":
        candidates = affected or find_untyped_classes(graph)
        for cls in candidates:
            if (cls, RDF.type, OWL.Class) not in graph and (cls, RDF.type, RDFS.Class) not in graph:
                add.add((cls, RDF.type, OWL.Class))
        return (add.serialize(format="turtle"), "", "Declared class resources explicitly.") if len(add) else ("", "", "No untyped classes found.")

    if code == "P35":
        # rdf:Property is intentionally used instead of guessing object/data
        # property semantics; this fixes P35 without creating P11.
        candidates = affected or find_untyped_properties(graph)
        candidates = list(dict.fromkeys(candidates + find_untyped_properties(graph)))
        for prop in candidates:
            if not _is_property_declared(graph, prop):
                add.add((prop, RDF.type, RDF.Property))
        return (add.serialize(format="turtle"), "", "Declared property resources as rdf:Property.") if len(add) else ("", "", "No untyped properties found.")

    if code == "P11":
        candidates = affected or find_missing_domain_range_properties(graph)
        specific = 0
        neutral = 0
        details: list[str] = []
        for prop in candidates:
            is_object = (prop, RDF.type, OWL.ObjectProperty) in graph
            is_data = (prop, RDF.type, OWL.DatatypeProperty) in graph
            if not (is_object or is_data):
                continue
            has_domain = any(graph.objects(prop, RDFS.domain))
            has_range = any(graph.objects(prop, RDFS.range))
            if has_domain and has_range:
                continue
            inferred_domain, inferred_range, inferred_notes = infer_p11_domain_range(graph, prop)
            if not has_domain:
                value = inferred_domain or OWL.Thing
                add.add((prop, RDFS.domain, value))
                if inferred_domain:
                    specific += 1
                else:
                    neutral += 1
            if not has_range:
                value = inferred_range or (RDFS.Literal if is_data else OWL.Thing)
                add.add((prop, RDFS.range, value))
                if inferred_range:
                    specific += 1
                else:
                    neutral += 1
            if inferred_notes:
                details.append(f"{prop}: " + ", ".join(inferred_notes))
        if len(add):
            note = (
                f"Added missing domain/range axioms ({specific} evidence-backed, "
                f"{neutral} semantics-neutral top-type fallback(s))."
            )
            if details:
                note += " " + "; ".join(details[:8])
            return add.serialize(format="turtle"), "", note
        return "", "", "No declared object/datatype property was missing a domain or range."

    if code == "P25":
        candidates = affected or [s for s, o in graph.subject_objects(OWL.inverseOf) if s == o and isinstance(s, URIRef)]
        for prop in candidates:
            if (prop, OWL.inverseOf, prop) in graph:
                delete.add((prop, OWL.inverseOf, prop))
                add.add((prop, RDF.type, OWL.ObjectProperty))
                add.add((prop, RDF.type, OWL.SymmetricProperty))
        return (add.serialize(format="turtle"), delete.serialize(format="turtle"), "Replaced self-inverse declaration with owl:SymmetricProperty.") if len(delete) else ("", "", "No direct self-inverse axiom found.")

    if code in {"P06", "P24"}:
        candidates = affected
        for entity in candidates:
            for pred in (RDFS.subClassOf, RDFS.subPropertyOf, OWL.equivalentClass, OWL.equivalentProperty):
                if (entity, pred, entity) in graph:
                    delete.add((entity, pred, entity))
        return ("", delete.serialize(format="turtle"), "Removed direct tautological self-reference.") if len(delete) else ("", "", "The recursion/cycle is not a direct tautology and requires semantic assessment.")

    if code == "P19":
        candidates = affected or sorted(
            {s for s in graph.subjects(RDFS.domain, None) if len(set(graph.objects(s, RDFS.domain))) > 1}
            | {s for s in graph.subjects(RDFS.range, None) if len(set(graph.objects(s, RDFS.range))) > 1}, key=str
        )
        for prop in candidates:
            for pred in (RDFS.domain, RDFS.range):
                values = set(graph.objects(prop, pred))
                if len(values) > 1:
                    for top in (OWL.Thing, RDFS.Resource):
                        if top in values:
                            delete.add((prop, pred, top))
        return ("", delete.serialize(format="turtle"), "Removed only redundant top-domain/range declarations.") if len(delete) else ("", "", "Multiple domain/range semantics are ambiguous.")

    return "", "", "No deterministic repair policy for this pitfall."


# ---------------------------------------------------------------------------
# LLM assessment and prompt
# ---------------------------------------------------------------------------
def code_specific_instruction(code: str) -> str:
    return {
        "P01": "Determine whether one entity conflates distinct meanings. Prefer splitting only if existing distinct IRIs already support it; otherwise ask the user.",
        "P03": "Determine whether the custom 'is' predicate means subclass, instance-of, or individual identity. Do not guess.",
        "P05": "Check inverse semantics and swapped domain/range. Remove or correct only the specific inverse axiom.",
        "P06": "Identify the minimal hierarchy edge causing the cycle. Ask the user if multiple edge removals are plausible.",
        "P10": "Propose disjointness only when the supplied domain knowledge clearly establishes mutual exclusion.",
        "P11": "Repair automatically. Infer domain/range from existing schema-derived classes, inverse/equivalent properties, restrictions, assertions, labels, and documentation. Never ask the user and never invent a class. If a specific type is not justified, use owl:Thing for an object-property side, owl:Thing for a datatype-property domain, or rdfs:Literal for a datatype-property range; these are semantics-neutral explicit fallbacks.",
        "P12": "Declare equivalence only for exactly identical semantics, not merely related or inverse properties.",
        "P14": "Use allValuesFrom only for universal constraints; preserve existential requirements separately.",
        "P15": "Correct quantifier/negation order only when intended meaning is clear from context.",
        "P16": "Use equivalentClass only if necessary-and-sufficient classification is intended.",
        "P17": "Distinguish classes from instances using domain meaning; ask the user when uncertain.",
        "P18": "Broaden domain/range only to an existing justified superclass or datatype.",
        "P19": "Model alternatives with owl:unionOf when intended, or remove an erroneous statement; do not simply choose one without evidence.",
        "P23": "Reuse an existing XSD datatype only when the class truly duplicates it.",
        "P24": "Decide whether recursion is intentional (for example Employee reportsTo Employee). If intentional, request user confirmation to keep it; otherwise propose the smallest deletion.",
        "P26": "Determine whether to keep symmetry or the separate inverse based on relation meaning.",
        "P27": "Remove equivalentProperty only when the meanings differ; suggest inverse/subproperty only if justified.",
        "P28": "Retain symmetry only if the relation necessarily holds in both directions.",
        "P29": "Retain transitivity only if chaining the relation is always valid.",
        "P30": "Propose equivalentClass only for identical necessary-and-sufficient meaning. If OOPS is ontology-wide, identify up to five evidence-backed candidate pairs for USER REVIEW, or state that no supported pair exists. Never imply that OOPS named a pair unless it actually returned both IRIs.",
        "P31": "Remove or weaken equivalentClass when equivalence is not justified.",
        "P37": "This is a publishing/deployment issue. Do not mutate ontology semantics; prepare a user action.",
        "P39": "Use a stable existing local namespace. Never derive a permanent ontology IRI from a temporary file path.",
        "P40": "Do not define terms in a namespace the ontology does not control. Reuse a valid existing local IRI or ask the user.",
    }.get(code, "Apply established OWL 2 DL modeling practices and prefer the smallest semantics-preserving change.")


def llm_assess_finding(finding: Finding, policy: PitfallPolicy, context: str, feedback: str = "") -> dict[str, Any]:
    prompt = f"""You are a senior OWL 2 DL ontology engineer reviewing an OOPS! finding.

PITFALL
Code: {finding.code}
Severity: {finding.severity}
Name: {finding.name}
Description: {finding.description}
Suggestion from OOPS: {finding.suggestion}
Affected IRIs: {json.dumps(finding.affected, indent=2)}

CODE-SPECIFIC GUIDANCE
{code_specific_instruction(finding.code)}

ONTOLOGY CONTEXT
{context}

PREVIOUS VALIDATION FEEDBACK
{feedback or '(none)'}

Return exactly one decision: REPAIR, KEEP_INTENTIONAL, or ASK_USER.

Strict rules:
1. Preserve OWL 2 DL syntax and logical consistency.
2. Preserve database/schema coverage, provenance assertions, external alignments,
   readable annotations, restrictions, and other enrichment unless one is the
   exact offending axiom.
3. Reuse only IRIs already present in the context. Never invent placeholders.
4. A deletion block must contain exact existing triples and nothing else.
5. Use rdfs:domain/rdfs:range, never owl:domain/owl:range.
6. Do not confuse equivalence, inverse, subclass, subproperty, or identity.
7. Prefer a one-axiom repair over broad deletion. Never delete a class/property
   or a whole connected subgraph merely to reduce the scanner count.
8. If domain meaning is ambiguous, choose ASK_USER and write one short,
   nontechnical question with 2-3 clear choices.
9. KEEP_INTENTIONAL is allowed only when the flagged pattern is valid and useful.
10. For REPAIR, provide minimal valid Turtle additions/deletions.
11. For P10, P12, or P30 with ACTION=ASK_USER, you may place candidate axioms
    in the Turtle addition block as NON-COMMITTED proposals. Each candidate must
    use existing IRIs and be supported by the supplied ontology context.

###start_decision###
ACTION: REPAIR|KEEP_INTENTIONAL|ASK_USER
CONFIDENCE: 0.00-1.00
RATIONALE: one concise paragraph
QUESTION: blank unless ASK_USER or user confirmation is prudent
CHOICES: choice 1 || choice 2 || choice 3
RECOMMENDED: one exact choice
CUSTOM_INPUT_KIND: none|domain_range|iri|text
CUSTOM_INPUT_LABEL: short label or blank
###end_decision###

###start_turtle###
###end_turtle###

###start_delete###
###end_delete###
"""
    raw = api_utils.send_prompt_raw(
        prompt,
        persona="OWL 2 DL ontology repair and review expert",
        step_name=f"oops_assess_{finding.code}",
    )
    decision_blocks = extract_between_markers(raw, "###start_decision###", "###end_decision###")
    add_blocks = extract_between_markers(raw, "###start_turtle###", "###end_turtle###")
    del_blocks = extract_between_markers(raw, "###start_delete###", "###end_delete###")
    decision_text = decision_blocks[0] if decision_blocks else ""
    fields: dict[str, str] = {}
    for line in decision_text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().upper()] = value.strip()
    action = fields.get("ACTION", "ASK_USER").upper()
    if action not in {"REPAIR", "KEEP_INTENTIONAL", "ASK_USER"}:
        action = "ASK_USER"
    try:
        confidence = max(0.0, min(1.0, float(fields.get("CONFIDENCE", "0"))))
    except ValueError:
        confidence = 0.0
    choices = [x.strip() for x in fields.get("CHOICES", "").split("||") if x.strip()]
    return {
        "action": action,
        "confidence": confidence,
        "rationale": fields.get("RATIONALE", ""),
        "question": fields.get("QUESTION", ""),
        "choices": choices,
        "recommended": fields.get("RECOMMENDED", ""),
        "custom_input_kind": fields.get("CUSTOM_INPUT_KIND", "none").lower(),
        "custom_input_label": fields.get("CUSTOM_INPUT_LABEL", ""),
        "add_ttl": add_blocks[0].strip() if add_blocks else "",
        "delete_ttl": del_blocks[0].strip() if del_blocks else "",
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------
def prefix_header(graph: Graph) -> str:
    lines = []
    for prefix, ns in graph.namespaces():
        if prefix is None:
            continue
        lines.append(f"@prefix {prefix}: <{ns}> .")
    return "\n".join(lines) + "\n"


def parse_patch(graph: Graph, text: str, label: str) -> Graph:
    out = Graph()
    if not text.strip():
        return out
    try:
        out.parse(data=prefix_header(graph) + text, format="turtle")
    except Exception as exc:
        raise ValueError(f"{label} is invalid Turtle: {exc}") from exc
    return out


def validate_vocabulary(base: Graph, additions: Graph, deletions: Graph, allowed_new: Iterable[URIRef] = ()) -> list[str]:
    errors: list[str] = []
    existing = all_uris(base)
    allowed = set(allowed_new)
    for block_name, block in (("addition", additions), ("deletion", deletions)):
        for s, p, o in block:
            if str(p).startswith(str(OWL)) and p not in ALLOWED_OWL_PREDICATES:
                errors.append(f"{block_name} uses invalid OWL predicate <{p}>")
            for term in (s, p, o):
                if not isinstance(term, URIRef):
                    continue
                if term in existing or term in allowed or str(term).startswith(STANDARD_NAMESPACES):
                    continue
                errors.append(f"{block_name} introduces unknown IRI <{term}>")
    for triple in deletions:
        if triple not in base:
            errors.append(f"delete triple does not exist: {triple}")
    return errors


def reasoner_status(graph: Graph, java_exe: str, robot_jar: str | None) -> tuple[str, str]:
    if not robot_jar:
        return "unavailable", "ROBOT jar not supplied"
    try:
        import validate_fix_ontology_consistency as consistency
    except Exception as exc:
        return "unavailable", str(exc)
    jar = Path(robot_jar)
    if not jar.exists() or not consistency._java_available(java_exe):
        return "unavailable", "Java or ROBOT unavailable"
    handle = tempfile.NamedTemporaryFile(suffix=".ttl", delete=False)
    path = Path(handle.name); handle.close()
    try:
        graph.serialize(destination=str(path), format="turtle")
        return consistency.robot_reason_status(path, jar, reasoner="HermiT")
    except Exception as exc:
        return "error", str(exc)
    finally:
        path.unlink(missing_ok=True)


def structural_guard(before: Graph, after: Graph, target_code: str, deletions: Graph, user_approved: bool = False) -> tuple[bool, bool, list[str]]:
    """Return (valid, auto_safe, reasons)."""
    m0, m1 = graph_metrics(before), graph_metrics(after)
    reasons: list[str] = []
    auto_safe = True
    if m1["classes"] < m0["classes"]:
        reasons.append("repair removes a declared class")
        auto_safe = False
    if m1["object_properties"] < m0["object_properties"] or m1["data_properties"] < m0["data_properties"]:
        reasons.append("repair removes a declared object/data property")
        auto_safe = False
    if m1["provenance"] < m0["provenance"] or m1["source_targets"] < m0["source_targets"]:
        reasons.append("repair reduces provenance/schema traceability")
        return False, False, reasons
    if m1["external_alignments"] < m0["external_alignments"]:
        reasons.append("repair removes an external alignment")
        auto_safe = False
    deletion_limit = max(10, min(50, int(max(1, len(before)) * 0.01)))
    if len(deletions) > deletion_limit:
        reasons.append(f"repair deletes {len(deletions)} triples (automatic limit {deletion_limit})")
        auto_safe = False
    if len(after) < int(len(before) * 0.98):
        reasons.append("repair removes more than 2% of the ontology")
        auto_safe = False
    if user_approved:
        # User approval permits a narrowly scoped class/property/alignment change,
        # but never loss of provenance or a broad graph deletion.
        if len(after) < int(len(before) * 0.95):
            return False, False, reasons + ["user-approved repair still removes more than 5% of ontology"]
        return True, True, reasons
    return True, auto_safe, reasons


def validate_candidate_graph(
    graph: Graph,
    trial: Graph,
    finding: Finding,
    baseline_findings: list[Finding],
    *,
    levels: Iterable[str],
    robot_jar: str | None,
    java_exe: str,
    allowed_new: Iterable[URIRef] = (),
    user_approved: bool = False,
    allow_target_unchanged: bool = False,
) -> PatchValidation:
    """Validate an already-built candidate graph.

    This is used for structural repairs involving blank-node OWL axioms, which
    cannot be represented safely as a separate Turtle delete block because
    blank-node identifiers are local to each parse.
    """
    result = PatchValidation(False, False)
    additions = Graph()
    deletions = Graph()
    additions.namespace_manager = graph.namespace_manager
    deletions.namespace_manager = graph.namespace_manager
    for triple in trial:
        if triple not in graph:
            additions.add(triple)
    for triple in graph:
        if triple not in trial:
            deletions.add(triple)
    if not len(additions) and not len(deletions):
        result.reasons.append("empty patch")
        return result
    result.reasons.extend(validate_vocabulary(graph, additions, deletions, allowed_new))
    if result.reasons:
        return result
    try:
        serialized = trial.serialize(format="turtle")
        probe = Graph(); probe.parse(data=serialized, format="turtle")
    except Exception as exc:
        result.reasons.append(f"patched graph is not parseable: {exc}")
        return result
    status, diag = reasoner_status(trial, java_exe, robot_jar)
    if status == "incoherent":
        result.reasons.append("ROBOT/HermiT reports inconsistency")
        return result
    if status in {"unavailable", "error"}:
        result.reasons.append(f"reasoner validation unavailable: {diag[:160]}")
        reasoner_auto_safe = finding.code in {"P34", "P35", "P38", "P39", "P41"}
    else:
        reasoner_auto_safe = True
    valid_structure, auto_safe, structural_reasons = structural_guard(
        graph, trial, finding.code, deletions, user_approved=user_approved
    )
    result.reasons.extend(structural_reasons)
    if not valid_structure:
        return result
    try:
        after_oops = run_oops(trial)
        after_findings = merge_findings(
            findings_from_oops(after_oops, levels), local_findings(trial, levels)
        )
    except Exception as exc:
        result.reasons.append(f"fresh OOPS verification failed: {exc}")
        return result
    before_summary = summary_from_findings(baseline_findings)
    after_summary = summary_from_findings(after_findings)
    result.before_summary = before_summary
    result.after_summary = after_summary
    before_target = before_summary.get(finding.code, 0)
    after_target = after_summary.get(finding.code, 0)
    if after_target > before_target:
        result.reasons.append(
            f"target {finding.code} increased ({before_target} -> {after_target})"
        )
        return result
    if after_target == before_target and not (user_approved and allow_target_unchanged):
        result.reasons.append(
            f"target {finding.code} did not decrease ({before_target} -> {after_target})"
        )
        return result
    new_codes = {
        code for code, count in after_summary.items()
        if count > before_summary.get(code, 0)
    }
    if new_codes:
        result.reasons.append(
            "new/increased Critical or Important findings: " + ", ".join(sorted(new_codes))
        )
        return result
    result.valid = True
    result.auto_safe = auto_safe and reasoner_auto_safe
    result.graph = trial
    result.metrics_before = graph_metrics(graph)
    result.metrics_after = graph_metrics(trial)
    return result


def validate_patch(
    graph: Graph,
    finding: Finding,
    add_ttl: str,
    delete_ttl: str,
    baseline_findings: list[Finding],
    *,
    levels: Iterable[str],
    robot_jar: str | None,
    java_exe: str,
    allowed_new: Iterable[URIRef] = (),
    user_approved: bool = False,
    allow_target_unchanged: bool = False,
) -> PatchValidation:
    result = PatchValidation(False, False)
    try:
        additions = parse_patch(graph, add_ttl, "addition block")
        deletions = parse_patch(graph, delete_ttl, "delete block")
    except ValueError as exc:
        result.reasons.append(str(exc))
        return result
    if not len(additions) and not len(deletions):
        result.reasons.append("empty patch")
        return result
    trial = clone_graph(graph)
    for triple in deletions:
        trial.remove(triple)
    for triple in additions:
        trial.add(triple)
    return validate_candidate_graph(
        graph,
        trial,
        finding,
        baseline_findings,
        levels=levels,
        robot_jar=robot_jar,
        java_exe=java_exe,
        allowed_new=allowed_new,
        user_approved=user_approved,
        allow_target_unchanged=allow_target_unchanged,
    )


# ---------------------------------------------------------------------------
# Review questions and reports
# ---------------------------------------------------------------------------
def friendly_entity(graph: Graph, iri: str) -> str:
    term = URIRef(iri)
    label = next((str(x) for x in graph.objects(term, RDFS.label)), "")
    return label or local_name(iri)


def _p03_predicates(graph: Graph, affected: Iterable[str]) -> list[URIRef]:
    """Resolve the custom predicate(s) reported by OOPS for P03."""
    predicates: list[URIRef] = []
    for value in affected:
        candidate = URIRef(str(value))
        if (
            any(graph.triples((None, candidate, None)))
            or any(graph.triples((candidate, None, None)))
        ):
            predicates.append(candidate)

    if not predicates:
        predicates = sorted(
            {
                predicate
                for _, predicate, _ in graph
                if isinstance(predicate, URIRef)
                and local_name(str(predicate)).strip().lower() == "is"
            }
            | {
                subject
                for subject in graph.subjects()
                if isinstance(subject, URIRef)
                and local_name(str(subject)).strip().lower() == "is"
            },
            key=str,
        )
    return list(dict.fromkeys(predicates))


def p03_relation_occurrences(
    graph: Graph, affected: Iterable[str]
) -> list[tuple[URIRef, URIRef, URIRef]]:
    """Return concrete URI-to-URI triples using the flagged P03 predicate."""
    triples: list[tuple[URIRef, URIRef, URIRef]] = []
    for predicate in _p03_predicates(graph, affected):
        for subject, _, obj in graph.triples((None, predicate, None)):
            if isinstance(subject, URIRef) and isinstance(obj, URIRef):
                triples.append((subject, predicate, obj))
    return sorted(set(triples), key=lambda triple: tuple(map(str, triple)))


def p03_relation_basis(
    graph: Graph, affected: Iterable[str]
) -> tuple[URIRef, URIRef, URIRef, list[tuple]] | None:
    """Find the two entities P03 relates and the exact triples to replace.

    OOPS may flag either a concrete ``A :is B`` assertion or only a custom
    property declaration such as ``:is rdfs:domain Person; rdfs:range
    Employee``.  The latter was the reason the UI previously fell back to the
    generic Apply/Keep buttons.  When no concrete assertion exists, the domain
    and range become the two entities shown in the fixed P03 template.
    """
    occurrences = p03_relation_occurrences(graph, affected)
    if occurrences:
        left, predicate, right = occurrences[0]
        return left, predicate, right, [(left, predicate, right)]

    for predicate in _p03_predicates(graph, affected):
        domains = [x for x in graph.objects(predicate, RDFS.domain) if isinstance(x, URIRef)]
        ranges = [x for x in graph.objects(predicate, RDFS.range) if isinstance(x, URIRef)]
        if not domains or not ranges:
            continue
        left, right = domains[0], ranges[0]
        # Replacing the custom P03 relation means removing its declaration and
        # metadata.  Otherwise OOPS continues to flag the still-declared ``is``
        # property even after the standard axiom is added.
        delete_triples = list(graph.triples((predicate, None, None)))
        delete_triples.extend(graph.triples((None, predicate, None)))
        return left, predicate, right, list(dict.fromkeys(delete_triples))
    return None


def p03_choice_specs(
    graph: Graph, affected: Iterable[str]
) -> list[dict[str, Any]]:
    """Build the fixed, exhaustive P03 decision template.

    Direction matters for ``rdf:type`` and ``rdfs:subClassOf``, so both
    permutations are always exposed.  ``owl:sameAs`` and
    ``owl:equivalentClass`` are symmetric and therefore appear once.
    """
    basis = p03_relation_basis(graph, affected)
    if basis is None:
        return []
    left, predicate, right, delete_triples = basis
    left_name = friendly_entity(graph, str(left))
    right_name = friendly_entity(graph, str(right))
    return [
        {
            "label": f"Class membership ({left_name} is an instance of {right_name})",
            "add": (left, RDF.type, right),
            "delete": delete_triples,
        },
        {
            "label": f"Class membership ({right_name} is an instance of {left_name})",
            "add": (right, RDF.type, left),
            "delete": delete_triples,
        },
        {
            "label": f"Subclass relationship ({left_name} is a subclass of {right_name})",
            "add": (left, RDFS.subClassOf, right),
            "delete": delete_triples,
        },
        {
            "label": f"Subclass relationship ({right_name} is a subclass of {left_name})",
            "add": (right, RDFS.subClassOf, left),
            "delete": delete_triples,
        },
        {
            "label": f"Equivalent classes ({left_name} and {right_name} denote the same class)",
            "add": (left, OWL.equivalentClass, right),
            "delete": delete_triples,
        },
        {
            "label": f"Same individual/entity ({left_name} is the same as {right_name})",
            "add": (left, OWL.sameAs, right),
            "delete": delete_triples,
        },
        {
            "label": f"Keep as custom relationship ({left_name} is related to {right_name})",
            "add": None,
            "delete": None,
        },
        {
            "label": "Leave unresolved",
            "add": None,
            "delete": None,
        },
    ]


def class_options_for_review(graph: Graph) -> list[dict[str, str]]:
    """Return real class IRIs that can be used in expert pair templates.

    The list includes declared classes and URI classes participating in class
    hierarchy/equivalence/disjointness axioms. Blank nodes and generic OWL/RDFS
    top classes are excluded. Labels are display-only; the UI submits full IRIs.
    """
    classes: set[URIRef] = set()
    classes.update(x for x in graph.subjects(RDF.type, OWL.Class) if isinstance(x, URIRef))
    classes.update(x for x in graph.subjects(RDF.type, RDFS.Class) if isinstance(x, URIRef))
    for predicate in (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith):
        for left, right in graph.subject_objects(predicate):
            if isinstance(left, URIRef):
                classes.add(left)
            if isinstance(right, URIRef):
                classes.add(right)

    classes.difference_update({OWL.Thing, OWL.Nothing, RDFS.Resource, RDFS.Class, OWL.Class})
    options = []
    for iri in sorted(classes, key=lambda value: (friendly_entity(graph, str(value)).lower(), str(value))):
        options.append({
            "label": f"{friendly_entity(graph, str(iri))} — {iri}",
            "iri": str(iri),
        })
    return options


def _pair_key(left: str, right: str) -> str:
    ordered = sorted((str(left), str(right)))
    return json.dumps({"left": ordered[0], "right": ordered[1]}, sort_keys=True)


def class_pair_keys(graph: Graph, predicate: URIRef) -> set[str]:
    pairs: set[str] = set()
    for left, right in graph.subject_objects(predicate):
        if isinstance(left, URIRef) and isinstance(right, URIRef) and left != right:
            pairs.add(_pair_key(str(left), str(right)))
    if predicate == OWL.disjointWith:
        for node in graph.subjects(RDF.type, OWL.AllDisjointClasses):
            for list_node in graph.objects(node, OWL.members):
                try:
                    members = [x for x in Collection(graph, list_node) if isinstance(x, URIRef)]
                except Exception:
                    members = []
                for left, right in combinations(members, 2):
                    pairs.add(_pair_key(str(left), str(right)))
    return pairs


def atomic_proposals_from_patch(
    graph: Graph,
    code: str,
    add_ttl: str,
    delete_ttl: str = "",
) -> list[dict[str, str]]:
    """Split multi-axiom semantic proposals into independently selectable units.

    This is intentionally limited to axioms whose meaning is naturally pairwise:
    disjoint classes, equivalent classes, and equivalent properties.
    """
    predicate_by_code = {
        "P10": OWL.disjointWith,
        "P30": OWL.equivalentClass,
        "P12": OWL.equivalentProperty,
    }
    symbol_by_code = {"P10": "\u27c2", "P30": "\u2261", "P12": "\u2261"}
    predicate = predicate_by_code.get(code)
    if predicate is None or not (add_ttl or "").strip():
        return []

    try:
        patch = parse_patch(graph, add_ttl, f"{code} proposed additions")
    except Exception:
        return []

    pairs: set[tuple[URIRef, URIRef]] = set()
    for left, right in patch.subject_objects(predicate):
        if isinstance(left, URIRef) and isinstance(right, URIRef) and left != right:
            ordered = tuple(sorted((left, right), key=str))
            pairs.add(ordered)

    # Support owl:AllDisjointClasses by expanding the member list into pairs.
    if code == "P10":
        for node in patch.subjects(RDF.type, OWL.AllDisjointClasses):
            for list_node in patch.objects(node, OWL.members):
                try:
                    members = [x for x in Collection(patch, list_node) if isinstance(x, URIRef)]
                except Exception:
                    members = []
                for left, right in combinations(members, 2):
                    pairs.add(tuple(sorted((left, right), key=str)))

    proposals: list[dict[str, str]] = []
    for left, right in sorted(pairs, key=lambda pair: (str(pair[0]), str(pair[1]))):
        unit = Graph()
        unit.namespace_manager = graph.namespace_manager
        unit.add((left, predicate, right))
        left_name = friendly_entity(graph, str(left))
        right_name = friendly_entity(graph, str(right))
        label = f"{left_name} {symbol_by_code[code]} {right_name}"
        proposals.append({
            "id": f"{code}|{left}|{right}",
            "label": label,
            "add_ttl": unit.serialize(format="turtle"),
            "delete_ttl": "",
            "left": str(left),
            "right": str(right),
            "source": "LLM-suggested candidate",
        })
    return proposals


def oops_candidate_proposals(graph: Graph, finding: Finding) -> list[dict[str, str]]:
    """Turn concrete OOPS P30 candidate groups into selectable pair proposals.

    OOPS reports candidate groups, not asserted equivalences. For groups with
    more than two classes, expose each pair independently so the user can
    approve only the exact equivalences they support.
    """
    if finding.code != "P30":
        return []
    declared = _declared_class_iris(graph)
    proposals: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group_index, group in enumerate(finding.candidate_groups, start=1):
        classes = [URIRef(value) for value in group if URIRef(value) in declared]
        for left, right in combinations(classes, 2):
            ordered = tuple(sorted((str(left), str(right))))
            if ordered in seen:
                continue
            seen.add(ordered)
            if ((left, OWL.equivalentClass, right) in graph
                    or (right, OWL.equivalentClass, left) in graph):
                continue
            unit = Graph(); unit.namespace_manager = graph.namespace_manager
            unit.add((left, OWL.equivalentClass, right))
            proposals.append({
                "id": f"P30|OOPS|{group_index}|{ordered[0]}|{ordered[1]}",
                "label": f"{friendly_entity(graph, str(left))} ≡ {friendly_entity(graph, str(right))}",
                "add_ttl": unit.serialize(format="turtle"),
                "delete_ttl": "",
                "left": str(left),
                "right": str(right),
                "source": "OOPS-reported candidate",
            })
    return proposals


def merge_atomic_proposals(*proposal_lists: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge proposal sources while preserving OOPS provenance over duplicates."""
    merged: dict[tuple[str, str, str], dict[str, str]] = {}
    for proposals in proposal_lists:
        for proposal in proposals:
            left = str(proposal.get("left") or "")
            right = str(proposal.get("right") or "")
            code = str(proposal.get("id") or "").split("|", 1)[0]
            key = (code, *sorted((left, right)))
            if key not in merged:
                merged[key] = proposal
            elif proposal.get("source", "").startswith("OOPS"):
                merged[key] = proposal
    return list(merged.values())


def named_subclass_relations(graph: Graph) -> list[dict[str, str]]:
    """Return directed named subclass links for P30 strengthening warnings."""
    out: list[dict[str, str]] = []
    for child, parent in graph.subject_objects(RDFS.subClassOf):
        if isinstance(child, URIRef) and isinstance(parent, URIRef) and child != parent:
            out.append({"subclass": str(child), "superclass": str(parent)})
    return out


def _declared_class_iris(graph: Graph) -> set[URIRef]:
    classes: set[URIRef] = set()
    classes.update(x for x in graph.subjects(RDF.type, OWL.Class) if isinstance(x, URIRef))
    classes.update(x for x in graph.subjects(RDF.type, RDFS.Class) if isinstance(x, URIRef))
    for predicate in (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith):
        for left, right in graph.subject_objects(predicate):
            if isinstance(left, URIRef):
                classes.add(left)
            if isinstance(right, URIRef):
                classes.add(right)
    classes.difference_update({OWL.Thing, OWL.Nothing, RDFS.Resource, RDFS.Class, OWL.Class})
    return classes


def _short_names(graph: Graph, values: Iterable[URIRef], limit: int = 5) -> str:
    names = [friendly_entity(graph, str(value)) for value in sorted(set(values), key=str)]
    if not names:
        return "none"
    clipped = names[:limit]
    suffix = " …" if len(names) > limit else ""
    return ", ".join(clipped) + suffix


def class_pair_evidence(graph: Graph, left: URIRef, right: URIRef) -> str:
    """Build concise, inspectable evidence for a candidate class pair."""
    left_label = friendly_entity(graph, str(left))
    right_label = friendly_entity(graph, str(right))
    left_props = {p for p in graph.subjects(RDFS.domain, left) if isinstance(p, URIRef)}
    right_props = {p for p in graph.subjects(RDFS.domain, right) if isinstance(p, URIRef)}
    common_props = left_props & right_props
    left_supers = {o for o in graph.objects(left, RDFS.subClassOf) if isinstance(o, URIRef)}
    right_supers = {o for o in graph.objects(right, RDFS.subClassOf) if isinstance(o, URIRef)}
    common_supers = left_supers & right_supers
    relations: list[str] = []
    if (left, OWL.equivalentClass, right) in graph or (right, OWL.equivalentClass, left) in graph:
        relations.append("already equivalent")
    if (left, OWL.disjointWith, right) in graph or (right, OWL.disjointWith, left) in graph:
        relations.append("already disjoint")
    if (left, RDFS.subClassOf, right) in graph:
        relations.append(f"{left_label} is a subclass of {right_label}")
    if (right, RDFS.subClassOf, left) in graph:
        relations.append(f"{right_label} is a subclass of {left_label}")
    pieces = [
        f"Labels: {left_label} / {right_label}",
        f"shared domain properties: {_short_names(graph, common_props)}",
        f"common named superclasses: {_short_names(graph, common_supers)}",
    ]
    if relations:
        pieces.append("existing relation: " + "; ".join(relations))
    return "; ".join(pieces) + "."


def candidate_evidence_for_review(
    graph: Graph,
    finding: Finding,
    atomic_proposals: list[dict[str, str]],
    rationale: str = "",
) -> list[dict[str, str]]:
    """Return source-labelled evidence for P10/P30 class-pair review.

    Concrete P30 groups returned by OOPS are shown first and preserved as
    independently selectable pair candidates. LLM suggestions are labelled
    separately. Ontology-wide findings are never presented as if OOPS named a
    concrete pair.
    """
    if finding.code not in {"P10", "P30"}:
        return []

    evidence: list[dict[str, str]] = []
    classes = _declared_class_iris(graph)

    concrete_groups = []
    for group in finding.candidate_groups:
        filtered = [URIRef(value) for value in group if URIRef(value) in classes]
        if len(filtered) >= 2:
            concrete_groups.append(filtered)

    if concrete_groups:
        symbol = "⟂" if finding.code == "P10" else "≡"
        for index, group in enumerate(concrete_groups, start=1):
            names = [friendly_entity(graph, str(value)) for value in group]
            pair_summaries = [
                class_pair_evidence(graph, left, right)
                for left, right in combinations(group, 2)
            ]
            evidence.append({
                "source": "OOPS-reported candidate group",
                "label": f"Group {index}: " + f" {symbol} ".join(names),
                "summary": (
                    "OOPS returned these classes as a possible duplicate-concept "
                    "group. OOPS does not assert equivalence; approve only the "
                    "individual pairs whose meanings are exactly identical. "
                    + " ".join(pair_summaries)
                ),
            })
    else:
        affected_classes = [URIRef(value) for value in finding.affected if URIRef(value) in classes]
        if len(affected_classes) == 2:
            left, right = affected_classes
            symbol = "⟂" if finding.code == "P10" else "≡"
            evidence.append({
                "source": "OOPS-returned classes",
                "label": f"{friendly_entity(graph, str(left))} {symbol} {friendly_entity(graph, str(right))}",
                "left": str(left),
                "right": str(right),
                "summary": (
                    "OOPS returned these two affected class IRIs, but did not itself "
                    "assert the proposed axiom. The pair still requires semantic review. "
                    + class_pair_evidence(graph, left, right)
                ),
            })
        elif affected_classes:
            evidence.append({
                "source": "OOPS scope",
                "label": f"{len(affected_classes)} affected classes",
                "summary": (
                    "OOPS returned affected classes but no explicit candidate grouping. "
                    "The app does not expand them into all pairwise axioms automatically."
                ),
            })
        else:
            evidence.append({
                "source": "OOPS scope",
                "label": "Ontology-wide finding",
                "summary": (
                    "OOPS reported this pattern for the ontology but did not identify a "
                    "specific class pair in the REST response."
                ),
            })

    llm_proposals = [
        proposal for proposal in atomic_proposals
        if str(proposal.get("source") or "").startswith("LLM")
    ]
    for proposal in llm_proposals:
        left_s = proposal.get("left", "")
        right_s = proposal.get("right", "")
        if not left_s or not right_s:
            continue
        left = URIRef(left_s); right = URIRef(right_s)
        evidence.append({
            "source": "LLM-suggested candidate",
            "label": proposal.get("label", f"{left_s} / {right_s}"),
            "left": left_s,
            "right": right_s,
            "proposal_id": proposal.get("id", ""),
            "summary": class_pair_evidence(graph, left, right) + (f" LLM rationale: {rationale}" if rationale else ""),
        })

    if not llm_proposals:
        evidence.append({
            "source": "LLM assessment",
            "label": "No additional supported candidate pair proposed",
            "summary": (
                rationale or
                "The LLM did not find enough evidence to recommend another concrete pair. "
                "The OOPS candidates above and expert dropdowns remain available."
            ),
        })
    return evidence


def default_question(graph: Graph, finding: Finding, rationale: str = "") -> tuple[str, list[str], str, str, str]:
    names = [friendly_entity(graph, x) for x in finding.affected[:3]]
    joined = ", ".join(names) or "the flagged ontology element"
    templates = {
        "P05": (f"Are the relationships involving {joined} exact inverses?", ["Keep the inverse relationship", "Remove the inverse relationship", "Review details"], "Review details"),
        "P06": (f"Which hierarchy link should be removed to break the cycle involving {joined}?", ["Apply the proposed minimal removal", "Keep the hierarchy", "Review details"], "Review details"),
        "P10": ("Select only the class pairs that must be mutually exclusive.", ["Add selected disjoint axioms", "Add none", "Review details"], "Add none"),
        "P11": (f"What domain and range should be used for {joined}?", ["Use the suggested domain and range", "Keep them unspecified", "Enter different values"], "Use the suggested domain and range"),
        "P12": ("Select only the property pairs that mean exactly the same thing.", ["Add selected equivalences", "Keep all distinct", "Review details"], "Keep all distinct"),
        "P18": (f"Is the current domain or range for {joined} too narrow?", ["Apply the proposed broader type", "Keep the current type", "Review details"], "Review details"),
        "P24": (f"Is the recursive relation involving {joined} intentional?", ["Keep the recursive modeling", "Apply the proposed minimal removal", "Review details"], "Keep the recursive modeling"),
        "P26": (f"For {joined}, should symmetry or the separate inverse relationship be kept?", ["Keep symmetry", "Keep the separate inverse", "Review details"], "Review details"),
        "P27": (f"Do the properties involving {joined} have exactly the same meaning?", ["Keep equivalence", "Apply the proposed correction", "Review details"], "Review details"),
        "P28": (f"Must {joined} always hold in both directions?", ["Keep symmetric", "Remove symmetry", "Review details"], "Review details"),
        "P29": (f"Can {joined} always be chained transitively?", ["Keep transitive", "Remove transitivity", "Review details"], "Review details"),
        "P30": ("Select only the class pairs that describe exactly the same concept.", ["Add selected equivalences", "Keep all distinct", "Review details"], "Keep all distinct"),
        "P31": (f"Are the classes involving {joined} truly equivalent?", ["Keep equivalence", "Apply the proposed correction", "Review details"], "Review details"),
        "P37": ("Should the ontology be published at its declared stable web address?", ["Add this to the deployment checklist", "I will publish it now", "Ignore for local use"], "Add this to the deployment checklist"),
        "P39": ("What stable base IRI should this ontology use?", ["Use the suggested base IRI", "Enter another base IRI", "Keep file-based namespace"], "Use the suggested base IRI"),
        "P40": (f"The ontology appears to define {joined} in a namespace it does not control. Which local term should replace it?", ["Use the proposed local term", "Enter another IRI", "Review details"], "Review details"),
    }
    question, choices, recommended = templates.get(
        finding.code,
        (f"How should the app handle {finding.code} for {joined}?", ["Apply the proposed repair", "Keep current modeling", "Review details"], "Review details"),
    )
    custom_kind = "domain_range" if finding.code == "P11" else ("iri" if finding.code in {"P39", "P40"} else "none")
    custom_label = "Domain IRI | Range IRI" if custom_kind == "domain_range" else ("Stable IRI" if custom_kind == "iri" else "")
    return question, choices, recommended, custom_kind, custom_label


def make_review_item(graph: Graph, finding: Finding, assessment: dict[str, Any] | None, validation_notes: list[str], context: str) -> ReviewItem:
    assessment = assessment or {}
    q, choices, recommended, custom_kind, custom_label = default_question(graph, finding, assessment.get("rationale", ""))
    if assessment.get("question"):
        q = assessment["question"]
    if assessment.get("choices"):
        choices = assessment["choices"]
    if assessment.get("recommended") in choices:
        recommended = assessment["recommended"]
    if assessment.get("custom_input_kind") not in {None, "", "none"}:
        custom_kind = assessment["custom_input_kind"]
        custom_label = assessment.get("custom_input_label") or custom_label

    llm_atomic_proposals = atomic_proposals_from_patch(
        graph, finding.code, assessment.get("add_ttl", ""), assessment.get("delete_ttl", "")
    )
    atomic_proposals = merge_atomic_proposals(
        oops_candidate_proposals(graph, finding),
        llm_atomic_proposals,
    )
    candidate_evidence = candidate_evidence_for_review(
        graph, finding, atomic_proposals, assessment.get("rationale", "")
    )

    # P03/P10/P30 controls are fixed UI templates, not LLM-authored options.
    # The LLM may provide rationale/proposals, but it never decides button
    # labels or omits direction-sensitive semantic alternatives.
    entity_options: list[dict[str, str]] = []
    pair_relation = ""
    if finding.code == "P03":
        specs = p03_choice_specs(graph, finding.affected)
        if specs:
            left, _, right, _ = p03_relation_basis(graph, finding.affected)
            q = (
                f"What does the custom 'is' relationship between "
                f"{friendly_entity(graph, str(left))} and "
                f"{friendly_entity(graph, str(right))} mean?"
            )
            choices = [spec["label"] for spec in specs]
            recommended = "Leave unresolved"
            custom_kind = "none"
            custom_label = ""
    elif finding.code == "P10":
        q = "Select or add only class pairs that must be mutually exclusive."
        choices = ["Add selected disjoint axioms", "Add none", "Leave unresolved"]
        recommended = "Add none"
        custom_kind = "none"
        custom_label = ""
        entity_options = class_options_for_review(graph)
        pair_relation = "disjoint"
    elif finding.code == "P30":
        q = "Select or add only class pairs that describe exactly the same concept."
        choices = ["Add selected equivalences", "Keep all distinct", "Leave unresolved"]
        recommended = "Keep all distinct"
        custom_kind = "none"
        custom_label = ""
        entity_options = class_options_for_review(graph)
        pair_relation = "equivalent"
    elif finding.code == "P12" and atomic_proposals:
        q = "Select only the property pairs that mean exactly the same thing."
        choices = ["Add selected equivalences", "Keep all distinct", "Leave unresolved"]
        recommended = "Keep all distinct"

    manual_pair_supported = finding.code in {"P10", "P30"} and len(entity_options) >= 2
    has_executable_patch = bool(
        atomic_proposals
        or manual_pair_supported
        or assessment.get("add_ttl", "").strip()
        or assessment.get("delete_ttl", "").strip()
        or (finding.code == "P03" and bool(p03_choice_specs(graph, finding.affected)))
        or (finding.code == "P24" and p24_candidate_exists(graph, finding.affected))
    )
    if not has_executable_patch:
        apply_words = ("apply", "remove", "add", "mark", "use the proposed")
        choices = [
            choice for choice in choices
            if not choice.strip().lower().startswith(apply_words)
        ]
        if "Leave unresolved" not in choices:
            choices.insert(max(1, len(choices) - 1), "Leave unresolved")
        if recommended not in choices:
            recommended = choices[0] if choices else "Leave unresolved"
        validation_notes = list(validation_notes) + [
            "No executable RDF patch is available; an apply option was not shown."
        ]
    existing_pair_keys: list[str] = []
    conflicting_pair_keys: list[str] = []
    subclass_relations: list[dict[str, str]] = []
    if finding.code == "P10":
        existing_pair_keys = sorted(class_pair_keys(graph, OWL.disjointWith))
        conflicting_pair_keys = sorted(class_pair_keys(graph, OWL.equivalentClass))
    elif finding.code == "P30":
        existing_pair_keys = sorted(class_pair_keys(graph, OWL.equivalentClass))
        conflicting_pair_keys = sorted(class_pair_keys(graph, OWL.disjointWith))
        subclass_relations = named_subclass_relations(graph)

    return ReviewItem(
        id=str(uuid.uuid4()), code=finding.code, severity=finding.severity,
        title=f"{finding.code} — {finding.name}", question=q,
        explanation=assessment.get("rationale") or finding.description,
        affected=finding.affected, choices=choices, recommended=recommended,
        proposed_add_ttl=assessment.get("add_ttl", ""),
        proposed_delete_ttl=assessment.get("delete_ttl", ""),
        atomic_proposals=atomic_proposals,
        candidate_evidence=candidate_evidence,
        entity_options=entity_options, pair_relation=pair_relation,
        existing_pair_keys=existing_pair_keys,
        conflicting_pair_keys=conflicting_pair_keys,
        subclass_relations=subclass_relations,
        custom_input_kind=custom_kind, custom_input_label=custom_label,
        validation_notes=validation_notes,
        context_excerpt=context[:5000],
    )


def write_reports(
    out_dir: str | Path,
    source_label: str,
    applied: list[dict[str, Any]],
    reviews: list[ReviewItem],
    unresolved: list[dict[str, Any]],
    final_summary: dict[str, int],
) -> tuple[str, str]:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source_label,
        "applied": applied,
        "needs_user_decision": [asdict(x) for x in reviews],
        "unresolved": unresolved,
        "final_pitfall_summary": final_summary,
    }
    json_path = out / "pitfall_repair_report.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = ["# Ontology Repair Report", "", f"Source: `{source_label}`", ""]
    lines += [f"- Automatically fixed: **{len(applied)}**", f"- Needs user decision: **{len(reviews)}**", f"- Unresolved/report-only: **{len(unresolved)}**", ""]
    if applied:
        lines += ["## Automatically fixed"]
        for item in applied:
            lines.append(f"- **{item['code']}**: {item.get('note', 'verified repair')} ({item.get('before', '?')} → {item.get('after', '?')})")
        lines.append("")
    if reviews:
        lines += ["## Needs your decision"]
        for item in reviews:
            lines += [f"### {item.title}", item.question, f"Affected: {', '.join(item.affected) or 'ontology-wide'}", ""]
    if unresolved:
        lines += ["## Could not fix automatically"]
        for item in unresolved:
            lines += [f"- **{item.get('code')}**: {item.get('reason')}"]
    lines += ["", "## Remaining Critical/Important findings", "", "```json", json.dumps(final_summary, indent=2), "```"]
    md_path = out / "pitfall_repair_report.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return str(json_path), str(md_path)


# ---------------------------------------------------------------------------
# Main repair orchestration
# ---------------------------------------------------------------------------
def _trusted_affected(graph: Graph, finding: Finding) -> list[str]:
    return [x for x in finding.affected if uri_occurs(graph, URIRef(x))]


def repair_pitfalls(
    ontology_graph: Graph,
    *,
    model: str | None = None,
    max_iterations: int = MAX_REPAIR_ITERATIONS,
    source_label: str = "ontology",
    out_dir: str | Path | None = None,
    progress=None,
    importance_levels: Iterable[str] = DEFAULT_IMPORTANCE_LEVELS,
    java_exe: str = "java",
    robot_jar: str | None = None,
    license_uri: str = DEFAULT_LICENSE_URI,
) -> RepairResult:
    if model:
        api_utils.set_model(model)
    levels = tuple(x.lower() for x in importance_levels)
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="rigor_oops_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    applied: list[dict[str, Any]] = []
    reviews: list[ReviewItem] = []
    unresolved: list[dict[str, Any]] = []
    reviewed_signatures: set[tuple[str, tuple[str, ...]]] = set()

    def emit(message: str):
        print(message, flush=True)
        if progress:
            try: progress(message)
            except Exception: pass

    graph = clone_graph(ontology_graph)
    for iteration in range(1, max_iterations + 1):
        emit(f"OOPS repair pass {iteration}/{max_iterations}")
        oops_graph = run_oops(graph)
        findings = merge_findings(findings_from_oops(oops_graph, levels), local_findings(graph, levels))
        if not findings:
            emit("No Critical/Important pitfalls remain.")
            break
        findings.sort(key=lambda f: (0 if f.severity == "critical" else 1, f.code))
        emit(f"Detected: {summary_from_findings(findings)}")
        committed = False

        for finding in findings:
            policy = PITFALL_POLICIES[finding.code]
            finding.affected = _trusted_affected(graph, finding)
            signature = (finding.code, tuple(sorted(finding.affected)))

            if policy.mode == "human_only":
                if signature not in reviewed_signatures:
                    reviews.append(make_review_item(graph, finding, None, ["This is a publishing/deployment decision, not a safe graph mutation."], ontology_summary(graph)))
                    reviewed_signatures.add(signature)
                continue

            add_ttl, delete_ttl, note = deterministic_patch(graph, finding, license_uri)
            if add_ttl or delete_ttl:
                allowed_new: list[URIRef] = []
                if finding.code == "P41":
                    allowed_new.append(URIRef(license_uri))
                if finding.code in {"P38", "P39", "P41"}:
                    node, minted = _resolve_ontology_node(graph)
                    if minted and node:
                        allowed_new.append(node)
                validation = validate_patch(
                    graph, finding, add_ttl, delete_ttl, findings,
                    levels=levels, robot_jar=robot_jar, java_exe=java_exe,
                    allowed_new=allowed_new,
                )
                if validation.valid and validation.auto_safe and validation.graph is not None:
                    before = summary_from_findings(findings).get(finding.code, 0)
                    after = validation.after_summary.get(finding.code, 0)
                    graph = validation.graph
                    applied.append({"code": finding.code, "kind": "deterministic", "note": note, "before": before, "after": after})
                    emit(f"Applied deterministic {finding.code}: {before} → {after}")
                    committed = True
                    break
                if validation.valid and not validation.auto_safe:
                    if signature not in reviewed_signatures:
                        assessment = {"rationale": note, "add_ttl": add_ttl, "delete_ttl": delete_ttl}
                        reviews.append(make_review_item(graph, finding, assessment, validation.reasons, context_for_finding(graph, finding, policy)))
                        reviewed_signatures.add(signature)
                    continue
                # If deterministic fix failed OOPS/logic validation, fall through
                # to LLM with the validation feedback unless policy is strictly deterministic.
                deterministic_feedback = "; ".join(validation.reasons)
            else:
                deterministic_feedback = note

            context = context_for_finding(graph, finding, policy)
            if not api_utils.API_KEY:
                if finding.code == "P11":
                    unresolved.append({
                        "code": finding.code, "severity": finding.severity,
                        "affected": finding.affected,
                        "reason": "Automatic P11 repair remained incomplete and no OpenRouter API key was configured for the fallback assessment.",
                    })
                elif signature not in reviewed_signatures:
                    reviews.append(make_review_item(
                        graph, finding, None,
                        ["No OpenRouter API key was configured, so semantic assessment was not attempted."],
                        context,
                    ))
                    reviewed_signatures.add(signature)
                continue

            last_assessment: dict[str, Any] | None = None
            feedback = deterministic_feedback
            for attempt in range(1, MAX_LLM_ATTEMPTS_PER_FINDING + 1):
                emit(f"Calling {api_utils.MODEL} for {finding.code} (attempt {attempt}/{MAX_LLM_ATTEMPTS_PER_FINDING})")
                assessment = llm_assess_finding(finding, policy, context, feedback)
                last_assessment = assessment
                if assessment["action"] != "REPAIR":
                    break
                if not assessment["add_ttl"] and not assessment["delete_ttl"]:
                    feedback = "You selected REPAIR but returned an empty patch."
                    continue
                validation = validate_patch(
                    graph, finding, assessment["add_ttl"], assessment["delete_ttl"], findings,
                    levels=levels, robot_jar=robot_jar, java_exe=java_exe,
                )
                if validation.valid and validation.graph is not None:
                    if policy.auto_apply_llm and validation.auto_safe:
                        before = summary_from_findings(findings).get(finding.code, 0)
                        after = validation.after_summary.get(finding.code, 0)
                        graph = validation.graph
                        applied.append({"code": finding.code, "kind": "llm", "note": assessment["rationale"], "before": before, "after": after})
                        emit(f"Applied validated LLM repair for {finding.code}: {before} → {after}")
                        committed = True
                        break
                    if signature not in reviewed_signatures:
                        reviews.append(make_review_item(graph, finding, assessment, validation.reasons, context))
                        reviewed_signatures.add(signature)
                    break
                feedback = "; ".join(validation.reasons)
            if committed:
                break

            if signature in reviewed_signatures:
                continue
            if last_assessment and last_assessment["action"] in {"KEEP_INTENTIONAL", "ASK_USER"}:
                if finding.code == "P11":
                    unresolved.append({
                        "code": finding.code, "severity": finding.severity,
                        "affected": finding.affected,
                        "reason": "P11 requires automatic handling; the LLM did not produce a validated repair, so it was reported rather than shown as a user question.",
                    })
                else:
                    reviews.append(make_review_item(graph, finding, last_assessment, [], context))
                    reviewed_signatures.add(signature)
            else:
                unresolved.append({
                    "code": finding.code, "severity": finding.severity,
                    "affected": finding.affected,
                    "reason": feedback or "No safe deterministic or validated LLM repair was produced.",
                })

        if not committed:
            # Every current finding has either been queued for a focused user
            # decision or recorded as unresolved. Repeating would only duplicate
            # model calls and questions.
            break
        time.sleep(1)

    final_oops = run_oops(graph)
    final_findings = merge_findings(findings_from_oops(final_oops, levels), local_findings(graph, levels))
    final_summary = summary_from_findings(final_findings)
    report_json, report_md = write_reports(out_dir, source_label, applied, reviews, unresolved, final_summary)
    return RepairResult(graph, applied, reviews, unresolved, final_summary, report_json, report_md)


def fix_pitfalls(
    ontology_graph: Graph,
    model: str | None = None,
    max_iterations: int = MAX_REPAIR_ITERATIONS,
    outfile: str | None = None,
    source_label: str = "ontology",
    progress=None,
    importance_levels: Iterable[str] = DEFAULT_IMPORTANCE_LEVELS,
    verify_consistency: bool = True,
    java_exe: str = "java",
    robot_jar: str | None = None,
    license_uri: str = DEFAULT_LICENSE_URI,
    **_: Any,
) -> Graph:
    """Backward-compatible wrapper returning only the repaired graph."""
    out_dir = Path(outfile).parent if outfile else None
    return repair_pitfalls(
        ontology_graph, model=model, max_iterations=max_iterations,
        source_label=source_label, out_dir=out_dir, progress=progress,
        importance_levels=importance_levels, java_exe=java_exe,
        robot_jar=robot_jar, license_uri=license_uri,
    ).graph


# ---------------------------------------------------------------------------
# User decision application
# ---------------------------------------------------------------------------
def _expand_curie_or_iri(graph: Graph, value: str) -> URIRef:
    value = value.strip()
    if value.startswith(("http://", "https://", "urn:")):
        return URIRef(value)
    if ":" in value:
        prefix, local = value.split(":", 1)
        for p, ns in graph.namespaces():
            if p == prefix:
                return URIRef(str(ns) + local)
    # Accept a local name only if exactly one existing URI matches.
    matches = [uri for uri in all_uris(graph) if local_name(uri) == value]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"Could not resolve '{value}' to one existing IRI")


RECURSIVE_LINK_PREDICATES = (RDFS.subClassOf, OWL.equivalentClass)
RECURSIVE_FILLER_PREDICATES = (
    OWL.allValuesFrom,
    OWL.someValuesFrom,
    OWL.onClass,
    OWL.hasValue,
)


def _bnode_closure_contains(graph: Graph, root: BNode, target: URIRef, max_depth: int = 12) -> bool:
    seen: set[BNode] = set()
    frontier: list[tuple[BNode, int]] = [(root, 0)]
    while frontier:
        node, depth = frontier.pop()
        if node in seen or depth > max_depth:
            continue
        seen.add(node)
        for _, _, obj in graph.triples((node, None, None)):
            if obj == target:
                return True
            if isinstance(obj, BNode):
                frontier.append((obj, depth + 1))
    return False


def _remove_orphan_bnode_closure(graph: Graph, root: BNode) -> int:
    """Remove a blank-node axiom closure only when it has no remaining owners."""
    if any(graph.triples((None, None, root))):
        return 0
    removed = 0
    outgoing = list(graph.triples((root, None, None)))
    children = [o for _, _, o in outgoing if isinstance(o, BNode)]
    for triple in outgoing:
        graph.remove(triple)
        removed += 1
    for child in children:
        removed += _remove_orphan_bnode_closure(graph, child)
    return removed


def p24_candidate_exists(graph: Graph, affected: Iterable[str]) -> bool:
    trial, removed, _ = build_p24_user_candidate(graph, affected)
    return trial is not None and removed > 0


def build_p24_user_candidate(
    graph: Graph,
    affected: Iterable[str],
) -> tuple[Graph | None, int, str]:
    """Build the concrete user-approved P24 removal directly on a graph clone.

    The repair removes only self-referential definition axioms. It preserves the
    class/property declarations, domains/ranges, provenance, alignments, and the
    underlying relation itself. Blank-node restrictions are matched structurally
    rather than by unstable Turtle blank-node identifiers.
    """
    trial = clone_graph(graph)
    removed = 0
    details: list[str] = []
    targets = [URIRef(x) for x in affected if x]
    for target in targets:
        # Direct tautological/self-definition triples.
        direct = (
            (target, RDFS.subClassOf, target),
            (target, OWL.equivalentClass, target),
            (target, OWL.equivalentProperty, target),
            (target, OWL.inverseOf, target),
            (target, RDFS.domain, target),
            (target, RDFS.range, target),
        )
        for triple in direct:
            if triple in trial:
                trial.remove(triple)
                removed += 1
                details.append(f"removed direct self-definition {triple[1]}")

        # Anonymous class restrictions/expressions containing the class itself.
        for link_pred in RECURSIVE_LINK_PREDICATES:
            for node in list(trial.objects(target, link_pred)):
                if not isinstance(node, BNode):
                    continue
                if not _bnode_closure_contains(trial, node, target):
                    continue
                # Prefer clear restriction patterns; nested equivalent/subclass
                # expressions are still removed at the single owning-axiom level.
                trial.remove((target, link_pred, node))
                removed += 1
                removed += _remove_orphan_bnode_closure(trial, node)
                details.append(
                    f"removed one recursive anonymous axiom linked by {link_pred}"
                )

    if removed == 0:
        return None, 0, "No executable self-referential axiom was found."
    return trial, removed, "; ".join(details)


def build_p26_user_candidate(
    graph: Graph,
    affected: Iterable[str],
    choice: str,
) -> tuple[Graph | None, int, str]:
    """Apply the user's P26 semantics choice as a minimal exact deletion."""
    trial = clone_graph(graph)
    targets = [URIRef(x) for x in affected if x]
    if not targets:
        targets = find_p26_properties(graph)
    removed = 0
    details: list[str] = []
    for prop in targets:
        if choice == "Keep symmetry":
            inverse_triples = list(trial.triples((prop, OWL.inverseOf, None)))
            inverse_triples += list(trial.triples((None, OWL.inverseOf, prop)))
            for triple in inverse_triples:
                other = triple[2] if triple[0] == prop else triple[0]
                if isinstance(other, URIRef) and other != prop and triple in trial:
                    trial.remove(triple)
                    removed += 1
                    details.append(f"removed inverse axiom {triple[0]} owl:inverseOf {triple[2]}")
        elif choice == "Keep the separate inverse":
            triple = (prop, RDF.type, OWL.SymmetricProperty)
            if triple in trial:
                trial.remove(triple)
                removed += 1
                details.append(f"removed owl:SymmetricProperty from {prop}")
        else:
            return None, 0, f"Unsupported P26 choice: {choice}"
    if removed == 0:
        return None, 0, "No matching P26 axiom was found for the selected choice."
    return trial, removed, "; ".join(details)


def build_user_approved_candidate(
    graph: Graph,
    item: ReviewItem,
) -> tuple[Graph | None, str]:
    if item.code == "P24":
        trial, removed, note = build_p24_user_candidate(graph, item.affected)
        if trial is None:
            return None, note
        return trial, f"Prepared structural P24 removal ({removed} triples/links removed): {note}"
    return None, "No executable patch was stored for this decision."


def custom_patch_for_review(graph: Graph, item: ReviewItem, custom_value: str) -> tuple[str, str]:
    if item.custom_input_kind == "domain_range":
        parts = [x.strip() for x in custom_value.split("|")]
        if len(parts) != 2 or not item.affected:
            raise ValueError("Enter domain and range as: DomainIRI | RangeIRI")
        prop = URIRef(item.affected[0])
        domain = _expand_curie_or_iri(graph, parts[0])
        range_ = _expand_curie_or_iri(graph, parts[1])
        patch = Graph(); patch.add((prop, RDFS.domain, domain)); patch.add((prop, RDFS.range, range_))
        return patch.serialize(format="turtle"), ""
    if item.custom_input_kind == "iri":
        raise ValueError("Custom namespace rewrites require an explicit expert patch and are not applied from a single IRI field yet.")
    return item.proposed_add_ttl, item.proposed_delete_ttl


def apply_user_decisions(
    graph: Graph,
    review_items: list[ReviewItem] | list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    robot_jar: str | None,
    java_exe: str = "java",
    importance_levels: Iterable[str] = DEFAULT_IMPORTANCE_LEVELS,
    progress_cb=None,
) -> tuple[Graph, list[dict[str, Any]]]:
    items = [x if isinstance(x, ReviewItem) else ReviewItem(**x) for x in review_items]
    by_id = {x.id: x for x in items}
    outcomes: list[dict[str, Any]] = []
    current = clone_graph(graph)

    def emit(message: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(str(message))
            except Exception:
                pass

    total_decisions = len(decisions)
    for decision_index, decision in enumerate(decisions, start=1):
        item = by_id.get(decision.get("id", ""))
        if not item:
            continue
        selected = decision.get("choice", "")
        custom = decision.get("custom", "").strip()
        emit(
            f"Decision {decision_index}/{total_decisions}: {item.code} — "
            f"{selected or 'no choice'}"
        )
        selected_atomic = decision.get("selected_atomic", []) or []
        manual_pairs = decision.get("manual_pairs", []) or []
        subclass_mode = str(decision.get("subclass_mode") or "cancel").strip().lower()
        if isinstance(selected_atomic, str):
            try:
                selected_atomic = json.loads(selected_atomic)
            except Exception:
                selected_atomic = [selected_atomic] if selected_atomic else []
        if isinstance(manual_pairs, str):
            try:
                manual_pairs = json.loads(manual_pairs)
            except Exception:
                manual_pairs = [manual_pairs] if manual_pairs else []
        if (
            selected in {"Keep current modeling", "Keep the recursive modeling", "Keep the hierarchy", "They may overlap", "They are related but not equivalent", "Keep them unspecified", "Keep symmetric", "Keep transitive", "Keep them distinct", "Keep all distinct", "Add none", "Keep equivalence", "Ignore for local use"}
            or selected.startswith("Keep as custom relationship (")
        ):
            outcomes.append({"id": item.id, "code": item.code, "status": "retained", "choice": selected})
            emit(f"{item.code}: retained without ontology changes.")
            continue
        if selected in {"Review details", "Leave unresolved", ""}:
            outcomes.append({"id": item.id, "code": item.code, "status": "unresolved", "choice": selected})
            emit(f"{item.code}: left unresolved.")
            continue
        try:
            # P26 choices are both executable: either remove the inverse axiom
            # and retain symmetry, or remove the symmetric characteristic and
            # retain the separate inverse relationship.
            if item.code == "P26":
                candidate, removed, candidate_note = build_p26_user_candidate(
                    current, item.affected, selected
                )
                if candidate is None:
                    outcomes.append({
                        "id": item.id, "code": item.code, "status": "rejected",
                        "choice": selected, "reasons": [candidate_note],
                    })
                    continue
                emit(f"{item.code}: running OOPS and logical validation...")
                baseline_oops = run_oops(current)
                findings = merge_findings(
                    findings_from_oops(baseline_oops, importance_levels),
                    local_findings(current, importance_levels),
                )
                finding = next(
                    (f for f in findings if f.code == item.code),
                    Finding(item.code, item.title, item.severity, affected=item.affected),
                )
                validation = validate_candidate_graph(
                    current, candidate, finding, findings,
                    levels=importance_levels, robot_jar=robot_jar,
                    java_exe=java_exe, user_approved=True,
                )
                validation.reasons.insert(0, candidate_note)
                if validation.valid and validation.graph is not None:
                    current = validation.graph
                    outcomes.append({
                        "id": item.id, "code": item.code, "status": "applied",
                        "choice": selected, "removed_axioms": removed,
                    })
                else:
                    outcomes.append({
                        "id": item.id, "code": item.code, "status": "rejected",
                        "choice": selected, "reasons": validation.reasons,
                    })
                continue

            # P03 uses a stable template with both direction-sensitive
            # permutations. Convert the chosen meaning into an executable
            # add/delete patch before entering the common validation path.
            if item.code == "P03":
                spec = next(
                    (candidate for candidate in p03_choice_specs(current, item.affected)
                     if candidate["label"] == selected),
                    None,
                )
                if spec is None or spec.get("add") is None or spec.get("delete") is None:
                    outcomes.append({
                        "id": item.id, "code": item.code, "status": "unresolved",
                        "choice": selected,
                        "reasons": ["No executable P03 semantic option was selected."],
                    })
                    continue
                add_graph = Graph(); add_graph.namespace_manager = current.namespace_manager
                delete_graph = Graph(); delete_graph.namespace_manager = current.namespace_manager
                add_graph.add(spec["add"])
                delete_value = spec.get("delete")
                if isinstance(delete_value, tuple) and len(delete_value) == 3:
                    delete_graph.add(delete_value)
                else:
                    for triple in (delete_value or []):
                        delete_graph.add(triple)
                add_ttl = add_graph.serialize(format="turtle")
                delete_ttl = delete_graph.serialize(format="turtle")
            else:
                add_ttl = delete_ttl = ""

            def handle_p30_subclass(left_s: str, right_s: str, mode: str, delete_graph: Graph) -> None:
                """Apply the user's explicit policy for an existing subclass link."""
                if item.code != "P30":
                    return
                left = URIRef(left_s); right = URIRef(right_s)
                directed = []
                if (left, RDFS.subClassOf, right) in current:
                    directed.append((left, RDFS.subClassOf, right))
                if (right, RDFS.subClassOf, left) in current:
                    directed.append((right, RDFS.subClassOf, left))
                if not directed:
                    return
                normalised = mode.replace(" ", "_")
                if normalised in {"cancel", "cancel_if_subclass", ""}:
                    child, _, parent = directed[0]
                    raise ValueError(
                        f"{friendly_entity(current, str(child))} is already a subclass of "
                        f"{friendly_entity(current, str(parent))}. Choose whether to keep "
                        "that subclass axiom while adding equivalence, replace it with "
                        "equivalence, or cancel."
                    )
                if normalised in {"replace", "replace_subclass", "replace_with_equivalence"}:
                    for triple in directed:
                        delete_graph.add(triple)
                elif normalised not in {"keep", "keep_subclass", "add_equivalence_anyway"}:
                    raise ValueError(f"Unknown subclass-handling option: {mode}")

            if item.code == "P03":
                pass  # add_ttl/delete_ttl were built from the selected permutation above.
            elif selected.startswith("Add selected"):
                chosen = [
                    proposal for proposal in item.atomic_proposals
                    if proposal.get("label") in selected_atomic or proposal.get("id") in selected_atomic
                ]
                add_graph = Graph()
                add_graph.namespace_manager = current.namespace_manager
                delete_graph = Graph()
                delete_graph.namespace_manager = current.namespace_manager
                for proposal in chosen:
                    if item.code == "P30" and proposal.get("left") and proposal.get("right"):
                        handle_p30_subclass(
                            str(proposal["left"]), str(proposal["right"]),
                            subclass_mode, delete_graph,
                        )
                    if (proposal.get("add_ttl") or "").strip():
                        for triple in parse_patch(current, proposal["add_ttl"], f"{item.code} selected axiom"):
                            add_graph.add(triple)
                    if (proposal.get("delete_ttl") or "").strip():
                        for triple in parse_patch(current, proposal["delete_ttl"], f"{item.code} selected deletion"):
                            delete_graph.add(triple)

                # Expert-added class pairs are encoded as JSON values by the UI.
                # Resolve them only against the class options captured from the
                # actual ontology; arbitrary/new IRIs are never accepted.
                allowed_classes = {opt.get("iri") for opt in item.entity_options if opt.get("iri")}
                pair_predicate = {"P10": OWL.disjointWith, "P30": OWL.equivalentClass}.get(item.code)
                accepted_manual: list[str] = []
                for encoded in manual_pairs:
                    try:
                        pair = json.loads(encoded) if isinstance(encoded, str) else encoded
                        left = str(pair["left"]); right = str(pair["right"])
                    except Exception as exc:
                        raise ValueError(f"Invalid expert pair value: {encoded!r}") from exc
                    if pair_predicate is None:
                        raise ValueError(f"Manual class-pair input is not supported for {item.code}.")
                    pair_mode = str(pair.get("subclass_mode") or subclass_mode or "cancel")
                    if left == right:
                        raise ValueError("A class cannot be paired with itself.")
                    if left not in allowed_classes or right not in allowed_classes:
                        raise ValueError("Both selected classes must already exist in the ontology.")
                    key = _pair_key(left, right)
                    if key in set(item.conflicting_pair_keys):
                        conflict = "equivalent" if item.code == "P10" else "disjoint"
                        raise ValueError(
                            f"The selected classes are already declared {conflict}; "
                            f"the requested axiom would make the ontology inconsistent."
                        )
                    if item.code == "P30":
                        handle_p30_subclass(left, right, pair_mode, delete_graph)
                    ordered = sorted((left, right))
                    triple = (URIRef(ordered[0]), pair_predicate, URIRef(ordered[1]))
                    reverse = (triple[2], pair_predicate, triple[0])
                    if triple in current or reverse in current or triple in add_graph or reverse in add_graph:
                        continue
                    add_graph.add(triple)
                    accepted_manual.append(encoded if isinstance(encoded, str) else json.dumps(encoded, sort_keys=True))

                if not len(add_graph) and not len(delete_graph):
                    outcomes.append({
                        "id": item.id, "code": item.code, "status": "unresolved",
                        "choice": selected, "reasons": ["No new individual axioms were selected."],
                    })
                    continue
                add_ttl = add_graph.serialize(format="turtle") if len(add_graph) else ""
                delete_ttl = delete_graph.serialize(format="turtle") if len(delete_graph) else ""
            elif item.code != "P03":
                add_ttl, delete_ttl = (
                    custom_patch_for_review(current, item, custom)
                    if custom and ("Enter" in selected or item.custom_input_kind != "none")
                    else (item.proposed_add_ttl, item.proposed_delete_ttl)
                )
            emit(f"{item.code}: running OOPS and logical validation...")
            baseline_oops = run_oops(current)
            findings = merge_findings(findings_from_oops(baseline_oops, importance_levels), local_findings(current, importance_levels))
            finding = next((f for f in findings if f.code == item.code), Finding(item.code, item.title, item.severity, affected=item.affected))
            if not add_ttl.strip() and not delete_ttl.strip():
                candidate, candidate_note = build_user_approved_candidate(current, item)
                if candidate is None:
                    outcomes.append({
                        "id": item.id,
                        "code": item.code,
                        "status": "rejected",
                        "reasons": [candidate_note],
                    })
                    continue
                validation = validate_candidate_graph(
                    current,
                    candidate,
                    finding,
                    findings,
                    levels=importance_levels,
                    robot_jar=robot_jar,
                    java_exe=java_exe,
                    user_approved=True,
                )
                validation.reasons.insert(0, candidate_note)
            else:
                validation = validate_patch(
                    current, finding, add_ttl, delete_ttl, findings,
                    levels=importance_levels, robot_jar=robot_jar, java_exe=java_exe,
                    user_approved=True,
                    # P10/P30 are ontology-wide completeness findings. A user may
                    # approve a useful subset while OOPS still reports the global
                    # pattern; all other safety checks must still pass.
                    allow_target_unchanged=(
                        selected.startswith("Add selected")
                        and item.code in {"P10", "P12", "P30"}
                    ),
                )
            if validation.valid and validation.graph is not None:
                current = validation.graph
                outcome = {"id": item.id, "code": item.code, "status": "applied", "choice": selected}
                if selected_atomic:
                    outcome["selected_axioms"] = selected_atomic
                if manual_pairs:
                    outcome["expert_pairs"] = manual_pairs
                if item.code == "P30" and (selected_atomic or manual_pairs):
                    outcome["subclass_mode"] = subclass_mode
                outcomes.append(outcome)
                emit(f"{item.code}: repair applied.")
            else:
                rejection_reasons = list(validation.reasons)
                if item.code == "P30" and any("P31" in reason for reason in rejection_reasons):
                    rejection_reasons.append(
                        "OOPS! P31 means the resulting ontology contains an equivalentClass "
                        "assertion that the scanner considers unjustified or incorrect. "
                        "The selected classes may be related, but the available evidence did "
                        "not support exact equivalence."
                    )
                outcome = {
                    "id": item.id, "code": item.code, "status": "rejected",
                    "choice": selected, "reasons": rejection_reasons,
                }
                if selected_atomic:
                    outcome["selected_axioms"] = selected_atomic
                if manual_pairs:
                    outcome["expert_pairs"] = manual_pairs
                if item.code == "P30" and (selected_atomic or manual_pairs):
                    outcome["subclass_mode"] = subclass_mode
                outcomes.append(outcome)
                emit(f"{item.code}: repair rejected by validation.")
        except Exception as exc:
            outcomes.append({"id": item.id, "code": item.code, "status": "rejected", "reasons": [str(exc)]})
            emit(f"{item.code}: repair failed — {exc}")
    emit("Decision validation complete.")
    return current, outcomes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Repair Critical and Important OOPS! pitfalls safely.")
    parser.add_argument("input")
    parser.add_argument("-o", "--output")
    parser.add_argument("--input-format")
    parser.add_argument("--output-format")
    parser.add_argument("--model")
    parser.add_argument("--robot-jar")
    parser.add_argument("--java", default="java")
    parser.add_argument("--max-iterations", type=int, default=MAX_REPAIR_ITERATIONS)
    parser.add_argument("--license-uri", default=DEFAULT_LICENSE_URI)
    args = parser.parse_args()
    graph, detected = load_ontology(args.input, args.input_format)
    output = args.output or str(Path(args.input).with_name(Path(args.input).stem + "_oops_fixed" + (Path(args.input).suffix or ".ttl")))
    out_dir = Path(output).with_suffix("").with_name(Path(output).stem + "_repair")
    result = repair_pitfalls(
        graph, model=args.model, max_iterations=args.max_iterations,
        source_label=args.input, out_dir=out_dir, robot_jar=args.robot_jar,
        java_exe=args.java, license_uri=args.license_uri,
    )
    fmt = args.output_format or guess_format(output) or detected
    serialize_ontology(result.graph, output, fmt)
    # Verify exact serialized bytes can be parsed and scanned.
    probe, _ = load_ontology(output, fmt)
    run_oops(probe)
    print(f"Repaired ontology: {output}")
    print(f"Automatic fixes: {len(result.applied)}")
    print(f"Needs user decision: {len(result.review_items)}")
    print(f"Report: {result.report_markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
