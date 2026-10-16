"""UI-facing R-MST, adapted from ipsqer/submission/code/mst.py.

Terminals are retrieved articles. A bounded multi-target Dijkstra supplies
path witnesses, then Kruskal selects a tree over the terminal distance graph.
The undirected pair cost is min(d(a,b), d(b,a)); this agrees with the reference
on its symmetric article-distance model and remains independent of node IDs
when users configure asymmetric costs. No synthetic penalty edges are used.
"""
from __future__ import annotations

import heapq
import re
import time

MAX_INPUTS = 200
MAX_TERMINALS = 50


class InputError(ValueError):
    pass


def _integer(value, label, low, high):
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise InputError(f"{label} must be an integer from {low} to {high}") from None
    if isinstance(value, bool) or str(result) != str(value) or not low <= result <= high:
        raise InputError(f"{label} must be an integer from {low} to {high}")
    return result


def _api(api):
    if api is not None:
        return api
    if __package__:
        from . import explorer
    else:
        import explorer
    return explorer


def _resolve(con, item, default_language, api):
    if isinstance(item, dict) and "id" in item:
        node = api.q_node(con, _integer(item["id"], "Article ID", 0, 2**63 - 1))
        if not node:
            return None, "article ID not found"
        if item.get("language") and item["language"] != node["language"]:
            return None, "article ID belongs to another language"
        return (node, None) if node["type"] == "article" else (None, "page is not an article")
    title = item if isinstance(item, str) else item.get("title", "")
    language = default_language if isinstance(item, str) else item.get("language", default_language)
    if not isinstance(title, str) or not title.strip() or len(title) > 1000:
        raise InputError("Each entry needs an article title (at most 1,000 characters)")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", language):
        raise InputError("Invalid article language")
    title = " ".join(title.replace("_", " ").strip().split())
    if isinstance(item, str) and ":" in title:
        prefix, rest = title.split(":", 1)
        if prefix in api.CONFIG.get("languages", {}) or re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]+)*", prefix):
            language, title = prefix, rest.strip()
    multilingual = "language" in api._columns(con, "nodes")
    if not multilingual and language != "en":
        return None, f"{language} is not loaded"
    scope = " AND language=?" if multilingual else ""
    params = (language,) if multilingual else ()
    if multilingual and not con.execute("SELECT 1 FROM nodes WHERE language=? LIMIT 1", params).fetchone():
        return None, f"{language} is not loaded"
    redirect_columns = api._columns(con, "redirects")
    seen = set()
    for _ in range(12):
        if title in seen:
            return None, "redirect cycle"
        seen.add(title)
        normalized = api.norm_title(title)
        replacements = api.CONFIG.get("languages", {}).get(language, {}).get("case_replacements", {})
        capital = next((upper for upper, lower in replacements.items() if title[:1] == lower), None)
        if capital:
            normalized = capital + normalized[1:]
        candidates = list(dict.fromkeys((title, normalized)))
        for candidate in candidates:
            row = con.execute(f"SELECT {api._node_fields(con)} FROM nodes WHERE title=?{scope} LIMIT 1",
                              (candidate, *params)).fetchone()
            if row:
                return (dict(row), None) if row["type"] == "article" else (None, "page is not an article")
        redirect = None
        if redirect_columns and ("language" in redirect_columns or language == "en"):
            rscope = " AND language=?" if "language" in redirect_columns else ""
            rparams = (language,) if rscope else ()
            for candidate in candidates:
                redirect = con.execute(f"SELECT to_ FROM redirects WHERE frm=?{rscope} LIMIT 1",
                                       (candidate, *rparams)).fetchone()
                if redirect:
                    break
        if redirect:
            title = redirect["to_"]
            continue
        rows = con.execute(f"SELECT {api._node_fields(con)} FROM nodes WHERE tl=?{scope} LIMIT 2",
                           (api.search_key(title, language), *params)).fetchall()
        if len(rows) > 1:
            return None, "ambiguous title case; select the exact article in search"
        if rows:
            return (dict(rows[0]), None) if rows[0]["type"] == "article" else (None, "page is not an article")
        return None, "title not found in the loaded graph"
    return None, "redirect chain is too long"


def resolve(con, payload, *, api=None):
    """Resolve the fixed top-k before deduplication; never backfill later ranks."""
    api = _api(api)
    if not isinstance(payload, dict):
        raise InputError("Input must be a JSON object")
    items = payload.get("retrieved_articles")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_INPUTS:
        raise InputError(f"Supply between 1 and {MAX_INPUTS} articles")
    top_k = _integer(payload.get("top_k", 20), "Top-k", 1, MAX_TERMINALS)
    default_language = payload.get("language", api.CONFIG.get("multilingual", {}).get("base_language", "en"))
    ranked = []
    for position, item in enumerate(items, 1):
        if not isinstance(item, (str, dict)):
            raise InputError("Articles must be titles or article objects")
        rank = _integer(item.get("rank", position) if isinstance(item, dict) else position,
                        "Rank", 1, 1000000)
        ranked.append((rank, position, item))
    entries, terminals, duplicates, unresolved = [], {}, [], []
    for rank, position, item in sorted(ranked, key=lambda row: row[:2]):
        if rank > top_k:
            continue
        node, error = _resolve(con, item, default_language, api)
        requested = item if isinstance(item, str) else item.get("title", f"ID {item.get('id')}")
        entry = {"rank": rank, "position": position, "requested": requested,
                 "node": node, "status": "unresolved" if error else "resolved", "reason": error}
        if error:
            unresolved.append(entry)
        elif node["id"] in terminals:
            entry["status"] = "duplicate"
            duplicates.append(entry)
        else:
            terminals[node["id"]] = node
        entries.append(entry)
    if not entries:
        raise InputError("No articles remain within top-k")
    if len(terminals) > MAX_TERMINALS:
        raise InputError(f"At most {MAX_TERMINALS} unique articles can be evaluated")
    return {"top_k": top_k, "input_count": len(items), "included_count": len(entries),
            "unique_article_count": len(terminals), "entries": entries,
            "terminals": list(terminals.values()), "duplicates": duplicates,
            "unresolved": unresolved, "ready": not unresolved and bool(terminals)}


def shortest_paths(con, source, target_ids, *, api, hub_max, settle_cap, deadline, policy=None):
    """Reference multi-target Dijkstra with current costs and language guard.

    A result is certified only after its target is settled without pruning,
    or when a zero-cost witness proves optimality with nonnegative costs.
    """
    policy = api._route_policy(con, policy)
    for node in set(target_ids) | {source}:
        api._policy_node(api.q_node(con, node), policy, "R-MST terminal")
    targets = set(target_ids) - {source}
    remaining, ends, previous = set(targets), {}, {(source, 0): None}
    distances, done, heap = {(source, 0): 0.0}, set(), [(0.0, 2, source, 0)]
    typecache = {}
    adjacency = api._PolicyAdjacency(con, policy, hub_max, keep=targets)
    pruned = adjacency.pruned
    reasons, settled = [], 0
    neighbors = set()
    for target in targets:
        for direction in ("fwd", "rev"):
            neighbors.update(r["neighbor"] for r in adjacency.rows(target, direction, hint=True))

    def node_type(node):
        if node not in typecache:
            row = con.execute("SELECT type FROM nodes WHERE id=?", (node,)).fetchone()
            typecache[node] = row["type"] if row else "?"
        return typecache[node]

    while heap and remaining:
        distance, _, node, state = heapq.heappop(heap)
        key = (node, state)
        if key in done:
            continue
        if settle_cap is not None and settled >= settle_cap:
            reasons.append("node limit")
            break
        if deadline is not None and time.perf_counter() >= deadline:
            reasons.append("time limit")
            break
        done.add(key)
        settled += 1
        if node in remaining:
            ends[node] = (key, not pruned or distance == 0)
            remaining.remove(node)
            if not remaining:
                break
        source_type = node_type(node)
        for direction in ("fwd", "rev"):
            for row in adjacency.rows(node, direction):
                neighbor, edge_type = row["neighbor"], row["type"]
                cost = api._fwd_cost(edge_type) if direction == "fwd" else api._rev_cost(edge_type)
                next_state = api._traversal_state(source_type, node_type(neighbor), edge_type,
                                                 state, policy.ambiguity_guard)
                if next_state is None:
                    continue
                next_key, next_distance = (neighbor, next_state), distance + cost
                if next_key not in distances or next_distance < distances[next_key]:
                    distances[next_key] = next_distance
                    previous[next_key] = (key, (edge_type, direction, cost))
                    priority = 0 if neighbor in targets else 1 if neighbor in neighbors else 2
                    heapq.heappush(heap, (next_distance, priority, neighbor, next_state))
    if pruned:
        reasons.append("hub pruning")
    paths = {}
    for target in targets:
        if target in ends:
            end, exact = ends[target]
        else:
            candidates = [(distances[k], k) for k in ((target, 0), (target, 1)) if k in distances]
            if not candidates:
                continue
            _, end = min(candidates)
            exact = distances[end] == 0
        sequence, traversals, cursor = [], [], end
        while cursor is not None:
            sequence.append(cursor[0])
            back = previous[cursor]
            if back is None:
                break
            cursor, traversal = back
            traversals.append(traversal)
        sequence.reverse()
        traversals.reverse()
        paths[target] = api._path_result(con, sequence, distances[end], settled,
                                        approx=not exact, traversals=traversals)
        paths[target]["effective_policy"] = policy.as_dict()
    return {"paths": paths, "settled": settled, "reasons": reasons,
            "exhausted": not heap and bool(remaining) and not reasons,
            "missing": sorted(targets - paths.keys())}


def mst(terminals, pairs):
    """Kruskal from the reference evaluator; retain a forest when disconnected."""
    parent = {node["id"]: node["id"] for node in terminals}

    def find(node):
        while node != parent[node]:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    selected = []
    for pair in sorted(pairs, key=lambda p: (p["distance"], p["source_id"], p["target_id"])):
        left, right = find(pair["source_id"]), find(pair["target_id"])
        if left != right:
            parent[left] = right
            selected.append(pair)
    groups = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)
    return selected, list(groups.values())


def evaluate(con, payload, *, api=None, low_mem=False):
    api = _api(api)
    if not isinstance(payload, dict):
        raise InputError("Input must be a JSON object")
    started = time.perf_counter()
    if "policy" in payload and payload["policy"] is None:
        raise InputError("policy must be an object")
    policy = api._route_policy(con, payload.get("policy"))
    try:
        seconds = api.parse_search_seconds(payload.get("seconds", 30))
    except ValueError as error:
        raise InputError(str(error)) from None
    exhaustive = payload.get("exhaustive", False)
    if type(exhaustive) is not bool:
        raise InputError("exhaustive must be true or false")
    result = resolve(con, payload, api=api)
    for node in result["terminals"]:
        api._policy_node(node, policy, "R-MST terminal")
    result.update({"metric": "R-MST", "status": "unresolved", "certified": False,
                   "total_cost": None, "mean_edge_cost": None, "edges": [], "components": [],
                   "pair_policy": "minimum of the two directional shortest-path costs",
                   "search_options": {"seconds": seconds, "exhaustive": exhaustive},
                   "effective_policy": policy.as_dict(),
                   "costs": {name: {"forward": api._fwd_cost(name), "reverse": api._rev_cost(name)}
                             for name in api.EDGE_TYPES}})
    dataset = api.q_dataset(con)
    result["dataset"] = {key: dataset.get(key) for key in ("bounded", "source_mode", "total_nodes", "language_counts")}
    if not result["ready"]:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        return result
    terminals = result["terminals"]
    node_ids = sorted(node["id"] for node in terminals)
    search = api.CONFIG["search_low_memory" if low_mem else "search"]
    # Give search its own allowance after title resolution and dataset metadata.
    deadline = time.perf_counter() + seconds if seconds else None
    remaining_states = None if exhaustive else min(
        500000 if low_mem else 2000000, search["settle_cap"] * len(node_ids))
    directions, searches, reasons = {}, [], set()
    for source in node_ids if len(node_ids) > 1 else []:
        if deadline is not None and time.perf_counter() >= deadline:
            reasons.add("time limit")
            break
        if remaining_states is not None and remaining_states <= 0:
            reasons.add("node limit")
            break
        found = shortest_paths(con, source, node_ids, api=api,
                               hub_max=None if exhaustive else search["hub_max"],
                               settle_cap=None if exhaustive else min(search["settle_cap"], remaining_states),
                               deadline=deadline, policy=policy)
        if remaining_states is not None:
            remaining_states -= found["settled"]
        reasons.update(found["reasons"])
        searches.append({"source_id": source, "settled": found["settled"], "reasons": found["reasons"]})
        for target in node_ids:
            if target != source:
                directions[source, target] = (found["paths"].get(target), found["exhausted"])
    pairs, uncertain = [], 0
    for index, left in enumerate(node_ids):
        for right in node_ids[index + 1:]:
            candidates, known = [], []
            for a, b in ((left, right), (right, left)):
                path, exhausted = directions.get((a, b), (None, False))
                known.append(exhausted or bool(path and not path["approx"]))
                if path:
                    candidates.append({"source_id": a, "target_id": b, **path})
            best = min(candidates, key=lambda p: (p["distance"], p["source_id"])) if candidates else None
            exact = all(known) or bool(best and best["distance"] == 0)
            if not exact:
                uncertain += 1
            if best:
                best["approx"] = not exact
                pairs.append(best)
    selected, components = mst(terminals, pairs)
    connected = len(components) == 1
    certified = connected and uncertain == 0
    total = sum(edge["distance"] for edge in selected)
    result.update({"status": "complete" if certified else "incomplete" if uncertain else "disconnected",
                   "certified": certified, "edges": selected, "components": components,
                   "total_cost": total if certified else None,
                   "mean_edge_cost": total / (len(node_ids) - 1) if certified and len(node_ids) > 1 else 0.0 if certified else None,
                   "partial_tree_cost": total, "uncertain_pair_count": uncertain,
                   "expected_pair_count": len(node_ids) * (len(node_ids) - 1) // 2,
                   "available_pair_count": len(pairs), "limit_reasons": sorted(reasons),
                   "searches": searches, "elapsed_seconds": round(time.perf_counter() - started, 3)})
    return result
