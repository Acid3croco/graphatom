import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** La fusion de classes de shadcn/ui : la dernière gagne, sans doublon. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** La liste sans `value` si elle l'avait, avec sinon — un dépliant qui bascule. */
export function toggled<T>(list: T[], value: T): T[] {
  return list.includes(value)
    ? list.filter((item) => item !== value)
    : [...list, value];
}
