from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import unquote, urlparse

from services.catalog_intelligence.models import CategoryCandidate
from services.catalog_intelligence.utils import clean_text, load_json, slug

# Category classification is deliberately deterministic.  These multilingual
# concept tags help compare supplier data and marketplace taxonomies written in
# different European languages without allowing the system to invent a category.
# They are only additional search signals: the returned category must still be a
# real leaf from the synchronized taxonomy snapshot.
_CONCEPT_PHRASES: dict[str, tuple[str, ...]] = {
    "air_fryer": (
        "air fryer", "airfryer", "friggitrici ad aria", "friggitrice ad aria",
        "fritadeira sem oleo", "fritadeiras sem oleo", "freidora de aire",
        "freidoras de aire", "heissluftfritteuse", "heissluftfritteusen",
        "heißluftfritteuse", "friteuse sans huile", "frytkownica beztluszczowa",
        "teplovzdusna friteza", "teplovzdusne fritezy",
    ),
    "vacuum_cleaner": (
        "aspirapolvere", "vacuum cleaner", "vacuum cleaners", "aspirador",
        "aspiradores", "staubsauger", "aspirateur", "odkurzacz", "vysavac",
    ),
    "robot_vacuum": (
        "robot aspirapolvere", "robot vacuum", "robotic vacuum", "aspirador robot",
        "saugroboter", "robot aspirateur", "odkurzacz automatyczny",
    ),
    "coffee_machine": (
        "macchina da caffe", "macchine da caffe", "coffee machine", "coffee maker",
        "maquina de cafe", "cafeteira", "kaffeemaschine", "machine a cafe",
        "ekspres do kawy", "kavovar",
    ),
    "microwave": (
        "microonde", "microwave", "microondas", "mikrowelle", "micro ondes",
        "kuchenka mikrofalowa", "mikrovlnna rura",
    ),
    "blender": (
        "frullatore", "frullatori", "blender", "liquidificador", "batidora de vaso",
        "standmixer", "mixeur", "mikser kielichowy",
    ),
    "mixer": (
        "sbattitore", "impastatrice", "mixer", "hand mixer", "batidora",
        "batedeira", "handmixer", "robot patissier", "mikser reczny",
    ),
    "kettle": (
        "bollitore", "electric kettle", "kettle", "chaleira eletrica", "hervidor",
        "wasserkocher", "bouilloire", "czajnik elektryczny",
    ),
    "toaster": (
        "tostapane", "toaster", "torradeira", "tostadora", "grille pain", "toster",
    ),
    "oven": (
        "forno elettrico", "electric oven", "mini oven", "horno electrico",
        "forno eletrico", "elektrobackofen", "four electrique", "piekarnik elektryczny",
    ),
    "washing_machine": (
        "lavatrice", "washing machine", "maquina de lavar roupa", "lavadora",
        "waschmaschine", "machine a laver", "pralka",
    ),
    "dishwasher": (
        "lavastoviglie", "dishwasher", "maquina de lavar louca", "lavavajillas",
        "geschirrspuler", "lave vaisselle", "zmywarka",
    ),
    "dryer": (
        "asciugatrice", "tumble dryer", "secadora", "maquina de secar",
        "waschetrockner", "seche linge", "suszarka bebnowa",
    ),
    "refrigerator": (
        "frigorifero", "refrigerator", "fridge", "frigorifico", "geladeira",
        "kuhlschrank", "refrigerateur", "lodowka",
    ),
    "freezer": (
        "congelatore", "freezer", "congelador", "gefrierschrank", "congelateur",
        "zamrazarka",
    ),
    "television": (
        "televisore", "television", "smart tv", "fernseher", "televisor", "telewizor",
    ),
    "smartphone": (
        "smartphone", "telefono cellulare", "mobile phone", "telemovel", "telefono movil",
        "mobiltelefon", "telephone portable", "telefon komorkowy",
    ),
    "laptop": (
        "notebook", "laptop", "computer portatile", "portatil", "ordinateur portable",
    ),
    "tablet": ("tablet", "tablet pc", "tableta"),
    "headphones": (
        "cuffie", "auricolari", "headphones", "earphones", "fones", "auriculares",
        "kopfhorer", "casque audio", "sluchawki",
    ),
    "speaker": (
        "altoparlante", "cassa bluetooth", "speaker", "coluna bluetooth",
        "lautsprecher", "enceinte", "glosnik",
    ),
    "fan": (
        "ventilatore", "fan", "ventoinha", "ventilador", "ventilator", "ventilateur",
        "wentylator",
    ),
    "heater": (
        "stufa elettrica", "termoventilatore", "electric heater", "aquecedor",
        "calefactor", "heizlufter", "chauffage", "grzejnik elektryczny",
    ),
    "iron": (
        "ferro da stiro", "steam iron", "plancha de vapor", "ferro a vapor",
        "dampfbugeleisen", "fer a repasser", "zelazko",
    ),
    "hair_dryer": (
        "asciugacapelli", "phon", "hair dryer", "secador de cabelo", "secador de pelo",
        "haartrockner", "seche cheveux", "suszarka do wlosow",
    ),
    "electric_toothbrush": (
        "spazzolino elettrico", "electric toothbrush", "escova de dentes eletrica",
        "cepillo electrico", "elektrische zahnburste", "brosse a dents electrique",
    ),
    "pressure_washer": (
        "idropulitrice", "pressure washer", "lavadora de alta pressao",
        "hidrolimpiadora", "hochdruckreiniger", "nettoyeur haute pression",
    ),
    "drill": (
        "trapano", "drill", "berbequim", "taladro", "bohrmaschine", "perceuse", "wiertarka",
    ),
    "saw": (
        "sega circolare", "seghetto", "circular saw", "serra circular", "sierra circular",
        "kreissage", "scie circulaire", "pila tarczowa",
    ),
    "lawn_mower": (
        "tosaerba", "rasaerba", "lawn mower", "cortador de relva", "cortacesped",
        "rasenmaher", "tondeuse", "kosiarka",
    ),
    "barbecue": ("barbecue", "bbq", "grill", "churrasqueira"),
    "mattress": ("materasso", "mattress", "colchao", "colchon", "matratze", "matelas"),
    "sofa": ("divano", "sofa", "couch", "canape"),
    "office_chair": (
        "sedia ufficio", "office chair", "cadeira de escritorio", "silla de oficina",
        "burostuhl", "chaise de bureau",
    ),
    "cat_tree": (
        "tiragraffi", "cat tree", "scratching post", "arranhador para gatos",
        "rascador para gatos", "kratzbaum", "arbre a chat",
    ),
}

_STOPWORDS = {
    "a", "ad", "al", "alla", "alle", "con", "da", "dal", "dalla", "de", "dei", "del",
    "della", "delle", "di", "e", "ed", "for", "from", "in", "la", "le", "lo", "of", "per",
    "the", "to", "un", "una", "uno", "with", "und", "von", "fur", "für", "mit", "der", "die",
    "das", "des", "den", "dem", "el", "los", "las", "para", "por", "y", "en", "do", "da",
    "dos", "das", "com", "sem", "pour", "avec", "sans", "et", "les", "des", "du", "dla", "i",
    "z", "na", "do", "pro", "s", "se", "a", "v", "na", "od", "product", "products", "prodotto",
    "prodotti", "artikel", "item", "items", "other", "altri", "altro", "diverse", "varie",
}

_CATEGORY_FIELD_TOKENS = (
    "category", "categoria", "categorie", "kategoria", "categoría", "categorias",
    "family", "famiglia", "familia", "famille", "product_type", "tipo_prodotto",
    "tipo_de_producto", "tipo_produto", "product_group", "gruppo", "grupo", "gruppe",
    "department", "dipartimento", "departamento", "rayon", "taxonomy", "tassonomia",
    "breadcrumb", "class", "classe", "segment", "segmento",
)


def _plain(value: Any) -> str:
    return slug(value).replace("_", " ")


def _phrases_text(value: Any) -> str:
    text = _plain(value)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: Any) -> list[str]:
    text = _phrases_text(value)
    result: list[str] = []
    for token in text.split():
        if len(token) < 2 or token in _STOPWORDS or token.isdigit():
            continue
        result.append(token)
    return result


def _concept_tags(text: str) -> set[str]:
    plain = f" {_phrases_text(text)} "
    result: set[str] = set()
    for concept, phrases in _CONCEPT_PHRASES.items():
        for phrase in phrases:
            normalized = _phrases_text(phrase)
            if normalized and f" {normalized} " in plain:
                result.add(f"concept_{concept}")
                break
    return result


def _flatten_text(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 3:
        return []
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, item in value.items():
            if clean_text(item):
                result.append(clean_text(key))
            result.extend(_flatten_text(item, depth=depth + 1))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten_text(item, depth=depth + 1))
        return result
    text = clean_text(value)
    return [text] if text else []


def _url_filename_tokens(url: str) -> list[str]:
    try:
        parsed = urlparse(clean_text(url))
        path = unquote(parsed.path)
    except Exception:
        return []
    filename = path.rsplit("/", 1)[-1]
    filename = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", filename)
    generic = {"image", "img", "main", "front", "photo", "picture", "large", "small", "01", "1"}
    return [token for token in _tokens(filename) if token not in generic]


def source_category_signature(product: Mapping[str, Any]) -> tuple[str, str]:
    """Return a stable supplier classification signature when one is present.

    Supplier category/family fields are the strongest deterministic signal and
    become reusable Knowledge Base rules after a human approval.
    """
    normalized = load_json(product.get("normalized_json"), {})
    raw = load_json(product.get("raw_json"), {})
    source = normalized.get("source_attributes") if isinstance(normalized, Mapping) else None
    if not isinstance(source, Mapping):
        source = raw if isinstance(raw, Mapping) else {}
    values: list[tuple[str, str]] = []
    for key, value in source.items():
        key_token = slug(key)
        if not any(marker in key_token for marker in _CATEGORY_FIELD_TOKENS):
            continue
        text = clean_text(value)
        if not text or len(text) > 500:
            continue
        values.append((key_token, text))
    if not values:
        return "", ""
    values.sort()
    label = " > ".join(dict.fromkeys(value for _, value in values))
    signature = slug("|".join(f"{key}:{value}" for key, value in values))[:500]
    return signature, label[:1000]


def product_search_fields(product: Mapping[str, Any]) -> dict[str, str]:
    normalized = load_json(product.get("normalized_json"), {})
    raw = load_json(product.get("raw_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    if not isinstance(raw, Mapping):
        raw = {}
    title = clean_text(product.get("title") or normalized.get("title"))
    description = clean_text(product.get("description") or normalized.get("description"))
    short_description = clean_text(normalized.get("short_description"))
    brand = clean_text(product.get("brand") or normalized.get("brand"))
    model = clean_text(product.get("model") or normalized.get("model"))
    category_signature, category_label = source_category_signature(product)

    technical_parts: list[str] = []
    for key, value in normalized.items():
        if key in {"source_attributes", "images", "documents", "title", "description", "short_description"}:
            continue
        if value not in (None, "", [], {}):
            technical_parts.extend((clean_text(key), clean_text(value)))
    source_parts = _flatten_text(raw)
    image_tokens: list[str] = []
    for url in normalized.get("images") or []:
        image_tokens.extend(_url_filename_tokens(clean_text(url)))
    return {
        "title": title,
        "description": " ".join(part for part in (description, short_description) if part),
        "brand_model": " ".join(part for part in (brand, model) if part),
        "supplier_category": category_label,
        "supplier_signature": category_signature,
        "technical": " ".join(technical_parts),
        "raw": " ".join(source_parts)[:20000],
        "image": " ".join(image_tokens),
    }


def _counter(text: str) -> Counter[str]:
    tokens = _tokens(text)
    tokens.extend(sorted(_concept_tags(text)))
    return Counter(tokens)


def _weighted_overlap(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = sum(min(value, right.get(token, 0)) for token, value in left.items())
    denominator = sum(left.values())
    return intersection / denominator if denominator else 0.0


def _category_document(category: Mapping[str, Any]) -> tuple[str, Counter[str], set[str]]:
    label = clean_text(category.get("label"))
    code = clean_text(category.get("code"))
    path = clean_text(category.get("path"))
    product_type = clean_text(category.get("product_type"))
    raw = load_json(category.get("raw_json"), {})
    raw_values: list[str] = []
    if isinstance(raw, Mapping):
        for key in ("title_singular", "title_plural", "name", "label", "display_name"):
            if clean_text(raw.get(key)):
                raw_values.append(clean_text(raw.get(key)))
    document = " ".join((label, code, path, product_type, *raw_values))
    counter = _counter(document)
    concepts = {token for token in counter if token.startswith("concept_")}
    return document, counter, concepts


def rank_categories(
    product: Mapping[str, Any],
    categories: Iterable[Mapping[str, Any]],
    *,
    top_k: int = 5,
) -> list[CategoryCandidate]:
    """Rank only real leaf categories from a cached taxonomy snapshot."""
    fields = product_search_fields(product)
    field_weights = {
        "supplier_category": 42.0,
        "title": 28.0,
        "description": 10.0,
        "technical": 7.0,
        "raw": 7.0,
        "image": 3.0,
        "brand_model": 3.0,
    }
    field_counters = {key: _counter(value) for key, value in fields.items() if clean_text(value)}
    product_concepts = {
        token
        for counter in field_counters.values()
        for token in counter
        if token.startswith("concept_")
    }
    title_plain = _phrases_text(fields.get("title"))
    supplier_plain = _phrases_text(fields.get("supplier_category"))

    ranked: list[CategoryCandidate] = []
    for category in categories:
        if not bool(category.get("is_leaf")):
            continue
        external_id = clean_text(category.get("external_id"))
        if not external_id:
            continue
        document, category_counter, category_concepts = _category_document(category)
        if not category_counter:
            continue
        overlap_score = 0.0
        field_signals: dict[str, float] = {}
        for key, weight in field_weights.items():
            counter = field_counters.get(key)
            if not counter:
                continue
            overlap = _weighted_overlap(counter, category_counter)
            field_signals[key] = round(overlap, 4)
            overlap_score += overlap * weight

        category_label_plain = _phrases_text(category.get("label"))
        category_path_plain = _phrases_text(category.get("path"))
        sequence = max(
            SequenceMatcher(None, title_plain, category_label_plain).ratio() if title_plain else 0.0,
            SequenceMatcher(None, supplier_plain, category_label_plain).ratio() if supplier_plain else 0.0,
            SequenceMatcher(None, supplier_plain, category_path_plain).ratio() if supplier_plain else 0.0,
        )
        sequence_score = sequence * 12.0

        concept_matches = product_concepts & category_concepts
        concept_score = 22.0 if concept_matches else 0.0
        exact_score = 0.0
        if supplier_plain and category_label_plain:
            if supplier_plain == category_label_plain:
                exact_score = 28.0
            elif category_label_plain in supplier_plain or supplier_plain in category_label_plain:
                exact_score = 18.0
        elif title_plain and category_label_plain and category_label_plain in title_plain:
            exact_score = 12.0

        depth = int(category.get("level") or 0)
        depth_bonus = min(3.0, max(0.0, depth * 0.35))
        score = min(100.0, overlap_score + sequence_score + concept_score + exact_score + depth_bonus)
        # Do not return noise-only candidates.  A low-confidence fallback is
        # still kept when it has at least one real lexical signal.
        if score <= 0.25:
            continue
        ranked.append(
            CategoryCandidate(
                category_external_id=external_id,
                category_label=clean_text(category.get("label")),
                category_path=clean_text(category.get("path")),
                score=round(score, 2),
                source="LOCAL_TAXONOMY",
                signals={
                    "field_overlap": field_signals,
                    "sequence": round(sequence, 4),
                    "concept_matches": sorted(concept_matches),
                    "exact_score": exact_score,
                    "depth_bonus": round(depth_bonus, 2),
                },
                raw={"category_code": clean_text(category.get("code"))},
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.category_path.lower(), item.category_external_id))
    return ranked[: max(1, int(top_k))]


def _identifier_like_title(value: Any, product: Mapping[str, Any]) -> bool:
    text = clean_text(value)
    if not text:
        return True
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    normalized = load_json(product.get("normalized_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    identifiers = {
        clean_text(product.get("supplier_sku") or normalized.get("supplier_sku")),
        clean_text(product.get("ean") or normalized.get("ean")),
        clean_text(product.get("model") or normalized.get("model")),
    }
    for identifier in identifiers:
        candidate = re.sub(r"[^a-z0-9]", "", identifier.lower())
        if candidate and compact == candidate:
            return True
    # A compact model/SKU without descriptive words is not a useful title for
    # Kaufland's category decision endpoint.  It is still retained as evidence
    # in the description and never discarded from the canonical product.
    return bool(
        len(text) <= 32
        and " " not in text.strip()
        and re.fullmatch(r"[A-Za-z0-9._/+\-]+", text)
    )


def _source_title_candidates(product: Mapping[str, Any]) -> list[str]:
    normalized = load_json(product.get("normalized_json"), {})
    raw = load_json(product.get("raw_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    if not isinstance(raw, Mapping):
        raw = {}
    source = normalized.get("source_attributes")
    if not isinstance(source, Mapping):
        source = raw
    title_markers = (
        "product_name", "product_title", "item_name", "article_name",
        "article_title", "designation", "denomination", "denominazione",
        "bezeichnung", "nome", "name", "title", "nazwa", "label",
    )
    candidates: list[str] = []
    for key, value in source.items():
        key_token = slug(key)
        if not any(marker == key_token or marker in key_token for marker in title_markers):
            continue
        candidate = clean_text(value)
        if len(candidate) < 3 or candidate in candidates:
            continue
        candidates.append(candidate)
    return candidates


def _kaufland_decide_title(product: Mapping[str, Any], fields: Mapping[str, str]) -> str:
    title = clean_text(fields.get("title"))
    if title and not _identifier_like_title(title, product):
        return title[:1000]
    for candidate in _source_title_candidates(product):
        if not _identifier_like_title(candidate, product):
            return candidate[:1000]
    # Build a truthful descriptive title only from source evidence.  This helps
    # feeds where the supplier exposed the model as the title but still supplied
    # brand, family and model in other fields.
    parts = []
    for value in (
        fields.get("brand_model"),
        fields.get("supplier_category"),
        title,
    ):
        token = clean_text(value)
        if token and token not in parts:
            parts.append(token)
    return " - ".join(parts)[:1000]


def _kaufland_decide_description(fields: Mapping[str, str]) -> str:
    sections: list[str] = []
    labelled = (
        ("Descrizione", fields.get("description")),
        ("Categoria del fornitore", fields.get("supplier_category")),
        ("Marca e modello", fields.get("brand_model")),
        ("Specifiche tecniche", fields.get("technical")),
        ("Dati completi del fornitore", fields.get("raw")),
        ("Riferimenti immagini", fields.get("image")),
    )
    seen: set[str] = set()
    for label, value in labelled:
        value = clean_text(value)
        if not value:
            continue
        normalized = re.sub(r"\s+", " ", value).strip().lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        sections.append(f"{label}: {value}")
    # The API guide uses title, description and manufacturer for category
    # suggestions.  Keep all supplier evidence while staying safely below the
    # product-data Text limit.
    return "\n".join(sections)[:60000]


def kaufland_decide_payload(product: Mapping[str, Any]) -> dict[str, Any]:
    normalized = load_json(product.get("normalized_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    fields = product_search_fields(product)
    price = normalized.get("purchase_price")
    try:
        cents = max(0, int(round(float(price) * 100))) if price not in (None, "") else 0
    except (TypeError, ValueError):
        cents = 0
    item = {
        "title": _kaufland_decide_title(product, fields),
        "description": _kaufland_decide_description(fields),
        "manufacturer": clean_text(product.get("brand") or normalized.get("brand")),
    }
    payload: dict[str, Any] = {"item": {key: value for key, value in item.items() if value}}
    if cents:
        payload["price"] = cents
    return payload


def parse_kaufland_suggestions(
    payload: Any,
    categories_by_id: Mapping[str, Mapping[str, Any]],
) -> list[CategoryCandidate]:
    if isinstance(payload, Mapping):
        items = payload.get("data") or payload.get("categories") or payload.get("results") or []
    else:
        items = payload
    if not isinstance(items, list):
        return []
    # Kaufland documents an ordered list, not numeric probabilities.  The
    # internal values below are ranking weights only and are explicitly marked
    # as such in signals; they must not be presented as API probabilities.
    rank_weights = (99.0, 90.0, 82.0, 74.0, 66.0)
    result: list[CategoryCandidate] = []
    for index, raw in enumerate(items[:5]):
        if not isinstance(raw, Mapping):
            continue
        external_id = clean_text(raw.get("id_category") or raw.get("category_id") or raw.get("id"))
        if not external_id:
            continue
        category = categories_by_id.get(external_id)
        snapshot_match = bool(category)
        if category and not bool(category.get("is_leaf")):
            # /categories/decide returns the best product categories.  If the
            # local snapshot says the item is not a leaf, keep it for manual
            # review instead of silently turning it into a wrong local match.
            snapshot_match = False
        label = clean_text(
            (category or {}).get("label")
            or raw.get("title_singular")
            or raw.get("title_plural")
            or raw.get("name")
            or external_id
        )
        path = clean_text((category or {}).get("path") or label)
        result.append(
            CategoryCandidate(
                category_external_id=external_id,
                category_label=label,
                category_path=path,
                score=rank_weights[index] if index < len(rank_weights) else max(50.0, 90.0 - index * 10),
                source="KAUFLAND_DECIDE_API",
                signals={
                    "official_rank": index + 1,
                    "score_kind": "ORDERED_RANK_WEIGHT_NOT_API_PROBABILITY",
                    "taxonomy_snapshot_match": snapshot_match,
                },
                raw=dict(raw),
            )
        )
    return result


def merge_candidates(
    local_candidates: Iterable[CategoryCandidate],
    official_candidates: Iterable[CategoryCandidate] = (),
    *,
    mapping_rule: Mapping[str, Any] | None = None,
    top_k: int = 5,
) -> list[CategoryCandidate]:
    """Merge cached-taxonomy ranking with Kaufland's official ranking.

    The official endpoint returns categories ordered by probability.  Its first
    valid leaf must not be diluted into a low-confidence local score: local
    matching is supporting evidence, not a replacement for Kaufland's own
    classifier.
    """
    if mapping_rule:
        category_id = clean_text(mapping_rule.get("category_external_id"))
        if category_id:
            return [
                CategoryCandidate(
                    category_external_id=category_id,
                    category_label=clean_text(mapping_rule.get("category_label")),
                    category_path=clean_text(
                        mapping_rule.get("category_path") or mapping_rule.get("category_label")
                    ),
                    score=max(95.0, min(100.0, float(mapping_rule.get("confidence") or 1.0) * 100.0)),
                    source="APPROVED_KNOWLEDGE_BASE",
                    signals={"mapping_rule_id": mapping_rule.get("id")},
                    raw=dict(mapping_rule),
                )
            ]

    local_by_id = {item.category_external_id: item for item in local_candidates}
    official_by_id = {item.category_external_id: item for item in official_candidates}
    category_ids = set(local_by_id) | set(official_by_id)
    merged: list[CategoryCandidate] = []
    for category_id in category_ids:
        local = local_by_id.get(category_id)
        official = official_by_id.get(category_id)
        if local and official:
            # Preserve the official ordering and add a small agreement bonus.
            # Even a weak lexical score can be useful evidence, but it must not
            # drag the official first choice below the review threshold.
            score = min(100.0, official.score + min(2.0, local.score / 25.0))
            source = "KAUFLAND_DECIDE_API+LOCAL_TAXONOMY"
            signals = {
                "official": official.signals,
                "local": local.signals,
                "agreement": True,
            }
            raw = {"official": official.raw, "local": local.raw}
            template = official
        elif official:
            score = official.score
            source = official.source
            signals = official.signals
            raw = official.raw
            template = official
        else:
            assert local is not None
            # When Kaufland returned official suggestions, a different local
            # lexical match must never outrank the first official category.
            score = min(local.score, 78.0) if official_by_id else local.score
            source = local.source
            signals = local.signals
            raw = local.raw
            template = local
        merged.append(
            CategoryCandidate(
                category_external_id=category_id,
                category_label=template.category_label,
                category_path=template.category_path,
                score=round(score, 2),
                source=source,
                signals=signals,
                raw=raw,
            )
        )
    merged.sort(key=lambda item: (-item.score, item.category_path.lower(), item.category_external_id))
    return merged[: max(1, int(top_k))]

def _candidate_official_rank(candidate: CategoryCandidate) -> int | None:
    signals = candidate.signals if isinstance(candidate.signals, Mapping) else {}
    if "official_rank" in signals:
        try:
            return int(signals.get("official_rank"))
        except (TypeError, ValueError):
            return None
    official = signals.get("official")
    if isinstance(official, Mapping):
        try:
            return int(official.get("official_rank"))
        except (TypeError, ValueError):
            return None
    return None


def _candidate_snapshot_match(candidate: CategoryCandidate) -> bool:
    signals = candidate.signals if isinstance(candidate.signals, Mapping) else {}
    if "taxonomy_snapshot_match" in signals:
        return bool(signals.get("taxonomy_snapshot_match"))
    official = signals.get("official")
    if isinstance(official, Mapping) and "taxonomy_snapshot_match" in official:
        return bool(official.get("taxonomy_snapshot_match"))
    return True


def category_decision(candidates: Iterable[CategoryCandidate]) -> dict[str, Any]:
    ranked = list(candidates)
    if not ranked:
        # Category confidence is not a product-data validation error.  The
        # product remains available for manual category selection rather than
        # being incorrectly labelled BLOCKED.
        return {
            "status": "REVIEW",
            "confidence": 0.0,
            "decision_source": "NO_MATCH_REVIEW",
            "candidate": None,
            "margin": 0.0,
            "review_reason": "Nessuna categoria candidata: selezione manuale richiesta.",
        }
    top = ranked[0]
    second = ranked[1].score if len(ranked) > 1 else 0.0
    margin = max(0.0, top.score - second)
    source = top.source
    official_rank = _candidate_official_rank(top)
    snapshot_match = _candidate_snapshot_match(top)
    review_reason = ""
    if source == "APPROVED_KNOWLEDGE_BASE":
        status = "AUTO_APPROVED"
    elif "KAUFLAND_DECIDE_API" in source and official_rank == 1 and snapshot_match:
        # The official endpoint returns categories ordered by probability.  The
        # first valid leaf is therefore the authoritative automatic proposal.
        status = "AUTO_APPROVED"
    elif "KAUFLAND_DECIDE_API" in source and official_rank == 1:
        status = "REVIEW"
        review_reason = "La categoria ufficiale non è presente come foglia nello snapshot locale: aggiorna la tassonomia o confermala manualmente."
    elif top.score >= 93.0 and margin >= 12.0:
        status = "AUTO_APPROVED"
    else:
        # A weak local similarity is an invitation to review, never a reason to
        # block product creation.  Blocking is reserved for deterministic feed
        # validation after a category has been selected.
        status = "REVIEW"
        review_reason = "Categoria proposta con bassa evidenza: verifica o approva manualmente."
    return {
        "status": status,
        "confidence": round(top.score, 2),
        "decision_source": source,
        "candidate": top,
        "margin": round(margin, 2),
        "review_reason": review_reason,
        "official_rank": official_rank,
    }


def candidates_as_dicts(candidates: Iterable[CategoryCandidate]) -> list[dict[str, Any]]:
    return [asdict(item) for item in candidates]


__all__ = [
    "candidates_as_dicts",
    "category_decision",
    "kaufland_decide_payload",
    "merge_candidates",
    "parse_kaufland_suggestions",
    "product_search_fields",
    "rank_categories",
    "source_category_signature",
]
