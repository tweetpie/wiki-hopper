"""Immutable traversal policy for graph search and route measurements."""

from dataclasses import dataclass
from collections.abc import Mapping

if __package__:
    from .wikihopper_config import CONFIG, EDGE_NAMES
else:
    from wikihopper_config import CONFIG, EDGE_NAMES


@dataclass(frozen=True)
class TraversalPolicy:
    allowed_languages: tuple[str, ...]
    allowed_edge_types: tuple[str, ...]
    ambiguity_guard: bool

    def as_dict(self):
        return {"allowed_languages": list(self.allowed_languages),
                "allowed_edge_types": list(self.allowed_edge_types),
                "ambiguity_guard": self.ambiguity_guard}


def normalize_policy(policy, loaded_languages):
    """Omission means all loaded languages/types and the configured guard default."""
    loaded = tuple(sorted(set(loaded_languages)))
    if isinstance(policy, TraversalPolicy):
        policy = policy.as_dict()
    if policy is None:
        policy = {}
    if not isinstance(policy, Mapping):
        raise ValueError("policy must be an object")
    unknown = set(policy) - {"allowed_languages", "allowed_edge_types", "ambiguity_guard"}
    if unknown:
        raise ValueError("Unknown policy settings: " + ", ".join(sorted(map(str, unknown))))

    def selection(key, supported):
        if key not in policy:
            return tuple(supported)
        value = policy[key]
        if (not isinstance(value, (list, tuple)) or not value
                or any(not isinstance(item, str) or not item for item in value)):
            raise ValueError(f"{key} must be a nonempty list of strings")
        bad = set(value) - set(supported)
        if bad:
            raise ValueError(f"Unsupported {key}: {', '.join(sorted(bad))}")
        return tuple(item for item in supported if item in value)

    languages = selection("allowed_languages", loaded)
    edges = selection("allowed_edge_types", EDGE_NAMES)
    guard = policy.get("ambiguity_guard", CONFIG["explorer"]["ambiguity_guard"])
    if type(guard) is not bool:
        raise ValueError("ambiguity_guard must be true or false")
    return TraversalPolicy(languages, edges, guard)


def traversal_state(source_type, target_type, edge_type, state, ambiguity_guard):
    if not ambiguity_guard:
        return 0
    if edge_type in ("HAS_DISAMBIGUATION", "DISAMBIGUATES_TO"):
        if source_type == "article" and target_type == "disambiguation":
            return None if state == 1 else 0
        if source_type == "disambiguation" and target_type == "article":
            return 1
        return 0
    return state if edge_type == "LANGUAGE_EQUIVALENT" else 0
