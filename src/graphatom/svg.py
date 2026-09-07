"""Le graph d'un item en SVG, l'état courant marqué — sans dépendance."""

import html

W, H, DX, DY, X0, Y0 = 132, 34, 185, 64, 12, 12


def layers(bundle: dict) -> list[list[str]]:
    """Couches BFS depuis entry + cibles on_kernel — pour le placement."""
    nodes = bundle["nodes"]
    depth: dict[str, int] = {}
    queue = [(bundle["entry"], 0)]
    for target in bundle["on_kernel"].values():
        queue.append((target, 0))
    while queue:
        n, d = queue.pop(0)
        if n in depth and depth[n] <= d:
            continue
        depth[n] = d
        for t in (nodes[n].get("edges") or {}).values():
            queue.append((t, d + 1))
    found: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for n in sorted(depth, key=lambda k: (depth[k], k)):
        found[depth[n]].append(n)
    return found


def _edges(nodes: dict) -> list[tuple[str, str, list[str]]]:
    """Une arête par couple (source, cible) : deux issues vers la même cible
    partagent le trait et l'étiquette, sinon elles se superposent illisiblement."""
    edges = []
    for n, spec in nodes.items():
        merged: dict[str, list[str]] = {}
        for outcome, t in (spec.get("edges") or {}).items():
            merged.setdefault(t, []).append(outcome)
        edges.extend((n, t, outcomes) for t, outcomes in merged.items())
    return edges


def _path(start: tuple, end: tuple, lane: int | None, height: int) -> tuple[str, float, float]:
    """Le tracé d'une arête et le point de son étiquette.

    Une arête avant part du bord droit vers le bord gauche ; une arête de
    retour contourne par le bas, dans sa propre voie : deux retours
    partageant le même milieu écrivaient leur étiquette au même point.
    """
    (x1, y1), (x2, y2) = start, end
    if lane is None:
        sx, sy = x1 + W, y1 + H / 2
        ex, ey = x2, y2 + H / 2
        d = f"M{sx},{sy} C{sx + 40},{sy} {ex - 40},{ey} {ex},{ey}"
        return d, (sx + ex) / 2, (sy + ey) / 2 - 5
    sx, sy = x1, y1 + H / 2
    ex, ey = x2 + W, y2 + H / 2
    my = height - 6 - lane * 14
    d = f"M{sx},{sy} C{sx - 60},{my} {ex + 60},{my} {ex},{ey}"
    return d, (sx + ex) / 2, my - 4


def _node(n: str, spec: dict, x: int, y: int, current: str) -> str:
    fill = "#ffb74d" if n == current else ("#eee" if spec.get("terminal") else "#e3ecf7")
    dash = " stroke-dasharray='4 2'" if spec.get("escalade") else ""
    label = n if spec.get("terminal") else f"{n} · {spec['block']}"
    return (f"<rect x='{x}' y='{y}' width='{W}' height='{H}' rx='7' "
            f"fill='{fill}' stroke='#888'{dash}/>"
            f"<text x='{x + W / 2}' y='{y + H / 2 + 4}' font-size='11' "
            f"text-anchor='middle'>{html.escape(label)}</text>")


def graph_svg(bundle: dict, current: str) -> str:
    nodes = bundle["nodes"]
    stacked = layers(bundle)
    pos = {n: (X0 + d * DX, Y0 + i * DY)
           for d, layer in enumerate(stacked) for i, n in enumerate(layer)}
    edges = _edges(nodes)
    backs = [(n, t) for n, t, _ in edges if pos[t][0] <= pos[n][0]]
    lanes = {couple: i for i, couple in enumerate(backs)}
    width = X0 + len(stacked) * DX
    height = Y0 + max(len(la) for la in stacked) * DY + 10 + 14 * len(lanes)

    parts = [f"<svg viewBox='0 0 {width} {height}' xmlns='http://www.w3.org/2000/svg'>",
             "<defs><marker id='arr' markerWidth='7' markerHeight='7' refX='6' refY='3' "
             "orient='auto'><path d='M0,0 L6,3 L0,6' fill='none' stroke='#999'/></marker></defs>"]
    used: set[tuple[float, float]] = set()
    for n, t, outcomes in edges:
        d, lx, ly = _path(pos[n], pos[t], lanes.get((n, t)), height)
        while (lx, ly) in used:  # deux milieux confondus : on décale, jamais on n'empile
            ly -= 10
        used.add((lx, ly))
        parts.append(f"<path d='{d}' fill='none' stroke='#999' marker-end='url(#arr)'/>")
        parts.append(f"<text x='{lx}' y='{ly}' font-size='9' fill='#777' "
                     f"text-anchor='middle'>{html.escape(', '.join(outcomes))}</text>")
    parts.extend(_node(n, spec, *pos[n], current) for n, spec in nodes.items())
    parts.append("</svg>")
    return "".join(parts)
