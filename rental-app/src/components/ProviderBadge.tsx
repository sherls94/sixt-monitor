import { providerLogo } from '@/lib/providers';

export function ProviderBadge({ provider, size = 28 }: { provider: string; size?: number }) {
  const logo = providerLogo(provider);
  // Radius scales with size instead of a fixed value — a fixed radius reads
  // as nearly circular at small sizes and properly square-ish at large ones.
  const radius = Math.max(6, size * 0.16);

  if (!logo) {
    return (
      <div
        className="shrink-0 bg-secondary flex items-center justify-center font-bold text-foreground"
        style={{ width: size, height: size, fontSize: size * 0.5, borderRadius: radius }}
      >
        {provider.charAt(0)}
      </div>
    );
  }
  return (
    <div
      className="shrink-0 flex items-center justify-center overflow-hidden p-1"
      style={{ width: size, height: size, background: logo.lightBg ? '#FFFFFF' : 'transparent', borderRadius: radius }}
    >
      <img src={logo.src} alt={provider} className="max-h-full max-w-full object-contain" />
    </div>
  );
}
