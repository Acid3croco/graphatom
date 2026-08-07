/**
 * Les mêmes formats que les pages stdlib : un âge, une durée, un usage.
 *
 * Le front rend les mêmes vues que `src/graphatom/web.py` ; il les écrit
 * donc pareil — sinon deux clients de la même API racontent deux histoires
 * du même item.
 */
import type { Usage } from "@/lib/api";

/** Un âge lisible : en secondes sous la minute, en minutes au-delà. */
export function ago(seconds: number): string {
  return seconds < 60
    ? `${seconds.toFixed(0)} s`
    : `${(seconds / 60).toFixed(0)} min`;
}

/** Une durée en français court : 4,2 s, 3 min 12 s, 1 h 04. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "";
  }
  if (seconds < 60) {
    return `${seconds.toFixed(1).replace(".", ",")} s`;
  }
  if (seconds < 3600) {
    const s = Math.floor(seconds % 60);
    return `${Math.floor(seconds / 60)} min ${String(s).padStart(2, "0")} s`;
  }
  const m = Math.floor((seconds % 3600) / 60);
  return `${Math.floor(seconds / 3600)} h ${String(m).padStart(2, "0")}`;
}

// étiquettes cosmétiques : un type de token que personne n'a prévu s'affiche
// sous son propre nom, et se compte comme les autres
const TOKEN_LABELS: Record<string, string> = {
  input_tokens: "in",
  output_tokens: "out",
  cache_read_input_tokens: "cache lu",
  cache_creation_input_tokens: "cache écrit",
  total_cost_usd: "$",
};

/** Un usage en une ligne : les types tels qu'ils viennent, rien d'inventé. */
export function tokens(usage: Usage | null | undefined): string {
  if (!usage) {
    return "";
  }
  return Object.entries(usage)
    .filter(([, value]) => typeof value === "number")
    .map(([key, value]) => {
      const count = Number.isInteger(value)
        ? value.toLocaleString("fr-FR").replace(/ | /g, " ")
        : value.toFixed(4);
      return `${count} ${TOKEN_LABELS[key] ?? key}`;
    })
    .join(" · ");
}

/** Un horodatage ISO en jour/mois heure — vide quand il n'y en a pas. */
export function moment(iso: string | null | undefined, withSeconds = false): string {
  if (!iso) {
    return "";
  }
  const at = new Date(iso);
  const time = at.toLocaleTimeString("fr-FR", {
    hour: "2-digit",
    minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
  });
  const day = at.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
  return `${day} ${time}`;
}
