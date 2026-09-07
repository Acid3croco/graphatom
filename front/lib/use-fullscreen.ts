/**
 * Le plein écran d'un bloc, par l'API du navigateur quand elle est là.
 *
 * Safari iOS ne la donne pas sur un `<div>` : `requestFullscreen` n'y
 * existe pas, et l'appeler à l'aveugle jetait une erreur, donc le bouton ne
 * faisait rien. À défaut d'API — ou si elle refuse — le bloc se met
 * lui-même en `fixed inset-0`, ce qui donne la même vue sans rien demander
 * à personne. Le bouton Minimize en sort, la touche Échap aussi.
 */
import { useEffect, useState, type RefObject } from "react";

export function useFullscreen(box: RefObject<HTMLDivElement | null>) {
  // plein écran de l'API du navigateur, suivi par son propre événement
  const [native, setNative] = useState(false);
  // plein écran du repli CSS, quand l'API n'est pas là ou qu'elle refuse
  const [css, setCss] = useState(false);

  useEffect(() => {
    const sync = () => setNative(document.fullscreenElement === box.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, [box]);

  // le repli n'est qu'un jeu de classes : Échap n'en sort que si on l'écoute.
  // L'API native, elle, a déjà sa touche Échap — il n'y a rien à y ajouter.
  useEffect(() => {
    if (!css) {
      return;
    }
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setCss(false);
      }
    };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [css]);

  /** Entrer en plein écran, ou en sortir — par l'API si elle est là. */
  function toggle() {
    if (css) {
      setCss(false);
      return;
    }
    if (document.fullscreenElement) {
      document.exitFullscreen();
      return;
    }
    const el = box.current;
    if (!el?.requestFullscreen) {
      // Safari iOS ne met pas un `<div>` en plein écran : l'API n'y est pas
      setCss(true);
      return;
    }
    // le navigateur refuse le plein écran hors geste utilisateur : c'est
    // son droit, pas une panne du front, et le repli prend alors la main
    el.requestFullscreen().catch(() => setCss(true));
  }

  return { full: native || css, css, toggle };
}
