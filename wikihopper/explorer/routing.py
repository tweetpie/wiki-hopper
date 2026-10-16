"""Shortest-path (relevance distance) search via Dijkstra with directional costs,
and measurement of fixed user-selected routes."""

import heapq
import time

from ..config import CONFIG, FORWARD_COSTS, REVERSE_COSTS, parse_search_seconds
from ..policy import normalize_policy, traversal_state
from .provenance import q_provenance
from .queries import _columns, _loaded_languages, q_node

# Stored direction stays fixed. Request policy controls the ambiguity guard;
# traversal costs come from the shared config, including existing databases.
_DAB_TYPES = ("HAS_DISAMBIGUATION", "DISAMBIGUATES_TO")


def _fwd_cost(etype):
    return FORWARD_COSTS[etype]


def _rev_cost(etype):
    return REVERSE_COSTS[etype]


def _traversal_state(source_type, target_type, etype, state, ambiguity_guard=None):
    """Apply the same ambiguity guard to discovered and user-selected routes."""
    guard = CONFIG["explorer"]["ambiguity_guard"] if ambiguity_guard is None else ambiguity_guard
    return traversal_state(source_type, target_type, etype, state, guard)


def _route_policy(con, policy=None):
    return normalize_policy(policy, _loaded_languages(con))


def _policy_node(node, policy, label):
    if node and node["language"] not in policy.allowed_languages:
        raise ValueError(f"{label} {node['id']} [{node['language']}] is outside Route languages")


class _PolicyAdjacency:
    """Request-local adjacency: policy filtering precedes probing and pruning."""

    def __init__(self, con, policy, hub_max, keep=()):
        self.con, self.policy, self.hub_max = con, policy, hub_max
        self.keep = tuple(sorted(keep))
        self.cache, self.pruned = {}, set()
        self.restrict_languages = ("language" in _columns(con, "nodes")
                                   and set(policy.allowed_languages) != set(_loaded_languages(con)))

    def rows(self, node, direction, *, hint=False):
        key = (node, direction)
        if not hint and key in self.cache:
            return self.cache[key]
        col, other = ("src", "dst") if direction == "fwd" else ("dst", "src")
        edges = self.policy.allowed_edge_types
        where = f"e.{col}=? AND e.type IN ({','.join('?' for _ in edges)})"
        values = (node, *edges)
        join = ""
        if self.restrict_languages:
            join = f" JOIN nodes n ON n.id=e.{other}"
            where += f" AND n.language IN ({','.join('?' for _ in self.policy.allowed_languages)})"
            values += self.policy.allowed_languages
        query = f"SELECT e.{other} neighbor,e.type FROM edges e{join} WHERE {where}"
        if hint:
            limit = 2 * self.hub_max + 1 if self.hub_max is not None else 5001
            return self.con.execute(query + " LIMIT ?", (*values, limit)).fetchall()
        rows = (self.con.execute(query, values).fetchall() if self.hub_max is None else
                self.con.execute(query + " LIMIT ?", (*values, self.hub_max + 1)).fetchall())
        if self.hub_max is not None and len(rows) > self.hub_max:
            keep_types = (("HAS_CATEGORY", "SUBCATEGORY_OF", "LANGUAGE_EQUIVALENT")
                          if direction == "fwd" else
                          ("SUBCATEGORY_OF", "HAS_DISAMBIGUATION", "DISAMBIGUATES_TO", "LANGUAGE_EQUIVALENT"))
            keep = f"e.type IN ({','.join('?' for _ in keep_types)})"
            extra = tuple(keep_types)
            if self.keep:
                keep += f" OR e.{other} IN ({','.join('?' for _ in self.keep)})"
                extra += self.keep
            keep = "(" + keep + ")"
            if self.con.execute(query + f" AND NOT {keep} LIMIT 1", (*values, *extra)).fetchone():
                self.pruned.add(key)
            rows = self.con.execute(query + f" AND {keep}", (*values, *extra)).fetchall()
        self.cache[key] = rows
        return rows


def _rel_label(etype, direction):
    return {
        ("HAS_CATEGORY", "fwd"): "in category",
        ("HAS_CATEGORY", "rev"): "contains article",
        ("SUBCATEGORY_OF", "fwd"): "subcategory of",
        ("SUBCATEGORY_OF", "rev"): "has subcategory",
        ("HAS_DISAMBIGUATION", "fwd"): "has disambiguation",
        ("HAS_DISAMBIGUATION", "rev"): "disambiguation of",
        ("DISAMBIGUATES_TO", "fwd"): "disambiguates to",
        ("DISAMBIGUATES_TO", "rev"): "listed on",
        ("LANGUAGE_EQUIVALENT", "fwd"): "same concept in another language",
        ("LANGUAGE_EQUIVALENT", "rev"): "same concept in another language",
    }.get((etype, direction), etype)


def q_path(con, src_id, dst_id, hub_max=CONFIG["search"]["hub_max"],
           settle_cap=CONFIG["search"]["settle_cap"],
           time_budget=CONFIG["search"]["time_budget"], *, policy=None):
    """Dijkstra over the directional cost model and an immutable route policy.

    Costs come from wikihopper.toml. A state bit marks an article reached by
    exiting a disambiguation page; another disambiguation bridge is forbidden
    until a category step resets the state. With the guard off all states are 0;
    nonnegative costs and strict improvements still terminate zero-cost cycles."""
    search_started = time.perf_counter()
    policy = _route_policy(con, policy)
    seconds = parse_search_seconds(time_budget)
    deadline = search_started + seconds if seconds else None
    adjacency = _PolicyAdjacency(con, policy, hub_max, keep=(dst_id,))

    def finish(result):
        result["elapsed_seconds"] = round(time.perf_counter() - search_started, 3)
        result["effective_policy"] = policy.as_dict()
        result["hub_pruned"] = bool(adjacency.pruned)
        result["hub_pruned_nodes"] = len({node for node, _ in adjacency.pruned})
        return result

    _policy_node(q_node(con, src_id), policy, "Source node")
    _policy_node(q_node(con, dst_id), policy, "Target node")
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (src_id,)).fetchone() is None:
        return finish({"found": False, "note": "source node not found"})
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (dst_id,)).fetchone() is None:
        return finish({"found": False, "note": "target node not found"})
    if src_id == dst_id:
        return finish(_path_result(con, [src_id], 0, 0))

    # Prefer nodes adjacent to the target when distances tie. This preserves
    # Dijkstra correctness while avoiding a huge, arbitrary node-id-ordered
    # zero-cost closure before an obvious target-adjacent category is explored.
    target_neighbors = {r["neighbor"] for direction in ("fwd", "rev")
                        for r in adjacency.rows(dst_id, direction, hint=True)}

    def heap_item(distance, node_id, state):
        return (distance, 0 if node_id in target_neighbors else 1, node_id, state)

    typecache = {}

    def ntype(u):
        t = typecache.get(u)
        if t is None:
            r = con.execute("SELECT type FROM nodes WHERE id=?", (u,)).fetchone()
            t = r["type"] if r else "?"
            typecache[u] = t
        return t

    def successors(u):
        for direction, cost in (("fwd", _fwd_cost), ("rev", _rev_cost)):
            for r in adjacency.rows(u, direction):
                yield r["neighbor"], cost(r["type"]), r["type"], direction

    # state s: 0 = normal, 1 = article reached by exiting a disambiguation page
    start = (src_id, 0)
    dist = {start: 0}
    prev = {start: None}
    heap = [heap_item(0, src_id, 0)]
    done = set()
    settled = 0
    capped = False
    stop_reason = None
    best_dst = None                           # smallest distance found to dst

    while heap:
        d, _, u, su = heapq.heappop(heap)
        # Dijkstra pops in nondecreasing distance: once the frontier reaches the
        # best distance already found for the target, that distance is optimal.
        if best_dst is not None and d >= best_dst:
            break
        if (u, su) in done:
            continue
        if settle_cap is not None and settled >= settle_cap:
            capped = True
            stop_reason = "node limit"
            break
        if deadline is not None and time.perf_counter() >= deadline:
            capped = True
            stop_reason = "time limit"
            break
        done.add((u, su))
        settled += 1
        tu = ntype(u)
        # Nonnegative configured costs make the per-successor prune valid.
        for v, w, et, direction in successors(u):
            sv = _traversal_state(tu, ntype(v) if et in _DAB_TYPES else None, et, su,
                                  policy.ambiguity_guard)
            if sv is None:
                continue
            nd = d + w
            # once we have a candidate distance to dst, anything >= it can't
            # improve the answer, so don't bother exploring it (this stops the
            # expensive category-member frontier from being generated).
            if best_dst is not None and nd >= best_dst:
                continue
            key = (v, sv)
            if key not in dist or nd < dist[key]:
                dist[key] = nd
                prev[key] = ((u, su), (et, direction, w))
                heapq.heappush(heap, heap_item(nd, v, sv))
                if v == dst_id and (best_dst is None or nd < best_dst):
                    best_dst = nd

    cand = [(dist[k], k) for k in ((dst_id, 0), (dst_id, 1)) if k in dist]
    if not cand:
        hub_pruned = bool(adjacency.pruned)
        note = (f"No path found within the search {stop_reason} "
                f"({settled:,} nodes explored)."
                if capped else
                "No path found in the loaded graph under the selected route policy.")
        if hub_pruned:
            note += " Hub pruning omitted some edges; another route may exist."
        return finish({"found": False, "settled": settled, "note": note,
                       "timed_out": stop_reason == "time limit",
                       "limit_reason": stop_reason})
    _, endk = min(cand)

    seq, traversals = [], []
    cur = endk
    while cur is not None:
        seq.append(cur[0])
        previous = prev[cur]
        if previous is None:
            break
        cur, traversal = previous
        traversals.append(traversal)
    seq.reverse()
    traversals.reverse()
    # If we stopped on the budget and the answer is >0, optimality is not proven
    # (a shorter path might exist among unexplored 0-cost nodes).
    approx = (capped or adjacency.pruned) and dist[endk] > 0
    result = _path_result(con, seq, dist[endk], settled,
                          approx=bool(approx), traversals=traversals)
    result["timed_out"] = stop_reason == "time limit"
    result["limit_reason"] = stop_reason
    return finish(result)


def q_selected_path(con, node_ids, *, policy=None):
    """Measure a fixed ordered route, choosing the cheapest legal parallel edges.

    Only the supplied hops are evaluated. Reading outgoing edges by source
    avoids scanning all members of a high-degree category for a single hop.
    Two guard states suffice even when parallel relationships change legality.
    """
    if (not isinstance(node_ids, list) or not 1 <= len(node_ids) <= 500
            or any(type(nid) is not int or not 0 <= nid <= 9223372036854775807 for nid in node_ids)):
        raise ValueError("node_ids must be an ordered list of 1 to 500 nonnegative integer node IDs")
    started = time.perf_counter()
    policy = _route_policy(con, policy)
    meta = {nid: q_node(con, nid) for nid in set(node_ids)}
    for nid, node in meta.items():
        if node is None:
            raise ValueError(f"Selected node {nid} was not found in the loaded graph")
        _policy_node(node, policy, "Selected node")
    wanted = {nid: set() for nid in meta}
    for a, b in zip(node_ids, node_ids[1:]):
        wanted[a].add(b)
        wanted[b].add(a)
    adjacent = {}

    def outgoing(nid):
        if nid not in adjacent:
            matches = {}
            for row in con.execute("SELECT dst,type FROM edges WHERE src=?", (nid,)):
                if row["dst"] in wanted[nid] and row["type"] in policy.allowed_edge_types:
                    matches.setdefault(row["dst"], set()).add(row["type"])
            adjacent[nid] = matches
        return adjacent[nid]

    frontier = {0: (0.0, None)}
    for index, (a, b) in enumerate(zip(node_ids, node_ids[1:]), 1):
        choices = [(et, "fwd", _fwd_cost(et)) for et in outgoing(a).get(b, ())]
        choices += [(et, "rev", _rev_cost(et)) for et in outgoing(b).get(a, ())]
        next_frontier = {}
        for state, (distance, trace) in frontier.items():
            for et, direction, cost in sorted(choices, key=lambda step: (step[2], step[0], step[1])):
                next_state = _traversal_state(meta[a]["type"], meta[b]["type"], et, state,
                                             policy.ambiguity_guard)
                if next_state is None:
                    continue
                total = distance + cost
                if next_state not in next_frontier or total < next_frontier[next_state][0]:
                    next_frontier[next_state] = (total, (trace, (et, direction, cost)))
        if not next_frontier:
            reason = ("No direct relationship allowed by Route relationships connects these consecutive nodes."
                      if not choices else
                      "This step chains disambiguation bridges without a category step in between; a language switch preserves that guard.")
            return {"found": False, "valid": False, "failed_step": index,
                    "from_id": a, "to_id": b, "hops": len(node_ids) - 1,
                    "note": f"Step {index}: {meta[a]['title']} [{meta[a]['language']}] → {meta[b]['title']} [{meta[b]['language']}]. {reason}",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "effective_policy": policy.as_dict()}
        frontier = next_frontier
    distance, trace = min(frontier.values(), key=lambda item: item[0])
    traversals = []
    while trace is not None:
        trace, traversal = trace
        traversals.append(traversal)
    traversals.reverse()
    result = _path_result(con, node_ids, distance, 0, traversals=traversals)
    result.update(valid=True, elapsed_seconds=round(time.perf_counter() - started, 3),
                  effective_policy=policy.as_dict())
    return result


def _edge_between(con, x, y):
    """Return (etype, direction, cost) for a single hop x -> y."""
    candidates = []
    for r in con.execute("SELECT type FROM edges WHERE src=? AND dst=?", (x, y)):
        candidates.append((r["type"], "fwd", _fwd_cost(r["type"])))
    for r in con.execute("SELECT type FROM edges WHERE src=? AND dst=?", (y, x)):
        candidates.append((r["type"], "rev", _rev_cost(r["type"])))
    return min(candidates, key=lambda step: step[2]) if candidates else (None, None, 0)


def _path_result(con, node_seq, distance, settled, approx=False, traversals=None):
    meta = {nid: q_node(con, nid) or {"id": nid, "title": str(nid), "type": "?",
                                    "language": "en", "wikidata_id": None,
                                    "page_id": None} for nid in node_seq}
    path = [meta[nid] for nid in node_seq]
    steps = []
    for index, (a, b) in enumerate(zip(node_seq, node_seq[1:])):
        # Preserve the edge actually chosen when parallel/reciprocal edges exist.
        et, direction, cost = (traversals[index] if traversals is not None
                               else _edge_between(con, a, b))
        step = {"from": meta[a]["title"], "to": meta[b]["title"], "type": et,
                      "from_id": a, "to_id": b,
                      "from_language": meta[a]["language"],
                      "to_language": meta[b]["language"],
                      "direction": direction, "rel": _rel_label(et, direction),
                      "cost": cost}
        if et == "LANGUAGE_EQUIVALENT":
            stored_src, stored_dst = (a, b) if direction == "fwd" else (b, a)
            provenance = q_provenance(con, None, stored_src, stored_dst, et)
            step["evidence"] = provenance["evidence"]
            qid = meta[a].get("wikidata_id")
            if qid and qid == meta[b].get("wikidata_id"):
                step["wikidata_id"] = qid
        steps.append(step)
    return {"found": True, "distance": distance, "hops": len(steps),
            "path": path, "steps": steps, "settled": settled, "approx": approx}
