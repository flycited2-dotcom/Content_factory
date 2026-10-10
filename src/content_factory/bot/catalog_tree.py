"""Supplier catalogue navigation without category or product truncation."""
from dataclasses import dataclass, field
import hashlib


def category_key(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


@dataclass
class CatalogNode:
    identity: str
    name: str
    path: tuple[str, ...]
    parent: str = ""
    children: list[str] = field(default_factory=list)
    # Indices, rather than names, preserve same-named products in different groups.
    item_indices: list[int] = field(default_factory=list)


def build_tree(items, sections=None):
    """Every item belongs to every ancestor; ids disambiguate same-named groups."""
    nodes = {"": CatalogNode("", "Каталог", ())}
    for index, item in enumerate(items):
        names = tuple(item.category_path) or tuple(
            part.strip() for part in item.section.split(" / ") if part.strip())
        names = names or ("Без раздела",)
        ids = tuple(item.category_ids)
        parent = ""
        nodes[parent].item_indices.append(index)
        for depth, name in enumerate(names):
            identity = (ids[depth] if len(ids) == len(names)
                        else " / ".join(names[:depth + 1]))
            if identity not in nodes:
                nodes[identity] = CatalogNode(identity, name, names[:depth + 1], parent)
                nodes[parent].children.append(identity)
            nodes[identity].item_indices.append(index)
            parent = identity
    # Flat XLSX sections keep their original ranking and stable legacy callbacks.
    if sections is not None:
        known_paths = {" / ".join(node.path) for identity, node in nodes.items() if identity}
        for section in sections:
            if section not in known_paths and section not in nodes and " / " not in section:
                nodes[section] = CatalogNode(section, section, (section,))
                nodes[""].children.append(section)
        rank = {name: i for i, name in enumerate(sections)}
        nodes[""].children.sort(key=lambda key: (
            rank.get(key, len(rank)), nodes[key].name.casefold()))
    return nodes


def find_node(nodes, key):
    return next((node for identity, node in nodes.items()
                 if identity and category_key(identity) == key), None)
