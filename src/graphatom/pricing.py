"""Tarifs publics des modèles et coût API équivalent des runs.

Le rail utilise des abonnements ou des modèles gratuits. Le montant calculé
ici n'est donc jamais une facture : c'est le prix qu'aurait eu le même usage
aux tarifs API standard publics. Chaque estimation épingle le relevé de prix
qui l'a produite. Un changement de tarif ne réécrit pas l'histoire.

Le processus ``pricing-sync`` fait deux gestes indépendants : il relève les
tarifs arrivés à échéance, au plus une fois par jour, puis chiffre les runs
qui portent un usage mais n'ont pas encore d'estimation. Une panne réseau
laisse les anciens relevés utilisables et se voit dans le journal.
"""

from __future__ import annotations

import datetime as dt
import html.parser
import json
import os
import re
import shlex
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

from . import db, executors
from .graph import candidate_node


MILLION = Decimal(1_000_000)
REFRESH_AFTER = dt.timedelta(days=1)
HTTP_TIMEOUT_S = 20.0
POLL_S = 60.0

OPENAI_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
OPENAI_URL = "https://developers.openai.com/api/docs/models/{model}.md"
DEEPSEEK_URL = "https://api-docs.deepseek.com/quick_start/pricing"


@dataclass(frozen=True)
class Price:
    """Un tarif public, en dollars par million de tokens."""

    provider: str
    model: str
    source_url: str
    input: Decimal
    cache_read: Decimal
    cache_write: Decimal
    output: Decimal


@dataclass(frozen=True)
class Tokens:
    """Les quatre classes facturables, sans recouvrement."""

    input: int
    cache_read: int
    cache_write: int
    output: int


def _get(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "graphatom-pricing/1.0", "Accept": "text/plain,text/html"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as response:
        return response.read().decode("utf-8", "replace")


def parse_openai(model: str, text: str, source_url: str | None = None) -> Price:
    """Lit la table ``Text tokens`` de la page Markdown d'un modèle OpenAI."""
    section = text.partition("### Text tokens")[2].partition("\n## ")[0]
    values = {
        name.lower(): Decimal(value)
        for name, value in re.findall(
            r"^\|\s*(Input|Cached input|Output)\s*\|\s*\$([0-9.]+)\s*\|",
            section,
            re.MULTILINE,
        )
    }
    missing = {"input", "cached input", "output"} - values.keys()
    if missing:
        raise ValueError(f"tarif OpenAI incomplet pour {model}: {sorted(missing)}")
    if "Cache writes are billed at 1.25x" not in section:
        raise ValueError(f"tarif d'écriture de cache absent pour {model}")
    return Price(
        "openai",
        model,
        source_url or OPENAI_URL.format(model=model),
        values["input"],
        values["cached input"],
        values["input"] * Decimal("1.25"),
        values["output"],
    )


class _Tables(html.parser.HTMLParser):
    """Le texte des cellules HTML, table par table."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None and (value := " ".join(data.split())):
            self._cell.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def parse_deepseek(text: str, source_url: str = DEEPSEEK_URL) -> list[Price]:
    """Lit la table officielle DeepSeek, sans dépendance HTML tierce."""
    parser = _Tables()
    parser.feed(text)
    table = next(
        (table for table in parser.tables
         if table and any("deepseek-v4-flash" in cell for row in table for cell in row)),
        None,
    )
    if table is None:
        raise ValueError("table des tarifs DeepSeek absente")
    models = [cell for cell in table[0] if cell.startswith("deepseek-")]
    metrics: dict[str, list[Decimal]] = {}
    for row in table:
        joined = " ".join(row).upper()
        name = next((name for name in ("CACHE HIT", "CACHE MISS", "OUTPUT TOKENS")
                     if name in joined), None)
        if name:
            metrics[name] = [Decimal(value) for value in re.findall(r"\$([0-9.]+)", joined)]
    if not models or any(len(metrics.get(name, [])) != len(models)
                         for name in ("CACHE HIT", "CACHE MISS", "OUTPUT TOKENS")):
        raise ValueError("tarif DeepSeek incomplet")
    return [
        Price("deepseek", model, source_url,
              metrics["CACHE MISS"][index], metrics["CACHE HIT"][index],
              metrics["CACHE MISS"][index], metrics["OUTPUT TOKENS"][index])
        for index, model in enumerate(models)
    ]


def fetch_prices() -> list[Price]:
    """Relève les modèles employés par le graph auprès de leurs pages officielles."""
    prices = [
        parse_openai(model, _get(OPENAI_URL.format(model=model)))
        for model in OPENAI_MODELS
    ]
    prices.extend(parse_deepseek(_get(DEEPSEEK_URL)))
    return prices


def _number(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def normalize(provider: str, usage: dict) -> Tokens:
    """Retire les recouvrements propres à chaque CLI avant de chiffrer."""
    if provider == "openai":
        total_input = _number(usage, "input_tokens")
        cache_read = _number(usage, "cached_input_tokens")
        cache_write = _number(usage, "cache_write_input_tokens")
        # Codex donne le cache dans le total d'entrée et le raisonnement dans
        # le total de sortie. Les soustraire ou l'ajouter deux fois surfacture.
        return Tokens(max(0, total_input - cache_read - cache_write),
                      cache_read, cache_write, _number(usage, "output_tokens"))
    # OpenCode donne cache et raisonnement à côté de input/output. DeepSeek
    # facture une écriture comme un cache miss, donc au tarif d'entrée.
    return Tokens(_number(usage, "input_tokens"),
                  _number(usage, "cache_read_tokens"),
                  _number(usage, "cache_write_tokens"),
                  _number(usage, "output_tokens") + _number(usage, "reasoning_tokens"))


def components(tokens: Tokens, price: Price | dict) -> dict[str, Decimal]:
    """Les tarifs standard de base, par classe, en arithmétique décimale.

    Codex rend un total de tour, pas la taille d'entrée de chaque requête API
    du tour. Le seuil de surcharge de contexte long ne peut donc pas être
    appliqué sans inventer. L'API et le frontend nomment cette base.
    """
    get = (lambda key: getattr(price, key)) if isinstance(price, Price) else price.__getitem__
    costs = {
        "input_cost_usd": Decimal(tokens.input) * Decimal(get("input")) / MILLION,
        "cache_read_cost_usd": Decimal(tokens.cache_read) * Decimal(get("cache_read")) / MILLION,
        "cache_write_cost_usd": Decimal(tokens.cache_write) * Decimal(get("cache_write")) / MILLION,
        "output_cost_usd": Decimal(tokens.output) * Decimal(get("output")) / MILLION,
    }
    return costs | {"estimated_cost_usd": sum(costs.values(), Decimal(0))}


def _shell_assignments(command: str) -> dict[str, str]:
    """Les affectations simples au début d'une commande historique."""
    try:
        words = shlex.split(command)
    except ValueError:
        return {}
    result = {}
    for word in words:
        if "=" not in word:
            break
        name, value = word.split("=", 1)
        if name.isidentifier():
            result[name] = value
    return result


def _model_in_command(command: str, name: str) -> str | None:
    """Une affectation de modèle, même derrière le garde d'un ancien shell."""
    match = re.search(
        rf"(?:^|[\s;]){name}=(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z0-9._:/-]+))",
        command,
    )
    return next((part for part in match.groups() if part), None) if match else None


def run_model(bundle: dict, run: dict, usage: dict) -> tuple[str, str, str] | None:
    """Rend fournisseur, modèle tarifé et provenance du choix.

    Le modèle écrit dans l'usage gagne. Vient ensuite l'exécuteur structuré
    du graph, puis les anciennes commandes à variables d'environnement. Les
    tout premiers runs Codex n'épinglaient rien : un modèle historique peut
    être fourni explicitement au service, et reste marqué ``legacy_default``.
    """
    reported = usage.get("model")
    if isinstance(reported, str) and reported:
        if reported.startswith("opencode/deepseek-v4-flash"):
            return "deepseek", "deepseek-v4-flash", "usage"
        if reported.startswith("gpt-"):
            return "openai", reported, "usage"

    spec = bundle["nodes"].get(run["node"], {})
    if run.get("candidate") is not None:
        spec = candidate_node(spec, run["candidate"])
    resolved = executors.resolve(bundle, spec)
    command = resolved.cmd or ""
    assignments = _shell_assignments(command)
    model = resolved.model
    cli = resolved.cli
    if not model:
        model = (assignments.get("CODEX_MODEL") or assignments.get("OPENCODE_MODEL")
                 or _model_in_command(command, "CODEX_MODEL")
                 or _model_in_command(command, "OPENCODE_MODEL"))
    if not model and "agent-opencode.sh" in command:
        positional = re.search(r"agent-opencode\.sh[^\n]*?\s+(opencode/[A-Za-z0-9._/-]+)",
                               command)
        model = positional.group(1) if positional else None
    if not cli:
        if "agent-codex.sh" in command:
            cli = "codex"
        elif "agent-opencode.sh" in command:
            cli = "opencode"
    if cli == "codex" and model:
        return "openai", model, "graph"
    if cli == "opencode" and model and "deepseek-v4-flash" in model:
        return "deepseek", "deepseek-v4-flash", "graph"
    if cli == "codex" or "agent-codex.sh" in command:
        legacy = os.environ.get("GRAPHATOM_LEGACY_CODEX_MODEL")
        if legacy:
            return "openai", legacy, "legacy_default"
    return None


def _latest(conn, provider: str, model: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM model_price WHERE provider = %s AND model = %s "
        "ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (provider, model),
    ).fetchone()


def refresh(conn, *, force: bool = False) -> int:
    """Ajoute un relevé frais pour chaque tarif dû. Rend le nombre ajouté."""
    now = dt.datetime.now(dt.timezone.utc)
    due = {(provider, model) for provider, models in (
        ("openai", OPENAI_MODELS),
        ("deepseek", ("deepseek-v4-flash", "deepseek-v4-pro")),
    ) for model in models if force or not (latest := _latest(conn, provider, model))
       or now - latest["fetched_at"] >= REFRESH_AFTER}
    if not due:
        return 0
    prices = fetch_prices()
    selected = [price for price in prices if (price.provider, price.model) in due]
    missing = due - {(price.provider, price.model) for price in selected}
    if missing:
        raise ValueError(f"tarifs attendus absents: {sorted(missing)}")
    for price in selected:
        conn.execute(
            "INSERT INTO model_price (provider, model, source_url, fetched_at, "
            "input_per_million, cache_read_per_million, cache_write_per_million, "
            "output_per_million) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (price.provider, price.model, price.source_url, now, price.input,
             price.cache_read, price.cache_write, price.output),
        )
    return len(selected)


def estimate_missing(conn) -> tuple[int, int]:
    """Épingle un tarif sur chaque run chiffrable. Rend (faits, inconnus)."""
    rows = conn.execute(
        "SELECT nr.*, g.bundle FROM node_run nr "
        "JOIN work_item w ON w.id = nr.item_id "
        "JOIN graph_revision g ON g.id = w.revision "
        "LEFT JOIN run_cost rc ON rc.run_id = nr.id "
        "WHERE rc.run_id IS NULL AND nr.result ? 'usage' ORDER BY nr.id"
    ).fetchall()
    done = unknown = 0
    for run in rows:
        usage = (run["result"] or {}).get("usage") or {}
        identity = run_model(run["bundle"], run, usage)
        if identity is None:
            unknown += 1
            continue
        provider, model, model_source = identity
        price = _latest(conn, provider, model)
        if price is None:
            unknown += 1
            continue
        tokens = normalize(provider, usage)
        costs = components(tokens, {
            "input": price["input_per_million"],
            "cache_read": price["cache_read_per_million"],
            "cache_write": price["cache_write_per_million"],
            "output": price["output_per_million"],
        })
        conn.execute(
            "INSERT INTO run_cost (run_id, price_id, model_source, input_tokens, "
            "cache_read_tokens, cache_write_tokens, output_tokens, input_cost_usd, "
            "cache_read_cost_usd, cache_write_cost_usd, output_cost_usd, "
            "estimated_cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id) DO NOTHING",
            (run["id"], price["id"], model_source, tokens.input, tokens.cache_read,
             tokens.cache_write, tokens.output, costs["input_cost_usd"],
             costs["cache_read_cost_usd"], costs["cache_write_cost_usd"],
             costs["output_cost_usd"], costs["estimated_cost_usd"]),
        )
        done += 1
    return done, unknown


def once(conn, *, force: bool = False) -> tuple[int, int, int]:
    prices = refresh(conn, force=force)
    estimated, unknown = estimate_missing(conn)
    return prices, estimated, unknown


def ready(conn) -> bool:
    """Les deux tables existent : la migration du service est terminée."""
    row = conn.execute(
        "SELECT to_regclass('model_price') AS prices, "
        "to_regclass('run_cost') AS costs"
    ).fetchone()
    return bool(row["prices"] and row["costs"])


def sync_forever(poll_s: float = POLL_S) -> None:
    """Boucle dédiée : une panne de tarif ne touche ni worker, ni web, ni GitHub."""
    with db.connect() as conn:
        print(f"tarifs publics — relève quotidienne, contrôle toutes les {poll_s:g} s",
              flush=True)
        while True:
            try:
                prices, estimated, unknown = once(conn)
                print(f"tarifs: {prices} relevé(s), {estimated} run(s) chiffré(s), "
                      f"{unknown} sans modèle ou tarif", flush=True)
            except (OSError, ValueError) as exc:
                print(f"tarifs non actualisés: {exc} — anciens relevés conservés",
                      flush=True)
                # Même si le réseau casse, les tarifs déjà épinglés peuvent
                # encore chiffrer les runs arrivés depuis le tour précédent.
                estimated, unknown = estimate_missing(conn)
                print(f"tarifs: {estimated} run(s) chiffré(s), {unknown} inconnu(s)",
                      flush=True)
            time.sleep(poll_s)
