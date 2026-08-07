import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** La fusion de classes de shadcn/ui : la dernière gagne, sans doublon. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
