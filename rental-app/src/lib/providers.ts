// Real provider logos, self-hosted in public/logos/ (sourced from Wikimedia
// Commons + nationalcar.com's own site assets — see chat history for exact
// sources). Self-hosted rather than a third-party favicon/logo service:
// Clearbit's logo API is dead, and provider sites inconsistently serve
// touch-icon assets (several 404).
const PROVIDER_LOGOS: Record<string, { src: string; lightBg: boolean }> = {
  SIXT:       { src: '/logos/sixt.svg',       lightBg: true },
  Hertz:      { src: '/logos/hertz.svg',      lightBg: true },
  Enterprise: { src: '/logos/enterprise.svg', lightBg: true },
  National:   { src: '/logos/national.svg',   lightBg: true },
  Alamo:      { src: '/logos/alamo.svg',      lightBg: true },
  Dollar:     { src: '/logos/dollar.gif',     lightBg: true },
  Thrifty:    { src: '/logos/thrifty.jpg',    lightBg: true },
};

export function providerLogo(provider: string) {
  return PROVIDER_LOGOS[provider] ?? null;
}
