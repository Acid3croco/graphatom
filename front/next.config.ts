import type { NextConfig } from "next";

// `standalone` : le build recopie dans `.next/standalone` le serveur et les
// seules dépendances qu'il utilise. L'image de runtime n'a donc ni
// `node_modules` complet ni sources — c'est ce qui tient le budget de taille.
const config: NextConfig = {
  output: "standalone",
};

export default config;
